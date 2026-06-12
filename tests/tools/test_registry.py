"""Registry shape: all 10 tools, approval kinds, catalog rendering, meta stubs."""

from __future__ import annotations

from pathlib import Path

from agentclip.config import Config, LimitsConfig, caps_for_budget
from agentclip.protocol.types import ToolCall
from agentclip.tools import meta
from agentclip.tools.registry import ToolContext, default_registry
from agentclip.tools.sandbox import Workspace

ALL_TOOLS = (
    "read_file",
    "write_file",
    "edit_file",
    "delete_file",
    "list_dir",
    "glob",
    "grep",
    "run_command",
    "ask_user",
    "task_done",
)


def make_ctx(root: Path) -> ToolContext:
    return ToolContext(
        workspace=Workspace(root, Config().excluded_names()),
        limits=LimitsConfig(),
        caps=caps_for_budget(12_000),
    )


def test_default_registry_has_all_ten_tools_in_order() -> None:
    assert default_registry().names() == ALL_TOOLS


def test_approval_kinds() -> None:
    reg = default_registry()
    for name in ("read_file", "list_dir", "glob", "grep", "ask_user", "task_done"):
        assert reg.get(name).approval_kind == "auto", name
    for name in ("write_file", "edit_file", "delete_file"):
        assert reg.get(name).approval_kind == "edit", name
    assert reg.get("run_command").approval_kind == "command"


def test_gated_tools_have_previews_auto_tools_do_not() -> None:
    reg = default_registry()
    for name in ALL_TOOLS:
        spec = reg.get(name)
        if spec.approval_kind == "auto":
            assert spec.preview is None, name
        else:
            assert spec.preview is not None, name


def test_unknown_tool_returns_none() -> None:
    assert default_registry().get("rm_rf") is None


def test_render_catalog_contains_every_tool_and_examples() -> None:
    catalog = default_registry().render_catalog()
    for name in ALL_TOOLS:
        assert f"tool={name}" in catalog, name  # each entry has a worked example
        assert name in catalog
    assert catalog.count("===CLIP:CALL") == len(ALL_TOOLS)
    assert catalog.count("===CLIP:END===") == len(ALL_TOOLS)
    # bootstrap section-4 size target: ~4,200 chars
    assert 2_500 <= len(catalog) <= 6_000, len(catalog)


def test_meta_handlers_are_inert_stubs(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    call = ToolCall(id=7, tool="ask_user", params={"question": "hm?"}, raw="")
    res = meta.ask_user(ctx, call)
    assert (res.call_id, res.status, res.body, res.tool) == (7, "ok", "", "ask_user")

    call = ToolCall(id=8, tool="task_done", params={"summary": "done"}, raw="")
    res = meta.task_done(ctx, call)
    assert (res.call_id, res.status, res.body, res.tool) == (8, "ok", "", "task_done")
