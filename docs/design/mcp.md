# MCP servers, the OpenCode way

Status: binding. Phase 1 scope; §9 lists what is deliberately out.

AgentClip gains MCP (Model Context Protocol) support by reading the **same
config OpenCode reads** - the `mcp` block of `opencode.json` - and exposing
each server's tools to the model through the clipboard protocol. The design
repeats the two moves that already worked once: permissions (read OpenCode's
file so a rule the user already trusts means the same thing here) and skills
(list cheaply in the bootstrap, load detail on demand, because the paste
budget is the scarcest resource in the system).

## 1. Config: the same file, the same shape

The `mcp` key of `opencode.json` maps server name -> server table:

- `{"type": "local", "command": [...], "cwd"?, "environment"?, "enabled"?,
  "timeout"?}` - a stdio server AgentClip spawns.
- `{"type": "remote", "url": ..., "headers"?, "enabled"?, "timeout"?,
  "oauth"?}` - a Streamable-HTTP server (with automatic SSE fallback,
  matching OpenCode's transport probing).
- `{"enabled": false}` with no `type` - a bare disable, valid because
  OpenCode accepts it (it exists to switch off a server declared in another
  config layer). Any other type-less entry is skipped with a warning.

`timeout` is milliseconds. The effective default is **30000**, not the 5000
OpenCode's docs claim: their runtime constants say 30s (mcp/index.ts:38,
catalog.ts:11) and behaviour beats documentation when the point is
compatibility.

Files read, both from **this PC** (never through the Host):

    ~/.config/opencode/opencode.json          always
    <project root>/opencode.json              LOCAL sessions only

Same-name entries merge per-field, project over global, mirroring OpenCode's
deep merge. The project file is skipped in remote sessions for the same
reason permissions are pinned local (config.py header, remote-ssh.md
decision 6), with the stakes raised: a `local` server is a *command this PC
will run*, and a remote machine must not get to decide what the operator's
PC executes. `opencode.jsonc` is out of scope for phase 1 (JSON with
comments needs a real stripper; a wrong one corrupts strings).

`{env:VAR}` and `{file:path}` placeholders are substituted on every string
leaf after parsing. OpenCode substitutes over the raw text before parsing;
the post-parse form covers every real use (secrets in `headers`,
`environment`, `url`) without letting a placeholder rewrite the config's
structure. An unset `{env:...}` substitutes empty, matching OpenCode.

The loader lives in `agentclip/mcp/config.py`, follows
`_load_permission_rules`'s triage exactly (silent when the file is absent,
one warning when it exists but cannot be understood, never fatal), and is
called from `load_config` next to the permission loader, gated by a new
`[mcp]` table: `enabled` (default true) and `opencode_config` (blank = the
same `default_opencode_config_path()` the permission reader uses - one
function, not a copy). Parsed servers land on `Config.mcp_servers`.

## 2. Dependency: an extra, like cv

The client runtime uses the official `mcp` SDK (>=2,<3), which drags in
httpx/anyio/pydantic - real weight for a PyInstaller one-file build. It
follows the `cv` extra's precedent to the letter: an optional
`agentclip[mcp]` extra, imported lazily inside `agentclip/mcp/client.py`,
and an install without it stays fully functional - configured servers get
status `missing_sdk` and a warning naming the fix (`uv pip install
'agentclip[mcp]'`). The dev dependency group carries the SDK so the test
suite always exercises the real thing. Hand-rolling the protocol was
considered and rejected: stdio framing plus Streamable HTTP plus SSE
fallback plus capability negotiation is exactly the kind of surface that
looks small and is not.

`agentclip/mcp/types.py` and `agentclip/mcp/config.py` stay stdlib-only so
`config.py` may import them unconditionally; only `client.py` touches the
SDK. The package is a leaf below config in the layering rules
(test_layering.py), which is why the ToolSpecs of section 4 live in
`agentclip/tools/mcp_tools.py` - the tools layer imports mcp, never the
reverse.

## 3. Runtime: one manager, one loop thread, per process

