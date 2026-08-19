"""mcp_schema / mcp: cache reads, arg parsing, error mapping, preview, listing.

The MCP runtime is stubbed (`StubSource`), never imported: `mcp_tools` talks to
a structural McpToolSource precisely so this file can run on an install without
the optional SDK (docs/design/mcp.md section 2). Nothing here imports `mcp`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import BudgetCaps, Config, LimitsConfig, caps_for_budget
from agentclip.executor.mcp.client import McpCallError, McpErrorCode
from agentclip.executor.mcp.types import McpToolInfo
from agentclip.executor.tools.mcp_tools import make_mcp_specs, mcp_listing
from agentclip.executor.tools.registry import ToolContext, ToolSpec
from agentclip.executor.tools.sandbox import Workspace
from agentclip.protocol.types import ToolCall


def info(
    server: str, name: str, description: str = "", schema: dict[str, Any] | None = None
) -> McpToolInfo:
    return McpToolInfo(
        id=f"{server}_{name}",
        server=server,
        name=name,
        description=description,
        input_schema_json=json.dumps(schema or {"type": "object"}, separators=(",", ":")),
    )


GITHUB_CREATE = info(
    "github",
    "create_issue",
    "Open a new issue on a repository.",
    {"type": "object", "properties": {"title": {"type": "string"}}},
)
GITHUB_LIST = info("github", "list_issues", "List the open issues.")
WEATHER = info("weather", "forecast", "Tomorrow's forecast for a city.")


class StubSource:
    """A stand-in McpToolSource: records calls, answers from a fixed cache."""

    def __init__(
        self,
        tools: tuple[McpToolInfo, ...] = (),
        *,
        text: str = "ok",
        error: McpCallError | None = None,
    ) -> None:
        self._tools = tools
        self._text = text
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def tools(self) -> tuple[McpToolInfo, ...]:
        return self._tools

    def schema(self, tool_id: str) -> McpToolInfo | None:
        return next((i for i in self._tools if i.id == tool_id), None)

    def call(self, tool_id: str, args: dict[str, Any]) -> str:
        self.calls.append((tool_id, args))
        if self._error is not None:
            raise self._error
        return self._text


def make_ctx(
    root: Path, *, limits: LimitsConfig | None = None, caps: BudgetCaps | None = None
) -> ToolContext:
    return ToolContext(
        workspace=Workspace(root, Config().excluded_names()),
        limits=limits or LimitsConfig(),
        caps=caps or caps_for_budget(12_000),
    )


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return make_ctx(tmp_path)


def specs(source: StubSource, *, max_listing_chars: int = 500) -> tuple[ToolSpec, ToolSpec]:
    return make_mcp_specs(source, max_listing_chars=max_listing_chars)


def schema_call(**params: str) -> ToolCall:
    return ToolCall(id=1, tool="mcp_schema", params=dict(params), raw="")


def mcp_call(**params: str) -> ToolCall:
    return ToolCall(id=1, tool="mcp", params=dict(params), raw="")


ALL = (GITHUB_CREATE, GITHUB_LIST, WEATHER)


# -- mcp_schema ---------------------------------------------------------------


def test_schema_by_id_returns_description_and_schema(ctx: ToolContext) -> None:
    schema_spec, _ = specs(StubSource(ALL))
    res = schema_spec.handler(ctx, schema_call(tool="github_create_issue"))
    assert res.status == "ok"
    assert res.body.splitlines()[0] == "github_create_issue = github.create_issue"
    assert "Open a new issue on a repository." in res.body
    assert GITHUB_CREATE.input_schema_json in res.body


def test_schema_with_no_params_lists_every_cached_tool(ctx: ToolContext) -> None:
    schema_spec, _ = specs(StubSource(ALL))
    res = schema_spec.handler(ctx, schema_call())
    assert res.status == "ok"
    for tool in ALL:
        assert f"  - {tool.id}:" in res.body
    # unbounded here: a result, not the bootstrap - no `+N more` footer
    assert "more MCP tool(s) not listed" not in res.body


def test_schema_no_params_with_nothing_connected(ctx: ToolContext) -> None:
    schema_spec, _ = specs(StubSource(()))
    res = schema_spec.handler(ctx, schema_call())
    assert res.status == "ok"
    assert "none connected yet" in res.body


def test_schema_unknown_id_names_the_near_miss(ctx: ToolContext) -> None:
    schema_spec, _ = specs(StubSource(ALL))
    res = schema_spec.handler(ctx, schema_call(tool="github_creat_issue"))
    assert res.status == "error" and res.code == "unknown_tool"
    assert "github_creat_issue" in res.body
    hint = res.body.splitlines()[-1]
    assert hint.startswith("hint: ")
    assert "github_create_issue" in hint


def test_schema_unknown_id_with_no_near_miss_points_at_the_full_list(ctx: ToolContext) -> None:
    schema_spec, _ = specs(StubSource(ALL))
    res = schema_spec.handler(ctx, schema_call(tool="zzzz"))
    assert res.status == "error" and res.code == "unknown_tool"
    assert res.body.splitlines()[-1] == (
        "hint: call mcp_schema with no params for the full list of connected MCP tools."
    )


# -- mcp: the happy path -------------------------------------------------------


def test_call_returns_the_servers_text(ctx: ToolContext) -> None:
    source = StubSource(ALL, text="issue #42 created")
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call(tool="github_create_issue"))
    assert res.status == "ok"
    assert res.body == "issue #42 created"


def test_args_json_is_parsed_and_passed_through(ctx: ToolContext) -> None:
    source = StubSource(ALL)
    _, call_spec = specs(source)
    res = call_spec.handler(
        ctx, mcp_call(tool="github_create_issue", args='{"title": "bug", "labels": ["a", "b"]}')
    )
    assert res.status == "ok"
    assert source.calls == [
        ("github_create_issue", {"title": "bug", "labels": ["a", "b"]}),
    ]


def test_absent_args_means_an_empty_object(ctx: ToolContext) -> None:
    source = StubSource(ALL)
    _, call_spec = specs(source)
    call_spec.handler(ctx, mcp_call(tool="github_list_issues"))
    assert source.calls == [("github_list_issues", {})]


def test_blank_args_heredoc_also_means_an_empty_object(ctx: ToolContext) -> None:
    source = StubSource(ALL)
    _, call_spec = specs(source)
    call_spec.handler(ctx, mcp_call(tool="github_list_issues", args="\n  \n"))
    assert source.calls == [("github_list_issues", {})]


def test_a_result_with_no_text_says_so(ctx: ToolContext) -> None:
    _, call_spec = specs(StubSource(ALL, text=""))
    res = call_spec.handler(ctx, mcp_call(tool="github_list_issues"))
    assert res.status == "ok"
    assert res.body == "(the tool returned no text content)"


def test_missing_tool_param(ctx: ToolContext) -> None:
    source = StubSource(ALL)
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call())
    assert res.status == "error" and res.code == "missing_param"
    assert "tool" in res.body
    assert source.calls == []  # nothing was invoked


def test_long_result_text_is_tail_capped_like_a_command(tmp_path: Path) -> None:
    caps = BudgetCaps(
        600, 100, command_tail_lines=5, command_tail_chars=400,
        listing_max_entries=400, advised_max_calls=8,
    )
    text = "\n".join(f"line{i}" for i in range(50))
    _, call_spec = specs(StubSource(ALL, text=text))
    res = call_spec.handler(make_ctx(tmp_path, caps=caps), mcp_call(tool="github_list_issues"))
    assert res.status == "ok"
    assert "[truncated: showing last 5 of 50 output lines]" in res.body
    assert "line49" in res.body  # the tail survives
    assert "line0\n" not in res.body  # the head is gone


def test_the_char_cap_from_limits_applies_too(tmp_path: Path) -> None:
    text = "\n".join(f"line{i}" for i in range(100))
    _, call_spec = specs(StubSource(ALL, text=text))
    ctx = make_ctx(tmp_path, limits=LimitsConfig(max_command_output_chars=200))
    res = call_spec.handler(ctx, mcp_call(tool="github_list_issues"))
    assert "[truncated:" in res.body
    assert "line99" in res.body
    assert len(res.body) < 600


# -- mcp: bad args -------------------------------------------------------------


def test_unparseable_args_show_the_heredoc_form(ctx: ToolContext) -> None:
    source = StubSource(ALL)
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call(tool="github_create_issue", args="{title: bug"))
    assert res.status == "error" and res.code == "bad_param"
    hint = res.body.splitlines()[-1]
    assert "args << EOT" in hint
    assert '{"key": "value"}' in hint
    assert source.calls == []


def test_non_object_json_args_are_rejected(ctx: ToolContext) -> None:
    source = StubSource(ALL)
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call(tool="github_create_issue", args="[1,2]"))
    assert res.status == "error" and res.code == "bad_param"
    assert "must be a JSON object" in res.body
    assert "list" in res.body
    assert source.calls == []


def test_a_bare_json_string_is_rejected_too(ctx: ToolContext) -> None:
    _, call_spec = specs(StubSource(ALL))
    res = call_spec.handler(ctx, mcp_call(tool="github_create_issue", args='"title"'))
    assert res.status == "error" and res.code == "bad_param"


# -- mcp: error mapping (docs/design/mcp.md section 8) -------------------------


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("mcp_unavailable", "MCP server 'github' is connecting, not connected"),
        ("mcp_error", "tool call timed out after 30000 ms"),
        ("unknown_tool", "no connected MCP server exports 'github_creat_issue'"),
    ],
)
def test_call_errors_keep_their_code_and_message(
    ctx: ToolContext, code: McpErrorCode, message: str
) -> None:
    source = StubSource(ALL, error=McpCallError(code, message))
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call(tool="github_create_issue"))
    assert res.status == "error" and res.code == code
    assert message in res.body
    assert res.body.splitlines()[-1].startswith("hint: ")


def test_unavailable_points_at_the_status_panel(ctx: ToolContext) -> None:
    source = StubSource(ALL, error=McpCallError("mcp_unavailable", "'github' is failed"))
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call(tool="github_create_issue"))
    hint = res.body.splitlines()[-1]
    assert "MCP status panel" in hint
    assert "connecting" in hint or "credentials" in hint


def test_mcp_error_points_at_the_schema(ctx: ToolContext) -> None:
    source = StubSource(ALL, error=McpCallError("mcp_error", "title is required"))
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call(tool="github_create_issue"))
    assert "title is required" in res.body
    assert "mcp_schema" in res.body.splitlines()[-1]


def test_unknown_tool_from_the_call_names_the_near_miss(ctx: ToolContext) -> None:
    source = StubSource(ALL, error=McpCallError("unknown_tool", "no such tool"))
    _, call_spec = specs(source)
    res = call_spec.handler(ctx, mcp_call(tool="github_creat_issue"))
    assert res.status == "error" and res.code == "unknown_tool"
    assert "github_create_issue" in res.body.splitlines()[-1]


# -- preview -------------------------------------------------------------------


def test_preview_uses_the_server_qualified_name(ctx: ToolContext) -> None:
    _, call_spec = specs(StubSource(ALL))
    assert call_spec.preview is not None
    line = call_spec.preview(ctx, mcp_call(tool="github_create_issue", args='{"title": "bug"}'))
    assert line == 'github.create_issue {"title": "bug"}'


def test_preview_falls_back_to_the_raw_id_when_unknown(ctx: ToolContext) -> None:
    _, call_spec = specs(StubSource(ALL))
    assert call_spec.preview is not None
    assert call_spec.preview(ctx, mcp_call(tool="mystery_tool")) == "mystery_tool"


def test_preview_flattens_and_truncates_long_args(ctx: ToolContext) -> None:
    _, call_spec = specs(StubSource(ALL))
    assert call_spec.preview is not None
    args = '{\n  "body": "' + "w" * 500 + '"\n}'
    line = call_spec.preview(ctx, mcp_call(tool="github_create_issue", args=args))
    assert line.startswith("github.create_issue ")
    assert line.endswith("…")
    assert len(line) == len("github.create_issue ") + 120
    assert "\n" not in line


def test_preview_without_args_is_just_the_name(ctx: ToolContext) -> None:
    _, call_spec = specs(StubSource(ALL))
    assert call_spec.preview is not None
    assert call_spec.preview(ctx, mcp_call(tool="github_list_issues")) == "github.list_issues"


def test_preview_without_a_tool_param(ctx: ToolContext) -> None:
    """The gate can be asked to preview a call the handler would reject."""
    _, call_spec = specs(StubSource(ALL))
    assert call_spec.preview is not None
    assert call_spec.preview(ctx, mcp_call()) == "(missing tool parameter)"


# -- the listing ---------------------------------------------------------------


def test_listing_lists_every_tool_when_the_budget_allows() -> None:
    text = mcp_listing(ALL, max_chars=1_000)
    assert text.splitlines() == [
        "  - github_create_issue: Open a new issue on a repository.",
        "  - github_list_issues: List the open issues.",
        "  - weather_forecast: Tomorrow's forecast for a city.",
    ]


def test_listing_is_bounded_and_counts_what_it_dropped() -> None:
    text = mcp_listing(ALL, max_chars=60)
    lines = text.splitlines()
    assert lines[0] == "  - github_create_issue: Open a new issue on a repository."
    assert lines[-1] == (
        "  (+2 more MCP tool(s) not listed; mcp_schema with no params lists them all)"
    )


def test_listing_always_keeps_at_least_one_line() -> None:
    lines = mcp_listing(ALL, max_chars=1).splitlines()
    assert lines[0] == "  - github_create_issue: Open a new issue on a repository."
    assert "+2 more" in lines[-1]


def test_listing_clips_a_long_description_to_one_line() -> None:
    chatty = info("srv", "tool", "first line\n\nsecond   line " + "w" * 400)
    line = mcp_listing((chatty,), max_chars=10_000)
    assert "\n" not in line
    assert line.startswith("  - srv_tool: first line second   line"[:24])
    assert line.endswith("…")
    assert len(line) == len("  - srv_tool: ") + 200


def test_listing_with_no_tools() -> None:
    assert mcp_listing((), max_chars=500) == (
        "  (none connected yet - mcp_schema with no params returns the live list)"
    )


# -- the specs themselves ------------------------------------------------------


def test_spec_order_names_and_approval_kinds() -> None:
    schema_spec, call_spec = specs(StubSource(ALL))
    assert (schema_spec.name, schema_spec.approval_kind) == ("mcp_schema", "auto")
    assert (call_spec.name, call_spec.approval_kind) == ("mcp", "command")
    assert schema_spec.preview is None  # cache-only: nothing to approve
    assert call_spec.preview is not None


def test_the_schema_doc_carries_the_bounded_listing() -> None:
    schema_spec, _ = specs(StubSource(ALL), max_listing_chars=60)
    doc = schema_spec.catalog_doc
    assert "  - github_create_issue: Open a new issue on a repository." in doc
    assert "+2 more MCP tool(s) not listed" in doc
    assert "===CLIP:CALL id=1 tool=mcp_schema===" in doc
    assert "tool: github_create_issue" in doc
    assert doc.endswith("===CLIP:END===")


def test_the_call_doc_shows_the_args_heredoc() -> None:
    _, call_spec = specs(StubSource(ALL))
    doc = call_spec.catalog_doc
    assert "===CLIP:CALL id=1 tool=mcp===" in doc
    assert "tool: github_create_issue" in doc
    assert "args << EOT" in doc
    assert '{"key": "value"}' in doc
    assert doc.splitlines()[-2] == "EOT"
    assert doc.endswith("===CLIP:END===")


def test_the_docs_use_a_placeholder_id_when_nothing_is_connected() -> None:
    schema_spec, call_spec = specs(StubSource(()))
    assert "tool: server_tool" in schema_spec.catalog_doc
    assert "tool: server_tool" in call_spec.catalog_doc
    assert "none connected yet" in schema_spec.catalog_doc


def test_both_docs_stay_small_enough_for_the_paste_budget() -> None:
    """Budget prose is the scarcest resource here (docs/design/mcp.md section 5;
    the smallest bootstraps run ~67 chars under budget) - a doc that grew a
    paragraph would break sessions that armed yesterday."""
    schema_spec, call_spec = specs(StubSource(ALL), max_listing_chars=200)
    assert len(schema_spec.catalog_doc) + len(call_spec.catalog_doc) < 1_200
