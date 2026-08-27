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

`agentclip` opens the **Chat UI** — a native window rendering an HTML frontend in the WebView2 runtime Windows already ships. It is the only shell: the Textual terminal shell that used to sit behind `--tui` was removed, and the flag survives one release as a message saying so. (`--gui` is still accepted and does nothing.)

**Three commands, three jobs** — the words below mean exactly one thing each, everywhere in this project:

| Command | Is | Runs where |
|---|---|---|
| `agentclip` | the **Chat UI**: what you look at and type into, and where every decision is made | your machine |
| `agentclip-monitor` | the **Monitor** and its **Monitor UI**: watches the **Browser**, clicks it, owns the clipboard — and a window to configure and serve it from | the machine whose *screen* shows the chat |
| `agentclip-engine` | the **Executor**: permission-gated files and commands on behalf of the agent | the machine whose *files* you are working on |

In the ordinary case all three are this PC and you only ever type the first. A plain `agentclip` — or a double-click — starts **idle**: no screen is watched and nothing is clicked until you attach one from the Chat UI's **Monitor** tab, whose **Launch & connect a local monitor** button starts an `agentclip-monitor` beside this window and dials it on `127.0.0.1`. From the terminal, `agentclip --monitor local` does that at startup instead. Either way it is the same wire a remote monitor is reached on, because there is only one way to reach a screen (`docs/design/ui-monitor.md` §10, §11.1). ("GUI" is not a term any more; older design docs still say it and mean the Chat UI.)

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

It connects, authenticates (agent, keys, then a password prompt) and probes the machine *before* the app window opens, so a bad target fails in the terminal rather than inside the app. Your permission rules stay local; the project's `.agentclip.toml` and its skills come off the remote machine. Backups and transcripts are kept on your PC. See `docs/design/remote-executor.md` (`remote-ssh.md` is the earlier per-call design, superseded where the two disagree).

The target needs `agentclip-engine` on it — `uv tool install agentclip` over there, or the standalone binary below.

> `uv tool install` writes `~/.local/bin`, which sshd's *non-interactive* shell does not have on `PATH` (that comes from `~/.profile`, and no profile is read for `ssh host command`). AgentClip therefore also tries `~/.local/bin/agentclip-engine` before giving up. If you installed somewhere else, symlink the binary into `/usr/local/bin`.

### Driving a browser on another machine

The other half of the split: your files stay put and the *screen* moves. Run the monitor where the browser is — a VM, or a second PC:

```sh
agentclip-monitor                     # the Monitor UI: a window on that machine
agentclip-monitor --headless --port 7777   # no desktop over there? no window either
```

The window is where you set that machine up: capture what the chat service looks like, drag a box around the browser, watch the ELEMENTS column confirm it is recognising things — and, in the **SERVE** band, pick an address, pick a port, and press Start. The band shows a **token**, with a Copy button; that token is what lets your PC in, and the port requires one by default even on loopback.

Then, on your PC, open the **Connect** dialog's **Monitor** tab (or the sidebar's `Attach a monitor...`) and either:

- **Direct** — host, port and token; or
- **Via SSH** — pick a saved SSH target and give the port as seen from *that* machine. AgentClip forwards it over the SSH connection it already holds: no second login, no `ssh -L` to leave running.

Attaching does **not** restart your session — the transcript, the engine and your files stay exactly where they are while the browser automation moves. **Disconnect** drops the link (and ends a local monitor this window started); you are left with no monitor until you attach or launch one from the same tab.

The scriptable spelling still works: `agentclip --monitor 10.0.0.5:7777`, or `agentclip --monitor @vm` for a monitor you saved as `[monitor.vm]` in your global config. See [docs/configuration.md](docs/configuration.md) for the saved-target format and [docs/design/ui-monitor.md](docs/design/ui-monitor.md) for why any of this exists.

### Standalone executables (Windows)

To use `agentclip` from any directory without the checkout, freeze it into self-contained exes:

