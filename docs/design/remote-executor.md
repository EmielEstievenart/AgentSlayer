# Remote Executor — plan (draft)

> **Status: PLAN, not yet binding.** Decisions below were made in a design
> interview on 2026-08-18, grounded in a code investigation of the current
> Shell/Driver/Executor layering. Open points are marked **OPEN** and must be
> resolved (or consciously deferred again) before implementation starts. Once
> the design session for the first increment happens, this document graduates
> into a binding design doc and `remote-ssh.md` gets amended where superseded.
>
> **Exception: §2.2, §2.6, §2.9, §2.10, §2.11 and §2.12 are built, and
> binding** — increment 1 (the link seam), increment 2 (the engine-side package,
> the wire codec, the server loop, `RemoteLink` and the localhost e2e suite) and
> increment 3 (the console script, the SSH link channel, launch-failure
> classification and the opt-in remote factory) shipped in full, so those parts
> describe code rather than intent. Everything else here is still plan.
>
> **The default `--ssh` mode is still the per-call `SshHost` path.** Increment 3
> built the remote-engine transport and left it *additive*: `cli.make_remote_link_factory`
> is reachable and tested, and nothing calls it yet. The flip waits on increment
> 4's parity pass (§4), and §2.8's deletion on increment 5.

## 1. Goal

Split AgentClip across a process boundary:

- **Shell** — must stay local: clipboard watcher, screen detection and
  automation (driver), GUI/TUI. It is the machine the LLM chat browser runs on.
