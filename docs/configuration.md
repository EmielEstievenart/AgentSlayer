# Configuration

AgentClip reads two kinds of files: **TOML app config** (settings) and **JSON permission config** (rules + MCP servers). Everything has a working built-in default — all files are optional.

## Files and merge order

| File | Format | Location | Holds |
|---|---|---|---|
| Global config | TOML | `%APPDATA%\agentclip\config.toml` (Windows) / `~/.config/agentclip/config.toml` (Linux) | All settings below |
| Project config | TOML | `<project>/.agentclip.toml` | Same tables; project overrides |
| Global permissions | JSON | `permissions.json`, same folder as the global config | Permission rules + MCP servers |
| Project permissions | JSON | `<project>/.agentclip/permissions.json` | Same schema; project outranks global |
| Appearance profiles | PNG + JSON | `profiles/<service-key>/`, same folder as the global config | Captured screen templates + click points — edited in the **calibration window**, not by hand |

> **The calibration window** is where everything made of pixels is edited: the
> per-service editor (sizes, finish signals, captured templates, click points),
> the box you draw around a chat window, and the live ELEMENTS column showing
> what the tool is recognising right now. It is a **window of its own**, not a
> panel — open it from the Chat UI with `F2`, the titlebar's **calibrate**
> button, or the sidebar's **Edit services...** / **Set chat region...**, and
> run it alone as `agentclip-monitor` on the machine the browser is on.
> One at a time: a second press says it is already open. While it is up the chat
> Chat UI stops polling the screen, because its capture overlays land on the very
> browser the detectors watch. See `docs/design/ui-monitor.md` §2.6 and §6.4.

TOML merge order: **built-in defaults → global config.toml → project .agentclip.toml → CLI flags.** Tables merge per key; scalars *and lists* replace (a project can tighten a list, never extend it by accident). A broken file never crashes startup — problems become warnings and the default wins.

**Remote sessions** (`--ssh`): the project config and *both* permission files are read from the **target machine** — the machine running your code owns its policy. The global config.toml stays local (it describes this PC: clipboard, screen, themes).

**Split mode** (`--monitor host:port`): the *screen* moves instead of the files. The Chat UI runs here; the browser, the mouse, the clipboard and the appearance profiles all live on the machine running `agentclip-monitor` there. Two commands run over there, and neither needs a session or an engine:

- `agentclip-monitor` — the Monitor UI: capture the appearances and draw the chat region on that machine. The profiles stay there; they never cross the link.
- `agentclip-monitor --port 7777` — the standing monitor, on loopback. It keeps running across disconnects and serves one brain at a time.
- `agentclip-monitor --port 7777 --bind 0.0.0.0` — listen on something other than loopback, which is the explicit opt-in described below.
- `agentclip --monitor 127.0.0.1:7777` — here, once a forward is up; or `--monitor @name` for a saved `[monitor.<name>]` target, or the Connect dialog's **Monitor** tab, which needs no flag and no restart and can open the forward itself (**Via SSH**).

**The monitor port is unauthenticated** (`docs/design/ui-monitor.md` §5). It is a channel to that machine's mouse, keyboard and clipboard, so the default bind is `127.0.0.1` and `--bind` is the opt-in; the intended deployment is a VM on a private host-only network, or an SSH forward: `ssh -N -L 7777:127.0.0.1:7777 you@the-vm`.

If the link drops, the Chat UI parks on `disconnected`, says so, and redials on its own (1s, 2s, 4s… up to 10s) until it is back — nothing is buffered or replayed, and everything is re-derived from the screen on reconnect. While a monitor is attached, the Chat UI's `F2` is closed: calibration runs on the monitor's machine. Disconnecting from the Monitor tab opens it again.

In-app changes (the calibration window, theme pickers, saved remote targets, saved monitors) are persisted to the **global** config.toml.

## Running the monitor on Linux

The monitor half — `agentclip-monitor`, window and all — runs on **X11**. It captures with `XGetImage`, clicks and types through the XTest extension, and switches windows through EWMH's `_NET_ACTIVE_WINDOW`; on Windows the same operations are GDI and `SendInput`, and the backend is picked by platform with nothing to configure.

