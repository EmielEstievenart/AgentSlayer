# AgentClip — agent instructions

AgentClip is a Python Textual TUI app, frozen into a single `agentclip.exe` with PyInstaller. Design docs in `docs/design/` are binding.

## Build & deploy

Always deploy the exe onto PATH when building. Use the build script — never run PyInstaller by hand:

```powershell
.\scripts\build-exe.ps1
```

This builds from `packaging/agentclip.spec`, smoke-tests the frozen exe, and copies it into the PATH folder (`$env:AGENTCLIP_INSTALL_DIR` if set, otherwise `$HOME\Documents\PATH`). Whenever a change should be usable from the terminal — or the user asks for a build — finish by running this script so the installed `agentclip.exe` is up to date. Only skip the install step (`-NoInstall`) if the user explicitly asks for a build without deploying.

## Workflow

Commit after every change — each coherent edit gets its own commit; don't batch unrelated changes into one.

## Subagents

Delegate work to subagents rather than doing it inline. Use Sonnet for exploration, search and reading (e.g. the Explore agent); use Opus for implementation, editing and debugging. Launch independent agents in a single message so they run concurrently.

## Dev commands

- Tests: `uv run pytest`
- Lint / types: `uv run ruff check .` and `uv run mypy src`
- All third-party assets must stay embedded in Python source (e.g. CSS lives in the `AgentClipApp.CSS` string, no `.tcss` files) so the PyInstaller build needs no `--add-data`.
