# AgentClip

Use any web-chat LLM (ChatGPT, Microsoft 365 Copilot, Claude.ai, Gemini, ...) as a **coding agent** — no API access required. The transport is your system clipboard and you doing copy-paste.

## How it works

1. Start `agentclip` in your project directory and type a task. AgentClip copies a **bootstrap prompt** (protocol spec + tool catalog + your task) to the clipboard.
2. Paste it into the chat UI and send. The LLM replies with structured tool calls — many per reply, to keep round trips down.
3. Click the reply's **Copy** button. AgentClip's clipboard watcher detects it automatically, executes the tool calls locally (file edits show a diff for approval; commands are gated by an allowlist), and copies the combined results back to the clipboard.
4. Paste the results back into the chat. Repeat until the LLM declares the task done.

Every file change is backed up per turn — `undo turn` restores it without git.

## Approving actions

By default AgentClip **gates** every file edit and every command that isn't on the allowlist, so you review before it runs. At the gate: `y` approve · `n` reject (with an optional reason) · `a` approve **and** auto-accept edits for the rest of the session (commands still gate).

For trusted or throwaway projects you can skip the gate entirely with **YOLO mode** — type `/yolo` in the chat box to auto-approve *everything* (edits **and** commands, bypassing the allowlist and deny tokens). The status bar shows a red `⚡ YOLO` badge while it's armed; `/yolo off` turns it back off. It can also be armed from config with `[approval] yolo = true`.

Chat-box commands (type with a leading slash):

| Command | Effect |
|---|---|
| `/yolo [on\|off]` | Toggle auto-approve-everything (bare `/yolo` toggles). |
| `/new` | Clear the chat and start a fresh session. |
| `/help` | List the commands. |

## Install / run

Requires Python 3.11+.

```sh
uv sync
uv run agentclip            # in the project you want the agent to work on
# or: uv run agentclip --project path/to/project --service chatgpt-attach
```

Linux clipboard: the bundled backend works on X11 and Wayland-with-XWayland out of the box. On a pure-Wayland system install `wl-clipboard` (and `xclip` for X11 fallback).

## Configuration

TOML, merged in order: built-in defaults → `~/.config/agentclip/config.toml` (Windows: `%APPDATA%\agentclip\config.toml`) → `<project>/.agentclip.toml` → CLI flags. See `docs/design/architecture.md` for the full default config, service presets (paste-size budgets per chat service), and the command allowlist format.

## Design documents

- `docs/design/protocol.md` — the CLIP/1 wire protocol
- `docs/design/tui.md` — TUI design (Textual)
- `docs/design/architecture.md` — module layout, config, persistence, tests
- `docs/design/research-*.md` — paste-limit / clipboard / Textual research underpinning the design
