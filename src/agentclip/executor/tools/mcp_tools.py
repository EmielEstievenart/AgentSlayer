"""The two MCP tools: `mcp_schema` reads the cache, `mcp` calls a server.

MCP servers come from the user's `permissions.json` (docs/design/mcp.md section 1)
and reach the model as ordinary CLIP tools, so nothing in the engine, the
protocol, or ToolContext knows MCP exists. The split into two tools is the
same progressive-disclosure move skills make, for the same reason - the paste
budget is the scarcest resource in the system (design section 5):

- `mcp_schema` (approval_kind "auto") answers from the connect-time cache: a
  tool's description and JSON input schema, or the whole listing. It never
  touches a server, which is exactly why it is safe unapproved and usable
  under plan mode (design section 4).
- `mcp` (approval_kind "command") invokes one. An MCP tool can do anything, so
  it rides the kind that is plan-mode-denied and always gated.

The bootstrap only ever carries the cheap half: composite ids plus one clipped
description line, bounded by `max_listing_chars`. The schemas - the expensive
part - load on demand.

Handlers close over an `McpToolSource` (the process-wide McpManager in
production) rather than reaching through ToolContext: the tools layer may
import mcp, never the reverse (test_layering.py), and a closure keeps the
context free of a dependency only these two handlers have.

Errors follow docs/design/mcp.md section 8: the manager pre-classifies every
failure into `mcp_unavailable` / `mcp_error` / `unknown_tool` and this module
maps the code straight through, adding the hint line that says what to do next.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Sequence
from typing import Any, Protocol

from agentclip.executor.mcp.client import McpCallError
from agentclip.executor.mcp.types import McpToolInfo
from agentclip.executor.tools.registry import (
    ToolContext,
    ToolError,
    ToolSpec,
    require,
    tool_handler,
)

# Private on purpose, imported anyway: run_command and this share ONE output
# capping policy ("truncated per ToolContext.caps, like every other tool",
# docs/design/mcp.md section 4). A local copy would drift from the tail-cap
# semantics - keep the end, mark what was dropped - the moment either changed.
from agentclip.executor.tools.shell import _tail_cap
from agentclip.protocol.types import ToolCall

# One listed description is one clipped line, as in skills.py: the full text
# (and the schema) is a mcp_schema call away, and the bootstrap pays per char.
_MAX_DESCRIPTION_CHARS = 200

# The args blob is the model's text on a user-facing surface, so the approval
# drawer takes it flattened and clipped - a 200-line JSON object must not push
# the tool name itself out of view (mirrors shell.reason_line).
_ARGS_PREVIEW_CHARS = 120

_EMPTY_LISTING = "  (none connected yet - mcp_schema with no params returns the live list)"

# Shown whenever `args` cannot become a dict. It spells the heredoc out because
# the failure mode it fixes is a model sending JSON on the `args: {...}` single
# line, where a brace-heavy value cannot survive the line parser.
_ARGS_HINT = (
    'send args as a heredoc holding one JSON object: `args << EOT`, then `{"key": "value"}`,'
    " then a line that is exactly EOT."
)


class McpToolSource(Protocol):
    """The three things the handlers need from the MCP runtime.

    `McpManager` satisfies this structurally - nothing implements it by name.
    It exists so the tests can stub the runtime with a plain class and no SDK
    installed (the `mcp` extra is optional, docs/design/mcp.md section 2), and
    so this module never depends on the manager's lifecycle surface.
    """

    def tools(self) -> tuple[McpToolInfo, ...]: ...

    def schema(self, tool_id: str) -> McpToolInfo | None: ...

    def call(self, tool_id: str, args: dict[str, Any]) -> str:
        """Raises McpCallError (pre-classified) on every failure."""
        ...


# -- listing ------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace to one line and clip to `limit` chars for listing.

    Also neuters CLIP sentinels: listing lines ride the deliberately UNFENCED
    bootstrap, and a server whose tool description contains `===CLIP:...` text
    would put sentinel-shaped prose inside the model's operating brief. Servers
    come from the user's own permissions.json, so this is hygiene at a trust
    boundary rather than a hole being closed - but hygiene is cheap.
    """
    flat = " ".join(text.split()).replace("===CLIP:", "==CLIP:")
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _listing_line(info: McpToolInfo) -> str:
    description = _clip(info.description, _MAX_DESCRIPTION_CHARS)
    return f"  - {info.id}: {description}" if description else f"  - {info.id}"


