# Remote development over SSH

Status: **HISTORICAL, NOT BINDING** (2026-08-25). Agreed and implemented in full
(grilling session 2026-08-11), then **superseded** (2026-08-19) by
`docs/design/remote-executor.md`, which moved the engine itself onto the target
— and, on 2026-08-25, **the code this document describes was deleted**
(remote-executor.md §2.8, increment 5).

> **What `--ssh` does now.** This document describes a mode where AgentClip runs
> here and reaches the target one primitive at a time — an exec channel per
> command, an SFTP read per file. That mode is gone. A remote session launches
> `agentclip-engine` on the target and drives it over one wire connection
> (remote-executor.md §2.12 "the flip"); tools, stores, backups, policy, skills
> and MCP servers are all local to the target and no primitive crosses the link.
>
> **What survives in the code**, and it is the reason this file is kept rather
> than deleted — it is the history of how that seam got built:
>
> * The whole **connect/auth/reconnect machinery** of `SshHost` (decisions 3, 5,
>   7): the ssh_config lookup, the auth ladder, the host-key question, the
>   password that is asked at most three times and then reused, and the
>   LIVE↔DEAD state machine. The link channel is opened on exactly this.
> * **`SshHost.open_link_channel`** and `LinkChannel` — the one exec channel a
>   session now has, described by remote-executor.md §2.12, not here.
> * **`SshHost.open_tunnel`** and `Tunnel` — **AS BUILT (2026-08-25)**, and new
>   rather than surviving: the connection carries a second kind of channel now.
>   A UI monitor runs where the *pixels* are, and when those pixels are on the
>   SSH target the Chat UI has to reach a TCP port over there. `open_tunnel(
>   dest_host, dest_port)` opens a `direct-tcpip` channel on the live connection
>   and binds an ephemeral `127.0.0.1` listener in front of it, so
>   `RemoteUIMonitor.connect("127.0.0.1", tunnel.local_port)` works unchanged —
>   no external `ssh -L`, no second SSH process, no port chosen by hand, and
>   nothing left running when the tunnel is closed. The channel opens **eagerly**,
>   at `open_tunnel` time, precisely so a destination that is not listening fails
>   into the connect dialog that asked rather than into a monitor handshake much
>   later. Exactly **one** local connection is served (the listener closes on the
>   first `accept`, so a second dial is refused — one brain), and two daemon
>   threads pump 64 KiB chunks each way until either side EOFs. The one place its
>   error mapping departs from `open_link_channel`: a `ChannelException` means the
>   *target* refused to reach the destination while the SSH link is healthy, so it
>   raises `ConnectionRefusedError` naming `host:port` and does **not** mark the
>   host dead — a mistyped port must not cost a re-dial and a fresh
>   authentication. Every other paramiko/OS failure is `mark_dead` as usual.
>   Covered by `tests/executor/hosts/test_ssh_tunnel.py` (fake channel, real
>   loopback sockets; no `real_ssh` marker).
> * The **connect sequence** (`executor/hosts/connect.py`), unchanged in shape:
>   its six steps still resolve, dial, probe the OS, check the remote root,
>   capture home + environment, and read the target's config. What they run on
>   shrank to match — one `probe_command` (a bare `bash -lc`, no `setsid`, no
>   pidfile, no `ExecHandle`) and three read-only SFTP calls.
> * **Decisions 1, 4, 6** and "the target owns its policy", which the flip made
>   *more* true rather than less.
>
> **What was deleted**, and with it every sentence below that describes it:
> `SshExec` (the `ExecHandle` over an exec channel), `wrap_command` (the
> `setsid`/pidfile kill-tree wrapper), `spawn`, `run_blocking`, `run_detached`,
> and the `Host` filesystem primitives `write_bytes`/`delete`/`mkdir`/`rmdir`/
> `lstat`/`listdir`. `SshHost` no longer implements the `Host` protocol at all —
> it is a **connection**, not a machine tools run on. The `host=` parameter of
> `make_engine_factory`/`make_engine_builder` went with it: an engine now always
> runs on its own machine.
>
> Individual sections that no longer describe what a remote session does carry
> their own marker below.

