# Remote Executor — plan (draft)

> **Status: PLAN, not yet binding.** Decisions below were made in a design
> interview on 2026-08-18, grounded in a code investigation of the current
> Shell/Driver/Executor layering. Open points are marked **OPEN** and must be
> resolved (or consciously deferred again) before implementation starts. Once
> the design session for the first increment happens, this document graduates
> into a binding design doc and `remote-ssh.md` gets amended where superseded.
>
> **Exception: §2.2, §2.4, §2.5, §2.6, §2.7, §2.8, §2.9, §2.10, §2.11 and §2.12
> are built, and binding** — increment 1 (the link seam), increment 2 (the
> engine-side package, the wire codec, the server loop, `RemoteLink` and the
> localhost e2e suite), increment 3 (the console script, the SSH link channel,
> launch-failure classification and the remote factory), increment 4 (policy,
> stores, skills, MCP on the target — and the flip) and increment 5 (the
> deletion, §2.8) shipped in full, so those parts describe code rather than
> intent. **All five increments in §4 are done.** Everything else here — §1,
> §3 and the §5 open points — is still plan.
>
> **The default `--ssh` mode IS the remote engine.** Increment 4's last slice
> flipped it: a remote session — terminal launch or in-app connect — dials the
> target as before, then launches `agentclip-engine` over an exec channel and
> drives it through `RemoteLink` (§2.12 "the flip"). Everything that used to be
> a per-call round trip is now local to the target: tools, stores, backups,
> policy, skills and MCP servers.
>
> **The per-call `SshHost` path is DELETED** (2026-08-25, increment 5, §2.8).
> There is no `make_engine_factory(host=…)` any more and no way to assemble a
> remote session on this PC; `SshHost` is the dialled connection the engine's
> link channel is opened on, and nothing else. The paragraphs of §2.4/§2.7 about
> "the legacy per-call path" describe what was removed, not what is there.

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

**As built (increment 4, verified not changed).** Increment 2's factory move
already made this true *mechanically*, and that is worth stating because it means
no store code had to learn about remoteness. `engine/link/factory.py` is the one
place a session's `SessionStore` and `BackupStore` are constructed, and both are
built from what the process it runs in can see: `SessionStore(project_root,
data_root=data_root)` and `BackupStore(session.session_dir, host=session_host)`.
In the `agentclip-engine` server process `__main__.py` passes `host=None` (so
`session_host` is a `LocalHost`) and `--data-root` is absent in production
(`engine_command` deliberately does not send one, §2.12), so the tree lands in
`<project>/.agentclip/` **on the target** and backups are captured by the target's
own filesystem reads. Nothing mirrors home.

The **legacy per-call `SshHost` path** — deleted in increment 5 (§2.8), described
here as the thing this replaced — kept phase 2's answer: `cli` passed
`data_root=default_remote_state_dir(target, root)`, a
`<user_data_dir>/agentclip/remote/<target>-<root>-<hash>/` tree on the OPERATOR's
PC, and the `BackupStore` reads remote bytes over the Host to store them there.
Both are correct for the mode they belong to — the store follows the engine, and
in that mode the engine is here — so the flip of the default is also the moment a
remote session's transcripts stop being local.

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