`McpManager` (`agentclip/mcp/client.py`) is built once per process in
`cli.py:main()` - the same lifetime as skill discovery - and handed both to
`AgentClipApp` (for status display) and into `make_engine_factory`'s closure
(for the tools). It owns a single background thread running an asyncio loop;
all SDK objects live on that loop, and the synchronous facade the tool
handlers call (`list_tools()`, `schema()`, `call()`, `statuses()`) crosses
via `asyncio.run_coroutine_threadsafe` with the per-server timeout. That
matches the existing threading contract: handlers already run on a worker
thread (`controller._engine_call` / `asyncio.to_thread`) and must not touch
Textual's loop.

Connection is **lazy but eager-on-arm**: nothing connects at import or TUI
start; the first session build (and any explicit status request) kicks off a
non-blocking connect of every enabled server, concurrently, so tools are
usually live by the time a model asks. Per-server outcomes are the status
union in `types.py` (`pending / connecting / connected / disabled / failed /
needs_auth / missing_sdk`); a failed server contributes no tools and one
line of status - never an exception that reaches a session. A remote server
answering 401/403 maps to `needs_auth` (phase 1 has no OAuth; the status
tells the user to authenticate via headers or OpenCode - §9). Tool lists are
cached at connect; `tools/list_changed` refresh is out of scope.

Teardown: `McpManager.close()` joins the loop thread after closing every
client, wired into `cli.py:main()`'s existing `finally` beside
`host.close()`.

## 4. Two tools: `mcp_schema` reads, `mcp` acts

Tool ids follow OpenCode's composite convention so permission globs mean the
same thing in both tools: `sanitize(server) + "_" + sanitize(tool)` with
`[^a-zA-Z0-9_-] -> _`.

- **`mcp_schema`** (approval_kind `auto`): given `tool: <id>`, returns the
  cached description and JSON input schema; given no params, the full
  listing. Serves from the connect-time cache - no server round-trip - which
  is why it is safe as `auto` and usable under plan mode.
- **`mcp`** (approval_kind `command`): `tool: <id>` plus `args` as a JSON
  object riding a **heredoc** (`args << EOT ... EOT`), the same mechanism
  write_file uses for bodies; a missing `args` means `{}`. The handler
  json-parses, calls through the manager, and renders the result's text
  content (truncated per `ToolContext.caps`, like every other tool).

`command` for the invoker is deliberate: an MCP tool can do anything, so it
must be plan-mode-denied and legacy-mode-gated, and reusing the existing
kind buys both without widening the `Literal`. In the approval UI the
preview renders `server tool + args` via `ToolSpec.preview`.

Permission wiring:

- `TOOL_PERMISSIONS` gains `"mcp": ("mcp", "tool")` - ruleset rules like
  `{"mcp": {"github_*": "allow"}}` gate per server or per tool.
  `mcp_schema` takes the default fallback (its own name as key): metadata
  listing stays cheap even where `mcp` is locked down.
- `default_rules()` appends `("mcp", "*", "ask")`. Without it the built-in
  `"*": allow` would silently auto-approve every MCP call in ruleset mode -
  the one outcome this design must make impossible. Yolo may answer the ask;
  an explicit user `deny` still wins, as everywhere.
- Legacy mode (no opencode.json ruleset): `mcp` always gates. The command
  allowlist matches shell prefixes and must not be consulted for MCP ids.
- "Always allow" remembered from the gate maps to
  `PermissionRule("mcp", <tool id>, "allow")` via a new `always_pattern`
  arm - per tool, not per server, because the user approved one tool's
  behaviour, not a server's whole surface.

## 5. The paste budget is a hard wall

The smallest presets bootstrap at ~11.9k of a 12k budget; `BudgetExceeded`
on the bootstrap is a session that never arms. Therefore, binding rule: 
**adding MCP must never push a preset that bootstrapped yesterday over its
budget.** Mechanism, in order of degradation:

1. The `mcp`/`mcp_schema` catalog docs are added only when at least one
   server is configured and enabled.
2. The listing inside the catalog doc (composite ids + one-line clipped
   descriptions, `skill_listing`-style with a `+N more` footer) is bounded
   by a budget derived in `cli.py:build()` from the preset - and the
   *whole* MCP addition (fixed prose + listing) must fit in
   `max_paste_chars` minus a measured MCP-free bootstrap size minus margin.