## Goal

AgentClip must be able to drive development on a remote Linux device while the tool
itself keeps running on the host PC (clipboard relay and screen detection require the
host). The model's world is the remote machine; the chat/copy-paste machinery stays
local.

## Decisions (interview outcomes)

1. **All-remote scope.** When a remote session is active, *every* OS-touching tool
   (run_command, read/write/edit/delete, list_dir, glob, grep, skill discovery) runs
   against the remote host. No hybrid local-files/remote-shell mode — one source of
   truth. *(Still true, and more so: the whole executor is over there now.)*

2. **Persistent connection, fresh shell per command.** — **SUPERSEDED (2026-08-19)
   by remote-executor.md §2.1/§2.12, and DELETED (2026-08-25) by its §2.8.**
   Kept for the record only: `SshHost.spawn`, `SshExec` and the wrapper are
   gone. What a `--ssh` connect opens now is ONE
   long-lived exec channel running `agentclip-engine`, and a `run_command` is a
   wire message to a process that is already on the box — it does not cross the
   link at all. The no-state-between-commands semantics below still hold, because
   the engine over there runs each command the way a local engine does.
   *(Original text.)* Connect + authenticate once at
   launch. Each `run_command` runs on its own exec channel as `bash -lc '<cmd>'` from
   the workspace root. The login shell sources profile/rc files every time — the same
   semantics native CLI agents (e.g. Claude Code) have locally, and the same
   no-state-between-commands semantics AgentClip already has locally. Exported vars /
   `cd` do not persist between commands, by design.

3. **Narrow `Host` primitives seam.** New interface with two implementations,
   `LocalHost` and `SshHost` (plus `FakeHost` for tests). Primitives only:
   - `exec(command, cwd, timeout, cancel-poll) -> (exit_code, merged_output)` with
     cooperative cancellation (poll slices, as today) and kill-tree semantics
   - `read_bytes`, `write_bytes` (creating parent dirs), `delete`
   - `stat`/`lstat`, `listdir` (entries with type info)
   - `realpath` (for the sandbox jail)
   All higher logic — grep's pure-Python regex scan, glob's pruning walk, edit_file's
   matching, tail-capping, caps/limits — stays **above** the seam as shared code.
   **Rule for future tools: write against Host primitives and it works everywhere.**
   Escape hatch (explicitly deferred): a Host may later advertise an accelerated
   bulk-search capability (e.g. shipping the same Python scan code to remote `python3`
   via stdin) if SFTP-walk grep proves too slow. Not in v1.

4. **Launch-time target only.** `agentclip --ssh <target> --remote-root <path>` plus a
   `[remote]` table in config for saved targets. One session = one host; no mid-session
   switching (bootstrap OS claim, Workspace jail, and learned paths must stay
   consistent). Host-hopping = new session. A UI for this may come later.

5. **Connection loss: lazy auto-reconnect, honest reporting.** Keepalives (~30s). A
   command in flight when the link dies returns a tool result stating *connection lost;
   command outcome unknown* (it may have run, half-run, or still be running) — never
   pretend it didn't execute. The harness marks the connection dead and transparently
   re-dials + re-authenticates on the next command (interactive prompt if needed).
   Session/chat/workspace state survive.

6. **Policy local, project remote.** — **SUPERSEDED, see "Revision: the target owns
   its policy" below.** Kept for the record only; the code no longer reads this way.
   - Permissions (the JSON ruleset + deny-token backstop): **always local** —
     the user's policy must not weaken because of remote config.
   - Skills (SKILL.md discovery): **remote** in a remote session — via Host reads.
   - Project `.agentclip.toml`: **remote** — read via Host after connect, before engine
     construction. Global `config.toml` and CLI flags stay local.
   Rule of thumb: anything governing what the harness *may do* is local; anything
   describing the *project* is remote.