**Native Wayland is not supported.** A Wayland session gives no client a way to screenshot another client's surface or synthesise input into it, so there is nothing for the monitor to build on. A browser running under **XWayland** *is* a normal X client and works — start it with `GDK_BACKEND=x11` (Firefox: `MOZ_ENABLE_WAYLAND=0`), or log into an "X11"/"Xorg" session at the display manager. `echo $XDG_SESSION_TYPE` says which you are in.

What the machine needs:

- **`DISPLAY` must be set** in the environment the monitor is started from — a bare `ssh you@the-vm` gives you a shell with no `DISPLAY`, so export the desktop's own (usually `export DISPLAY=:0`) before launching it.
- **python-xlib** — a normal dependency on Linux, so `uv sync` / `pip install agentclip` already brings it; the distro package is `python3-xlib` if you would rather not install it from PyPI.
- **`python3-tk`** — tkinter, for the `--pick-region` overlay you drag the chat region with. `scripts/build-exe.sh` refuses to build the monitor without it.
- **`xclip` or `xsel`** — pyperclip's clipboard backends. Without one of them the clipboard is unavailable and AgentClip says so at startup instead of copying silently into nothing.
- **copykitten** is the preferred clipboard backend and is *untested* on Linux; if it misbehaves, uninstall it and the pyperclip fallback takes over (set `[clipboard] provider = "pyperclip"` to force it).
- **A window manager that speaks EWMH** — anything mainstream does. Without one, the snap-back after an auto-copy click simply reports that focus did not come, and the rest keeps working.

`agentclip-monitor --port 7777` binds loopback on that machine, so reach it with an SSH forward rather than `--bind`: `ssh -N -L 7777:127.0.0.1:7777 you@the-vm`, then `agentclip --monitor 127.0.0.1:7777` here.

## config.toml reference

### [general]

| Key | Default | Meaning |
|---|---|---|
| `service` | `"chatgpt-attach"` | Active service preset for the master window |
| `subagent_service` | `""` | Preset for the sub-agent window (`""` = same as master) |
| `chars_per_token` | `3` | Token-estimate divisor, 1–10. Sizes in the UI are shown as `~N tokens` — this is what they are divided by |
| `theme` | `"textual-dark"` | Legacy theme name, kept so an existing config file still loads: `textual-light`, `textual-dark`, `claude-warm`, `claude-dark`. The window reads `[gui] theme`; this one named the terminal shell's theme, and that shell is gone |

### [clipboard]

| Key | Default | Meaning |
|---|---|---|
| `provider` | `"auto"` | `auto` \| `copykitten` \| `pyperclip` \| `manual` |
| `poll_interval_ms` | `300` | Watcher poll rate, 100–5000 |

