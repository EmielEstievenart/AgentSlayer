# Commands

Slash commands are typed in the chat box: Enter sends, and a newline is `ctrl+j` (both shells) or `shift+enter` (GUI). Keyboard shortcuts work when the chat box is *not* focused (press Esc in an empty box to get there). Both shells share the commands; the key list below is the TUI's.

## Slash commands

| Command | Does |
|---|---|
| `/help` | List all commands (aliases: `/commands`, `/?`) |
| `/new` | Start over: click "new chat" in the browser, clear the transcript, fresh session. Works mid-turn — the running turn is aborted first |
| `/abort` | End a **sub-agent delegation** in flight (contrast `ctrl+x`, which only cancels the tool calls running right now) |
| `/identify` | Draw boxes where AgentClip sees the chat window's parts — calibration aid, touches nothing |
| `/log` | Toggle the harness decision-log pane |
| `/mcp` | List MCP servers: state, tools, errors — including entries whose config was refused (`invalid`, with the reason) |
| `/skills` | List loaded skills and the folder each came from |
| `/armed [on\|off]` | May AgentClip touch the screen at all (click/paste/scroll). Same as F5. A machine property — survives `/new` |
| `/mode [build\|plan]` | Set the permission mode; bare `/mode` reports it. Same dial as `shift+tab` |
| `/theme [name]` | Set the theme; bare `/theme` lists them |
| `/config [global\|local]` | Bare: report where both permissions.json files live. With a layer: create that file (defaults) if missing and copy its path to the clipboard |
| `/config global\|local reset` | Overwrite that file's permission rules with the shipped defaults, **preserving its `mcp` block**. Rules are read at launch — restart to run under them |
| `/unattended [on\|off]` | Auto-**deny** everything that would ask you (you're away). Bare toggles. Amber `⚠ UNATTENDED` badge; survives `/new` |
| `/yolo [on\|off]` | Auto-**approve** everything that would ask you. Bare toggles. Red `⚡ YOLO` badge; dies with the session, never inherited by sub-agents. Explicit deny rules and the deny-token backstop still hold |

Notes:

- A skill that bundles side files (`scripts/`, `references/`) works: loading it tells the model the skill's folder and what is in it, and those files are readable — read-only, by full path. Nothing else outside the project is.
- `/config local` and `/config local reset` refuse in a remote session — the project's rules live on the target; edit them there.
- Unknown `/command` warns and lists the real ones; the box is cleared either way.
- Typing `/` pops an autocomplete list (nothing preselected on a bare `/` — a stray `/` + Enter can never fire a command). Enter/Tab completes; the next Enter sends.

## Three parsing rules

- **`//text`** sends a literal message starting with `/` — the only escape.
- **Answers win.** While the model is waiting on an `ask_user` question, whatever you type is the answer, verbatim — `/no` or `/etc/hosts` is delivered, never parsed as a command.
- **Esc dismisses a question.** Esc (empty box) parks a pending question without answering: commands route again, `■ QUESTION PARKED` shows in the status bar, and your next regular message is delivered as "the user declined to answer and wants this instead".

## Approval gate

| Key | Does |
|---|---|
| `y` | Approve this call |
| `n` | Reject — opens a field for an optional reason (Enter sends, Esc cancels) |
| `a` | Approve **and remember**: edits → allow all edits this session; commands/MCP → allow "calls like this one" (e.g. `git push *`) |

## Keyboard reference

**Anywhere:** `F1`/`?` help · `F2` service editor · `F4` theme settings · `F5` toggle armed.

**Main screen** (chat box unfocused):

| Key | Does |
|---|---|
| `t` | Focus the chat box |
| `u` | Undo the last turn's file changes |
| `c` | Re-copy the last outbound payload; **double-tap within 1.5 s** re-delivers it into the chat |
| `i` | Force-ingest: "the reply is on the clipboard right now" |
| `w` | Toggle the clipboard watcher |
| `r` | Arm/disarm the service's `extra_instructions` for the next send |
| `e` | End the session (summary screen) |
| `l` | Export the session log |
| `x` | Expand/collapse the last transcript entry |
| `shift+tab` | Cycle permission mode build ↔ plan (always live, even mid-turn) |
| `ctrl+x` | Cancel the tool calls executing right now (the turn still reports) |
| `ctrl+o` | Toggle the live output of a running command |
| `ctrl+s` / `ctrl+enter` | Send the chat box |
| `F3` / `F7` / `F8` | Toggle sidebar / elements panel / harness log |
| `F6` | Cycle transcript tabs (master / sub-agent) |

**Esc** is staged: text in the box → clear it (Ctrl+Z restores) → pending question → dismiss it → otherwise blur the box so the single-key shortcuts work.
