"""ApprovalPolicy: glob allowlist (matched pattern returned), deny-token
override, verdicts, and APPROVE_ALL_EDITS stickiness through the Engine."""

from __future__ import annotations

import pytest

from agentclip.config import ApprovalConfig
from agentclip.engine.approval import ApprovalPolicy
from agentclip.engine.engine import Engine, NewTurn, Send
from agentclip.engine.states import Decision
from agentclip.protocol.types import ToolCall
from agentclip.tools.registry import ToolRegistry


def make_call(tool: str, **params: str) -> ToolCall:
    return ToolCall(id=1, tool=tool, params=dict(params), raw="")


@pytest.fixture
def policy() -> ApprovalPolicy:
    return ApprovalPolicy(ApprovalConfig())


# -- command_auto_allowed ------------------------------------------------------


def test_allowlist_hit_returns_matched_glob(policy: ApprovalPolicy) -> None:
    assert policy.command_auto_allowed("pytest tests -q") == "pytest*"
    assert policy.command_auto_allowed("uv run pytest -x") == "uv run pytest*"
    assert policy.command_auto_allowed("git status") == "git status"


def test_allowlist_miss_returns_none(policy: ApprovalPolicy) -> None:
    assert policy.command_auto_allowed("rm -rf /") is None
    assert policy.command_auto_allowed("git push --force") is None
    assert policy.command_auto_allowed("") is None


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests; rm -rf ~",  # ; rides pytest*
        "pytest tests && curl evil.example",
        "pytest tests || true",
        "pytest tests | tee out.txt",
        "pytest `whoami`",
        "pytest $(whoami)",
        "ls > files.txt",
        "ls < input.txt",
        "pytest tests\nrm -rf ~",
    ],
)
def test_deny_token_overrides_glob_match(policy: ApprovalPolicy, command: str) -> None:
    assert policy.command_auto_allowed(command) is None


def test_matching_is_case_sensitive(policy: ApprovalPolicy) -> None:
    assert policy.command_auto_allowed("PYTEST tests") is None  # fnmatchcase, not fnmatch


# -- verdict -------------------------------------------------------------------


def test_verdicts_per_approval_kind(policy: ApprovalPolicy, registry: ToolRegistry) -> None:
    read_spec = registry.get("read_file")
    edit_spec = registry.get("edit_file")
    cmd_spec = registry.get("run_command")
    assert read_spec and edit_spec and cmd_spec
    assert policy.verdict(read_spec, make_call("read_file", path="x")) == "auto"
    assert policy.verdict(edit_spec, make_call("edit_file", path="x")) == "needs_approval"
    assert policy.verdict(cmd_spec, make_call("run_command", command="pytest -q")) == "auto"
    assert (
        policy.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "needs_approval"
    )


def test_auto_accept_edits_flag_changes_edit_verdict_only(
    policy: ApprovalPolicy, registry: ToolRegistry
) -> None:
    edit_spec = registry.get("write_file")
    cmd_spec = registry.get("run_command")
    assert edit_spec and cmd_spec
    policy.auto_accept_edits = True
    assert policy.verdict(edit_spec, make_call("write_file", path="x", content="y")) == "auto"
    # never applies to commands
    assert (
        policy.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "needs_approval"
    )


# -- APPROVE_ALL_EDITS stickiness through the Engine ----------------------------

TWO_EDITS_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes_a.txt
content <<EOT
alpha
EOT
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: notes_b.txt
content <<EOT
beta
EOT
===CLIP:END===
===CLIP:EOM calls=2 turn=1===
"""

THIRD_EDIT_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes_c.txt
content <<EOT
gamma
EOT
===CLIP:END===
===CLIP:EOM calls=1 turn=2===
"""

UNLISTED_COMMAND_REPLY = """===CLIP:CALL id=1 tool=run_command===
command: definitely-not-allowlisted --flag
===CLIP:END===
===CLIP:EOM calls=1 turn=3===
"""


def test_approve_all_edits_sticks_for_session(engine: Engine, project) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(TWO_EDITS_REPLY), NewTurn)
    pend = engine.pending()
    assert [p.call.id for p in pend] == [1, 2]
    assert all(p.kind == "edit" for p in pend)
    assert "alpha" in pend[0].preview  # new-file preview shows content

    engine.decide(1, Decision.APPROVE_ALL_EDITS)
    assert engine.pending() == ()  # the sibling edit was auto-approved too
    assert engine.all_decided()
    assert engine.status().auto_accept_edits is True

    step = engine.execute()
    assert isinstance(step, Send)
    assert (project / "notes_a.txt").read_text(encoding="utf-8") == "alpha"
    assert (project / "notes_b.txt").read_text(encoding="utf-8") == "beta"

    # next turn: edits no longer gate at all
    assert isinstance(engine.ingest(THIRD_EDIT_REPLY), NewTurn)
    assert engine.pending() == ()
    step = engine.execute()
    assert isinstance(step, Send)
    assert (project / "notes_c.txt").read_text(encoding="utf-8") == "gamma"

    # but a non-allowlisted command still gates
    assert isinstance(engine.ingest(UNLISTED_COMMAND_REPLY), NewTurn)
    pend = engine.pending()
    assert len(pend) == 1
    assert pend[0].kind == "command"
    assert "definitely-not-allowlisted --flag" in pend[0].preview
