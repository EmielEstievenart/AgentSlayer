# Commands

Slash commands are typed in the chat box: Enter sends, and a newline is `shift+enter` or `ctrl+j`. Most keyboard shortcuts only fire when the chat box is *not* focused (press Esc in an empty box to get there) — a focused text box swallows bare letters, which is the only thing keeping `y`/`n`/`a` out of a sentence you are typing. **The key list below is the Chat UI's** — the shell `agentclip` opens, and the one rendering this page behind the titlebar's **docs** button. It is the only shell; the terminal one that used to share these keys was removed.

## Slash commands

| Command | Does |
|---|---|
| `/help` | List all commands (aliases: `/commands`, `/?`) |
| `/new` | Start over: click "new chat" in the browser, clear the transcript, fresh session. Works mid-turn — the running turn is aborted first |
| `/abort` | End a **sub-agent delegation** in flight (contrast `ctrl+x`, which only cancels the tool calls running right now) |
| `/identify` | Open the calibration window, where the boxes are drawn over the real screen — calibration aid, touches nothing |
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
| `F2` | Open the **Monitor UI** over this machine's own screen (closed while a remote monitor is attached): what each service *looks* like, its sizes and finish signals, where its chat window is, and what the tool is recognising right now. The titlebar's **calibrate** button and the sidebar's **Edit services...** / **Set chat region...** are the same door |
| `F3` | Hide/show the sidebar |
| `F4` | Appearance (theme) |
| `F5` | ARM / DISARM the tool (same as `/armed`). Disarmed it still watches and shows everything, but never clicks, pastes or reads your clipboard |
| `F6` | Select the next window tab — view only, it never moves what the automation drives |
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
| `r` | Re-send this service's extra instructions with the next payload (set them in the calibration window, `F2`) |
| `e` | End the session / show the summary |
| `l` | Export the whole chat log to a file (raw blocks and payloads, for debugging) |
| `x` | Expand/collapse the last collapsed output |

The key strip along the bottom carries the ones worth remembering, and fades a
key that cannot fire yet (a turn has to finish, a gate has to open, a session
has to start). A key the current setup rules out entirely — `w` with no
clipboard watcher to run — leaves the strip instead of fading.

## Command line

| Flag | Does |
|---|---|
| `--ssh <target>` | Run this session's tools, files and skills on another machine (`user@host`, an ssh-config alias, or a pasted `ssh …` command) |
| `--monitor <host:port>` | Drive the **screen** of another machine — the one running `agentclip-monitor` |
| `--monitor @<name>` | The same, from a saved `[monitor.<name>]` target in your global `config.toml` (see `docs/configuration.md`) |
| `--monitor-token <token>` | The monitor's token. Prefer a saved target or `AGENTCLIP_MONITOR_TOKEN` — `argv` is world-readable |
| `--calibrate` | **Removed.** The window it opened is `agentclip-monitor`'s own now; the flag prints one line saying so and exits |
| `--tui` | **Removed.** The Textual terminal shell is gone; the flag prints one line saying so and exits. Kept for one release so a script that still carries it is told what happened |

The monitor's token comes from three places, and the first one that has it
wins: `--monitor-token`, then `$AGENTCLIP_MONITOR_TOKEN`, then the `token` key
of the saved `[monitor.<name>]` target. The flag is listed last on purpose —
anything on a command line is visible to every process on the machine.

`--ssh` and `--monitor` are separate questions and can be used apart or
together: `--ssh` moves where your *files and commands* live, `--monitor` moves
which *screen* the browser automation watches, clicks and pastes into.

### On the machine the browser is on

Two commands run over there, and neither needs a session or an engine:

- `agentclip-monitor` — the Monitor UI: capture what the service looks like, draw the chat region, watch what is being recognised, and start the port. Run this first; a monitor with nothing captured has nothing to find.
- `agentclip-monitor --port 7777` — the standing monitor. It keeps running across disconnects, serves one brain at a time, and hosts nothing else.
- `agentclip-monitor --port 7777 --bind 0.0.0.0` — listen on something other than loopback. Only with the warning below.

**The monitor port is unauthenticated.** Anything that can reach it can move
that machine's mouse, type on its keyboard and read its clipboard. So the
default bind is `127.0.0.1` and `--bind` is the explicit opt-in; the intended
deployment is a VM on a private host-only network, or an SSH forward from your
own machine:

```
ssh -N -L 7777:127.0.0.1:7777 you@the-vm     # then: agentclip --monitor 127.0.0.1:7777
```

### Attaching a monitor from the window

You do not have to know any of those flags, and you do not have to restart to
change your mind. The **Connect** dialog has two tabs:

- **Executor** — which machine this session's *files and commands* live on. Connecting there starts a new session, because one session is one host.
- **Monitor** — which machine's *screen* this window drives. Attaching there does **not** touch the session: the transcript, the engine and your files stay exactly where they are while the browser automation moves to the other machine.

The Monitor tab offers two ways to reach one:

- **Direct** — host, port and token. The monitor's own address, on a network this PC can reach.
- **Via SSH** — pick one of your saved SSH targets, then give the port *as seen from that machine* (usually `127.0.0.1:7777`, which is where a monitor bound to loopback is). AgentClip forwards it over the SSH connection it already holds: no second login, no password asked twice, no `ssh -L` to leave running. The Executor has to be connected to that target first — the Monitor tab rides the same connection, and it says so rather than quietly starting a second one.

A token is still required over the tunnel: SSH proves who reached the port, not
which of the several things on that machine did. **Disconnect** hands the
window back to this machine's screen.

A monitor you attached can be saved as a `[monitor.<name>]` target for next
time — the token goes with it, into your global `config.toml`.

While a monitor link is up the Chat UI's own calibration door (`F2`) is
closed: the pixels are on the other machine, so calibration runs there
(`agentclip-monitor`). Disconnecting opens it again.

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
5. a modal is up → close that;
6. an `ask_user` question is pending → park it (`■ QUESTION PARKED`);
7. nothing left to cancel → nothing happens.