7. **Library: Paramiko.** Synchronous (matches the threading engine), persistent
   connection + exec channels + SFTP in one dependency. `ssh.exe` subprocess is ruled
   out: Windows OpenSSH has no ControlMaster multiplexing, so it cannot give
   authenticate-once semantics. Auth flow: parse `~/.ssh/config` (aliases work), try
   agent + default keys, fall back to interactive password/2FA prompt at launch;
   honor `known_hosts`, prompt-to-accept on unknown host keys. Fail fast: connect,
   authenticate, probe `uname` and remote root existence *before* the TUI starts.

8. **Remote cancel/kill.** — **SUPERSEDED (2026-08-19) by remote-executor.md
   §2.9/§2.12, and DELETED (2026-08-25) by its §2.8.** Kept for the record only:
   there is no `setsid` wrapper, no pidfile and no kill exec left in `SshHost`.
   A cancel is now a `cancel` frame on the wire and
   the kill happens ON the target, by the engine over there, using the ordinary
   local kill-tree — which is also why the LINK channel is opened bare, with no
   `setsid`: the engine process must die WITH the channel (§2.3), where a tool's
   command must survive its own. *(Original text.)* Closing a Paramiko channel
   does not kill the remote
   process. Launch via `setsid bash -lc '…'` capturing the PID; cancel/timeout issues
   a separate `kill -- -<pgid>` exec — the remote twin of `_kill_tree`.

9. **Testing.** Unit tests: every tool runs against `FakeHost` (in-memory fs +
   scripted exec). `LocalHost` keeps today's real-subprocess tests. `SshHost`
   integration tests are gated: `AGENTCLIP_SSH_TESTS=1` +
   `AGENTCLIP_SSH_TARGET=user@host`, skipped by default, never run without the user's
   explicit go-ahead (same rule as the `real_os` gate).

## Implementation notes

- `ToolContext` carries the `Host`; it is the seam every handler already receives.
- `Workspace` (sandbox path jail) resolves through Host `realpath`/`lstat` so escape
  detection works identically on both sides. Remote paths are POSIX; keep local
  Windows semantics intact.
- ~~Backups (`backup_hook`) keep storing locally: read remote bytes before
  overwrite.~~ **Superseded (2026-08-19) by remote-executor.md §2.4:** the
  `BackupStore` is built by the engine, on the target, over its own `LocalHost` —
  the bytes never travel.
- Bootstrap: only the existing `on {os_name}` slot changes (e.g. `on Linux (ssh)`).
  **Amended (2026-08-19):** the slot is now filled by the target's own
  `platform.system()`, because the process rendering the bootstrap is over there
  (remote-executor.md §2.12); `cli` no longer supplies an `os_name` for a remote
  session at all.
  Paste budget has ~200 chars slack under 12k — add **no** new bootstrap prose.
- Permission gate is upstream of handlers (`Engine._build_plan`) and needs no changes.
- Launch order: CLI + local global config → connect SSH → read remote
  `.agentclip.toml` → build Config/Workspace/Engine → TUI.

## Phasing

- **Phase 1 (pure refactor, no behavior change) — done:** introduce `Host`, `LocalHost`,
  `FakeHost`; move `shell.py` and `fs_tools.py` onto the seam; `Workspace` resolution
  via Host; full suite green, ruff + mypy clean.
- **Phase 2 — done:** `SshHost` (Paramiko), `[remote]` config + CLI flags, launch/auth
  flow, reconnect, remote bootstrap facts, remote skills/project-config reads, gated
  integration tests.