def mcp_listing(tools: Sequence[McpToolInfo], *, max_chars: int) -> str:
    """The indented `- id: description` block carried by the catalog doc.

    Bounded exactly like skill_listing: lines are added until the budget is
    reached (always at least one, because a listing that fits nothing still has
    to name something), then a `(+N more ...)` footer says what was dropped and
    where to get it. The bound is what keeps a 60-tool server from pushing a
    bootstrap that armed yesterday over its paste budget (design section 5).
    """
    if not tools:
        return _EMPTY_LISTING
    lines: list[str] = []
    used = 0
    for i, info in enumerate(tools):
        line = _listing_line(info)
        if lines and used + len(line) + 1 > max_chars:
            dropped = len(tools) - i
            lines.append(
                f"  (+{dropped} more MCP tool(s) not listed;"
                " mcp_schema with no params lists them all)"
            )
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _full_listing(tools: Sequence[McpToolInfo]) -> str:
    """Every cached tool, unbounded - this one goes in a RESULT, not the
    bootstrap, so the engine's result caps are the only limit that applies."""
    if not tools:
        return _EMPTY_LISTING
    return "\n".join(_listing_line(info) for info in tools)


# -- errors -------------------------------------------------------------------


def _unknown_tool_hint(source: McpToolSource, tool_id: str) -> str:
    """The hint for an id no connected server exports (design section 8).

    Near misses first: the overwhelmingly likely cause is a typo or a
    half-remembered id, and naming the candidate saves a whole round trip.
    """
    ids = [info.id for info in source.tools()]
    near = difflib.get_close_matches(tool_id, ids, n=3, cutoff=0.6)
    if near:
        return f"did you mean {', '.join(near)}? call mcp_schema with no params for the full list."
    return "call mcp_schema with no params for the full list of connected MCP tools."


def _call_error_hint(source: McpToolSource, exc: McpCallError, tool_id: str) -> str:
    """One next action per error code. The message already carries the detail -
    the server's status for mcp_unavailable, the server's own text (timeouts
    already named in ms) for mcp_error - so this only says what to DO."""
    if exc.code == "unknown_tool":
        return _unknown_tool_hint(source, tool_id)
    if exc.code == "mcp_unavailable":
        return (
            "the server may still be connecting, or needs credentials/auth -"
            " check the MCP status panel and tell the user what it says."
        )
    return "check the arguments against this tool's mcp_schema before retrying."


# -- the `mcp_schema` tool ----------------------------------------------------


_MCP_SCHEMA_DOC = """\
mcp_schema(tool)
  MCP tools from the user's configured servers. With tool: <id>, returns that
  tool's description and JSON input schema; with no params, the full list.
  Answers from a cache - no server is contacted, so this is always free.
  Available:
{listing}
===CLIP:CALL id=1 tool=mcp_schema===
tool: {example}
===CLIP:END==="""


def _schema_body(source: McpToolSource, tool_id: str, max_schema_chars: int) -> str:
    info = source.schema(tool_id)
    if info is None:
        raise ToolError(
            "unknown_tool",
            f"no connected MCP server exports {tool_id!r}",
            _unknown_tool_hint(source, tool_id),
        )
    schema_json = info.input_schema_json
    if len(schema_json) > max_schema_chars:
        # Summarize rather than ship: a schema past the result cap would be cut
        # mid-JSON by the generic middle-truncator (engine/results.py), handing
        # the model syntactically broken text with no ranged re-request to
        # recover through. Names and the required list are the part a call can
        # actually be built from.
        schema_line = _schema_summary(schema_json)
    else:
        schema_line = f"input schema: {schema_json}"
    return "\n".join(
        (
            f"{info.id} = {info.server}.{info.name}",
            info.description.strip() or "(no description)",
            schema_line,
        )
    )


def _schema_summary(schema_json: str) -> str:
    """The legible stand-in for a schema too large to ship whole."""
    try:
        schema = json.loads(schema_json)
    except ValueError:
        return "input schema: (too large to include and not parseable for a summary)"
    props = schema.get("properties") if isinstance(schema, dict) else None
    names = sorted(props) if isinstance(props, dict) else []
    required = schema.get("required") if isinstance(schema, dict) else None
    required_names = [r for r in required if isinstance(r, str)] if isinstance(required, list) else []
    return "\n".join(
        (
            "input schema: too large to include whole; its top level:",
            f"  properties: {', '.join(names) if names else '(none)'}",
            f"  required: {', '.join(required_names) if required_names else '(none)'}",
        )
    )


# -- the `mcp` tool -----------------------------------------------------------


_MCP_DOC = """\
mcp(tool*, args)
  Call one of the MCP tools listed above (mcp_schema has their input
  schemas). args is a JSON object riding a heredoc - omit it entirely when
  the tool takes no arguments. The result body is the server's text output,
  tail-capped. The space before EOT is required.
===CLIP:CALL id=1 tool=mcp===
tool: {example}
args << EOT
{{"key": "value"}}
EOT
===CLIP:END==="""


