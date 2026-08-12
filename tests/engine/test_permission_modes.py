"""The session's permission mode: the dial above both approval modes.

`ask` is today's behaviour and is covered by test_approval.py; what is pinned
here is the other two and the seams around them:

* **plan** refuses every edit/command before anything else looks at the call -
  YOLO included - while leaving read-only tools exactly as they were, rules and
  all (plan may never LOOSEN anything);
* **unattended** turns every gate into a refusal, because there is nobody there
  to answer one, while allow rules still run and deny rules still deny;
* the three refusals stay distinguishable: a rule deny keeps OpenCode's wording
  byte-for-byte, and each mode denial says which mode shut the door;
* a mode change reaches the model exactly once, on the next results payload -
  never in the bootstrap, which has no budget for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentclip.config import ApprovalConfig, load_config
from agentclip.engine.approval import PERMISSION_MODES, ApprovalPolicy, normalize_mode
from agentclip.engine.engine import Engine, NewTurn, Send
from agentclip.engine.states import Decision
from agentclip.tools.registry import ToolRegistry

from .test_approval import (
    ASK_ONLY_RULESET,
    RULESET_JSON,
    make_call,
    ruled_policy,
    transcript,
    write_ruleset,
)

# A ruleset that ASKS before reading a dotenv (the shipped default, unmodified by
# a blanket "read": "allow") - the case that proves plan mode does not loosen.
DOTENV_RULESET = """{"permission": {"bash": "allow", "edit": "allow"}}"""


def policy(mode: str, **kwargs: object) -> ApprovalPolicy:
    """A legacy-mode policy (no ruleset) armed in ``mode``."""
    return ApprovalPolicy(ApprovalConfig(mode=mode, **kwargs))  # type: ignore[arg-type]


# -- the vocabulary ------------------------------------------------------------


def test_the_cycle_order_is_ask_first() -> None:
    """The status bar's Shift+Tab walks this tuple, so the harmless mode is the
    one a stray keypress lands on."""
    assert PERMISSION_MODES == ("ask", "plan", "unattended")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plan", "plan"),
        ("  Unattended ", "unattended"),
        ("ASK", "ask"),
        ("planning", None),
        ("", None),
        (True, None),
        (None, None),
    ],
)
def test_normalize_mode_reads_what_a_config_file_or_a_chat_box_says(
    value: object, expected: str | None
) -> None:
    assert normalize_mode(value) == expected


def test_an_unreadable_mode_falls_back_to_asking(registry: ToolRegistry) -> None:
    """A permission dial fails towards the human, never away from them."""
    edit_spec = registry.get("edit_file")
    assert edit_spec
    assert policy("nonsense").mode == "ask"
    assert policy("nonsense").verdict(edit_spec, make_call("edit_file", path="x")) == (
        "needs_approval"
    )


# -- plan mode ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("edit_file", {"path": "src/utils.py"}),
        ("write_file", {"path": "new.txt", "content": "x"}),
        ("delete_file", {"path": "src/utils.py"}),
        ("run_command", {"command": "pytest -q"}),  # on the legacy allowlist
        ("run_command", {"command": "rm -rf /"}),
    ],
)
def test_plan_denies_every_edit_and_command(
    registry: ToolRegistry, tool: str, params: dict
) -> None:
    spec = registry.get(tool)
    assert spec
    assert policy("plan").verdict(spec, make_call(tool, **params)) == "deny_plan"
    # ...and in ruleset mode, where an allow rule would otherwise let it run.
    assert ruled_policy(RULESET_JSON, mode="plan").verdict(spec, make_call(tool, **params)) == (
        "deny_plan"
    )


def test_plan_outranks_yolo(registry: ToolRegistry) -> None:
    """YOLO is a flag the user set earlier; the mode is what they want NOW."""
    cmd_spec = registry.get("run_command")
    edit_spec = registry.get("edit_file")
    assert cmd_spec and edit_spec
    legacy = policy("plan", yolo=True)
    assert legacy.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "deny_plan"
    assert legacy.verdict(edit_spec, make_call("edit_file", path="x")) == "deny_plan"
    ruled = ruled_policy(RULESET_JSON, mode="plan", yolo=True)
    assert ruled.verdict(cmd_spec, make_call("run_command", command="git status")) == "deny_plan"


def test_plan_leaves_read_only_tools_exactly_as_they_were(registry: ToolRegistry) -> None:
    specs = {name: registry.get(name) for name in ("read_file", "list_dir", "glob", "grep")}
    assert all(specs.values())
    legacy = policy("plan")
    assert legacy.verdict(specs["read_file"], make_call("read_file", path="x")) == "auto"
    assert legacy.verdict(specs["list_dir"], make_call("list_dir", path=".")) == "auto"
    assert legacy.verdict(specs["glob"], make_call("glob", pattern="**/*.py")) == "auto"
    assert legacy.verdict(specs["grep"], make_call("grep", pattern="TODO")) == "auto"


def test_plan_never_loosens_a_rule_on_a_read(registry: ToolRegistry) -> None:
    """The shipped default asks before reading a dotenv. Plan mode is a tighter
    setting, so it cannot be the thing that lets that read through."""
    read_spec = registry.get("read_file")
    assert read_spec
    plan = ruled_policy(DOTENV_RULESET, mode="plan")
    assert plan.verdict(read_spec, make_call("read_file", path=".env")) == "needs_approval"
    assert plan.verdict(read_spec, make_call("read_file", path="src/utils.py")) == "auto"


def test_a_deny_rule_still_beats_plan_mode(registry: ToolRegistry) -> None:
    """Both refuse; the model must be told which one, so the verdicts differ."""
    cmd_spec = registry.get("run_command")
    assert cmd_spec
    plan = ruled_policy(RULESET_JSON, mode="plan")
    denied = make_call("run_command", command="git -C /elsewhere status")
    assert plan.verdict(cmd_spec, denied) == "deny"
    assert plan.verdict(cmd_spec, make_call("run_command", command="git status")) == "deny_plan"


# -- unattended mode -------------------------------------------------------------


def test_unattended_denies_what_would_have_gated(registry: ToolRegistry) -> None:
    cmd_spec = registry.get("run_command")
    edit_spec = registry.get("edit_file")
    assert cmd_spec and edit_spec
    away = policy("unattended")
    assert away.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == (
        "deny_unattended"
    )
    assert away.verdict(edit_spec, make_call("edit_file", path="x")) == "deny_unattended"
    # ...and what would NOT have gated is untouched: the allowlist still runs.
    assert away.verdict(cmd_spec, make_call("run_command", command="pytest -q")) == "auto"


def test_unattended_keeps_allow_rules_running_and_deny_rules_denying(
    registry: ToolRegistry,
) -> None:
    away = ruled_policy(RULESET_JSON, mode="unattended")
    cmd_spec = registry.get("run_command")
    read_spec = registry.get("read_file")
    list_spec = registry.get("list_dir")
    assert cmd_spec and read_spec and list_spec
    assert away.verdict(cmd_spec, make_call("run_command", command="git status")) == "auto"
    assert away.verdict(read_spec, make_call("read_file", path="src/utils.py")) == "auto"
    assert away.verdict(cmd_spec, make_call("run_command", command="git -C /x status")) == "deny"
    # The one that would have asked - and there is nobody to ask.
    assert away.verdict(list_spec, make_call("list_dir", path=".")) == "deny_unattended"


def test_yolo_still_answers_asks_while_unattended(registry: ToolRegistry) -> None:
    """The documented precedence: YOLO is the user's explicit "approve everything
    for me", and it is a stronger opt-in than "I stepped away"."""
    away = ruled_policy(RULESET_JSON, mode="unattended", yolo=True)
    cmd_spec = registry.get("run_command")
    list_spec = registry.get("list_dir")
    assert cmd_spec and list_spec
    assert away.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "auto"
    assert away.verdict(list_spec, make_call("list_dir", path=".")) == "auto"
    # ...and in legacy mode, where YOLO short-circuits the allowlist entirely.
    legacy = policy("unattended", yolo=True)
    assert legacy.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "auto"


