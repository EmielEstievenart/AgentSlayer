"""Registry shape: all 11 tools, approval kinds, catalog rendering, meta stubs."""

from __future__ import annotations

from pathlib import Path

from agentclip.config import Config, LimitsConfig, caps_for_budget
from agentclip.executor.tools import meta
from agentclip.executor.tools.registry import (
    ToolContext,
    ToolRegistry,
    ToolSpec,
    default_registry,
    ok_result,
)
from agentclip.executor.tools.sandbox import Workspace
from agentclip.protocol.types import ToolCall

ALL_TOOLS = (
    "read_file",
    "write_file",
    "edit_file",
    "delete_file",
    "list_dir",
    "glob",
    "grep",
    "run_command",
    "fetch_chunk",
    "ask_user",
    "task_done",
)

# The one entry with no worked example: its syntax is taught by the truncation
# marker at the moment it is needed (executor/tools/chunks.py).
NO_EXAMPLE = ("fetch_chunk",)


def make_ctx(root: Path) -> ToolContext:
    return ToolContext(
        workspace=Workspace(root, Config().excluded_names()),
        limits=LimitsConfig(),
        caps=caps_for_budget(12_000),
    )


def test_default_registry_has_all_eleven_tools_in_order() -> None:
    assert default_registry().names() == ALL_TOOLS


def test_fetch_chunk_is_always_there_and_never_gated() -> None:
    """Truncation is a property of the transport, not of a preset: there is no
    configuration in which a body can be cut and the way back to it be absent."""
    for reg in (
        default_registry(),
        default_registry(edit_by_lines=True),
        default_registry(role="subagent"),
    ):
        spec = _get(reg, "fetch_chunk")
        assert spec.approval_kind == "auto"
        assert spec.preview is None


# -- the ranged-edit mode (protocol.md section 3.1) ----------------------------


def test_edit_by_lines_adds_replace_lines_behind_edit_file() -> None:
    """ADDED, not swapped: find/replace is still the better edit wherever the
    host carries code faithfully (the user's ruling, and protocol.md 3.1)."""
    names = default_registry(edit_by_lines=True).names()
    assert "edit_file" in names
    assert names.index("replace_lines") == names.index("edit_file") + 1
    assert names == ALL_TOOLS[:3] + ("replace_lines",) + ALL_TOOLS[3:]


def test_edit_by_lines_teaches_read_file_the_gutter() -> None:
    doc = _get(default_registry(edit_by_lines=True), "read_file").catalog_doc
    assert "numbered" in doc
    assert "no line-number gutter" not in doc


def test_the_catalog_is_byte_identical_with_the_toggle_off() -> None:
    """The whole point of an opt-in: a service that never turns it on must get
    exactly the bootstrap it got before the feature existed."""
    assert default_registry(edit_by_lines=False).render_catalog() == default_registry().render_catalog()
    catalog = default_registry().render_catalog()
    assert "replace_lines" not in catalog
    assert "numbered" not in catalog


def test_replace_lines_is_an_edit_with_a_preview() -> None:
    spec = _get(default_registry(edit_by_lines=True), "replace_lines")
    assert spec.approval_kind == "edit"
    assert spec.preview is not None


def _get(reg: ToolRegistry, name: str) -> ToolSpec:
    spec = reg.get(name)
    assert spec is not None, name
    return spec


def test_approval_kinds() -> None:
    reg = default_registry()
    for name in ("read_file", "list_dir", "glob", "grep", "ask_user", "task_done"):
        assert _get(reg, name).approval_kind == "auto", name
    for name in ("write_file", "edit_file", "delete_file"):
        assert _get(reg, name).approval_kind == "edit", name
    assert _get(reg, "run_command").approval_kind == "command"


def test_gated_tools_have_previews_auto_tools_do_not() -> None:
    reg = default_registry()
    for name in ALL_TOOLS:
        spec = _get(reg, name)
        if spec.approval_kind == "auto":
            assert spec.preview is None, name
        else:
            assert spec.preview is not None, name


def test_unknown_tool_returns_none() -> None:
    assert default_registry().get("rm_rf") is None


def test_render_catalog_contains_every_tool_and_examples() -> None:
    catalog = default_registry().render_catalog()
    with_examples = [name for name in ALL_TOOLS if name not in NO_EXAMPLE]
    for name in with_examples:
        assert f"tool={name}" in catalog, name  # each entry has a worked example
    for name in ALL_TOOLS:
        assert name in catalog
    assert catalog.count("===CLIP:CALL") == len(with_examples)
    assert catalog.count("===CLIP:END===") == len(with_examples)
    # bootstrap section-4 size target: ~4,200 chars
    assert 2_500 <= len(catalog) <= 6_000, len(catalog)


def test_mcp_specs_slot_between_run_command_and_the_meta_tools() -> None:
    """`mcp_specs` land after run_command (and skill, when present) and before
    delegate/ask_user/task_done - capabilities together, hand-off tools last."""

    def spec(name: str) -> ToolSpec:
        return ToolSpec(name, "auto", lambda ctx, call: ok_result(call, ""), None, f"{name}()")

    reg = default_registry(mcp_specs=(spec("mcp_schema"), spec("mcp")))
    assert reg.names() == (*ALL_TOOLS[:9], "mcp_schema", "mcp", "ask_user", "task_done")


def test_meta_handlers_are_inert_stubs(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    call = ToolCall(id=7, tool="ask_user", params={"question": "hm?"}, raw="")
    res = meta.ask_user(ctx, call)
    assert (res.call_id, res.status, res.body, res.tool) == (7, "ok", "", "ask_user")

    call = ToolCall(id=8, tool="task_done", params={"summary": "done"}, raw="")
    res = meta.task_done(ctx, call)
    assert (res.call_id, res.status, res.body, res.tool) == (8, "ok", "", "task_done")
