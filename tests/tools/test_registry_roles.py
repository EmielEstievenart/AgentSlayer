"""Role-aware catalog: who is offered `delegate`, who is taught `result`."""

from __future__ import annotations

from agentclip.tools.registry import default_registry


def test_master_with_delegation_enabled_gets_the_tool() -> None:
    reg = default_registry(allow_delegate=True)
    assert "delegate" in reg.names()
    spec = reg.get("delegate")
    assert spec is not None
    assert spec.approval_kind == "auto"  # the delegation itself is user-visible
    assert spec.preview is None
    # Catalog order: after run_command/skill, before the meta tools.
    names = reg.names()
    assert names.index("run_command") < names.index("delegate") < names.index("ask_user")


def test_master_without_delegation_has_no_delegate_tool() -> None:
    reg = default_registry()
    assert "delegate" not in reg.names()
    assert reg.get("delegate") is None
    assert "tool=delegate" not in reg.render_catalog()


def test_subagent_never_gets_delegate_even_when_asked() -> None:
    # Nesting is excluded by construction: the sub-agent's registry simply has
    # no such tool, so a nested attempt resolves as the usual unknown_tool.
    reg = default_registry(role="subagent", allow_delegate=True)
    assert "delegate" not in reg.names()
    assert reg.get("delegate") is None


def test_subagent_task_done_doc_teaches_the_result_param() -> None:
    doc = default_registry(role="subagent").get("task_done").catalog_doc
    assert "task_done(summary, result*)" in doc
    assert "result <<EOT" in doc
    assert "the ONLY thing they" in doc


def test_master_task_done_doc_does_not_mention_result() -> None:
    doc = default_registry().get("task_done").catalog_doc
    assert "task_done(summary)" in doc
    assert "result" not in doc


def test_delegate_catalog_entry_sets_expectations() -> None:
    catalog = default_registry(allow_delegate=True).render_catalog()
    assert "delegate(task*, context)" in catalog
    assert "===CLIP:CALL id=1 tool=delegate===" in catalog
    assert "One delegation runs at" in catalog  # single-flight is stated
    assert "It sees none of this conversation" in catalog


def test_roles_share_every_other_tool() -> None:
    master = [n for n in default_registry().names() if n != "delegate"]
    sub = list(default_registry(role="subagent").names())
    assert master == sub