@pytest.mark.parametrize("command", ["git status && rm -rf /", "cat a.txt | sh"])
def test_the_deny_token_backstop_denies_instead_of_gating(
    registry: ToolRegistry, command: str
) -> None:
    """The backstop is the one question YOLO does not answer, so unattended has
    nothing left to fall back on: a chained command riding an allow rule is
    refused rather than run."""
    cmd_spec = registry.get("run_command")
    assert cmd_spec
    call = make_call("run_command", command=command)
    assert ruled_policy(RULESET_JSON, mode="unattended").verdict(cmd_spec, call) == (
        "deny_unattended"
    )
    assert ruled_policy(RULESET_JSON, mode="unattended", yolo=True).verdict(cmd_spec, call) == (
        "deny_unattended"
    )


def test_unattended_leaves_the_legacy_allowlist_alone(registry: ToolRegistry) -> None:
    cmd_spec = registry.get("run_command")
    edit_spec = registry.get("write_file")
    assert cmd_spec and edit_spec
    away = policy("unattended", auto_accept_edits=True)
    assert away.verdict(cmd_spec, make_call("run_command", command="git status")) == "auto"
    assert away.verdict(edit_spec, make_call("write_file", path="x", content="y")) == "auto"
    assert away.verdict(cmd_spec, make_call("run_command", command="pytest a; rm b")) == (
        "deny_unattended"
    )