- **Phase 3 — done:** policy moves to the target. The permission ruleset and both
  MCP layers are read off the target through the Host, `{file:}`/`{env:}` resolve
  over there, and stdio MCP servers are refused and reported. `[approval]` was
  pinned to this PC by this phase and is **no longer** (remote-executor.md §2.5
  took the last table with it). See the revision section below.
  **Its MECHANICS are superseded (2026-08-19)** even where its *conclusions*
  survive: policy still belongs to the target, but nothing is read "off the
  target through the Host" any more — the engine is over there and reads its own
  machine's files locally (remote-executor.md §2.5, §2.6). The two consequences
  worth naming are that MCP no longer stays on this PC (§2.7 reverses it — stdio
  servers spawn on the target, and the refusal below fires only on the legacy
  path) and that `{file:}`/`{env:}` are plain local reads over there rather than
  SFTP and a cached `printenv`. Marked per-paragraph below.

## As built (phase 2)

Decisions taken while implementing that the interview did not settle. Where one
deviates from the text above, it says so.

**Configuration.** Saved targets are `[remote.<name>]` tables (`host`, `user`,
`port`, `root`); a target whose table has no `host` is named for its host. The
session's target comes only from `--ssh`, which takes a saved name, an
`~/.ssh/config` alias, or `[user@]host[:port]`; `--remote-root` overrides a saved
`root` and is required when nothing else supplies one. Nothing in the config file
selects a target: a session goes remote because the command line said so.

A leading `ssh ` is unwrapped before any of that, so a value pasted whole off a
shell prompt (`ssh wsl`) names the machine it obviously names (`config.py`,
`ssh_destination`, applied once in `_load_remote` so the saved-name lookup, the
parse and the proposed save name all see the destination). Only the plain
`ssh <destination>` form: flags change what the destination means, so anything
left holding whitespace fails the resolve step with a sentence saying what a
destination looks like, rather than ticking green and reaching `getaddrinfo`
two steps later.

**Remote paths.** The `Host` protocol keeps `pathlib.Path`. `SshHost` normalizes
every incoming path to a POSIX string on the way to the wire and builds every
outgoing one from a POSIX string, so a Windows `Path` carrying `/home/dev/app`
survives (`joinpath`/`relative_to`/`parts`/`as_posix` are lexical and correct on
it). Two accepted consequences, documented in the module: a remote file name
containing a literal backslash would be split into components, and Windows path
comparison is case-insensitive, so two remote paths differing only in case
compare equal above the seam. A parallel `RemotePath` type through every tool was
rejected as far more invasive than the failure modes are worth.

**Case sensitivity is the host's.** `Host.case_sensitive` was added: glob's
matching rules and the skill-folder scan order come from the machine the files
are on, not from `os.name`.

**Undo across the seam.** `Host` gained exactly two directory primitives,
`mkdir` and `rmdir`, because restoring a backup may need a directory back and an
undone creation may leave one empty. No general directory API: no tool may create
or remove directories. Undo no longer preserves mtime/mode (the seam moves bytes).

**Where a remote session's own state lives.** — **SUPERSEDED (2026-08-19) by
remote-executor.md §2.4.** The store follows the ENGINE, and the engine is on the
target: a remote session's transcripts and backups now land in
`<project>/.agentclip/` over there, and `cli.main` no longer creates or prunes
the local tree for one. `default_remote_state_dir` and `SessionStore`'s
`data_root` both still exist — the first because the connect sequence still
computes one, the second for the localhost e2e suite's isolation — but since
§2.8 nothing writes a remote session's state here. The accepted cost of the new answer is stated in §2.4:
transcripts and backups are unreachable while the target is. *(Original text.)*
Sessions, transcripts and backups
are AgentClip's state, so they stay local — but the project root they normally
sit beside is on another machine. A remote session therefore keeps its
`.agentclip` tree under `<user_data_dir>/agentclip/remote/<target>-<root>-<hash>/`
(`config.default_remote_state_dir`); `SessionStore` takes a `data_root` for this,
and records the remote root in `meta.json` unresolved.

**Reconnect, in practice.** A dropped link becomes a dead host; the next
operation re-dials. The re-dial reuses the credentials that already worked
(agent/keys, or the password from launch, held in memory) and does **not**
prompt: by then the terminal that could ask is underneath the TUI. The prompt
callbacks are the caller's, so a future UI can supply ones that work mid-session.
A command in flight when the link dies returns exit code 255 with
`connection lost to <target>; command outcome unknown (it may have completed or
still be running)`.