### [approval]

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"build"` | Starting permission mode: `build` or `plan` |
| `yolo` | `false` | Start with YOLO armed (auto-approve every ask; explicit deny rules still refuse) |
| `unattended` | `false` | Start unattended (auto-deny everything that would ask you) |
| `auto_accept_edits` | `false` | Seed the session rule "allow all edits" |
| `command_deny_tokens` | `";" "&&" "\|\|" "\|" "` `` ` `` `" "$(" ">" "<" newline` | Substrings that stop an allow-rule from auto-running a command (see permissions below) |

### [permission] and [mcp]

| Key | Default | Meaning |
|---|---|---|
| `permission.enabled` | `true` | `false` leaves the shipped default ruleset in charge (never "no rules") |
| `permission.permissions_config` | `""` | Override path to the global permissions.json |
| `mcp.enabled` | `true` | `false` skips MCP entirely (file not even opened) |
| `mcp.permissions_config` | `""` | Override path for the MCP-bearing file |

### [limits]

| Key | Default | Meaning |
|---|---|---|
| `max_file_read_chars` | `20000` | Per file read |
| `max_command_output_chars` | `0` (auto) | How much of a command's or MCP tool's raw output AgentClip **keeps** |
| `max_result_chars` | `0` (auto) | How much of one result the model **sees** in the payload |
| `max_grep_matches` | `200` | Per grep |
| `command_timeout_s` | `120` | Command wall clock |

**`0` means auto**, and it is the default for the two keys above: they are worked out from the service's `max_paste_chars` when a session starts, because the right value for them depends on the budget you are actually pasting into. Write a number to override; anything you write wins at every budget.

- `max_result_chars` auto = **half the paste budget**. That is 6,000 at the 12,000-char presets — exactly what AgentClip used to hard-code, so nothing changes for a default setup — and 48,000 on a 96,000-char preset, where a fixed 6,000 used to throw away seven eighths of the room you had paid for.
- `max_command_output_chars` auto = **512,000**, and it is not a display cap at all. It is how much output a tool hands to AgentClip in the first place — a memory guard, set to the same size the recovery cache below stops at. Output past it really is gone, and the result says so in as many words (`[truncated: showing last N of M output lines]`).

A result too big for `max_result_chars`, or a whole turn too big for the service's `max_paste_chars`, is truncated in the middle. Nothing is lost: AgentClip keeps the full output and the marker left in the cut body tells the model the id and part count to ask for, which it does with the built-in `fetch_chunk` tool (auto-approved — it only re-reads what AgentClip already produced). The cache holds the last truncated payload, so a fetch is worth making in the next turn or two, not ten turns later.

### [notify], [gui], [backup], [paths]

| Key | Default | Meaning |
|---|---|---|
| `notify.bell` / `notify.toast` | `true` / `true` | Attention signals |
| `gui.theme` | `"dark"` | Chat UI palette: `dark`, `light`, `claude-warm`, `claude-dark` (separate from `[general] theme` — different theme systems) |
| `backup.keep_sessions` | `5` | Per-turn backup retention, 1–1000 |
| `paths.exclude` | `.git`, `node_modules`, `.venv`, `__pycache__`, caches, `dist`, `build`, IDE dirs, … | Directory names kept out of listings and sweeps (`list_dir`, `glob`, `grep` never descend into them) and **write-protected** — but a file inside one **can be read** when the model names it explicitly, so "read `.vscode/settings.json`" works. This is budget hygiene, not secrecy. `.agentclip/` and `.agentclip.toml` are **always** excluded and stay sealed in *both* directions — the model can never read or rewrite its own rules and backups |

### [remote.\<name\>] — saved SSH targets

```toml
[remote.pi]
host = "raspberrypi.local"   # default: the table name itself
user = "emiel"               # default: ""
port = 22                    # default: 0 = ~/.ssh/config, else 22
root = "/home/emiel/code/thing"
```

Use with `agentclip --ssh pi`. `--ssh` also accepts a raw `user@host` or a pasted `ssh …` command.

### [monitor.\<name\>] — saved Monitors

Which machine's *screen* this PC can drive — the other half of the split
(`docs/design/ui-monitor.md` §9.2). **Global config.toml only:** a monitor
target is a fact about how this PC reaches a machine, not about the project,
and in a remote session the project's `.agentclip.toml` is on the target, which
has no view of your desk. A `[monitor.*]` table in a project file is ignored,
and says so as a warning.

```toml
[monitor.vm]
host = "10.0.0.5"            # default: the table name itself
port = 7777                  # default: 7777
token = "3f9a…"              # default: "" (a monitor started --no-token has none)

