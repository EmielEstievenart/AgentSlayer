# AgentClip — agent instructions

AgentClip is a Python desktop app with **two UI shells**, frozen into a single `agentclip.exe` with PyInstaller:

- the **GUI** (`agentclip.shell.gui`, pywebview + WebView2) — the primary shell, and what plain `agentclip` launches;
- the **TUI** (`agentclip.shell.tui`, Textual) — **deprecated**. It sits behind `--tui`, is frozen (no new features land there) and is removed in a later phase (`docs/design/ui-monitor.md` §2.12). `--gui` is still accepted and does nothing.

Design docs in `docs/design/` are binding — but **a status header inside a design doc qualifies that claim**: a document (or a section of one) whose header says "plan, not yet binding" is intent, not law, and only the parts its header calls built are binding.

## Build & deploy

Always deploy the exe onto PATH when building. Use the build script — never run PyInstaller by hand:

```powershell
.\scripts\build-exe.ps1
```

This syncs the build environment (including the `cv` extra, which the exe bundles), builds from `packaging/agentclip.spec`, smoke-tests the frozen exe, verifies the OpenCV matcher backend actually loads inside it, and copies it into the PATH folder (`$env:AGENTCLIP_INSTALL_DIR` if set, otherwise `$HOME\Documents\PATH`). Whenever a change should be usable from the terminal — or the user asks for a build — finish by running this script so the installed `agentclip.exe` is up to date. Only skip the install step (`-NoInstall`) if the user explicitly asks for a build without deploying.

On Linux/macOS the equivalent is `scripts/build-exe.sh`, which builds **two** binaries: `agentclip` and `agentclip-engine` (the engine half an SSH target runs, from `packaging/agentclip-engine.spec`). `--engine-only` builds just the engine and skips the `cv`/`gui` extras. Same flags otherwise: `--clean`, `--no-install`, `--install-dir DIR`.

## Workflow

Commit after every change — each coherent edit gets its own commit; don't batch unrelated changes into one.

## Subagents

Delegate work to subagents rather than doing it inline. Use Sonnet for exploration, search and reading (e.g. the Explore agent); use Opus for implementation, editing and debugging. Launch independent agents in a single message so they run concurrently.

## Dev commands

- Tests: `uv run pytest`
- Lint / types: `uv run ruff check .` and `uv run mypy src`
- All third-party assets must stay embedded in Python source (e.g. CSS lives in the `AgentClipApp.CSS` string, no `.tcss` files) so the PyInstaller build needs no `--add-data`.

### Running OS-touching tests

`uv run pytest` is safe to run while you use the machine. An autouse gate in `tests/conftest.py` neuters the three `user32` calls that inject input (`SendInput`, `SetCursorPos`, `SetForegroundWindow`), so nothing in the suite can click, scroll, move the cursor or type a Ctrl+V into whatever window you have in front of you, and `pick_region` raises instead of throwing a fullscreen overlay up. Read-only calls (desktop metrics, GDI screen capture) stay real.

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

The target must authenticate without a prompt and already be in `known_hosts`. Everything else about `SshHost` is covered by `tests/executor/hosts/test_ssh_host.py`, which never opens a socket.