**Kill-tree, in practice.** The wrapper is `bash -lc`, run under `setsid --wait`
when the box has it (both are probed by running them, since busybox has neither),
recording its PID to `/tmp/.agentclip-<uuid>.pid`; `kill()` reads that file over
SFTP and sends `kill -9 -- -<pgid>` on a separate channel. The command is not
`exec`'d — a compound command is not a simple command, and `exec` would run only
its head.

**Live output, in practice.** `ExecHandle.peek()` — the merged output so far,
which run_command diffs each poll slice to feed the TUI's run panel (tui.md §8a)
— is answered from the buffer the channel is already pumped into by `wait()`.
No transport call of its own, deliberately: the polling loop calls `wait()` five
times a second anyway, and a second place that can discover a dead link would be
a second place to get the two-state machine wrong. A remote command therefore
streams at exactly the local one's resolution.

**Auth, in practice.** `~/.ssh/config` is parsed first (aliases, user, port,
IdentityFile), then agent + default keys, then up to three password attempts
through the caller's callback. Keyboard-interactive 2FA is whatever paramiko's
own fallback does; a dedicated prompt path for it was not built and is untested.
(As of the GUI wave `SshHost` accepts a `keyboard_prompt` callback and
`executor/hosts/connect.py` carries it, so a UI can supply one - but `_authenticate`
still does not call it, and a TODO there says what wiring it would cost. The
sentence above is unchanged in substance: the path is plumbed, not built.)
`known_hosts` is honored and an unknown key is offered with its SHA256
fingerprint — never auto-added, and never trusted when there is no callback to
ask.

## Revision: the target owns its policy

Status: **implemented** (2026-08-13), and **partly superseded in turn**
(2026-08-19). Supersedes decision 6. The permission ruleset, both MCP layers,
`{file:}`/`{env:}` resolution and the stdio refusal are all as described below;
the one thing built differently is noted where it is described (the refused stdio
server reaches the status pane through the mount paint, so it gets a row and the
statusbar count but no toast).

> **`[approval]` is no longer pinned to this PC.** The "What stays on the host
> PC" subsection below said all of `[approval]` — `mode`, `yolo`,
> `command_allowlist`, `command_deny_tokens` — is read from the operator's
> `config.toml` alone in a remote session, and `config.py` had a branch enforcing
> exactly that. **Superseded by docs/design/remote-executor.md §2.5**, which
> decided that the engine owns policy wholesale: policy belongs to the config of
> the machine the work happens on, so `[approval]` merges like every other table
> and the branch (with its warning) is deleted. The paragraphs below are kept for
> the record, marked where they no longer describe the code. The half that
> survives is the *gate* — the human answering it still sits here, and the shell
> renders the mode the engine reports.

Decision 6 got the rule backwards. "The user's policy must not weaken because of
remote config" reads well until you notice which machine the policy is protecting:
every file a rule can save is on the *target*. A ruleset written on the host PC
describes paths that do not exist over there, so in practice it either matched
nothing or matched by accident. The machine whose files are at risk is the machine
that should say what may happen to them.

**New rule of thumb: the target owns the rules, the host owns the gate.**
(Half-superseded: remote-executor.md §2.5 moved the *gate's policy* to the target
too. What is left of the split is that the gate's **UI** — the human, the
keypress, shift+tab — stays here.) What may
be done to the project — the ruleset, the skills, the MCP servers — is described on
the target, because that is where the files are. How a question about it gets
answered — the permission mode, yolo, the legacy allowlist, the deny tokens — lives
on the host, because that is where the human who answers it is sitting. Everything
serving the tool's own operation (clipboard relay, screen matching, service
profiles, session storage, how to dial the target) stays on the host for the older
reason: that is where it physically happens.

### What moves