3. If the remainder cannot hold even the fixed prose plus one listing line,
   both specs are dropped for that session and a warning surfaces in the
   TUI ("paste budget too small for MCP tools on this service").

Build() measures rather than guesses: render the MCP-free spec once per
preset (`render_spec` is pure) and size the addition from the real number.

## 6. TUI surface

- Statusbar: a new optional `mcp` segment ("mcp 2/3" = connected over
  enabled; disabled entries count in neither), hidden via `.display = False`
  when the app has no manager, following the `armed`/`instr` pattern. It sits
  at the far right against the project root - both are facts about the app
  run, not about one session's turn.
- Sidebar: an "MCP" titled block with PROJECT (same scope), one `side-status`
  line per server (name + human state, tool count when connected, detail on
  `failed`/`needs_auth`, one ellipsized row each), painted from
  `McpManager.statuses()` - display only, like `show_profile()`. Composed
  only when the app was built with a manager, and painted once at mount so
  the lazy-connect resting states (pending/disabled) show before any
  transition fires. Rows are addressed by config-order index, not name -
  names can collide once sanitized into widget ids.
- Connect failures and `needs_auth` land in the transcript **once per server
  per state** (reconnect churn spams nothing) and toast as warnings; `connected` toasts quietly (severity
  information, once) and never notes. The note goes to the **master window's
  panel** directly rather than the controller-focused one: MCP state is
  app-level - sessions come and go under it, and mid-delegation the focused
  panel is the sub-agent's. The panels are mounted for the app's whole life,
  so the channel exists pre-session too; nothing has to be parked for the
  next session start.
- `/mcp` (app/commands.py registry, so help/popup/dispatch stay pinned): the
  full listing - state, tool count, detail - as one transcript note, no
  session gate. The controller stays clear of `agentclip.mcp` (layering): it
  takes a supplier of duck-typed status rows, and the TUI hands it
  `McpManager.statuses` bound.

Status flows from the manager to the TUI over a thread-safe callback the
manager invokes from its loop thread; the screen's hook only posts a
`McpStatusChanged` message (`post_message`, the same discipline as
`_on_call_output` - not `call_from_thread`, which refuses the same-thread
case `ensure_started`'s missing_sdk report can produce) and the handler
repaints both surfaces from a fresh `statuses()` on Textual's loop.

## 7. Testing

- Config: tests beside tests/test_config.py conventions; the existing
  `_no_real_opencode_config` fixture already isolates the path because the
  reader shares `default_opencode_config_path()`.
- Client: in-process servers via the SDK's `MCPServer` + memory transport
  (`Client(server_instance)`) - real protocol, no subprocesses, ungated.
  One stdio test may spawn a `sys.executable` child running a tiny script
  (run_command tests already spawn children); anything touching real OS
  input stays behind the usual gates.
- Tools/approval: FakeHost-style unit tests with a stub manager; UI pilot
  tests only where a screen changed.

## 8. Naming and errors

Handlers return the shared error shape (`error_result` codes + `hint:`
line): `unknown_tool` for an id no connected server exports (listing the
close matches), `bad_param` for unparseable `args` JSON, `mcp_unavailable`
for a server that is configured but not connected (naming its status), and
`mcp_error` for a tool call the server itself rejected (`isError` result),
carrying the server's text. Timeouts surface as `mcp_error` with the
timeout named in ms.

## 9. Out of scope for phase 1 (recorded so they stay decisions)

- OAuth (DCR/PKCE/callback server). `needs_auth` status + static `headers`
  cover API-key servers; reusing OpenCode's `mcp-auth.json` tokens is the
  planned phase 2, full OAuth only if that proves insufficient.
- MCP resources and prompts (OpenCode's synthetic `list_mcp_resources`
  family), `tools/list_changed` live refresh, `opencode.jsonc`, nested
  `.opencode/opencode.json` discovery, and remote-project config files.
- Per-server `[mcp]` overrides in `.agentclip.toml` beyond `enabled`.
