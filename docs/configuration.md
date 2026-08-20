# Configuration

AgentClip reads two kinds of files: **TOML app config** (settings) and **JSON permission config** (rules + MCP servers). Everything has a working built-in default — all files are optional.

## Files and merge order

| File | Format | Location | Holds |
|---|---|---|---|
| Global config | TOML | `%APPDATA%\agentclip\config.toml` (Windows) / `~/.config/agentclip/config.toml` (Linux) | All settings below |
| Project config | TOML | `<project>/.agentclip.toml` | Same tables; project overrides |
| Global permissions | JSON | `permissions.json`, same folder as the global config | Permission rules + MCP servers |
| Project permissions | JSON | `<project>/.agentclip/permissions.json` | Same schema; project outranks global |
| Appearance profiles | PNG + JSON | `profiles/<service-key>/`, same folder as the global config | Captured screen templates + click points — edited via the service editor (F2), not by hand |

TOML merge order: **built-in defaults → global config.toml → project .agentclip.toml → CLI flags.** Tables merge per key; scalars *and lists* replace (a project can tighten a list, never extend it by accident). A broken file never crashes startup — problems become warnings and the default wins.

**Remote sessions** (`--ssh`): the project config and *both* permission files are read from the **target machine** — the machine running your code owns its policy. The global config.toml stays local (it describes this PC: clipboard, screen, themes).

In-app changes (service editor, theme pickers, saved remote targets) are persisted to the **global** config.toml.

## config.toml reference

### [general]

| Key | Default | Meaning |
|---|---|---|
| `service` | `"chatgpt-attach"` | Active service preset for the master window |
| `subagent_service` | `""` | Preset for the sub-agent window (`""` = same as master) |
| `chars_per_token` | `3` | Token-estimate divisor, 1–10 |
| `theme` | `"textual-dark"` | TUI theme: `textual-light`, `textual-dark`, `claude-warm`, `claude-dark` |

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
| `max_command_output_chars` | `8000` | Per command |
| `max_result_chars` | `6000` | Per tool result |
| `max_grep_matches` | `200` | Per grep |
| `command_timeout_s` | `120` | Command wall clock |

### [notify], [gui], [backup], [paths]

| Key | Default | Meaning |
|---|---|---|
| `notify.bell` / `notify.toast` | `true` / `true` | Attention signals |
| `gui.theme` | `"dark"` | GUI palette: `dark`, `light`, `claude-warm`, `claude-dark` (separate from `[general] theme` — different theme systems) |
| `backup.keep_sessions` | `5` | Per-turn backup retention, 1–1000 |
| `paths.exclude` | `.git`, `node_modules`, `.venv`, `__pycache__`, caches, `dist`, `build`, IDE dirs, … | Directory names the tools skip. `.agentclip/` and `.agentclip.toml` are **always** excluded — the model can never read its own rules or backups |

### [remote.\<name\>] — saved SSH targets

```toml
[remote.pi]
host = "raspberrypi.local"   # default: the table name itself
user = "emiel"               # default: ""
port = 22                    # default: 0 = ~/.ssh/config, else 22
root = "/home/emiel/code/thing"
```

Use with `agentclip --ssh pi`. `--ssh` also accepts a raw `user@host` or a pasted `ssh …` command.

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

Built-ins can be edited but not deleted. Fields (all editable in the F2 service editor):

| Key | Default | Meaning |
|---|---|---|
| `label` | — | Display name |
| `max_paste_chars` | per preset | Per-message budget, engine-enforced |
| `total_context_chars` | per preset | Whole-conversation estimate |
| `wrap_blocks_in_fence` | `true` | Fence payloads against prose flattening |
| `attachment_note` | `true` | Mention attachments in the bootstrap |
| `delivery` | `"paste"` | `paste` \| `stream` (typed keystrokes) |
| `auto_submit` | `false` | May AgentClip press Enter after delivering |
| `snap_back` | `true` | Return focus to AgentClip after an auto-send (`false` = debug aid) |
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

## permissions.json — the rules

One schema for both the global and the project file. `/config` tells you where they live; `/config global|local reset` rewrites one to the shipped defaults (preserving your `mcp` block). **Rules are read once, at launch** — not even `/new` re-reads them; restart AgentClip after editing.

```json
{
  "permission": {
    "read": { "*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow" },
    "list": "allow", "glob": "allow", "grep": "allow",
    "skill": "allow", "mcp_schema": "allow",
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
| `edit` | `write_file`, `edit_file`, `delete_file` | file path |
| `list` / `glob` / `grep` | the search tools | path / pattern |
| `bash` | `run_command` | the whole command line |
| `task` | `delegate` (sub-agents) | the task text |
| `mcp` | MCP tool calls | `server_tool` composite id |
| `mcp_schema` | MCP schema listing (metadata only) | — |
| `skill` | loading a skill | skill name |

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

- `{env:VAR}` reads the **target machine's** environment (empty if unset); `{file:path}` reads a file, relative paths anchored to the config file that declared the entry.
- `{"enabled": false}` with no `type` is a legal disable-patch onto a server declared in the other layer.
- Local (stdio) servers are refused in remote sessions — a remote target only gets `remote` servers.
- MCP tool calls gate under the `mcp` permission kind; listing schemas only needs `mcp_schema`.