```powershell
.\scripts\build-exe.ps1                 # all three exes
.\scripts\build-exe.ps1 -EngineOnly     # just the engine
.\scripts\build-exe.ps1 -MonitorOnly    # just the monitor
.\scripts\build-dist.ps1                # clean build of all three into dist\, no install
```

This builds **three** artifacts (PyInstaller onefile, no Python needed to run them), smoke-tests each, and copies them to a folder on your `PATH` — `%AGENTCLIP_INSTALL_DIR%` if set, otherwise `%USERPROFILE%\Documents\PATH`:

- `dist\agentclip.exe` — the full app (smaller since it stopped carrying OpenCV and Tcl/Tk).
- `dist\agentclip-engine.exe` — the *engine* half alone, the binary an SSH target runs (`docs/design/remote-executor.md` §2.6). It carries the MCP SDK and nothing shell- or driver-shaped: no pywebview, no OpenCV, no region picker. Copy it onto a Windows target's `PATH` and remote sessions work there without a Python install.
- `dist\agentclip-monitor.exe` — the *monitor* half alone, the standing process that runs on the machine whose **screen** shows the chat: a VM on a host-only network, or this PC in split mode (`docs/design/ui-monitor.md` §2.5, §6.5, §9.1). It serves that machine's pixels, mouse, keyboard and clipboard to a brain over a TCP wire and keeps polling whether or not one is attached. It carries the OpenCV backend, the region picker **and the Monitor UI's own pywebview window** — and nothing engine-shaped: no MCP, no session, no transcript. Its `--headless` door imports no window toolkit at all, which is what keeps it honest on a server with no desktop.

Re-run to update after changing the source. Useful flags: `-Clean` (fresh build), `-EngineOnly` (skip the app, and its `cv`/`gui` extras), `-MonitorOnly` (skip the app and the `mcp` extra — the monitor keeps `gui`, because it opens a window), `-NoInstall` (build only), `-InstallDir <path>`. Naming both "only" switches builds those two halves and skips the full app.

The exe carries **the shell**: the Chat UI (plain `agentclip.exe`, rendering in the WebView2 runtime Windows already ships). It carries no screen half at all — the OpenCV matcher backend, the region picker's tkinter and the X11 bindings are `agentclip-monitor.exe`'s, and this exe *excludes* them, because the Chat UI reaches pixels only over the wire (§10). It also carries this user guide — `docs/commands.md` and `docs/configuration.md`, which the Chat UI's **docs** button opens — so the manual travels with the binary. Nothing extra to install. The build script proves that against the app exe it just produced (`--version`, and `--gui-smoke`, which reads the page assets *and* the guide back out of the freeze) and refuses to install one that fails.

The monitor exe is proved by `--version` (which walks its whole import tree, the Monitor UI dispatcher included) and `--list-matchers` (which imports the OpenCV backend for real — the only binary asked that question, because the monitor machine is where every template search runs). It gets no `--gui-smoke` of its own: that check lives in `cli.py`, which is off this binary's layering allowance, so its pywebview collection is proved by the app exe's `--gui-smoke` over the same `webview` out of the same environment.

The engine exe is proved by `--version` alone, which walks its whole module-level import tree — config, the session factory, the server loop, the executor's tool registry — and is checked for the exact `agentclip-engine <version>` answer, because on a target that stdout stream *is* the wire protocol.

The builds are driven by `packaging/agentclip.spec`, `packaging/agentclip-engine.spec` and `packaging/agentclip-monitor.spec`; a onefile exe unpacks to `%TEMP%` on each launch, costing a second or two of startup.

> If `agentclip` says the gui extra is not installed, you are running a *different* `agentclip` — most likely a stale `uv tool install`. Run `where.exe agentclip`; the build script prints the same warning when it spots one, and `uv tool uninstall agentclip` clears it.

### Standalone executables (Linux / macOS)

```bash
scripts/build-exe.sh               # all three binaries
scripts/build-exe.sh --engine-only
scripts/build-exe.sh --monitor-only
scripts/build-dist.sh              # clean build of all three into dist/, no install
```

The same three artifacts as the Windows script above, which matters because a POSIX box is usually a machine being *driven onto* rather than the one driving:

