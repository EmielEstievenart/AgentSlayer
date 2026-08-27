# AgentClip — agent instructions

AgentClip is a Python desktop app with **one chat shell**, frozen into a single `agentclip.exe` with PyInstaller: the **Chat UI** (`agentclip.shell.chat`, pywebview + WebView2), which is what `agentclip` launches. It had a second, a Textual TUI behind `--tui`; `docs/design/ui-monitor.md` §6.6 deleted it on 2026-08-24, and both `--tui` (a stub that says so and exits 2) and `--gui` (a no-op) survive one release for the scripts that carry them.

## The vocabulary

Fixed 2026-08-25. Five words, one meaning each — **"GUI" is not a term any more**; older design docs still say it and mean the Chat UI.

| term | what it is | where it lives | binary |
|---|---|---|---|
| **Chat UI** | what the user looks at and types into | `src/agentclip/shell/chat/` | `agentclip` |
| **Monitor** | watches the Browser, clicks it, owns the clipboard | `src/agentclip/driver/monitor/` | `agentclip-monitor` |
| **Monitor UI** | the Monitor's own window: Serve panel, service editor, ELEMENTS, region picker, `/identify` | `src/agentclip/shell/monitor_ui/` | `agentclip-monitor` (`agentclip --calibrate` is a stub that names the binary) |
| **Browser** | the desktop chat app AgentClip operates — what the Monitor watches | not ours | — |
| **Executor** | permission-gated execution for the agent, through the Host seam | `src/agentclip/executor/` | `agentclip-engine` (with the engine) |

`src/agentclip/shell/webview/` sits under both UIs: the bridge, the service editor's model and the asset resolution the two windows share. It never imports either of them.

Design docs in `docs/design/` are binding — but **a status header inside a design doc qualifies that claim**: a document (or a section of one) whose header says "plan, not yet binding" is intent, not law, and only the parts its header calls built are binding.

## Build & deploy

Always deploy the exes onto PATH when building. Use the build script — never run PyInstaller by hand:

```powershell
.\scripts\build-exe.ps1
```

That builds **three** exes and installs all of them: `agentclip.exe` (the full app, from `packaging/agentclip.spec`), `agentclip-engine.exe` (the engine half an SSH target runs, from `packaging/agentclip-engine.spec`) and `agentclip-monitor.exe` (the standing monitor that runs where the *pixels* are — a VM, or this PC in split mode — from `packaging/agentclip-monitor.spec`). It syncs the build environment in one `uv sync` (the `cv` extra for the monitor, `gui` for the app **and the monitor** — since `ui-monitor.md` §9.1 the monitor binary opens the Monitor UI, and only its `--headless` door runs without a toolkit — and `mcp` for the app and the engine), smoke-tests each frozen artifact (`--version` for all three, `--list-matchers` for the monitor, `--gui-smoke` for the app **only**: that check lives in `cli.py`, which is off the monitor binary's layering allowance, so the monitor's pywebview collection is proven by the app's `--gui-smoke` out of the same environment), and copies them into the PATH folder (`$env:AGENTCLIP_INSTALL_DIR` if set, otherwise `$HOME\Documents\PATH`). Whenever a change should be usable from the terminal — or the user asks for a build — finish by running this script so the installed exes are up to date. Only skip the install step (`-NoInstall`) if the user explicitly asks for a build without deploying; `scripts\build-dist.ps1` (and `scripts/build-dist.sh` on POSIX) is the packaged form of that ask — a clean build of all three into `dist\`, nothing installed. Since `ui-monitor.md` §10 `agentclip.exe` bundles no OpenCV, no tkinter and no Xlib at all — the Chat UI hosts no monitor, so the whole screen half lives in `agentclip-monitor.exe` and `packaging/agentclip.spec` excludes it by name. `-EngineOnly` builds just the engine (and skips the `cv`/`gui` extras); `-MonitorOnly` builds just the monitor (and skips `mcp` only — it keeps `gui`); naming both builds those two halves and skips the full app. `-Clean` and `-InstallDir <path>` are the other flags.

On Linux/macOS the equivalent is `scripts/build-exe.sh`, which builds the same three binaries — `agentclip`, `agentclip-engine`, `agentclip-monitor` — with the same flags in POSIX spelling: `--engine-only`, `--monitor-only`, `--clean`, `--no-install`, `--install-dir DIR`. The one check it has that the PowerShell script does not: the monitor needs tkinter for its `--pick-region` overlay, which many distributions ship apart from the interpreter, so that script checks `import tkinter` before building it and names the distro package (`python3-tk`) if it is missing. Windows bundles tkinter with the interpreter, so there is nothing to check for over here.

## Workflow

Commit after every change — each coherent edit gets its own commit; don't batch unrelated changes into one.

## Subagents

Delegate work to subagents rather than doing it inline. Use Sonnet for exploration, search and reading (e.g. the Explore agent); use Opus for implementation, editing and debugging. Launch independent agents in a single message so they run concurrently.

## Dev commands

- Tests: `uv run pytest`
- Lint / types: `uv run ruff check .` and `uv run mypy src`
- Third-party assets stay embedded in Python source wherever they can, so the PyInstaller build needs no `--add-data` for them. The one deliberate exception is the two windows' pages (`shell/chat/assets/` and `shell/monitor_ui/assets/`): a browser engine loads those over a `file://` URL, so they are real files and `packaging/agentclip.spec` collects them by package-relative path.

### Running OS-touching tests

`uv run pytest` is safe to run while you use the machine. An autouse gate in `tests/conftest.py` neuters the three `user32` calls that inject input (`SendInput`, `SetCursorPos`, `SetForegroundWindow`) — and, on Linux, the two `screen.x11` seams every injection passes through — so nothing in the suite can click, scroll, move the cursor or type a Ctrl+V into whatever window you have in front of you. It also blanks the injecting verbs at their bound names in `driver/monitor/ops.py` (the Driver's OS adapter from-imports each one), and makes `pick_region` and `draw_identify_overlay` raise instead of throwing a fullscreen overlay up. Read-only calls (desktop metrics, GDI screen capture) stay real.

Tests that genuinely need the real desktop or the real clipboard carry `@pytest.mark.real_os` and are skipped by default. To run them:

```powershell
$env:AGENTCLIP_OS_TESTS = '1'; uv run pytest
```

That disarms the gate for the whole run — do it only when nothing else is on screen. `tests/test_os_gate.py` tests the gate itself in both directions.

### Running the SSH tests

Same rule, different resource: `@pytest.mark.real_ssh` (tests/executor/hosts/test_ssh_real.py) talks to somebody's actual machine, so it is skipped unless you opt in — and you only opt in with the user's explicit go-ahead:

```powershell
$env:AGENTCLIP_SSH_TESTS = '1'; $env:AGENTCLIP_SSH_TARGET = 'user@host'; uv run pytest tests/executor/hosts/test_ssh_real.py
```

The target must authenticate without a prompt and already be in `known_hosts`. Everything else about `SshHost` — which since remote-executor.md §2.8 is a *connection*, not a `Host`: auth, reconnect, the engine's link channel and the connect sequence's probes — is covered by `tests/executor/hosts/test_ssh_host.py` and `test_link_channel.py`, neither of which opens a socket.