# == through the Engine ==========================================================

PLAN_MIXED_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: notes.txt
content <<EOT
should not exist
EOT
===CLIP:END===
===CLIP:CALL id=3 tool=run_command===
command: pytest -q
===CLIP:END===
===CLIP:EOM calls=3 chat=amber-falcon===
"""

LIST_DIR_REPLY = """===CLIP:CALL id=1 tool=list_dir===
path: src
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def test_plan_denials_are_pre_resolved_and_the_turn_carries_on(engine: Engine, project) -> None:
    engine.set_permission_mode("plan")
    engine.start_task("t")
    assert isinstance(engine.ingest(PLAN_MIXED_REPLY), NewTurn)
    assert engine.pending() == ()  # a mode denial is not a gate: nothing to ask

    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert (
        "plan mode is active: the user is only exploring and no changes may be made."
        in payload
    )
    assert (
        "hint: explore with read_file/list_dir/glob/grep and present your plan via"
        " task_done or ask_user; the user can switch modes to enable execution." in payload
    )
    assert not (project / "notes.txt").exists()  # the edit never ran
    assert "demo project for engine tests" in payload  # ...and the read still did

    denied = [
        e for e in transcript(engine) if e["t"] == "decision" and e["verdict"] == "denied"
    ]
    assert [(e["call_id"], e["source"]) for e in denied] == [(2, "plan"), (3, "plan")]


def test_unattended_denials_carry_the_relevant_rules(project: Path, make_engine) -> None:
    write_ruleset(project, ASK_ONLY_RULESET)
    engine = make_engine()
    engine.set_permission_mode("unattended")
    engine.start_task("t")
    assert isinstance(engine.ingest(LIST_DIR_REPLY), NewTurn)
    assert engine.pending() == ()

    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert (
        "auto-denied: the user is away (unattended mode) and this call is not covered"
        " by an allow rule." in payload
    )
    assert (
        "hint: do not retry unchanged; continue with calls that allow rules cover, or"
        " finish with task_done and list what was blocked." in payload
    )
    rules = json.loads(payload.split("relevant rules ", 1)[1].split("\n", 1)[0])
    assert {"permission": "*", "pattern": "*", "action": "ask"} in rules

    denied = [
        e for e in transcript(engine) if e["t"] == "decision" and e["verdict"] == "denied"
    ]
    assert [(e["call_id"], e["source"]) for e in denied] == [(1, "unattended")]