- `dist/agentclip` — the full app, exactly as above.
- `dist/agentclip-engine` (~21 MB) — the engine half alone, the binary an SSH target runs (`docs/design/remote-executor.md` §2.6). It carries the MCP SDK and nothing shell- or driver-shaped: no textual, no pywebview, no OpenCV. Copy it onto a target's `PATH` and remote sessions work there without a Python install.
- `dist/agentclip-monitor` — the monitor half alone, as under Windows above: the standing process for the machine whose screen shows the chat, Monitor UI included. Copy it onto a VM's `PATH` and that VM can serve its screen without a Python install.

`--engine-only` builds just the engine and skips the `cv`/`gui` extras, whose Linux wheels want system libraries a headless target need not have; `--monitor-only` builds just the monitor and skips `mcp` alone (it keeps `gui` — the monitor opens a window). Naming both builds those two halves and skips the full app. Every binary is smoke-tested before install (`--version` for all three, `--list-matchers` for the monitor, `--gui-smoke` for the full app), then copied to `$AGENTCLIP_INSTALL_DIR` or `~/.local/bin`. Other flags mirror the PowerShell script: `--clean`, `--no-install`, `--install-dir <path>`.

> The monitor's region picker draws with tkinter, which many distributions ship apart from the interpreter, so the script checks for it before freezing that binary: `sudo apt install python3-tk` (Debian/Ubuntu) or `sudo dnf install python3-tkinter` (Fedora).

A frozen binary is architecture- and glibc-specific, so build it on (or for) the machine family it will run on.

## Configuration

TOML, merged in order: built-in defaults → `~/.config/agentclip/config.toml` (Windows: `%APPDATA%\agentclip\config.toml`) → `<project>/.agentclip.toml` → CLI flags. See [docs/configuration.md](docs/configuration.md) for every setting, the service presets (paste-size budgets per chat service), the `permissions.json` rule format, and MCP server config — and [docs/commands.md](docs/commands.md) for all slash commands and keyboard shortcuts. Both pages are readable inside the app: the Chat UI's titlebar has a **docs** button that opens them (`F1` stays the key/command cheatsheet).

## Design documents

- `docs/design/architecture.md` — module layout (Shell / Driver / Executor), config, persistence, tests
- `docs/design/protocol.md` — the CLIP/1 wire protocol
- `docs/design/gui.md` — the Chat UI (the doc predates the name and still says "GUI"): one core, two shells, and why pywebview. Per-surface behavior contracts live in `docs/design/ui-briefs/`
- `docs/design/tui.md` — **historical**: the Textual shell, deleted 2026-08-24. Kept because the automation rules designed there — the finish decision, the send gate, the auto-copy flow — still run one layer down
- `docs/design/remote-executor.md` — running the engine on the SSH target instead of round-tripping every call: the link seam, the wire codec, `agentclip-engine`
- `docs/design/remote-ssh.md` — the earlier per-call SSH design, superseded by the above where they disagree
- `docs/design/mcp.md` — MCP server support, reading OpenCode's config shape
- `docs/design/skills.md` — Agent Skills: discovering `SKILL.md` files from the Claude Code / OpenCode folders and exposing them as a `skill` tool
- `docs/design/ui-monitor.md` — splitting the screen-automation half across a process boundary (the Monitor where the pixels are, the Chat UI where you are), retiring the TUI, and **fixing the vocabulary**: Chat UI / Monitor / Monitor UI / Browser / Executor, one meaning each, §9.0. Its header says which phases are built — all of them, as of 2026-08-25
- `docs/design/ui-briefs/` — one behaviour contract per surface, including `monitor-ui.md` (the Monitor UI window) and `ssh-connect.md` (the connect dialog, Executor and Monitor tabs)
- `docs/design/research-*.md` — paste-limit / clipboard / Textual research underpinning the design (the Textual one is historical: that dependency is gone)

Each design doc carries a status header, and it qualifies the list above: a doc marked "plan, not yet binding" describes intent, and only the sections its header calls built describe code.