**The permission ruleset (`permissions.json` → `permission`).** Read from the target
via the Host seam: the remote user's `~/.config/agentclip/permissions.json` (remote
home from `host.home_dir()`, as skills already resolve it — platformdirs answers for
THIS machine, so the remote path is composed rather than asked for) and, newly, the
remote project's `.agentclip/permissions.json`. The host PC's `permissions.json` is
**not consulted at all** in a remote session — not as a fallback, not as an overlay.
Two rulesets in play would mean every future permission question has to be answered
twice.

This is a real code change, not a flag: `_load_permission_rules` takes no `host`
parameter today, and that absence *is* the current enforcement.

`[permission] permissions_config`, when set, now names a path **on the target**. A
host-side `config.toml` may still set it, but the string is resolved remotely — the
setting says "which file holds the ruleset", and the ruleset is over there.

**A target with no `permissions.json`** behaves exactly like a local machine with none:
empty ruleset, legacy allowlist mode. No new concept, and no fallback to the host
PC's file — the whole point is that host file no longer participates.

**MCP configuration.** Both layers read from the target via the Host seam, which
also un-skips the project layer that `config.py:925` drops today. `{file:...}`
placeholders resolve against the remote config file's directory, over SFTP, so a
token sitting beside `permissions.json` on the box is found where its author put it.
`{env:VAR}` resolves from the **target's** login-shell environment — fetched once at
connect (`bash -lc printenv`) and cached for the session — for the same reason: the
person who wrote `{env:API_TOKEN}` into a file on that box exported it on that box.

**MCP transport stays on the host.** — **SUPERSEDED (2026-08-19) by
remote-executor.md §2.7**, which reverses it outright: the MCP manager is built by
the engine, so every server — stdio or HTTP — is spawned and dialled BY THE
TARGET, with the target's environment and cwd. The premise changed, not the
reasoning: this paragraph was written for a world where the only process
AgentClip had over there was an exec channel. Its "accepted cost" paragraph below
is the exact cost the reversal PAYS OFF — a `http://localhost:<port>` on the box
now resolves to the box. `mcp_remote_target` went from the engine builder with
§2.8's deletion (2026-08-25): the one arrangement it existed for — a config off
one machine, servers spawned by another — cannot happen any more. *(Original text.)* The config describing an HTTP server is read
from the target, but the connection is dialed by AgentClip itself, from the host PC.
Tunnelling it through paramiko (`direct-tcpip`) so it originated from the target was
considered and rejected: it is new transport code with its own lifecycle and
reconnect interactions, and it carried most of this wave's risk for a case the
intended use — public web APIs, reachable from anywhere — does not hit.

The accepted cost, eyes open: a URL only the target can reach does not work.
`http://localhost:<port>` on the box resolves to the *host PC's* localhost, an
internal or VPN-only endpoint is unreachable, and an API that allowlists the target's
IP sees the host's. Since one of those (localhost) fails by connecting to the wrong
thing rather than by failing, the connection error for a remote-session HTTP server
must say which machine dialed it. A bare timeout would send the user looking on the
wrong box.

**MCP stdio servers are not supported in a remote session.** — **SUPERSEDED
(2026-08-19) by remote-executor.md §2.7.** They are supported, and they are the
case the reversal was FOR: a stdio server now spawns on the target, by the
engine, with the target's argv, environment and cwd — exactly the "plausible
later wave" the last sentence below predicted. The refusal still exists in
`McpManager` and still fires when its `remote_target` is set, but since §2.8
(2026-08-25) nothing sets it: the arrangement it guarded is gone. The GUI's
connect banner keeps naming refused stdio servers, which is now dead paint. *(Original text.)* A `type: "local"` entry
in the target's config is reported as an unsupported-here server in the MCP status
pane, with its name. It is not spawned on the host PC (its argv and `cwd` describe
the target) and not silently dropped. Spawning stdio servers on the target over an
exec channel is a plausible later wave; it is not this one.

