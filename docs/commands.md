# Commands

Slash commands are typed in the chat box: Enter sends, and a newline is `shift+enter` or `ctrl+j`. Most keyboard shortcuts only fire when the chat box is *not* focused (press Esc in an empty box to get there) — a focused text box swallows bare letters, which is the only thing keeping `y`/`n`/`a` out of a sentence you are typing. **The key list below is the GUI's** — the shell plain `agentclip` opens, and the one rendering this page behind the titlebar's **docs** button. The deprecated TUI (`agentclip --tui`) shares every slash command and most of the keys.

## Slash commands

| Command | Does |
|---|---|
| `/help` | List all commands (aliases: `/commands`, `/?`) |
| `/new` | Start over: click "new chat" in the browser, clear the transcript, fresh session. Works mid-turn — the running turn is aborted first |
| `/abort` | End a **sub-agent delegation** in flight (contrast `ctrl+x`, which only cancels the tool calls running right now) |
| `/identify` | Draw boxes where AgentClip sees the chat window's parts — calibration aid, touches nothing |
| `/log` | Toggle the harness decision-log pane |
| `/mcp` | List MCP servers: state, tools, errors — including entries whose config was refused (`invalid`, with the reason) |
| `/skills` | List loaded skills grouped by the folder they came from — name, description, and a `[hidden from the model]` mark on the ones only you can reach |
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

`F1` (or `?`) opens the same list inside the app, drawn from the table the app
actually dispatches from — so it cannot advertise a key that does not exist.

**Always live**, even while you are typing in the chat box. (`?` is the one
exception: a printable character never fires a shortcut mid-sentence.)

| Key | Does |
|---|---|
| `F1` / `?` | This help sheet (`Esc` or `F1` closes it) |
| `F2` | Service editor — sizes, what each service *looks* like, which finish signals it may watch. The sidebar's **Edit services...** button is the same door |
| `F3` | Hide/show the sidebar |
| `F4` | Appearance (theme) |
| `F5` | ARM / DISARM the tool (same as `/armed`). Disarmed it still watches and shows everything, but never clicks, pastes or reads your clipboard |
| `F6` | Select the next window tab — view only, it never moves what the automation drives |
| `F7` | Hide/show ELEMENTS: the pictures the automation is recognising right now |
| `F8` | Hide/show the harness decision log (same as `/log`) |
| `shift+tab` | Cycle the permission mode build ↔ plan. Works before a session and mid-turn |
| `ctrl+enter` | Send the chat box without having to be in it |
| `ctrl+x` | Cancel the tool calls running now (the turn still ends cleanly and the model is told) |
| `ctrl+o` | Show/hide what the running command is printing (clicking the run panel does the same) |
| `ctrl+q` | Quit (asks first when a turn is mid-flight, as closing the window does) |

**Chat box unfocused** — press `Esc` in an empty box to get there. The approval
keys `y` / `n` / `a` above live here too.

| Key | Does |
|---|---|
| `t` | Jump back to the chat box |
| `u` | Undo the last turn (confirm first; a revert notice is copied for the model) |
| `c` | Re-copy the last outbound payload; press `c` **twice quickly** and it is pasted into the chat as well |
| `i` | Force-ingest the clipboard now — "the reply is on the clipboard right now" |
| `w` | Pause/resume the clipboard watcher |
| `r` | Re-send this service's extra instructions with the next payload (set them with `F2`) |
| `e` | End the session / show the summary |
| `l` | Export the whole chat log to a file (raw blocks and payloads, for debugging) |
| `x` | Expand/collapse the last collapsed output |

The key strip along the bottom carries the ones worth remembering, and fades a
key that cannot fire yet (a turn has to finish, a gate has to open, a session
has to start). A key the current setup rules out entirely — `w` with no
clipboard watcher to run — leaves the strip instead of fading.

**Inside the chat box:**

- `Enter` sends. `shift+enter` and `ctrl+j` insert a newline.
- `↑` / `↓` recall your previous sends, but only from the very top or bottom edge of the text — anywhere else they move the caret, so a pasted traceback stays navigable.
- Typing `/` pops the command autocomplete; `Enter` or `Tab` completes it.

**Esc is staged** — each press spends exactly one stage, so nothing you typed is
ever lost to a stray press:

1. the command autocomplete is open → close it, text and caret untouched;
2. text in the box → clear it (`Ctrl+Z` restores it);
3. empty box → blur it, which is what makes the single-key shortcuts reachable;
4. the reject-reason box is open but has no caret → close it without rejecting;
5. a modal, or the service editor, is up → close that (the editor may refuse while a capture overlay is on screen);
6. an `ask_user` question is pending → park it (`■ QUESTION PARKED`);
7. nothing left to cancel → nothing happens.
