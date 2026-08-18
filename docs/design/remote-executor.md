# Remote Executor — plan (draft)

> **Status: PLAN, not yet binding.** Decisions below were made in a design
> interview on 2026-08-18, grounded in a code investigation of the current
> Shell/Driver/Executor layering. Open points are marked **OPEN** and must be
> resolved (or consciously deferred again) before implementation starts. Once
> the design session for the first increment happens, this document graduates
> into a binding design doc and `remote-ssh.md` gets amended where superseded.
>
> **Exception: §2.2 is built, and its "As built" subsection is binding** —
> increment 1 (the link seam) shipped, so that part describes code rather than
> intent. Everything else here is still plan.

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

### 2.6 Deployment: SFTP source + uv

Each connect, push the needed source subtree (executor, engine, protocol,
config — no shell/driver; the investigation confirmed executor code imports
nothing from shell/driver) plus a lockfile over the existing SFTP machinery,
then start it with `uv run` on the target. uv resolves Python ≥3.11 and deps
(platformdirs, tomli_w, optional `mcp` extra). If uv is missing, offer a
one-time bootstrap install. No per-arch artifacts; version skew is a non-issue
because the host pushes exactly its own source. `paramiko` is not shipped
(the remote half never SSHes out; it uses `LocalHost`).

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
`executor/hosts/ssh.py`/`connect.py` is the connection/auth/reconnect/SFTP
machinery, repurposed to deploy and talk to the remote process. One remote
story, clean break — same style as the opencode.json → permissions.json split.

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
2. **Wire protocol + localhost subprocess.** JSON-lines over stdio; run the
   engine half as a local subprocess behind `RemoteLink` in tests only.
   Streaming (output deltas) and cancel as first-class messages.
3. **SSH transport + deployment.** SFTP push + `uv run` bootstrap, `RemoteLink`
   over the SSH exec channel, reusing `connect_remote` auth/reconnect.
4. **MCP + store on target, parity pass.** Target-side McpManager, stores,
   policy loading; the `/config`-wave permissions.json paths on the target.
5. **Delete SshHost per-call mode** once parity is verified; amend
   `remote-ssh.md`.

## 5. Open points (**OPEN**)

- **Wire protocol details**: framing (JSON-lines assumed), versioning/handshake
  shape, how streaming output deltas and best-effort cancel are encoded, and
  the latency budget for the RunPanel's ~5/sec peek cadence over the wire.
- **ToolContext redesign**: agreed it can't cross the wire, but its remote
  replacement (session-scoped server-side state + thin per-call payload) is
  undesigned. Includes where `backup_hook`, `cancel_event`, and `on_output`
  land in the new shape.
- **Bootstrap/first-connect UX**: how uv-missing is detected/reported, whether
  the bootstrap install needs explicit user consent in the connect flow
  (ui-briefs/ssh-connect.md will need a revision), offline targets.
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
