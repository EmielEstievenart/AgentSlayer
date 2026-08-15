# Remote development over SSH

Status: **agreed design** (grilling session 2026-08-11). Binding for implementation.

## Goal

AgentClip must be able to drive development on a remote Linux device while the tool
itself keeps running on the host PC (clipboard relay and screen detection require the
host). The model's world is the remote machine; the chat/copy-paste machinery stays
local.

## Decisions (interview outcomes)

1. **All-remote scope.** When a remote session is active, *every* OS-touching tool
   (run_command, read/write/edit/delete, list_dir, glob, grep, skill discovery) runs
   against the remote host. No hybrid local-files/remote-shell mode — one source of
   truth.

2. **Persistent connection, fresh shell per command.** Connect + authenticate once at
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
   - Permissions (`opencode.json` ruleset + deny-token backstop): **always local** —
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

8. **Remote cancel/kill.** Closing a Paramiko channel does not kill the remote
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
- Backups (`backup_hook`) keep storing locally: read remote bytes before overwrite.
- Bootstrap: only the existing `on {os_name}` slot changes (e.g. `on Linux (ssh)`).
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
  over there, stdio MCP servers are refused and reported, and `[approval]` is
  pinned to this PC. See the revision section below.

## As built (phase 2)

Decisions taken while implementing that the interview did not settle. Where one
deviates from the text above, it says so.

**Configuration.** Saved targets are `[remote.<name>]` tables (`host`, `user`,
`port`, `root`); a target whose table has no `host` is named for its host. The
session's target comes only from `--ssh`, which takes a saved name, an
`~/.ssh/config` alias, or `[user@]host[:port]`; `--remote-root` overrides a saved
`root` and is required when nothing else supplies one. Nothing in the config file
selects a target: a session goes remote because the command line said so.

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

**Where a remote session's own state lives.** Sessions, transcripts and backups
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

Status: **implemented** (2026-08-13). Supersedes decision 6. The permission
ruleset, `[approval]`, both MCP layers, `{file:}`/`{env:}` resolution and the
stdio refusal are all as described below; the one thing built differently is
noted where it is described (the refused stdio server reaches the status pane
through the mount paint, so it gets a row and the statusbar count but no toast).

Decision 6 got the rule backwards. "The user's policy must not weaken because of
remote config" reads well until you notice which machine the policy is protecting:
every file a rule can save is on the *target*. A ruleset written on the host PC
describes paths that do not exist over there, so in practice it either matched
nothing or matched by accident. The machine whose files are at risk is the machine
that should say what may happen to them.

**New rule of thumb: the target owns the rules, the host owns the gate.** What may
be done to the project — the ruleset, the skills, the MCP servers — is described on
the target, because that is where the files are. How a question about it gets
answered — the permission mode, yolo, the legacy allowlist, the deny tokens — lives
on the host, because that is where the human who answers it is sitting. Everything
serving the tool's own operation (clipboard relay, screen matching, service
profiles, session storage, how to dial the target) stays on the host for the older
reason: that is where it physically happens.

### What moves

**The permission ruleset (`opencode.json` → `permission`).** Read from the target
via the Host seam: the remote user's `~/.config/opencode/opencode.json` (remote home
from `host.home_dir()`, as skills already resolve it) and, newly, the remote
project's `opencode.json`. The host PC's `opencode.json` is **not consulted at all**
in a remote session — not as a fallback, not as an overlay. Two rulesets in play
would mean every future permission question has to be answered twice.

This is a real code change, not a flag: `_load_permission_rules` takes no `host`
parameter today, and that absence *is* the current enforcement.

`[permission] opencode_config`, when set, now names a path **on the target**. A
host-side `config.toml` may still set it, but the string is resolved remotely — the
setting says "which file holds the ruleset", and the ruleset is over there.

**A target with no `opencode.json`** behaves exactly like a local machine with none:
empty ruleset, legacy allowlist mode. No new concept, and no fallback to the host
PC's file — the whole point is that host file no longer participates.

**MCP configuration.** Both layers read from the target via the Host seam, which
also un-skips the project layer that `config.py:925` drops today. `{file:...}`
placeholders resolve against the remote config file's directory, over SFTP, so a
token sitting beside `opencode.json` on the box is found where its author put it.
`{env:VAR}` resolves from the **target's** login-shell environment — fetched once at
connect (`bash -lc printenv`) and cached for the session — for the same reason: the
person who wrote `{env:API_TOKEN}` into a file on that box exported it on that box.

**MCP transport stays on the host.** The config describing an HTTP server is read
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

**MCP stdio servers are not supported in a remote session.** A `type: "local"` entry
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
  path — `dev-box:~/.config/opencode/opencode.json` — or two identical-looking
  paths become indistinguishable in a screenshot.
- `[approval]` needs its layers separated: it is read from the merged config dict
  today, so honouring only the host layer means keeping that table out of the merge
  (or re-reading it from the host layer alone) rather than filtering after the fact.
- Tests: `FakeHost` gains the ruleset/MCP fixtures; the remote `printenv` belongs
  behind the existing `AGENTCLIP_SSH_TESTS=1` gate.
- `ApprovalConfig.yolo`'s comment (`config.py:345`) says yolo bypasses the deny
  tokens "entirely". True on the legacy path (`approval.py:180`), no longer true in
  ruleset mode, where an `allow`ed bash command carrying a deny token still gates
  (`approval.py:207`) with no yolo check. Pre-existing and unrelated to remote work;
  worth correcting while nearby.