[monitor.behind-pi]
via = "pi"                   # a saved [remote.<name>] SSH target
host = "127.0.0.1"           # default when `via` is set: as seen from THAT machine
port = 7777
token = "8c21…"
```

A target with `via` is reached over the SSH connection the Executor already
holds — one forwarded channel, no second login and no `ssh -L`. `host` and
`port` are then read *on that machine*, which is why they default to its own
loopback: that is where a monitor bound to `127.0.0.1` over there is.

Use with `agentclip --monitor @vm`, or pick it from the **Monitor** tab of the
Connect dialog — which is also what writes these tables, including the token.

**Where the token comes from**, first one wins: `--monitor-token`, then
`$AGENTCLIP_MONITOR_TOKEN`, then this table's `token` key. The flag is last on
purpose — `argv` is readable by every process on the machine — and the
environment variable exists for anyone who would rather not keep a secret in a
config file at all.

### [services.\<key\>] — service presets

A preset describes one chat service: its paste budget and how AgentClip drives it. Built-ins (key · paste budget · context estimate):

| Key | Budget | Context | | Key | Budget | Context |
|---|---|---|---|---|---|---|
| `chatgpt` | 4k | 500k | | `gemini` | 24k | 800k |
| `chatgpt-attach` | 12k | 500k | | `perplexity` | 6k | 100k |
| `copilot-work` | 96k | 400k | | `deepseek` | 12k | 250k |
| `copilot-web` | 12k | 150k | | `grok` | 100k | 400k |
| `copilot-free` | 6k | 128k | | `unknown` | 6k | 100k |
| `claude` | 24k | 700k | | `paranoid` | 4k | 50k |

Built-ins can be edited but not deleted. Fields (all editable in the calibration window — `F2`, the titlebar's **calibrate** button, or `agentclip-monitor` on the machine the browser is on):

| Key | Default | Meaning |
|---|---|---|
| `label` | — | Display name |
| `max_paste_chars` | per preset | Per-message budget, engine-enforced |
| `total_context_chars` | per preset | Whole-conversation estimate |
| `wrap_blocks_in_fence` | `true` | Fence payloads against prose flattening |
| `attachment_note` | `true` | Mention attachments in the bootstrap |
| `delivery` | `"paste"` | `paste` \| `stream` (typed keystrokes) |
| `auto_submit` | `false` | May AgentClip press Enter after delivering |
| `snap_back` | `true` | Return focus to AgentClip after an auto-send or auto-copy (`false` = debug aid: the browser keeps focus) |
| `finish_signals` | `["stale"]` | Which detectors may declare a reply finished: `busy`, `idle`, `stale`. Empty = never auto-detect |
| `stable_seconds` | `2.0` | Stillness window for the stale detector, 0.5–60 |
| `hover_scan` | `false` | May glide the cursor hunting a hover-only copy icon |
| `scroll_action` | `"scroll"` | `scroll` \| `page_down` \| `end` |
| `matcher` | `"anchors"` | `anchors` \| `opencv` (needs the `cv` extra) |
| `tolerance` | `24` | Pixel-match slack, 0–64 |
| `require_fenced_reply` | `false` | Refuse unfenced replies that carry tool calls |
| `extra_instructions` | `""` | Service-specific bootstrap text, armed with `r` |
| `alert_sound` | `false` | Play the two-tone alert when the loop needs you (manual copy/insert) |
| `alert_repeat_seconds` | `0` | 0 = alert once; N = repeat every N seconds while still waiting |
| `edit_by_lines` | `false` | Add `replace_lines` (edit by line range) to the catalog and teach `read_file` its `numbered` gutter. For a host that cannot echo code back verbatim — M365 Copilot rewrites lambdas, so a find/replace edit can never match there. Costs ~900 chars of bootstrap |

**Debugging delivery.** When a paste lands somewhere odd, set `snap_back = false` (or untick "focus back after send" in the calibration window): AgentClip stops taking the foreground back after its own auto-sends and auto-copies, so the browser keeps focus and you can watch where the clicks actually go. The beep you hear when the loop stalls and needs you is `alert_sound` ("beep when it stalls", same block) — it is off by default, and `alert_repeat_seconds` is how often it nags.

## permissions.json — the rules

One schema for both the global and the project file. `/config` tells you where they live; `/config global|local reset` rewrites one to the shipped defaults (preserving your `mcp` block). **Rules are read once, at launch** — not even `/new` re-reads them; restart AgentClip after editing.

```json
{
  "permission": {
    "read": { "*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow" },
    "list": "allow", "glob": "allow", "grep": "allow",
    "skill": "allow", "mcp_schema": "allow", "fetch_chunk": "allow",
    "edit": "ask", "task": "ask", "mcp": "ask",
    "bash": {
      "*": "ask",
      "git status *": "allow", "git diff *": "allow", "git log *": "allow",
      "ls *": "allow", "dir *": "allow"
    }
  },
  "agent": {
    "plan":  { "permission": { "bash": { "git status *": "allow", "git diff *": "allow" } } },
    "build": { "permission": {} }
  },
  "mcp": {}
}
```

That block *is* the built-in default — a machine with no permissions.json behaves exactly like one freshly reset.

**Actions**: `allow` (runs without asking) · `ask` (gates on you) · `deny` (refused, you never see it). A permission's value is either one action for everything (`"list": "allow"`) or a pattern table.

**Permission kinds** and what they match on:

| Kind | Gates | Pattern matches |
|---|---|---|
| `read` | `read_file` | file path |
| `edit` | `write_file`, `edit_file`, `replace_lines`, `delete_file` | file path |
| `list` / `glob` / `grep` | the search tools | path / pattern |
| `bash` | `run_command` | the whole command line |
| `task` | `delegate` (sub-agents) | the task text |
| `mcp` | MCP tool calls | `server_tool` composite id |
| `mcp_schema` | MCP schema listing (metadata only) | — |
| `skill` | loading a skill | skill name |
| `fetch_chunk` | re-reading a truncated result | chunk id |

A kind that is missing from every file defaults to `ask`. `ask_user` and `task_done` are deliberately ungateable — gating them would deadlock the conversation.

**Patterns**: `*` matches any run of characters (including spaces and slashes), `?` exactly one. A pattern ending in ` *` also matches without the argument (`ls *` matches bare `ls`). Backslashes are normalized to `/`; matching is case-insensitive on Windows. **Last match wins** — order your rules general-to-specific.

**Layering** (concatenated, last match wins):

1. built-in defaults
2. your shared `permission` blocks — global file, then project file
3. the mode overlay — in **plan** mode: `edit`, `bash`, `mcp`, `task` → deny
4. built-in `agent.<mode>` block (plan re-allows `git status` / `git diff`)
5. your `agent.<mode>.permission` blocks — global, then project

So a project file outranks the global one, `agent.plan` rules outrank shared rules, and you *can* deliberately loosen plan mode — but only from an explicit `agent.plan` block, never accidentally from the shared block.

**Modes**: `build` (default — full ruleset) and `plan` (read-only overlay: nothing that modifies runs). Cycle with `shift+tab` or set with `/mode`. Plan denials are reported to the model as "you're only exploring", distinct from hard rule denials.

**How a call is decided**, in order:

1. Rule says **deny** → refused. Nothing overrides deny, not even YOLO.
2. Rule says **allow** → runs — *unless* it's a command containing a deny token (`;`, `&&`, `||`, `|`, `` ` ``, `$(`, `>`, `<`, newline). AgentClip has no shell parser, so a chained command never auto-runs on a prefix match; it gates instead.
3. Rule says **ask** → auto-approved under YOLO, otherwise gated on you — or auto-denied under unattended.