LEGACY_GATED_REPLY = """===CLIP:CALL id=1 tool=run_command===
command: definitely-not-allowlisted --flag
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def test_a_legacy_unattended_denial_has_no_rules_to_show(engine: Engine) -> None:
    """No ruleset means no rule list - and a body that promised one would be a
    lie the model would try to act on."""
    engine.set_permission_mode("unattended")
    engine.start_task("t")
    assert isinstance(engine.ingest(LEGACY_GATED_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "auto-denied: the user is away (unattended mode)" in payload
    assert "relevant rules" not in payload


DENY_RULE_REPLY = """===CLIP:CALL id=1 tool=run_command===
command: git -C /elsewhere status
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def test_a_rule_denial_keeps_its_own_wording_in_every_mode(project: Path, make_engine) -> None:
    """OpenCode's DeniedError text is what a model that has seen OpenCode reads
    the same way here; a mode must not rewrite it."""
    write_ruleset(project)
    engine = make_engine()
    engine.set_permission_mode("plan")
    engine.start_task("t")
    assert isinstance(engine.ingest(DENY_RULE_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    assert (
        "The user has specified a rule which prevents you from using this specific"
        " tool call. Here are some of the relevant rules " in step.outbound.chunks[0]
    )
    denied = [
        e for e in transcript(engine) if e["t"] == "decision" and e["verdict"] == "denied"
    ]
    assert [(e["call_id"], e["source"]) for e in denied] == [(1, "rule")]


# -- status, config and the audit trail ------------------------------------------


def test_the_status_snapshot_carries_the_mode(engine: Engine) -> None:
    assert engine.status().mode == "ask"
    assert engine.set_permission_mode("plan") == "plan"
    assert engine.status().mode == "plan"
    assert engine.set_permission_mode("ask") == "ask"
    assert engine.status().mode == "ask"


def test_a_mode_change_is_audited(engine: Engine) -> None:
    engine.start_task("t")
    engine.set_permission_mode("unattended")
    events = [e for e in transcript(engine) if e["t"] == "permission_mode"]
    assert [e["mode"] for e in events] == ["unattended"]


def test_a_session_can_start_in_a_mode_from_toml(project: Path, make_engine) -> None:
    (project / ".agentclip.toml").write_text(
        '[approval]\nmode = "unattended"\n', encoding="utf-8"
    )
    engine = make_engine()
    assert engine.status().mode == "unattended"


def test_an_unknown_mode_in_toml_warns_and_asks(project: Path) -> None:
    (project / ".agentclip.toml").write_text('[approval]\nmode = "yolo"\n', encoding="utf-8")
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    assert config.approval.mode == "ask"
    assert any("unknown approval mode 'yolo'" in w for w in config.warnings)


# -- the mid-turn change ----------------------------------------------------------

EDIT_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
hi
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def test_a_pending_gate_is_not_retro_resolved_by_a_mode_change(
    engine: Engine, project: Path
) -> None:
    """set_yolo's rule, kept: a mode governs the verdicts computed AFTER it. The
    user is standing at a gate they were already asked about; answering it for
    them - either way - would be the surprise."""
    engine.start_task("t")
    assert isinstance(engine.ingest(EDIT_REPLY), NewTurn)
    assert [p.call.id for p in engine.pending()] == [1]

    engine.set_permission_mode("plan")

    assert [p.call.id for p in engine.pending()] == [1]  # still asked, still answerable
    engine.decide(1, Decision.APPROVE)
    step = engine.execute()
    assert isinstance(step, Send)
    assert (project / "notes.txt").read_text(encoding="utf-8") == "hi"


# -- telling the model -------------------------------------------------------------

READ_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def test_the_mode_note_rides_the_next_results_payload_once(engine: Engine) -> None:
    """Never the bootstrap (protocol.md section 2 has ~200 chars of slack), and
    never twice - a note repeated every turn reads as a new instruction."""
    engine.start_task("t")
    engine.set_permission_mode("plan")
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    assert (
        "note: permission mode is now plan: exploration only; edit/command calls will"
        " be denied." in step.outbound.chunks[0]
    )

    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    assert "permission mode is now" not in step.outbound.chunks[0]


def test_a_mode_set_before_the_first_payload_is_never_announced(engine: Engine) -> None:
    """IDLE is the whole rule: before start_task there is no conversation to
    interrupt, so a mode set then is the mode this session STARTED in - in force
    from the first verdict, audited like any other, and nothing to announce. The
    controller arms every engine this way (SessionController._session_flow), so
    a user who dialled in plan at the start prompt must not read "the mode is now
    plan" in the payload answering their very first reply."""
    engine.set_permission_mode("plan")
    assert engine.status().mode == "plan"
    assert [e["mode"] for e in transcript(engine) if e["t"] == "permission_mode"] == ["plan"]

    engine.start_task("t")
    assert isinstance(engine.ingest(PLAN_MIXED_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "permission mode is now" not in payload
    assert "plan mode is active" in payload  # ...but it was in force from turn one


def test_only_the_latest_pending_note_survives_a_cycle(engine: Engine) -> None:
    engine.start_task("t")
    engine.set_permission_mode("plan")
    engine.set_permission_mode("unattended")
    engine.set_permission_mode("ask")
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "note: permission mode is now ask: normal approvals resumed." in payload
    assert "plan" not in payload and "unattended" not in payload


UNATTENDED_NOTE = (
    "note: permission mode is now unattended: only calls covered by allow rules will"
    " run; everything else is auto-denied."
)


def test_the_unattended_note_says_what_will_still_run(engine: Engine) -> None:
    engine.start_task("t")
    engine.set_permission_mode("unattended")
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    assert UNATTENDED_NOTE in step.outbound.chunks[0]