- **Engine + Executor** — session state machine, CLIP protocol handling,
  approval verdicts, tool execution, MCP, config reading. Runs either
  in-process with the Shell (today's local mode) or deployed to an SSH target
  and run there, talking to the local Shell over RPC.

Drivers, in priority order (explicitly *not* latency):

1. **MCP servers must run natively on the target** — stdio servers spawned in
   the target's environment with its filesystem.
2. **Capability/env fidelity** — commands need the target's real toolchains,
   PATH, docker, long-running processes.
3. **Robustness** — honest, simple failure semantics on link loss (not
   daemon-grade resilience; see §4).

## 2. Decided

### 2.1 The engine goes remote

The RPC line is **Shell ↔ Engine**, not Engine ↔ Executor. The engine ships
with the executor as one process ("the remote half"). Rationale:

- The investigation showed the engine is welded to the executor by direct
  imports (`engine/engine.py` → `executor.hosts`, `executor.permissions`,
  `executor.tools.registry`) and calls `ToolSpec.handler(ctx, call)`
  in-process. Cutting there would mean serializing `ToolContext` (live `Host`,
  `threading.Event`, callables) — not a wire type.
- The Shell↔Engine surface is already narrow and message-shaped: clipboard
  payloads and gate/ask_user answers go up; paste requests, progress events
  (`CallProgress`), output deltas, and pause-points (`PendingAction`,
  `AskUser`, `Delegate` — all pause/resume, not callbacks) come down.
- The LLM conversation is inherently local (browser + clipboard), so no
  session can advance without the Shell anyway. Putting everything smart on
  one side keeps a single engine code path.

### 2.2 Local mode: in-process link

Define the Shell↔Engine boundary as a link interface, same idiom as the Host
seam: `LocalLink` calls the engine in-process exactly as today, `RemoteLink`
speaks the wire protocol over SSH, `FakeLink` for tests. Local startup and
debuggability are unchanged. Accepted risk: the wire path is only exercised by
remote/integration tests — mitigate with a wire-protocol test suite that runs
`RemoteLink` against a subprocess on localhost.

**As built (increment 1).** The interface lives at
`src/agentclip/shell/app/link.py`, and the Protocol is split by *thread
contract* rather than by topic, because that split is what a wire has to
reproduce:

- **Immutable per-session facts** — `chat_name`, `role`, `build_warnings` — stay
  plain sync attributes, snapshotted once at construction. The controller reads
  them from the event loop with nothing to await (transcript notes quote them
  the moment a session arms), so a `RemoteLink` carries them home in the
  handshake instead of paying a round trip per read.
- **State-changing calls are async and serialized**, one in flight:
  `start_task`, `follow_up`, `ingest`, `pending`, `decide`, `execute`,
  `answer_user`, `deliver_delegate_result`, `undo_last_turn`, `status`,
  `set_yolo`, `set_permission_mode`, `arm_extra_instructions`.
- **`request_cancel` is sync and out-of-band**, deliberately never gated by the
  serializing lock: the call it interrupts is the one holding it. Locally that
  is still `Engine.request_cancel` setting a `threading.Event`; on the wire it
  has to be a message the transport can send while a request is outstanding.
- **The two hook setters are sync registration**, wired once right after
  construction and before the first async call. The hooks keep today's contract,
  which is the engine's: fired from the worker thread mid-`execute()`, must not
  block, and one that raises is silently dropped for the rest of the session.

`LocalLink` owns the `asyncio.Lock` + `asyncio.to_thread` hop that used to be
`SessionController._engine_call` — they are the *local answer* to a question the
seam asks of every implementation ("one at a time, and do not block the loop"),
not a fact about the controller, which now holds a `Link` and never an `Engine`.
Exceptions cross unwrapped (`BudgetExceeded`, `EngineStateError`), because the
controller catches those by type.

`cli.make_engine_factory` returns a `Link` (a `LocalLink` over a freshly built
`Engine`); the name was kept because an engine is still exactly what it builds.
One controller-level lock became one lock per link, which is equivalent because
the controller only ever calls the link in the live session slot — verified call
site by call site when this landed.

**As built (increment 2, first slice: the engine half's home).** The other side
of the seam now exists as a package: `src/agentclip/engine/link/`. Increment 2
needs a process that builds an `Engine` with no shell in it, and today's
`cli` cannot be that process — importing it drags Textual in at module level —
so the assembly moved down before any wire was written:

- `engine/link/factory.py:make_engine_builder` is the shared assembly, verbatim
  what `cli.make_engine_factory` used to do (skills discovered once, config
  re-derived per request when the service differs, the MCP catalog sized by
  measurement, the session audit event) except that it returns the **bare
  `Engine`**. Both callers will be built on it: the server process on the target,
  and `cli`.
- `EngineRequest` (and the `Role` literal) moved **engine-side**, out of
  `shell/app/types.py`, because it is the message a remote engine is asked to
  build from: the server has to decode one without importing a shell.
  `shell/app/types.py` imports `Role` back for `SessionRef` and re-exports
  nothing else - every importer of `EngineRequest` names the new home.
- `cli.make_engine_factory` keeps its name and signature and is now three lines:
  build once, and return a closure that wraps each engine in a `LocalLink`. The
  wrap stays in `cli` on purpose - **nothing under `agentclip/engine/` may
  import `agentclip.shell` or `agentclip.driver`** (a `tests/test_layering.py`
  RULES entry pins `agentclip.engine.link` to config/engine/executor/protocol),
  since this package is precisely what runs on the target.

The wire codec landed next, in the same package: `engine/link/wire.py`, the
shared vocabulary both ends import (§2.9), then the server loop that speaks it
beside a bare engine (§2.10), and finally the Shell-side half that speaks it from
the other end: `shell/app/remote_link.py` (§2.11). The accepted risk this section
opens with — "the wire path is only exercised by remote/integration tests" — is
paid off by `tests/shell/app/test_remote_link.py`, which drives a real
`python -m agentclip.engine.link` subprocess on localhost through `RemoteLink`
alone, and ends with the same scripted flow run through BOTH links: equal
bootstrap payload, equal `StepResult`.

### 2.3 Disconnect semantics: lossy, honest, simple

- The remote process **dies with the SSH connection** (child of the exec
  channel). No detached daemon in v1. Today's `SshHost` semantics carry over:
  dead channel → "outcome unknown" → redial on next use, engine restarted.
- A model reply that lands on the local clipboard while the link is down is
  **lost**; the user re-copies (double-tap-c) after reconnect. No clipboard
  buffering, no local fallback engine.
- The wire should be designed so a detached/reattach daemon mode could be
  added later without protocol redesign (session IDs in the handshake), but
  nothing more than that in v1.

### 2.4 State lives with the engine

Session store (`transcript.jsonl`, session state) **and** backup store live
wherever the engine runs — on the target for remote sessions. No local
transcript mirror. This supersedes `remote-ssh.md`'s "backups capture remote
bytes locally": backups of the target's files now live next to those files.
Cost accepted: transcripts/backups are unreachable while the target is.

### 2.5 Policy: the engine owns it wholesale

User's formulation: **"the shell should just show the mode the engine is
in."** The remote engine reads approval mode, YOLO default, allowlist, deny
tokens, and the permission ruleset from the **target's** config
(`.agentclip.toml` + `permissions.json` on the target — see the config split
that landed with the `/config` wave). The Shell:

- renders the engine-reported mode/YOLO state (already the shape after the
  `/yolo` pre-arm change: view falls back to controller state, engine snapshot
  wins);
- relays `/yolo` and shift+tab mode cycling as RPC calls
  (`set_yolo`/`set_permission_mode`, same as today's engine API);
- renders approval gates when the engine pauses in an awaiting-approval state
  and answers with the user's verdict — the same pause/resume shape as
  `ask_user`/`delegate` (`PendingAction` → `decide(call_id, ...)`).

This supersedes `remote-ssh.md`'s "target owns the rules, host owns the gate"
*config* split: the gate **UI** stays local, but all policy data and verdict
computation are engine-side.

### 2.6 Deployment: a pre-installed engine on the target's PATH

**The engine half is a named executable the user installs on the target, and the
master launches it by name.** The user installs this same `agentclip` package
over there — `uv tool install agentclip`, `pipx install agentclip`, or plain pip
into an environment on PATH — which provides the console script
**`agentclip-engine`** (`[project.scripts]` → `agentclip.engine.link.__main__:main`,
the same `main` the localhost subprocess tests already run as
`python -m agentclip.engine.link`). Installing it is the user's job; AgentClip
ships no installer.

Each connect, the master opens an SSH exec channel running
`agentclip-engine --project <root> [--service KEY] …` and speaks wire v1 over
that channel's stdio — the transport the client was already written for (§2.11:
`RemoteLinkClient` takes two text streams and creates nothing). The process dies
with the channel, exactly as §2.3 says, so nothing is left behind on the target
between sessions. On the target, the server reads the **target's own** config and
platformdirs locations by plain local reads (`.agentclip.toml`, `permissions.json`,
skill folders) — the same policy-lives-with-the-engine rule as §2.5, with no path
translation and no remote file access anywhere in the loop. `paramiko` is never
used over there: the remote half never SSHes out, it uses `LocalHost`.

**The two halves are separate installs**, which is precisely why the handshake
exchanges both the wire version and the `agentclip` package version and refuses a
wire mismatch with a sentence naming both (§2.9). Upgrading one machine and not
the other is an expected state, not a bug — same wire version with different
package versions is legal and costs one line of the server's log.

An earlier plan — push the source subtree plus a lockfile over the existing SFTP
machinery each connect and start it with `uv run` — was considered and dropped in
favour of pre-installed-on-PATH: no first-connect copy step, no uv bootstrap to
detect and repair, and a target install that is a *version* the user can name and
upgrade rather than whatever the master happened to be running.

What this costs the package for now: `agentclip-engine` rides along with the full
install, so a target gets textual, pillow and the rest installed and never
imported. Acceptable in v1 — the engine half already may not import
`agentclip.shell` or `agentclip.driver` (pinned by `tests/test_layering.py`), so
the unused weight is disk, not coupling. A slimmer install (an extra/package
split, or a standalone binary) is §5 material.

### 2.7 MCP on target; interactive OAuth out of scope for v1

MCP servers spawn on the target (this deliberately reverses `remote-ssh.md`'s
"MCP transport stays on the host PC" decision — the premise changed). v1
supports stdio servers and header/token-authenticated HTTP servers; a server
requiring an interactive OAuth flow fails at startup with a clear "not
supported over SSH yet" warning. Ties into the existing MCP phase-2
token-reuse work for a later fix.

### 2.8 SshHost per-call mode is replaced outright

Once the remote-Executor mode reaches parity, the per-call `SshHost` tool path
(every file read / command as its own round trip; grep/glob pulling whole
files over SFTP to scan locally) is **deleted**. What survives from
`executor/hosts/ssh.py`/`connect.py` is the connection/auth/reconnect machinery,
repurposed to open the exec channel the remote process is launched and spoken to
on. Nothing deploys source any more (§2.6), so the SFTP side survives only as
connect/auth plumbing — no file is pushed to the target at connect time. One
remote story, clean break — same style as the opencode.json → permissions.json
split.

### 2.9 Wire protocol v1 (as built)

`src/agentclip/engine/link/wire.py` is the shared vocabulary both halves import
— the server loop on the target and the `RemoteLink` inside the Shell. It is one
module on purpose: a schema each end kept for itself would be two schemas, and
the drift would only ever show up in a remote run.

**Framing.** JSON Lines: one JSON object per line, UTF-8, `"\n"`-terminated,
compact separators, `ensure_ascii=False`. The only raw newline in an encoded
line is the terminator (JSON escapes the ones inside strings), so "one frame per
line" stays true of a 200k-char command output as much as of a handshake. Every
frame is **flushed as it is written**; nothing is batched. `stderr` is never
protocol data — it is the remote process's log, and anything printed to stdout
outside `encode_line` has corrupted the stream.

**Frames.** `hello` / `hello_ack` (both carry `version` AND `package` — see "Two
versions" below; the ack also carries a uuid4 `server_id`, which names the
PROCESS and exists so §2.3's detach/reattach mode can be added without a
protocol redesign — v1 only checks it is non-empty);
`call` (`id`, `method`, `params`, plus `session` for the 13 session-scoped
methods — `build_session` has none, because it is what mints one); `result`
(`id`, `value`); `error` (`id`, `kind`, `detail`, optional `data`); `progress`
and `output` (session-scoped events, no `id`); `cancel`.

**Two versions, and only one of them is a gate.** The handshake exchanges both
`version` (`WIRE_VERSION`, an integer) and `package` (`agentclip.__version__`).
`version` is the **compatibility gate**: a peer that speaks another one is
refused before any other frame is read, because everything after the handshake
is decoded as v1 and reading a v2 `call` frame as v1 is exactly the guess a
protocol boundary must not make. `package` is a **diagnostic** — nothing
branches on it, and *same wire version + different package versions is legal and
expected*. The reason is the deployment model (§2.6, as rewritten by the
deployment slice): the engine half is a console script (`agentclip-engine`) the
user pre-installs on the target's PATH, so the two halves are
independently-versioned installs and drift apart the moment one machine is
upgraded and the other is not.

The package version earns its place at the one moment the gate closes. A refusal
that could only say "wire v2 is not v1" would throw away the half a HUMAN can act
on — nobody chose a wire number; they chose an `agentclip` release on each
machine. So the mismatch has a type of its own, `WireVersionError(WireError)`,
carrying both ends' `Versions(wire, package)`, and the Shell turns those fields
into the sentence the user reads: *"the engine on the target speaks wire v2
(agentclip 0.1.0); this AgentClip speaks wire v1 (agentclip 0.4.2) — update the
target's install (e.g. `uv tool install --upgrade agentclip`)"*. The server's
refusal frame carries the same four numbers in its `detail` and the client passes
that detail through, so an ack that never arrives because the target refused
FIRST is just as legible as one that arrived wrong. A package difference on a
matching wire costs one line of the server's stderr and nothing else. `package`
is required in v1: a handshake frame without it is a malformed frame, not a
version mismatch.

**Events before the answer.** All `progress`/`output` frames for a call are
written and flushed strictly before that call's `result`/`error`. So the answer
IS the end of the call's event stream: nothing from a turn arrives after the
turn's answer, and no event needs a sequence number to be ordered against it.

**Cancel is out-of-band and unanswered.** It carries no `id` and gets no reply —
it is the wire's `Link.request_cancel`, and the call it interrupts is the one
whose answer is already outstanding. A cancel for an idle session is a no-op,
exactly like the local one.

**Values.** Dataclasses become JSON objects with every field present (decode
never fills a missing field in for a peer). The two unions — `IngestResult` and
`StepResult` — are tagged by the member's class name under `"kind"`. Enums travel
by `.name` (`Decision`, `Phase`); `Literal` aliases by their value
(`PermissionMode`, `ResultStatus`, `ArmResult`, and the inline `kind`/`phase`
literals). `StatusSnapshot.session_dir` is the ONE `Path` on the seam and travels
as a POSIX string: in a remote session it names a directory on another machine,
so the Shell must treat it as display data — never something to open or join.

**Errors.** Four kinds. `budget_exceeded` and `engine_state_error` rebuild
`BudgetExceeded` / `EngineStateError` on the far side, because the Shell catches
exactly those two by type; `bad_request` (unknown method, unknown session, wrong
version) and `internal` arrive as `EngineLinkError`, since a Shell that cannot
act on the difference is better told plainly that the far side broke. The
optional `data` object carries the structured fields an exception needs to be
rebuilt faithfully — today only `BudgetExceeded`'s two numbers, which the Shell
formats itself, so a message-only reconstruction would print a plausible
sentence with the wrong figures in it.

**Decoding is strict.** An unknown frame type, an unknown union tag, an unknown
enum name, a wrong version, a missing field, a field of the wrong JSON type, an
unknown method or an unknown parameter all raise `WireError`. A boundary that
guesses is a boundary that acts on a message it got wrong.

**Per-method plumbing.** `encode_params`/`decode_params` and
`encode_result`/`decode_result` are table-driven over the 14 methods, so the
server and the `RemoteLink` each hold one line per method and everything that
could drift between them is stated once. `build_session`'s params ARE
`EngineRequest`'s fields; its result is a `SessionInfo` — the session id plus the
three immutable facts the `Link` Protocol exposes synchronously (`chat_name`,
`role`, `build_warnings`), carried home in that one answer exactly as §2.2 said
a handshake would.

### 2.10 The server loop (as built)

`src/agentclip/engine/link/server.py` is the engine half's dispatch loop:
`serve(reader, writer, builder, *, log=sys.stderr) -> int`, taking **text
streams** rather than a socket so the whole protocol can be driven in-process
over a pair of pipes (tests/engine/link/test_server.py does exactly that, against
a real Engine on a tmp project). The process form is
`agentclip-engine --project PATH [--service KEY] [--global-config PATH]
[--home PATH] [--data-root PATH]` — the console script §2.6 deploys, and
equivalently `python -m agentclip.engine.link` with the same flags for a
checkout without an install, which is what the tests spawn — which is argument
parsing and assembly only: the flags map straight onto `load_config` and
`make_engine_builder`, and `--global-config`/`--home` are the isolation that
tests and sandboxed targets need (platformdirs ignores env vars on Windows, so
parameters are the only reliable way to keep the real global config and the real
home out). It re-opens stdin/stdout as UTF-8 with `\n` endings before the first
frame, because a Windows text stream would otherwise translate the terminator.

**Synchronous, threads, no asyncio.** The engine IS synchronous and the event
loop lives on the other side of the wire. So:

- **The reader thread only reads and routes.** It never runs an engine method —
  that is what keeps it free to notice the next line while a turn is running.
- **Every call runs on a thread of its own**, and the client's one-in-flight
  contract is enforced by a **per-session busy flag**: a second call for a
  session whose previous one is unanswered earns `bad_request` rather than a
  second worker. Dispatch is `wire.decode_params` → `getattr(engine, method)`
  → `wire.encode_result`, with the method checked against wire's 13-name
  whitelist first, so no string off the wire ever reaches `getattr` unvetted.
- **`cancel` is handled on the reader thread**, calling `Engine.request_cancel()`
  directly — the engine's one thread-safe method (it sets a `threading.Event`),
  thread-safe precisely so somebody who is not the worker can interrupt the
  worker. A cancel for an unknown or idle session is logged and ignored, never
  answered.
- **One lock guards every frame written**, and each write is encode + write +
  flush under it, so a 200k-char output delta cannot be spliced through somebody
  else's result frame and nothing waits on a buffer. §2.9's interleaving
  guarantee then falls out of the shape rather than being enforced: the hooks
  fire synchronously inside the engine method, on the worker's own thread, so
  every progress/output frame is through the lock before `encode_result` runs.

**Sessions.** `build_session` allocates `s1`, `s2`, … , calls the builder, wires
that engine's progress/output hooks to frames tagged with the new id **before**
answering (the client may call the instant it reads the `SessionInfo`), and
answers with the `SessionInfo`. The builder is called **more than once per
process** on purpose — one server hosts every session of one link, and the
controller builds sub-agent engines mid-session from the same factory. A build
that fails (config, project, a server that will not start) costs the client one
`internal` error and leaves the hosted sessions untouched.

**Failure.** Nothing a handler does kills the server: engine exceptions become
`error` frames (`budget_exceeded`/`engine_state_error` keep their identity, the
rest are `internal`), unknown sessions and unknown methods are `bad_request`, and
a malformed line is answered and read past. Error frames that answer no call
carry `id: 0` — wire's `error` frames are typed `id: int`, so "unattributable"
needs a value rather than an absence, and 0 is one no client id can be. **Two
malformed lines in a row** end the process (nonzero exit): at that point the
stream is not a stream of frames and answering it politely would be a guess. A
bad handshake — wrong version, garbage, anything before the hello, EOF — is
refused with a `bad_request` where a reply is possible and exits nonzero. **EOF
is the clean shutdown**: no new call can start, any in-flight worker gets a
bounded 5s to write its answer through the lock, exit 0.

**No MCP in this increment**: the builder is called with `mcp_manager=None`, so a
hosted session is byte-identical to a pre-MCP one. Increment 4 brings the
target-side McpManager, and it lands in `__main__.py`'s assembly like every other
argument.

### 2.11 The client (as built)

`src/agentclip/shell/app/remote_link.py` is the Shell's end: `RemoteLinkClient`,
which owns one connection, and `RemoteLink`, which is a `Link` the controller
cannot tell apart from `LocalLink` — same `asyncio.Lock` serializing the same 13
calls, same `asyncio.to_thread` hop, same three facts read synchronously off the
object. Only the body of the hop differs: a frame written and an answer read
instead of an engine method.

**Transport-agnostic, and spawn-free.** The client is constructed over a pair of
**text streams** and creates nothing. Increment 2's tests hand it a localhost
subprocess's pipes; increment 3 hands it an SSH exec channel's streams. That
prediction held, with one honest amendment: the two parameters are now
`LineReader`/`LineWriter` **Protocols** rather than `TextIO`, because the SSH
adapter is deliberately not a file object (§2.12). No behaviour moved — a pipe
satisfies both Protocols structurally. That is why there is no `Popen` in `src` — a
subprocess is one way to get a reader and a writer, and choosing it is a
launcher's decision, not the protocol's. (Deployment and the production transport
are increment 3 in full: this increment ships the client, not a way to run it
remotely.)

**One reader, inside the call.** There is no background reader thread.
`roundtrip` writes its call frame and then reads frames itself until that call's
answer arrives. This is sound because of the contract the whole seam already
rests on — **one call in flight per connection**: the controller serializes per
link and only one link is live, so whoever is inside `roundtrip` is the only one
entitled to read, and the server's per-session busy rule refuses a second call
anyway. A reader thread would have bought nothing and cost a queue, a shutdown
protocol and a second place for a frame to go missing.

**Events reach the link they belong to.** `progress`/`output` frames met on the
way to an answer are dispatched **by session id** through the client's registry,
so a parked link's events still reach ITS hooks while somebody else's call is
doing the reading. The engine's hook contract is reimplemented rather than
inherited: hooks fire from the worker thread, must not block, and one that raises
is dropped for good — a progress watcher may never fail a turn, and a wire is no
reason to change that.

**Cancel under the send lock.** One `threading.Lock` guards every write+flush and
is deliberately NOT held across the read that follows, so `request_cancel` — sync
and out-of-band, called from the event loop — gets its frame out while the
roundtrip it interrupts is blocked in a read. It never raises: a dead link fails
the call it was interrupting on its own, and reporting the same death twice out
of a method the UI calls on a keypress buys nothing.

**Death is an exception, never a hang.** EOF on the reader (the server exited,
the channel dropped, the process was killed) raises `EngineLinkError`
immediately, as do a malformed line, an unknown frame type, and an answer
carrying an id that is not the outstanding call's — with one call in flight there
is no other call it could belong to, so the two ends disagree and going on would
be guessing. The two kinds this side raises on its own account (`link_closed`,
`protocol`) name what happened to the LINK; the wire's four name what the far
side answered. `BudgetExceeded` and `EngineStateError` are rebuilt as themselves
(§2.9), because the controller catches exactly those two by type.

**A third kind: `version_mismatch`.** A target that speaks another wire version
is not a bug but a configuration the user can fix, so `hello()` raises it with
the sentence §2.9 specifies — both wire versions, both `agentclip` versions, and
what to do about it — whether the target answered with a wrong-version
`hello_ack` or refused first with an `error` frame. The server's `package`
version is kept on the client (`server_package`) so a later status line can show
which install is answering without another round trip.

A module-level `_conforms(link: RemoteLink) -> Link` pins conformance where mypy
can see it: the tests are not type-checked, and a Protocol nothing declares is a
Protocol nothing enforces.

**One thing the wire changed below the seam.** `LocalHost.spawn` now passes
`stdin=subprocess.DEVNULL`. A tool's command must never inherit the app's own
input — the user's keystrokes in the TUI, the link's frames in the server
process — and on Windows the inheritance is worse than untidy: handles are
synchronous, so a child's startup query of its own stdin (`GetFileType`, which
every CPython start does) queues behind the parent's unfinished blocking
`ReadFile` and never returns. That is exactly the shape of the server — a reader
thread parked on stdin for the next frame while a worker runs a command — and it
deadlocked every `python -c …` the e2e suite ran until this landed.

### 2.12 The SSH transport (as built)

The client was written over two text streams and told to create nothing (§2.11).
This is the increment that produces them: `SshHost.open_link_channel(command) ->
LinkChannel`, in `src/agentclip/executor/hosts/ssh.py`, plus two pure functions
in `src/agentclip/shell/app/engine_launch.py` and one factory in `cli.py`. The
three live in three layers on purpose — the seam may not import a protocol, the
Shell may not import the seam, and `cli` is the module allowed to know both.

**The channel is the opposite shape to a tool call.** `spawn` opens a channel per
command, wraps it in `wrap_command` (setsid, a pidfile, `bash -lc`), merges
stderr into stdout and reports an exit code. `open_link_channel` does none of
that, and each omission is a decision:

- **No `setsid`, no wrapper.** The engine process MUST die with the channel and
  with the connection — that IS §2.3's disconnect model. A session leader would
  survive exactly the event the design says ends the session, leaving an engine
  running on the target with a session store open under it and no way to reach
  it. So the command is sent bare, as the exec channel's own child.
- **`set_combine_stderr(False)`.** stdout is the protocol and nothing else may
  appear on it (§2.9); stderr is the remote process's log.
- **No pidfile, no `SshExec`, no kill path.** There is no "outcome unknown" to
  report because there is no command result: a dead transport surfaces as EOF on
  the reader, and the client turns that into one failed call.

**The two streams.** `LinkChannel.reader`/`.writer` are small adapters, not
`channel.makefile()` — paramiko's `BufferedFile` has its own newline rules and
its own idea of a short read, and the whole framing rests on "one `\n`-terminated
line is one frame". The reader runs the same `recv` loop `SshExec` uses, feeds an
**incremental** UTF-8 decoder (a multibyte character split across two chunks must
not become two replacement characters), and splits on `"\n"`, blocking until a
line or EOF; EOF answers `""`, which is what the client already reads as "the
link closed". The writer is `sendall(s.encode("utf-8"))` with `flush()` as a
documented no-op, and a dead channel surfaces as `OSError`, which the client
already catches as "closed mid-write". Because the two ends now differ in type,
`RemoteLinkClient` takes `LineReader`/`LineWriter` **Protocols** rather than
`TextIO`: a subprocess's pipes satisfy them structurally and so does this
adapter, and stating the two methods is cheaper than making every transport
impersonate a file.

**stderr is drained continuously, onto a daemon thread, into a bounded ~8KB
tail.** Two independent reasons, either sufficient. An unread stderr fills the
channel's window and then wedges the remote process the moment it writes one
more byte — a deadlock whose only symptom is a link that stops answering. And
when the handshake never arrives, what the launch printed to stderr *is* the
diagnosis. `stderr_tail()`, `exit_status()` (None while running) and `close()`
(close the channel, join the drain thread) are the rest of the surface; nothing
above the seam touches a paramiko type.

**What we ask the target to run.** `engine_command(project_root, service=None)`
builds `agentclip-engine --project <root> [--service <key>]` with `shlex.quote`
— POSIX quoting whatever machine this is, because the target's shell reads it and
a remote root with a space is ordinary. Deliberately absent are
`--global-config`, `--home` and `--data-root`: on the target the engine reads the
TARGET's own config, permissions and platform directories by plain local reads.
That is §2.5 seen from the launch side, and passing this machine's paths over
there would be both meaningless and a policy leak. (The localhost e2e suite
passes all three, because there the two machines are one and the isolation is the
whole point.)

**A launch that produced no handshake gets a sentence.** From the client's side a
missing engine, a broken install and a killed process are all EOF, so
`classify_launch_failure(exit_status, stderr_tail, target)` is handed the two
values the *channel* knows and returns what the user reads. It takes plain values
rather than a channel precisely because it lives in `shell.app`, which may not
import `executor.hosts` — which also makes it testable without a network. Exit
127, or `command not found`/`not found` in the tail, is the case worth naming
apart, because it is the one every first connect hits and the one with an action
attached:

> `agentclip-engine is not installed on dev@box - install it with e.g. `uv tool install agentclip``

Anything else stays honest instead of guessing: the status the channel ended with
(or "it is still running") and the target's own stderr, verbatim. A **version**
mismatch never reaches here — the far side answered, and `hello()` already built
the sentence naming both installs (§2.9, §2.11).

**The factory, and why it is not the default.** `cli.make_remote_link_factory(connected,
*, service=None)` takes what `connect_remote` hands back, opens the channel,
wraps its streams in a `RemoteLinkClient`, says hello, and returns
`(factory, RemoteEngine)` — the factory building one `RemoteLink` per
`EngineRequest`, the `RemoteEngine` carrying the client and the channel because
*one* server process on *one* channel hosts every session of a remote run (§2.10)
and closing that channel is how the engine is stopped. A failed handshake closes
the channel and re-raises the `EngineLinkError` carrying the classified message;
the exit status is polled for up to a second first, because EOF and the exit
status arrive from two different places on the transport and "exit 127" is the
difference between naming the fault and quoting stderr at somebody.

It is **not** wired into `main()`'s `--ssh` branch or the GUI's connect dialog.
Increment 3 ships the transport, not the switch. The remote engine still has no
MCP (`__main__.py` passes `mcp_manager=None`), and nobody has verified that the
target's policy loading, stores and skills behave as §2.4/§2.5 say they will —
that is increment 4's parity pass, and flipping the default before it would trade
a mode that works for one that has not been shown to. §2.8's deletion of the
per-call path is increment 5.

**Tested by** `tests/executor/hosts/test_link_channel.py` (framing across chunk
boundaries, a multibyte character split across one, EOF, the bytes actually sent,
the stderr tail and its bound, exit status before/after, the bare command line,
and a `FakeSSHClient`-backed host whose engine exits 127 producing the
not-installed sentence out of `make_remote_link_factory`),
`tests/shell/app/test_engine_launch.py` (the two pure functions), and — gated
behind `AGENTCLIP_SSH_TESTS=1`, needing `agentclip` actually installed on the
target — `tests/executor/hosts/test_link_real.py`. There is deliberately no
fake-SSH end-to-end `serve()` conversation: the localhost subprocess suite
already proves the wire, and these prove the transport under it.

## 3. Architecture sketch

```
LOCAL (Shell)                          REMOTE (target)
┌─────────────────────────┐            ┌──────────────────────────────┐
│ GUI/TUI                 │            │ Engine (session, protocol,   │
│ driver (clipboard,      │   RPC on   │   approvals, store, backups) │
│   screen, automation)   │◄──────────►│ Executor (tools, sandbox,    │
│ shell/app controller    │  SSH exec  │   permissions, LocalHost)    │
│   + RemoteLink          │  channel   │ McpManager (servers spawn    │
│                         │   stdio    │   here)                      │
└─────────────────────────┘            │ config: target's             │
  LocalLink = same interface,          │   .agentclip.toml +          │
  engine in-process (today's mode)     │   permissions.json           │
                                       └──────────────────────────────┘
```

Up (Shell → Engine): session bootstrap request, clipboard payloads (model
replies), gate verdicts, ask_user answers, delegate results, commands
(`set_yolo`, `set_permission_mode`, cancel, new-session).

Down (Engine → Shell): text to paste (outbound deliveries), pause-points
(pending approval w/ preview, ask_user question, delegate request), progress
events, output deltas (RunPanel streaming), notes/toasts, state snapshots.

## 4. Increments (proposed, not yet committed to)

1. **Carve the link seam locally.** — **done.** Introduce the Shell↔Engine link
   interface and route today's in-process calls through `LocalLink`. Pure
   refactor, no wire yet. As built: `shell/app/link.py` holds the Protocol and
   `LocalLink`; the factory returns a `Link`; `SessionController` holds
   `self._link` and its `_engine_call` is gone (the lock and the thread hop moved
   into the link). The progress/output hooks stayed *sync registration* with the
   engine's worker-thread contract rather than becoming link-mediated events —
   turning them into wire messages is increment 2's job, where streaming is
   designed as a first-class message anyway; making them events with no wire
   under them would have been churn, not design. See §2.2 "As built".
2. **Wire protocol + localhost subprocess.** — **done.** JSON-lines over stdio;
   the engine half as a local subprocess behind `RemoteLink` in tests only, with
   streaming (output deltas) and cancel as first-class messages. As built: the
   engine-side package (§2.2), the codec (§2.9), the server loop (§2.10) and the
   client (§2.11). The subprocess spawn lives in the tests and nowhere else —
   `RemoteLinkClient` takes two text streams — so the production transport and
   the deployment that produces it are increment 3's whole job, with no client
   changes owed.
3. **SSH transport + deployment.** — **done.** `RemoteLink` over an SSH exec
   channel running the pre-installed `agentclip-engine` (§2.6), reusing
   `connect_remote` auth/reconnect; detecting a target that has no
   `agentclip-engine` on PATH, and reporting it legibly. As built: the packaging
   half first (the engine entry point is a console script), then
   `SshHost.open_link_channel` + `LinkChannel`, `shell/app/engine_launch.py`'s
   two pure functions, and `cli.make_remote_link_factory` — see §2.12. The
   default `--ssh` mode is deliberately **not** flipped: the factory is additive
   and nothing calls it, because parity (below) has not been shown.
4. **MCP + store on target, parity pass.** Target-side McpManager, stores,
   policy loading; the `/config`-wave permissions.json paths on the target.
5. **Delete SshHost per-call mode** once parity is verified; amend
   `remote-ssh.md`.

## 5. Open points (**OPEN**)

- **RunPanel latency over a real link**: framing, versioning/handshake, streaming
  deltas and cancel are decided and built (§2.9). What is still open is the
  budget for the RunPanel's ~5/sec peek cadence over a real SSH connection — a
  cadence that is free in-process and unmeasured on the wire.
- **ToolContext redesign**: agreed it can't cross the wire, but its remote
  replacement (session-scoped server-side state + thin per-call payload) is
  undesigned. Includes where `backup_hook`, `cancel_event`, and `on_output`
  land in the new shape.
- **Install UX on the target**: *detection* is **resolved** — a launch that dies
  without a handshake is classified from the channel's exit status and stderr
  tail, and a missing engine produces a sentence naming the target and the
  install command (§2.12); a wire-incompatible one is the handshake's version
  refusal (§2.9). What remains open is the UX around it: does the connect flow
  *guide* the install/upgrade — offer to run it, re-check after — or only name
  the command to run, and where does that live (ui-briefs/ssh-connect.md will
  need a revision either way)? Nothing surfaces these messages in a UI yet,
  because nothing calls the remote factory yet. Offline or locked-down targets,
  where the user cannot install anything, may be the case the standalone binary
  below is really for.
- **A standalone single-file binary for the engine half** — a later packaging
  increment: one artifact copied to the target, no Python required over there,
  per-arch builds to produce. Optionally, the cheaper version of the same wish: a
  slim install path (an extra, or a split package) so a target does not drag the
  GUI/TUI dependencies it never imports (§2.6).
- **Backup guarantee across the coarser boundary**: capture-before-overwrite
  must be re-verified once the engine (not Host primitives) does file
  mutation on the target — a naive design could batch/reorder and lose it.
- **Session listing/cleanup on target**: stores now accumulate on the target;
  who prunes them, and does the Shell get a way to browse/pull a past remote
  transcript?
- **Delegate/sub-agent sessions in remote mode**: sub-agent chats are still
  local browser chats — confirm the delegate flow's pause/resume shape needs
  nothing extra over the wire beyond what ask_user needs.
- **Skills discovery in remote mode**: skills load from Claude/OpenCode
  folders — target's folders, local ones, or both? (Consistent choice would be
  target's, per §2.5, but undecided.)
- **`remote-ssh.md` amendments**: enumerate exactly which sections are
  superseded (MCP-stays-local, backups-local, rules/gate split) when this doc
  graduates to binding.
