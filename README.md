# AgentClip

Use any web-chat LLM (ChatGPT, Microsoft 365 Copilot, Claude.ai, Gemini, ...) as a **coding agent** — no API access required. The transport is your system clipboard and you doing copy-paste.

## How it works

1. Start `agentclip` in your project directory and type a task. AgentClip copies a **bootstrap prompt** (protocol spec + tool catalog + your task) to the clipboard.
2. Paste it into the chat UI and send. The LLM replies with structured tool calls — many per reply, to keep round trips down.
3. Click the reply's **Copy** button. AgentClip's clipboard watcher detects it automatically, executes the tool calls locally (file edits show a diff for approval; commands are gated by permission rules), and copies the combined results back to the clipboard.
4. Paste the results back into the chat. Repeat until the LLM declares the task done.

Every file change is backed up per turn — `undo turn` restores it without git.

## Approving actions

By default AgentClip **gates** every file edit and every command its permission rules don't already allow, so you review before it runs. At the gate: `y` approve · `n` reject (with an optional reason) · `a` approve **and** remember a rule for calls like it for the rest of the session. The rules live in an OpenCode-style `permissions.json` with a `build` and a read-only `plan` mode (`shift+tab` cycles them) — see [docs/configuration.md](docs/configuration.md).

For trusted or throwaway projects you can skip the gate entirely with **YOLO mode** — type `/yolo` in the chat box to auto-approve everything an explicit deny rule doesn't refuse (red `⚡ YOLO` badge while armed). Its opposite is `/unattended`, which auto-denies whatever would ask you while you're away. Both can also start from config (`[approval]`).

Chat-box commands (leading slash — full list in [docs/commands.md](docs/commands.md)):

| Command | Effect |
|---|---|
| `/help` | List all commands. |
| `/new` | Clear the chat and start a fresh session (works mid-turn). |
| `/mode [build\|plan]` | Set the permission mode. |
| `/config` | Show/create/reset the permissions files. |
| `/yolo` · `/unattended` | Auto-approve / auto-deny everything (bare toggles). |

## Install / run

Requires Python 3.11+.

```sh
uv sync
uv run agentclip            # in the project you want the agent to work on
# or: uv run agentclip --project path/to/project --service chatgpt-attach
```

Linux clipboard: the bundled backend works on X11 and Wayland-with-XWayland out of the box. On a pure-Wayland system install `wl-clipboard` (and `xclip` for X11 fallback).

### Working on a remote machine

AgentClip keeps running on your PC (it needs the clipboard and the screen), while every file it reads and every command it runs happens over SSH:

```sh
uv run agentclip --ssh dev@buildbox --remote-root /srv/app
uv run agentclip --ssh pi           # a saved target, or an ~/.ssh/config alias
```

Save targets in your config to avoid repeating them:

```toml
[remote.pi]
host = "raspberrypi.local"
user = "emiel"
port = 22
root = "/home/emiel/code/thing"
```

It connects, authenticates (agent, keys, then a password prompt) and probes the machine *before* the TUI starts, so a bad target fails in the terminal rather than inside the app. Your permission rules stay local; the project's `.agentclip.toml` and its skills come off the remote machine. Backups and transcripts are kept on your PC. See `docs/design/remote-ssh.md`.

The target needs `agentclip-engine` on it — `uv tool install agentclip` over there, or the standalone binary below.

> `uv tool install` writes `~/.local/bin`, which sshd's *non-interactive* shell does not have on `PATH` (that comes from `~/.profile`, and no profile is read for `ssh host command`). AgentClip therefore also tries `~/.local/bin/agentclip-engine` before giving up. If you installed somewhere else, symlink the binary into `/usr/local/bin`.

### Standalone executable (Windows)

To use `agentclip` from any directory without the checkout, freeze it into a single self-contained exe:

```powershell
.\scripts\build-exe.ps1
```

This builds `dist\agentclip.exe` (PyInstaller onefile, ~78 MB, no Python needed to run it), smoke-tests it, and copies it to a folder on your `PATH` — `%AGENTCLIP_INSTALL_DIR%` if set, otherwise `%USERPROFILE%\Documents\PATH`. Re-run it to update after changing the source. Useful flags: `-Clean` (fresh build), `-NoInstall` (build only), `-InstallDir <path>`.

The exe carries **both UI shells and every optional extra the desktop needs**: the TUI, the GUI shell (`agentclip.exe --gui`, rendering in the WebView2 runtime Windows already ships) and the OpenCV matcher backend. It also carries this user guide — `docs/commands.md` and `docs/configuration.md`, which the GUI's **docs** button opens — so the manual travels with the binary. Nothing extra to install, which is most of the 78 MB. The build script proves all three against the exe it just produced (`--version`, `--list-matchers`, `--gui-smoke`, the last of which reads the page assets *and* the guide back out of the freeze) and refuses to install one that fails.

The build is driven by `packaging/agentclip.spec`; a onefile exe unpacks to `%TEMP%` on each launch, costing a second or two of startup.

> If `agentclip --gui` says the gui extra is not installed, you are running a *different* `agentclip` — most likely a stale `uv tool install`. Run `where.exe agentclip`; the build script prints the same warning when it spots one, and `uv tool uninstall agentclip` clears it.

### Standalone executables (Linux / macOS)

```bash
scripts/build-exe.sh              # both binaries
scripts/build-exe.sh --engine-only
```

Same idea, **two** artifacts, because a POSIX box is usually the machine being *driven onto* rather than the one driving:

- `dist/agentclip` — the full app, exactly as above.
- `dist/agentclip-engine` (~21 MB) — the engine half alone, the binary an SSH target runs (`docs/design/remote-executor.md` §2.6). It carries the MCP SDK and nothing shell- or driver-shaped: no textual, no pywebview, no OpenCV. Copy it onto a target's `PATH` and remote sessions work there without a Python install.

`--engine-only` builds just that second one and skips the `cv`/`gui` extras, whose Linux wheels want system libraries a headless target need not have. Both binaries are smoke-tested before install (`--version`, plus `--list-matchers` and `--gui-smoke` for the full app), then copied to `$AGENTCLIP_INSTALL_DIR` or `~/.local/bin`. Other flags mirror the PowerShell script: `--clean`, `--no-install`, `--install-dir <path>`.

A frozen binary is architecture- and glibc-specific, so build it on (or for) the machine family it will run on.

## Configuration

TOML, merged in order: built-in defaults → `~/.config/agentclip/config.toml` (Windows: `%APPDATA%\agentclip\config.toml`) → `<project>/.agentclip.toml` → CLI flags. See [docs/configuration.md](docs/configuration.md) for every setting, the service presets (paste-size budgets per chat service), the `permissions.json` rule format, and MCP server config — and [docs/commands.md](docs/commands.md) for all slash commands and keyboard shortcuts. Both pages are readable inside the app: the GUI's titlebar has a **docs** button that opens them (`F1` stays the key/command cheatsheet).

## Design documents

- `docs/design/protocol.md` — the CLIP/1 wire protocol
- `docs/design/tui.md` — TUI design (Textual)
- `docs/design/architecture.md` — module layout, config, persistence, tests
- `docs/design/research-*.md` — paste-limit / clipboard / Textual research underpinning the design