As built, the refusal is a `failed` state set in `McpManager.__init__` - the same
never-connects shape `disabled` has, because it is the same kind of fact: known
from the config, before anything is attempted. That places it before the TUI
registers its status hook, so it is painted (sidebar row, statusbar count, `/mcp`)
rather than announced (transcript note, toast). Announcing the terminal states a
manager is *born* with would be a change to the screen's mount path, not to MCP,
and `missing_sdk` has the same gap today.

### What stays on the host PC

Global `config.toml`, CLI flags, `~/.ssh/*`, service appearance profiles, and the
`.agentclip` session/transcript/backup tree — all as built in phase 2.

**All of `[approval]` — `mode`, `yolo`, `command_allowlist`, `command_deny_tokens`.**
— **SUPERSEDED (2026-08-19) by docs/design/remote-executor.md §2.5.** Kept for the
record; the code no longer reads this way. `[approval]` merges like every other
table again, so a remote project's `.agentclip.toml` sets it for the session it
describes, and `config.py`'s pinning branch and its warning are gone. What follows
is the original text.

This is a change from today: the remote `.agentclip.toml` currently merges over the
host's, so a remote layer can set them. In a remote session, `[approval]` is now read
from the host's `config.toml` only, and the remote layer's `[approval]` table is
ignored.

They belong together on the host because they are one mechanism — the gate — and
because two of them are barely file settings at all. `mode` is cycled with shift+tab
and `yolo` is a chat command; the file supplies a starting value for live session
state that the human at the host keyboard drives. The other two are the same
mechanism seen from the other side: deny tokens decide whether a call needs a human
(`approval.py:207` demotes an `allow` to a gate), and `mode` decides what that gate
resolves to (`approval.py:219`).

An earlier draft of this section kept only `command_deny_tokens` local, on the
grounds that a brake a config file can release is not a brake. That was wrong on its
own terms: `approval.py:180` returns `auto` for yolo *before* deny tokens are
consulted in legacy mode, so a remote layer setting `yolo = true` would have released
the brake anyway. Splitting the table would have bought a guarantee that did not
hold.

*(End of the superseded `[approval]` text. The reasoning above still explains why
the four keys travel TOGETHER — they are one mechanism — which is exactly why
remote-executor.md §2.5 moved all four at once rather than picking some.)*

### Consequences to handle when implementing

- `McpManager` currently spawns servers on this PC and defaults `cwd` to
  `project_root` — a remote path handed to a local `subprocess`. Dropping stdio
  support in remote sessions removes that hazard rather than papering over it.
- Config load order gains a step: the ruleset and MCP blocks can only be read
  *after* connect, alongside the existing remote `.agentclip.toml` read, not
  during the boot load that selects the target. Both loads now live in
  `agentclip/executor/hosts/connect.py:connect_remote` - steps 1 and 6 of the
  sequence - which `cli.remote_launch` and the GUI's connect dialog both drive,
  so the order is stated once (docs/design/ui-briefs/ssh-connect.md).
- The permission-source string shown in the TUI must name the machine, not just the
  path — `dev-box:~/.config/agentclip/permissions.json` — or two identical-looking
  paths become indistinguishable in a screenshot.
- ~~`[approval]` needs its layers separated: it is read from the merged config dict
  today, so honouring only the host layer means keeping that table out of the merge
  (or re-reading it from the host layer alone) rather than filtering after the
  fact.~~ **Undone (2026-08-19):** the separation was built exactly as described
  and then removed — `[approval]` is read from the merged dict again
  (remote-executor.md §2.5).
- Tests: `FakeHost` gains the ruleset/MCP fixtures; the remote `printenv` belongs
  behind the existing `AGENTCLIP_SSH_TESTS=1` gate.
- `ApprovalConfig.yolo`'s comment (`config.py:345`) says yolo bypasses the deny
  tokens "entirely". True on the legacy path (`approval.py:180`), no longer true in
  ruleset mode, where an `allow`ed bash command carrying a deny token still gates
  (`approval.py:207`) with no yolo check. Pre-existing and unrelated to remote work;
  worth correcting while nearby.