def _parse_args(call: ToolCall) -> dict[str, Any]:
    """The `args` heredoc as a dict. Absent (or blank) means no arguments."""
    raw = call.params.get("args", "")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ToolError("bad_param", f"args is not valid JSON: {exc}", _ARGS_HINT) from None
    if not isinstance(parsed, dict):
        # A list/string/number parses fine and is still unusable: MCP arguments
        # are a name->value mapping, and calling with anything else would fail
        # inside the server with a far less legible message.
        raise ToolError(
            "bad_param",
            f"args must be a JSON object, got {type(parsed).__name__}",
            _ARGS_HINT,
        )
    return parsed


def _call_body(source: McpToolSource, ctx: ToolContext, call: ToolCall) -> str:
    (tool_id,) = require(call, "tool")
    tool_id = tool_id.strip()
    args = _parse_args(call)
    try:
        text = source.call(tool_id, args)
    except McpCallError as exc:
        # The manager already classified this into section 8's codes; the tool
        # layer's whole job is the hint line that follows.
        raise ToolError(exc.code, exc.message, _call_error_hint(source, exc, tool_id)) from None
    if not text.strip():
        return "(the tool returned no text content)"
    # Exactly run_command's cap: same policy, same marker, one implementation.
    max_chars = min(ctx.caps.command_tail_chars, ctx.limits.max_command_output_chars)
    return _tail_cap(text, ctx.caps.command_tail_lines, max_chars)


def _preview_body(source: McpToolSource, call: ToolCall) -> str:
    """One line for the approval drawer: `server.toolname` plus compact args.

    The user is deciding on WHAT is being invoked and WITH WHAT, so the raw
    composite id gives way to the server-qualified name whenever the cache
    knows it (an id it does not know is shown verbatim - the gate can be asked
    to preview a call the handler would reject).
    """
    tool_id = call.params.get("tool", "").strip()
    if not tool_id:
        return "(missing tool parameter)"
    info = source.schema(tool_id)
    label = f"{info.server}.{info.name}" if info is not None else tool_id
    args = " ".join(call.params.get("args", "").split())
    if not args:
        return label
    if len(args) > _ARGS_PREVIEW_CHARS:
        args = args[: _ARGS_PREVIEW_CHARS - 1].rstrip() + "…"
    return f"{label} {args}"


# -- assembly -----------------------------------------------------------------


def make_mcp_specs(source: McpToolSource, *, max_listing_chars: int) -> tuple[ToolSpec, ToolSpec]:
    """Build `(mcp_schema, mcp)` over one MCP runtime - catalog order too.

    The reader comes first so the model meets the listing (and the instruction
    to look a schema up) before the tool that spends an approval. Both docs are
    rendered from the tools cached at build time, which is also what the paste
    budget was measured against (docs/design/mcp.md section 5); the handlers
    themselves always read the live cache.
    """
    snapshot = source.tools()
    # A placeholder id when nothing is connected yet: the worked example still
    # has to show the shape, and a session can arm before a slow server answers.
    example = snapshot[0].id if snapshot else "server_tool"
    schema_doc = _MCP_SCHEMA_DOC.format(
        listing=mcp_listing(snapshot, max_chars=max_listing_chars), example=example
    )
    call_doc = _MCP_DOC.format(example=example)

    @tool_handler
    def mcp_schema_handler(ctx: ToolContext, call: ToolCall) -> str:
        tool_id = call.params.get("tool", "").strip()
        if not tool_id:
            return _full_listing(source.tools())
        # Room for the three-line body's framing under the engine's result cap:
        # the schema gets what the cap leaves after the id/description lines.
        return _schema_body(
            source, tool_id, max(1_000, ctx.limits.max_result_chars - 500)
        )

    @tool_handler
    def mcp_handler(ctx: ToolContext, call: ToolCall) -> str:
        return _call_body(source, ctx, call)

    def mcp_preview(ctx: ToolContext, call: ToolCall) -> str:
        return _preview_body(source, call)

    # "auto" is safe here and nowhere else in this module: mcp_schema serves
    # ONLY from the connect-time cache - no server round-trip, ever - so it has
    # no side effects to approve and stays usable under plan mode (design §4).
    schema_spec = ToolSpec("mcp_schema", "auto", mcp_schema_handler, None, schema_doc)
    call_spec = ToolSpec("mcp", "command", mcp_handler, mcp_preview, call_doc)
    return schema_spec, call_spec