**Session rules**: answering a gate with `a` ("always allow") appends an in-memory rule that outranks the files — blanket allow for edits, `server_tool` for MCP, the command's leading words + ` *` for bash (e.g. `git push *`). These die with the process.

## MCP servers

Declared in the `mcp` block of the same permissions.json files (global first, project outranks; same-name entries merge per field). `/mcp` lists their live state.

```json
"mcp": {
  "docs": { "type": "local", "command": ["uvx", "mcp-server-docs"],
            "cwd": "", "environment": {"TOKEN": "{env:DOCS_TOKEN}"},
            "enabled": true, "timeout": 30000 },
  "issues": { "type": "remote", "url": "https://example.com/mcp",
              "headers": {"Authorization": "Bearer {file:./token.txt}"},
              "enabled": true, "timeout": 30000 }
}
```

- `{env:VAR}` reads the **target machine's** environment (empty if unset); `{file:path}` reads a file, relative paths anchored to the config file that declared the entry. `${VAR}` is **not** expanded — it travels verbatim (straight into your `Authorization` header, where it reads as a 401). AgentClip warns when it sees one.
- `{"enabled": false}` with no `type` is a legal disable-patch onto a server declared in the other layer.
- An entry AgentClip cannot read — no `type`, a `remote` without `url`, a `local` without a `command` list — is not silently dropped: `/mcp` and the sidebar list it as `invalid`, with the reason on the row.
- Local (stdio) servers are refused in remote sessions — a remote target only gets `remote` servers.
- MCP tool calls gate under the `mcp` permission kind; listing schemas only needs `mcp_schema`.