**Skills are engine-side too, and that closes the question §5 left open.** Skills
load from the machine the ENGINE runs on — the target's `.claude`/`.opencode`/
`.agents` folders, project-local and under the target user's home, in a remote
session. Same reason as the ruleset: a SKILL.md describes what to do with files
that are over there, and it is read by a tool call that runs over there. This is
already what the code does (`engine/link/factory.py` calls `discover_skills` with
the session's `host`/`home`, which in the server process are `LocalHost` and the
target's own `Path.home()`), so the decision records behaviour rather than asking
for a change.

**As built (increment 4): the pinned-local `[approval]` branch is deleted.**
`config.py:load_config` had one special case left over from the `/config` wave —
when `host is not None`, `[approval]` was read from THIS PC's global config.toml
alone and a remote project's `[approval]` table was dropped with a warning. That
branch and its warning are gone: `approval_t = merged.get("approval", {})`, the
same one line every other table gets, with no `host` test anywhere near it. So
the mode, YOLO, the legacy allowlist and the deny tokens now come from the
engine's machine like everything else — global config.toml merged with the
project's `.agentclip.toml`, read through whatever `Host` the load was given.

Two modes, one rule, and it is worth being explicit about the second:

- In the **remote-server** world this section is written for, the engine's
  machine IS the target and the merge is entirely local to it — the operator's
  config.toml is not even reachable from over there (`engine_command` sends no
  `--global-config`, §2.12).
- On the **legacy per-call `SshHost` path**, still the default, the engine runs
  here and the merge is this PC's config.toml plus the TARGET's project file read
  through the Host. So a remote project can now set `yolo`/`mode`/the command
  rules for the session it describes. That is the decided §2.5 semantics — policy
  belongs to the config of the machine the work happens on — and the pinning it
  replaces was the answer to `remote-ssh.md`'s superseded model, not to this one.

The Chat UI's post-connect policy banner says so rather than claiming otherwise
(`shell/chat/remote.py:APPROVAL_POLICY`), for the same reason it names the
ruleset's machine: an invisible policy fact is a footgun whichever way it points.

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

**As built (increment 4).** MCP construction moved into the engine half:
`engine/link/factory.py`'s `EngineBuilder` builds the `McpManager` itself, from
the config *its* side reads, at the same altitude as skill discovery — one
runtime per builder, however many sessions it goes on to build. `cli.py` has no
`McpManager` construction site left (it had two: `main()` and the GUI's
reconnect), and neither shell gained an `executor.mcp` import: the builder
exposes `mcp_statuses()` / `set_mcp_status_hook()` / `close()`, and `cli`'s
`LinkFactory` re-states the first two under the `statuses()` /
`set_status_hook()` names the panes already consumed. So in the
`agentclip-engine` process the reversal is now real — `__main__.py` passes no
manager and no remote target, the builder reads the target's own
permissions.json, and a stdio server spawns **there**, with the target's
environment and cwd. Interactive OAuth is unchanged and still out of scope: the
401/403 → `needs_auth` mapping in `client.py` was already unconditional, so it
means the same thing on either machine.

The runtime is built on the builder's *first ask* rather than in its
constructor, because the config closure a Shell hands in usually closes over the
object it is about to be passed to (`lambda: app.app_config`). Every production
caller asks immediately — `cli.main` reads the statuses to decide whether the
shells get a status source at all — so the connects still kick off at launch,
overlapping the first paint exactly as they did when `main()` built the manager.

**Status now crosses the link.** The builder's `mcp_statuses()` is no longer
reachable only in-process: it is a link-scoped wire method and a field on
`SessionInfo` (§2.9), so a Shell driving a *remote* engine reads the target's MCP
runtime the same way it reads a local one — `await link.mcp_statuses()`, with no
`executor.mcp` import on either path. The `Link` Protocol grew the one call, and
`LocalLink` answers it from a statuses callable `cli.LinkFactory` hands it at
construction (its own `statuses`, which is the builder's). Layering is unchanged
and deliberately so: the Protocol's return type is a **shell-side structural
Protocol**, `shell.app.link.McpStatusLine` (moved down from `controller.py`,
which now imports it), so nothing in `shell/app` names `McpServerStatus` — the
real rows arrive as values, out of the builder in local mode and out of wire's
codec in remote mode.

**And the Shell's surfaces read it that way.** `/mcp` prefers the **live link**:
with a session up it is `await link.mcp_statuses()`, and only with no session
does it fall back to the `mcp_statuses` callable the controller was constructed
with (synchronously, which is what keeps the command a straight line before any
loop is running). The reason is this section's premise — the servers are on the
machine the engine runs on, so the link is the only thing that knows which
machine that is, and a remote session that listed this PC's servers would be
lying about its own tools. The fallback had a matching staleness of its own:
`SessionController.rebind` took the three things a session is built from and MCP
was a fourth, so a GUI that reconnected went on answering a *pre-session* `/mcp`
out of the machine it had just left. `rebind` now takes an optional
`mcp_statuses` (`None` = keep what is current), and the GUI's source is a method
that re-reads its `_mcp_manager` rather than a callable bound to the launch-time
one.

**The cadence in each mode.** Local mode is unchanged and instant: the builder's
status hook fires from the MCP manager's loop thread and both shells repaint on
it. Remote mode has **no push** (§2.9) — so `cli.RemoteEngine` is the status
source over there, duck-typed to exactly what `LinkFactory` gives the shells:
`statuses()` hands back the settle the client cached from the last
`build_session` (**never** a wire call — it is read on the UI thread on every
status paint, and a round trip there would freeze the window while painting a
number), `set_status_hook()` is accepted and dropped, and `close()` closes the
channel. What makes that cache enough is that it is refreshed on every session
build, and both shells now repaint their MCP block on the session-start edge
they were already detecting for the harness log — so the target's settle lands
on screen at the start of the session it belongs to. A server that comes up
later goes unnoticed until the next build or the next `/mcp`, which is §5's open
point and not a surprise.

What did **not** move is the legacy per-call `SshHost` path: it is still the
default `--ssh` mode, its config still comes off the target while the process
spawning servers is this PC, so `cli` passes `mcp_remote_target=<host name>`
there and `client.py`'s stdio refusal and "dialled from this PC" note fire
exactly as before. That parameter is `""` everywhere else, including in
`agentclip-engine`. **It went with §2.8, in increment 5 (2026-08-25)**: the
builder has no `mcp_remote_target` any more, because the arrangement it named —
a config read off one machine, servers spawned by another — cannot happen.

### 2.8 SshHost per-call mode is replaced outright — **AS BUILT (2026-08-25, increment 5)**

Once the remote-Executor mode reaches parity, the per-call `SshHost` tool path
(every file read / command as its own round trip; grep/glob pulling whole
files over SFTP to scan locally) is **deleted**. What survives from
`executor/hosts/ssh.py`/`connect.py` is the connection/auth/reconnect machinery,
repurposed to open the exec channel the remote process is launched and spoken to
on. Nothing deploys source any more (§2.6), so the SFTP side survives only as
connect/auth plumbing — no file is pushed to the target at connect time. One
remote story, clean break — same style as the opencode.json → permissions.json
split.

**As built.** `ssh.py` went from 945 lines to 762 (328 deleted, 145 written).
Gone: `SshExec` (the whole `ExecHandle` — `wait`/`peek`/`kill`/`drain` and the
"outcome unknown" verdict), `wrap_command`, `spawn`, `run_blocking`,
`run_detached`, and the `Host` primitives `write_bytes`/`delete`/`mkdir`/
`rmdir`/`lstat`/`listdir` with their `_mkdirs`/`_stat_mode` helpers. Kept:
connect/auth/reconnect, `open_link_channel`, `LinkChannel`, `_ChannelReader`,
`_ChannelWriter`.

**`SshHost` is now a connection, not a `Host`** — it implements none of the
protocol, and `hosts/connect.py` is its only real consumer. `cli.Launch.host`
and `GuiRuntime.host` are typed `Host | SshHost`, which is the union they
already held; both shells read that slot through `getattr` (`target`,
`connected`, `reconnects`, `reconnect`, `close`) and did not change.

**Two things the deletion list named had to survive in a smaller shape**, and
both for the same reason: they are not the tool path, they are the *connect*
path, and §2.12 keeps connect step 6's `Config` as "what the shell still keeps
locally".

* `read_bytes`, `stat` and `realpath` stay, read-only. Steps 4 and 6 ARE those
  calls — resolve and check the remote root, then read the target's
  `.agentclip.toml`/`permissions.json` through `load_config(host=…)`. This is
  exactly what "the SFTP side survives only as connect/auth plumbing" asks for;
  nothing writes to the target from this side any more.
* `run_blocking` is deleted and replaced by **`probe_command`**, half its size
  and named so it cannot be mistaken for a tool path: one bare `bash -lc` on its
  own channel, merged stderr, read to the exit status, no pidfile, no `setsid`,
  no handle, and never raising (a failed probe is a `(code, text)` pair, because
  steps 3 and 5 are non-fatal). Its only callers are `probe_os` and step 5's
  `printenv`. `bash -lc` is load-bearing for the second: `{env:…}` in the
  target's config means the LOGIN shell's environment.

**`host=` is gone from `make_engine_factory`/`make_engine_builder`/
`EngineBuilder`**, and `mcp_remote_target` with it (§2.7 said it goes in this
increment): an engine runs on the machine its own process runs on, so the
builder just constructs a `LocalHost`. `McpManager`'s own `remote_target`
parameter is left in place, defaulted, and is now unreachable.

**The config layer stopped asking for a `Host`.** `load_config` only ever used
`name` + `read_bytes`, so `hosts/base.py` carves those two out as `FileReader`
and `Host` inherits from it. A connection can satisfy that without pretending to
be a machine tools run on.

**Tests.** `tests/test_launch_remote.py` lost the legacy-assembly pin test and
its section header — the file is purely the flip now. `test_ssh_host.py` went
506 → 359 lines (the wrapper, `spawn`, peek/kill, the write/traverse primitives
and the in-flight-command verdict out; three `probe_command` tests in).
`test_ssh_real.py` was rewritten around the connection and the connect probes.
`test_connect.py`, `test_link_channel.py` and `test_link_real.py` needed only the
`run_blocking` → `probe_command` rename.

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
methods — the two **link-scoped** ones have none: `build_session` because it is
what mints a session, `mcp_statuses` because it never has one); `result`
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
`encode_result`/`decode_result` are table-driven over the 15 methods, so the
server and the `RemoteLink` each hold one line per method and everything that
could drift between them is stated once. `build_session`'s params ARE
`EngineRequest`'s fields; its result is a `SessionInfo` — the session id plus the
three immutable facts the `Link` Protocol exposes synchronously (`chat_name`,
`role`, `build_warnings`), carried home in that one answer exactly as §2.2 said
a handshake would.

**Link-scoped vs session-scoped, and why `mcp_statuses` is the former.**
`wire.LINK_METHODS` is `(build_session, mcp_statuses, skills)` and `is_link_method` is
what the server's dispatch routes on: a link-scoped call is served by the
**builder**, never by `getattr` on a hosted engine. `mcp_statuses` is there
because the MCP runtime is owned by the builder — one manager per process,
however many sessions it goes on to build (mcp.md §3, §2.7 above) — so every
session of one connection would answer it identically, and naming a session on
it would invent a per-session fact that does not exist. Its result is a list of
`McpServerStatus` (all four fields; `state` strict-decoded against the seven-name
lifecycle, so a state this side has never heard of is a `WireError` rather than a
status line nobody can act on).

`skills` is there for the same reason, one machine-shaped fact further: the
builder discovers the skills library once, through its own Host, so in a remote
session the folders scanned are the target's (remote-ssh.md decision 6). Its
result is a `SkillReport` — one body-free `SkillLine` per skill (name,
description, the folder its `SKILL.md` sits in, and whether the model may call
it) plus the roots that were searched, because "no skills found" is only
actionable beside the folders it looked in. The **bodies never cross**: `/skills`
is a listing, and the full instructions are what the `skill` tool loads one at a
time, on the far side.

**The settle rides `build_session`.** `SessionInfo` carries a fourth field,
`mcp_statuses` — the runtime's rows as they stood when the session was built. It
is a *snapshot*, not an immutable fact, and it rides along for the same reason
`build_warnings` does: the Shell wants to paint the settle at the top of a
session, and a round trip to learn what the far side already knew is a round trip
spent on nothing. It is also mostly *settled* by then — `factory._sized_registry`
has already given a pending/connecting runtime up to 0.5s while sizing the
catalog — so the states are usually final rather than "connecting". "Usually",
not "always", which is exactly what the link-scoped pull is for.

**Cadence, v1: the Shell pulls; nothing is pushed.** There is no
`mcp_status`-shaped event frame. The builder's `set_mcp_status_hook` fires
engine-side and stays engine-side, so a server that finishes connecting after
`build_session` answered does not tell the Shell — the Shell asks again. That is
an accepted limitation of v1 and not an oversight: a push would need a background
reader on the client (see §5), and the states that matter most (`failed`,
`missing_sdk`, `disabled`, and the connected ones the catalog was sized against)
are already final by the time the settle rides home.

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
answer arrives. That rests on **one call in flight per connection** — and since
link-scoped calls exist, the per-link `asyncio.Lock` can no longer promise it:
`mcp_statuses` belongs to no link, so no per-link lock stands between it and a
session call's reads, and two readers on one stream would each consume frames the
other was waiting for. So the serialization now lives **in the client**: a
`threading.Lock` held across the whole of `roundtrip`, write and
read-until-answer together. The per-link locks stay on top of it — they are the
`Link` contract, and they mirror the server's per-session busy rule — and the
double-serialization costs nothing, because a connection that allowed two calls
at once would have nowhere to put the second one's answer. A reader thread would
have bought the same guarantee and cost a queue, a shutdown protocol and a second
place for a frame to go missing.

`send_cancel` stays **outside** the call lock (it takes only the send lock, as
below): waiting for the call lock is precisely waiting for the call it exists to
interrupt.

**The MCP surface on this side.** `RemoteLinkClient.mcp_statuses()` is a sync
link-scoped roundtrip — usable from a worker thread *before any session exists*,
which is when a Shell first wants to paint the block — and `RemoteLink`
`await`s it through `asyncio.to_thread` behind the link's own lock. The settle
that came home on `build_session` is kept as `build_mcp_statuses` on both the
client and the link it minted, so the first paint costs no round trip at all.
That cache is also what `cli.RemoteEngine.statuses()` serves the shells' status
panes (§2.7): it is refreshed on every `build_session`, and serving it is the
only way a sidebar paint can be free of the network. The `await` version is for
callers who asked for a fresh reading and are prepared to wait for one — `/mcp`
is the whole of that list.

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
LinkChannel`, in `src/agentclip/executor/hosts/ssh.py`, plus the pure functions
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

**Two spellings, because sshd's PATH is not the user's** (added 2026-08-19, after
a live target). `uv tool install agentclip` — the method this document tells
people to use — puts the console script in `~/.local/bin`, and sshd's
*non-interactive* exec channel gets the stock
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` with nothing of
the user's own added: `~/.local/bin` is on an interactive PATH because
`~/.profile` puts it there, and no profile is read for `ssh host command`. On
Ubuntu that is the rule, not the exception, so our own documented install
produced a launch our own launcher called uninstalled. So a launch that comes
back "no such command" (`is_missing_engine`: exit 127, or `command not
found`/`not found` in the tail) is retried **once**, at
`fallback_engine_command(home, root, service)` — `<remote home>/.local/bin/
agentclip-engine`, joined POSIX-style onto the home connect step 5 already
captured, passed resolved rather than as a `~` since a bare exec channel has no
shell to expand one. If that answers, the session proceeds exactly as if the
plain name had worked. One well-known location and no further PATH-guessing:
anything past it is a guess about a machine we can see one command at a time.

**And never `bash -lc`.** A login shell would fix the PATH and break the
protocol: stdout of that process IS the wire (§2.9), and a `.profile`/`.bashrc`
that prints anything — a banner, an SDK's shell hook, a stray `echo` — prepends
its text to the first JSON line and corrupts the handshake. The command stays
bare (which is also what §2.3's die-with-the-channel needs), and the PATH problem
is solved by naming the path instead of by summoning a shell.

**A launch that produced no handshake gets a sentence.** From the client's side a
missing engine, a broken install and a killed process are all EOF, so
`classify_launch_failure(exit_status, stderr_tail, target)` is handed the two
values the *channel* knows and returns what the user reads. It takes plain values
rather than a channel precisely because it lives in `shell.app`, which may not
import `executor.hosts` — which also makes it testable without a network. The
"no such command" case is worth naming apart, because it is the one every first
connect hits and the one with an action attached — and by the time it is
classified BOTH spellings have been tried, so the sentence says so rather than
claiming an install that may well be sitting right there:

> `agentclip-engine is not on the non-interactive PATH of dev@box (tried 'agentclip-engine' and '~/.local/bin/agentclip-engine') - install it with e.g. `uv tool install agentclip`, or symlink it into /usr/local/bin`

Anything else stays honest instead of guessing: the status the channel ended with
(or "it is still running") and the target's own stderr, verbatim — and it is also
what stops the retry, since a traceback or a killed process is a diagnosis and
trying another path would replace it with a worse one. A **version** mismatch
never reaches here — the far side answered, and `hello()` already built the
sentence naming both installs (§2.9, §2.11); it does not retry either, since the
install this one found is the one to update.

**The factory.** `cli.make_remote_link_factory(connected, *, service=None)` takes
what `connect_remote` hands back, opens a channel per spelling (`_launch_engine`,
above), wraps its streams in a `RemoteLinkClient`, says hello, and returns
`(factory, RemoteEngine)` — the
factory building one `RemoteLink` per `EngineRequest`, the `RemoteEngine`
carrying the client and the channel because *one* server process on *one* channel
hosts every session of a remote run (§2.10) and closing that channel is how the
engine is stopped. A failed handshake closes the channel and re-raises the
`EngineLinkError` carrying the classified message; the exit status is polled for
up to a second first, because EOF and the exit status arrive from two different
places on the transport and "exit 127" is the difference between naming the fault
and quoting stderr at somebody.

### The flip (as built, increment 4)

**Both shells now call it, and nothing calls the per-call assembly.** `--ssh` and
the GUI's connect dialog run the same two beats: `connect_remote` as before, then
`make_remote_link_factory` over what it handed back. What each shell does with
the pair is where the two differ, and each difference is a decision:

- **The terminal launch** carries the dialled machine home on `cli.Launch.remote`
  — the whole `ConnectedRemote`, not a flattening of it, because the factory
  takes the host and the remote root and a launch's summary of them is a second
  place to get them wrong. `main` then branches once, and below that branch the
  TUI is handed the same two things either way: something that mints a `Link` per
  session, and something answering `statuses()`/`set_status_hook()`. A launch
  failure is `exc.detail` on stderr and exit 2 — the same stream and the same
  code as every other fatal step of going remote, and *`detail`* rather than
  `str(exc)` because "link_closed:" is the wire's vocabulary, not a sentence.
- **The dialog** runs the launch as a **seventh checklist row**
  (`connect.STEP_ENGINE`, "Start the engine on the target"). It cannot be a step
  of `connect_remote` — that module is in the host seam and may not import a
  protocol — so the vocabulary is split in two: `CONNECT_STEPS` is the sequence's
  own six and `CHECKLIST_STEPS` is the seven a human watches, with the seventh
  reported by whoever does the launch. It is the one row that can fail after
  every other row is green, and it shows the classified sentence verbatim.
  Failing it leaves the window on the machine it was already on, and Retry
  re-runs the whole thing in place — the point of the surface, now covering the
  newest failure (install the engine on the box, press Retry).

**What the shell still keeps locally**, and why the flip does not change it: the
`Config` built from the target's project file (connect step 6) still drives THIS
side's knobs — the service preset, the clipboard backend, the paste budget the
composer displays. The engine does not read it. It re-derives its own from the
target's layers, per service, on every session build (§2.5, §2.6), which is why
`engine_command` sends `--project` and at most `--service` and why none of
`os_name`/`data_root`/`home` is passed over there: they describe a session
assembled here, and there is not one. `os_name` in particular is now the target's
own answer — `make_engine_builder`'s `os_name=None` default reads
`platform.system()` in the process it runs in, which over there IS the target.

**Teardown is engine-then-transport, on both shells.** `RemoteEngine.close()`
closes the link channel; the host's `close()` closes the SSH connection under it.
A host closed first would take the channel with it and turn an orderly shutdown
into a dropped link. The GUI's ownership cell holds whichever object is live
(`LinkFactory` at launch, `RemoteEngine` after a connect — both answer `close()`,
which is all a teardown needs to know), and a reconnect closes the previous
engine before the previous host for the same reason. A launch that FAILS closes
the freshly dialled host on the way out: the failing path is the one that must
not leak a connection per retry.

**Two things stopped happening on this PC.** No local session tree is created or
pruned for a remote run — the store follows the engine (§2.4), so an empty
`<user_data_dir>/agentclip/remote/…` directory would be a history that is not
there. And `mcp_remote_target` now evaluates to `""` on every path that still
builds a local builder, because a remote session's servers are the target's and
its own engine spawns them; the parameter **went with §2.8's deletion**
(2026-08-25).

**The RemoteEngine is the status source unconditionally**, where the local
`LinkFactory` is passed through `_mcp_source` and dropped when it has no rows.
The asymmetry is the cadence's (§2.7): the remote settle only arrives with the
first `build_session`, so gating on a non-empty reading would drop the source
before it could ever answer. That also made one type honest that had been
coincidentally true: the TUI's `McpStatusSource` named `McpServerStatus`, and the
rows a remote session paints came off the wire and are not those objects. Both
shells now declare the structural `shell.app.link.McpStatusLine` — the four
fields a status line IS — so the local mode stops being a coincidence and the
remote mode stops being a type error.

**Tested by** `tests/executor/hosts/test_link_channel.py` (framing across chunk
boundaries, a multibyte character split across one, EOF, the bytes actually sent,
the stderr tail and its bound, exit status before/after, the bare command line,
and a `FakeSSHClient`-backed host whose engine exits 127 at both spellings,
producing the tried-both sentence out of `make_remote_link_factory`, with the
fallback built from the home the connect sequence captured),
`tests/shell/app/test_engine_launch.py` (the pure functions, including the
fallback spelling and the predicate the retry hangs on),
`tests/test_launch_remote.py` (the flip itself: `cli.main` end to end over a
`FakeSshHost` whose `open_link_channel` serves a **scripted handshake** — the real
connect sequence, the real factory, the real client, and no network — pinning the
command line, the `~/.local/bin` retry on a 127 and the launch proceeding
normally after it, a non-127 failure NOT retrying, the `--service` pass-through,
the absent local session tree, the
`RemoteEngine` as the shells' MCP source, channel-before-host teardown, and both
dead-launch sentences arriving on stderr with exit 2; plus the legacy assembly,
kept and renamed, still building a whole session over one host),
`tests/shell/chat/test_connect.py` (the seventh row: seven steps in order, a
not-installed launch failing that row with the six before it still green and
nothing adopted, a version mismatch naming both installs, and a retry in place),
and — gated behind `AGENTCLIP_SSH_TESTS=1`, needing `agentclip` actually
installed on the target — `tests/executor/hosts/test_link_real.py`. There is
deliberately no fake-SSH end-to-end `serve()` conversation: the localhost
subprocess suite already proves the wire, and these prove the transport under it.

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
   default `--ssh` mode was deliberately **not** flipped by this increment: the
   factory shipped additive, because parity (below) had not been shown.
4. **MCP + store on target, parity pass, and the flip.** — **done.** Policy
   loading, by deleting the one branch that still pinned `[approval]` to the
   operator's PC (§2.5 "as built"); the verification that stores (§2.4) and
   skills already live with the engine and needed no change; MCP, by moving
   construction out of `cli.py`'s two sites into the engine half's
   `EngineBuilder`, which is what makes `engine/link/__main__.py` need no MCP
   argument at all (§2.7 "as built"); the Shell's side of the same, so the
   surfaces a user actually reads work in remote mode — `/mcp` through the live
   link, `rebind` carrying the MCP source with the other three ingredients,
   `cli.RemoteEngine` as the status source the sidebars consume, and a repaint
   on the session-start edge (§2.7 "the cadence in each mode"); and finally the
   **flip** — `--ssh` and the GUI's connect dialog both launch the engine on the
   target, the dialog showing it as a seventh checklist row, and neither reaches
   the per-call assembly any more (§2.12 "the flip").
5. **Delete SshHost per-call mode** once parity is verified; amend
   `remote-ssh.md`. — **done (2026-08-25).** See §2.8 "as built" for what went
   and for the two things that had to survive in a smaller shape.

## 5. Open points (**OPEN**)

- **RunPanel latency over a real link**: framing, versioning/handshake, streaming
  deltas and cancel are decided and built (§2.9). What is still open is the
  budget for the RunPanel's ~5/sec peek cadence over a real SSH connection — a
  cadence that is free in-process and unmeasured on the wire. The flip makes this
  the *default* remote experience rather than an opt-in one, so it is the open
  point most likely to be felt before it is measured.
- **A real MCP push over the wire**: v1's cadence is a pull (§2.9) — the settle
  rides `build_session`, the shells repaint from it at the session-start edge,
  and `/mcp` re-asks through the link (§2.7 "the cadence in each mode"). A server
  that connects (or fails) *after* the build therefore goes unnoticed until the
  next build or the next `/mcp`, and `RemoteEngine.set_status_hook` is a
  documented no-op in the meantime — the shells register one either way, so a
  push that arrived later would have a listener waiting for it. Turning the
  builder's `set_mcp_status_hook` into an event frame
  needs a place on the client for an unsolicited frame to land while no call is
  outstanding — a background reader thread (with the queue and shutdown protocol
  §2.11 deliberately avoided) or a poll timer in the Shell. Which one, and
  whether the cost is worth a status pane that updates itself, is open.
- **ToolContext redesign**: agreed it can't cross the wire, but its remote
  replacement (session-scoped server-side state + thin per-call payload) is
  undesigned. Includes where `backup_hook`, `cancel_event`, and `on_output`
  land in the new shape.
- **Install UX on the target**: *detection* and *surfacing* are **resolved** — a
  launch that dies without a handshake is classified from the channel's exit
  status and stderr tail, a missing engine produces a sentence naming the target
  and the install command (§2.12), a wire-incompatible one is the handshake's
  version refusal (§2.9), and both now reach a human: stderr + exit 2 from the
  terminal launch, the checklist's `engine` row in the dialog, where Retry
  re-runs the attempt in place. What remains open is whether the connect flow
  should *guide* the install/upgrade — offer to run it, re-check after — rather
  than only naming the command. Offline or locked-down targets, where the user
  cannot install anything, may be the case the standalone binary below is really
  for.
- **A standalone single-file binary for the engine half** — **partly built.**
  `packaging/agentclip-engine.spec` freezes `agentclip.engine.link.__main__`
  into a onefile `agentclip-engine`, and `scripts/build-exe.sh` builds it on
  Linux/macOS (`--engine-only` skips the full app and its `cv`/`gui` extras, for
  a target that will never open a window). The binary carries the `mcp` SDK —
  §2.7 puts the servers on the target, so an engine that cannot speak MCP has
  given up its reason for being there — and *excludes* textual, pillow,
  pywebview, opencv, tkinter and paramiko, which is §2.6's "the unused weight is
  disk" complaint answered: **21 MB against the full app's 78**. The excludes are
  `tests/test_layering.py`'s import-direction rule expressed to PyInstaller, so a
  stray shell/driver import shows up as a fat artifact rather than as a target
  that needs GTK to start a session. `agentclip-engine --version` is the smoke
  test (argparse answers it before the `--project` required-check), and it is
  also what a human on a target uses to chase the handshake's version refusal.
  **Still open:** per-distro/per-arch builds — a frozen binary is glibc- and
  arch-specific, so the build has to happen on (or for) each target family, and
  nothing produces those in CI. Placing the artifact on the target is still the
  user's job; nothing is pushed at connect time (§2.6 unchanged). Optionally,
  still, the cheaper version of the same wish: a slim *install* path (an extra,
  or a split package) for targets that do have Python.
- **Backup guarantee across the coarser boundary**: capture-before-overwrite
  must be re-verified once the engine (not Host primitives) does file
  mutation on the target — a naive design could batch/reorder and lose it.
  Increment 4 settled *where* the backups land (§2.4) and nothing else about
  this: the ordering guarantee still has to be re-argued when a tool call
  becomes one wire message instead of a sequence of Host primitives.
- **Session listing/cleanup on target**: stores now accumulate on the target —
  and since the flip they really do, on every remote run, with nothing pruning
  them: `main` used to call `prune_sessions` over the local remote-state tree and
  that tree is not where a session lives any more. Who prunes the target's, and
  does the Shell get a way to browse/pull a past remote transcript?
- **Delegate/sub-agent sessions in remote mode**: sub-agent chats are still
  local browser chats — confirm the delegate flow's pause/resume shape needs
  nothing extra over the wire beyond what ask_user needs.
- ~~**`remote-ssh.md` amendments**: enumerate exactly which sections are
  superseded (MCP-stays-local, backups-local, rules/gate split) when this doc
  graduates to binding.~~ **Done with the flip.** `remote-ssh.md` now carries
  superseded markers on decision 2 (per-call exec channels), decision 8 (remote
  kill-tree), the phase-3 MCP mechanics (MCP-stays-on-the-host, the stdio
  refusal), and "where a remote session's own state lives" — each pointing at the
  section here that replaced it. §2.8's deletion (2026-08-25) closed the loop:
  each marker now says the machinery is GONE, and the document carries a
  HISTORICAL, NOT BINDING header naming what survives in the code and what does
  not.
