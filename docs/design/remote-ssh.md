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

6. **Policy local, project remote.**
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

- **Phase 1 (pure refactor, no behavior change):** introduce `Host`, `LocalHost`,
  `FakeHost`; move `shell.py` and `fs_tools.py` onto the seam; `Workspace` resolution
  via Host; full suite green, ruff + mypy clean.
- **Phase 2:** `SshHost` (Paramiko), `[remote]` config + CLI flags, launch/auth flow,
  reconnect, remote bootstrap facts, remote skills/project-config reads, gated
  integration tests.
