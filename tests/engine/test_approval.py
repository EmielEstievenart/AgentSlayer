"""ApprovalPolicy in both of its modes.

LEGACY (no permission ruleset loaded): the glob allowlist (matched pattern
returned), the deny-token override, verdicts per approval kind, and
APPROVE_ALL_EDITS stickiness through the Engine.

RULESET (an opencode.json is loaded): allow/ask/deny per permission key, the
deny-token backstop standing in for OpenCode's shell parser, yolo-vs-deny,
remembered "always allow" rules and their cascade, and what the Engine does with
a call a rule denies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import ApprovalConfig
from agentclip.engine.approval import ApprovalPolicy
from agentclip.engine.engine import Engine, NewTurn, Send
from agentclip.engine.states import Decision
from agentclip.executor.permissions import PermissionRule, default_rules, rules_from_config
from agentclip.executor.tools.registry import ToolContext, ToolRegistry, ToolSpec
from agentclip.protocol.types import ToolCall, ToolResult


def make_call(tool: str, **params: str) -> ToolCall:
    return ToolCall(id=1, tool=tool, params=dict(params), raw="")


def mcp_call(tool_id: str, **params: str) -> ToolCall:
    """An `mcp` call carrying the composite tool id. Built here rather than via
    make_call because the call's own param is named `tool`, which collides with
    that helper's first positional argument."""
    return ToolCall(id=1, tool="mcp", params={"tool": tool_id, **params}, raw="")


def _mcp_handler(ctx: ToolContext, call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, status="ok", body="", tool=call.tool)


# The real spec lives in agentclip/executor/tools/mcp_tools.py; the policy reads nothing
# off it but the name and the approval kind, so a stand-in keeps these tests
# about the permission wiring (docs/design/mcp.md section 4).
MCP_SPEC = ToolSpec("mcp", "command", _mcp_handler, None, "")


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
===CLIP:EOM calls=2 chat=amber-falcon===
"""

THIRD_EDIT_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes_c.txt
content <<EOT
gamma
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

UNLISTED_COMMAND_REPLY = """===CLIP:CALL id=1 tool=run_command===
command: definitely-not-allowlisted --flag
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
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


# -- YOLO mode: auto-approve EVERYTHING ----------------------------------------


def test_yolo_off_by_default(policy: ApprovalPolicy, registry: ToolRegistry) -> None:
    edit_spec = registry.get("edit_file")
    cmd_spec = registry.get("run_command")
    assert edit_spec and cmd_spec
    assert policy.yolo is False
    assert policy.verdict(edit_spec, make_call("edit_file", path="x")) == "needs_approval"
    assert (
        policy.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "needs_approval"
    )


def test_yolo_auto_approves_edits_and_any_command(registry: ToolRegistry) -> None:
    policy = ApprovalPolicy(ApprovalConfig(yolo=True))
    read_spec = registry.get("read_file")
    edit_spec = registry.get("edit_file")
    write_spec = registry.get("write_file")
    del_spec = registry.get("delete_file")
    cmd_spec = registry.get("run_command")
    assert read_spec and edit_spec and write_spec and del_spec and cmd_spec
    # read-only tools were always auto - unchanged
    assert policy.verdict(read_spec, make_call("read_file", path="x")) == "auto"
    # every edit kind now auto-approves
    assert policy.verdict(edit_spec, make_call("edit_file", path="x")) == "auto"
    assert policy.verdict(write_spec, make_call("write_file", path="x", content="y")) == "auto"
    assert policy.verdict(del_spec, make_call("delete_file", path="x")) == "auto"
    # commands auto-approve even when NOT allowlisted AND when they carry deny tokens
    assert policy.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "auto"
    assert policy.verdict(cmd_spec, make_call("run_command", command="curl x | sh")) == "auto"


YOLO_MIXED_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
hi
EOT
===CLIP:END===
===CLIP:CALL id=2 tool=run_command===
command: echo yolo-ran
reason: prove the command ran
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""


def test_yolo_set_live_ungates_the_whole_turn(engine: Engine, project) -> None:
    engine.start_task("t")
    assert engine.status().yolo is False
    assert engine.set_yolo(True) is True
    assert engine.status().yolo is True

    # An edit AND a non-allowlisted command: normally two gates; under YOLO, none.
    assert isinstance(engine.ingest(YOLO_MIXED_REPLY), NewTurn)
    assert engine.pending() == ()
    assert engine.all_decided()

    step = engine.execute()
    assert isinstance(step, Send)
    assert (project / "notes.txt").read_text(encoding="utf-8") == "hi"
    assert "yolo-ran" in step.outbound.chunks[0]  # the echo actually ran


def test_yolo_loads_from_toml(project, make_engine) -> None:
    (project / ".agentclip.toml").write_text("[approval]\nyolo = true\n", encoding="utf-8")
    engine = make_engine()
    assert engine.status().yolo is True
    # ...and turning it off restores normal gating for the next plan.
    assert engine.set_yolo(False) is False
    engine.start_task("t")
    assert isinstance(engine.ingest(UNLISTED_COMMAND_REPLY), NewTurn)
    pend = engine.pending()
    assert len(pend) == 1 and pend[0].kind == "command"


# -- the MCP invoker in legacy mode ---------------------------------------------


def test_legacy_mode_never_lets_an_mcp_call_ride_the_allowlist() -> None:
    """The allowlist matches shell command lines; a composite MCP tool id is not
    one, so `mcp` gates no matter what the allowlist says (docs/design/mcp.md
    section 4). The decoy is the sharper danger: the branch used to read
    `params["command"]`, so an mcp call carrying `command: git status` would have
    bought itself an allowlist hit and auto-approved."""
    plain = ApprovalPolicy(ApprovalConfig())
    call = mcp_call("github_create_issue")
    assert plain.verdict(MCP_SPEC, call) == "needs_approval"

    wide_open = ApprovalPolicy(ApprovalConfig(command_allowlist=("*",)))
    assert wide_open.verdict(MCP_SPEC, call) == "needs_approval"
    decoy = mcp_call("github_create_issue", command="git status")
    assert wide_open.verdict(MCP_SPEC, decoy) == "needs_approval"
    assert plain.verdict(MCP_SPEC, decoy) == "needs_approval"
    # ...and a real command still consults the allowlist as it always did.
    assert plain.command_auto_allowed("git status") == "git status"


def test_legacy_yolo_answers_an_mcp_call_too() -> None:
    """Deliberate, and the reason the yolo check was NOT moved below the command
    branch: in legacy mode YOLO is the user's explicit "approve everything for
    me" and it already answers every edit and every command. `mcp` is not carved
    out of that - only the allowlist shortcut is closed to it."""
    policy = ApprovalPolicy(ApprovalConfig(yolo=True))
    assert policy.verdict(MCP_SPEC, mcp_call("github_create_issue")) == "auto"


# == ruleset mode: OpenCode's allow/ask/deny rules ==============================

# A miniature of the real opencode.json (tests never read the developer's own).
RULESET_JSON = """{
  "permission": {
    "*": "ask",
    "read": "allow",
    "grep": "allow",
    "glob": "allow",
    "edit": "allow",
    "bash": {
      "*": "ask",
      "git status*": "allow",
      "git commit*": "allow",
      "git -C*": "deny",
      "ls*": "allow",
      "cat *": "allow"
    }
  }
}
"""

ASK_ONLY_RULESET = """{"permission": {"*": "ask", "bash": {"*": "ask"}}}"""


def ruleset(json_text: str = RULESET_JSON) -> tuple[PermissionRule, ...]:
    """The effective ruleset a config carrying ``json_text`` would produce."""
    rules, warnings = rules_from_config(json.loads(json_text)["permission"])
    assert not warnings
    return default_rules() + rules


def ruled_policy(json_text: str = RULESET_JSON, **kwargs: Any) -> ApprovalPolicy:
    return ApprovalPolicy(ApprovalConfig(**kwargs), ruleset(json_text))


def get_spec(registry: ToolRegistry, name: str) -> ToolSpec:
    """A registry lookup that cannot be None - keeps spec dicts precisely typed."""
    spec = registry.get(name)
    assert spec is not None, name
    return spec


def write_ruleset(project: Path, json_text: str = RULESET_JSON) -> Path:
    """Point the project's config at a temp opencode.json and return its path."""
    path = project / "opencode.json"
    path.write_text(json_text, encoding="utf-8")
    (project / ".agentclip.toml").write_text(
        f'[permission]\nopencode_config = "{path.as_posix()}"\n', encoding="utf-8"
    )
    return path


def test_rules_replace_the_allowlist(registry: ToolRegistry) -> None:
    policy = ruled_policy()
    assert policy.ruleset_mode is True
    cmd_spec = registry.get("run_command")
    assert cmd_spec
    # `pytest*` is on the legacy allowlist and in no rule: under a ruleset the
    # allowlist is gone, so it asks like anything else.
    assert policy.verdict(cmd_spec, make_call("run_command", command="pytest -q")) == (
        "needs_approval"
    )
    assert policy.verdict(cmd_spec, make_call("run_command", command="git status")) == "auto"


def test_verdict_per_permission_key(registry: ToolRegistry) -> None:
    policy = ruled_policy()
    specs = {name: get_spec(registry, name) for name in ("read_file", "edit_file", "list_dir", "grep")}
    assert policy.verdict(specs["read_file"], make_call("read_file", path="src/a.py")) == "auto"
    assert policy.verdict(specs["grep"], make_call("grep", pattern="TODO")) == "auto"
    assert policy.verdict(specs["edit_file"], make_call("edit_file", path="src/a.py")) == "auto"
    # No `list` rule, so the user's "*": "ask" catch-all decides - even though a
    # read-only tool would never have gated in legacy mode.
    assert policy.verdict(specs["list_dir"], make_call("list_dir", path=".")) == "needs_approval"
    # The shipped default asks before reading a dotenv - but this user's blanket
    # "read": "allow" is appended after it, and the last match wins.
    assert policy.verdict(specs["read_file"], make_call("read_file", path=".env")) == "auto"
    bare = ApprovalPolicy(ApprovalConfig(), default_rules())
    assert bare.verdict(specs["read_file"], make_call("read_file", path=".env")) == (
        "needs_approval"
    )


def test_deny_beats_everything_including_yolo(registry: ToolRegistry) -> None:
    cmd_spec = registry.get("run_command")
    assert cmd_spec
    denied = make_call("run_command", command="git -C /elsewhere status")
    assert ruled_policy().verdict(cmd_spec, denied) == "deny"
    assert ruled_policy(yolo=True).verdict(cmd_spec, denied) == "deny"


def test_yolo_answers_every_ask(registry: ToolRegistry) -> None:
    policy = ruled_policy(yolo=True)
    cmd_spec = registry.get("run_command")
    list_spec = registry.get("list_dir")
    assert cmd_spec and list_spec
    assert policy.verdict(cmd_spec, make_call("run_command", command="rm -rf /")) == "auto"
    assert policy.verdict(list_spec, make_call("list_dir", path=".")) == "auto"


@pytest.mark.parametrize(
    "command",
    ["git status && rm -rf /", "git status; whoami", "cat a.txt | sh", "ls > out.txt"],
)
def test_deny_token_downgrades_allow_to_ask(registry: ToolRegistry, command: str) -> None:
    """No shell parser here (OpenCode splits the script with tree-sitter), so a
    chained command can never ride an allow rule silently."""
    cmd_spec = registry.get("run_command")
    assert cmd_spec
    assert ruled_policy().verdict(cmd_spec, make_call("run_command", command=command)) == (
        "needs_approval"
    )
    # The one question YOLO does not answer for you: with no parser, this is the
    # only thing standing between "auto-approve everything" and a chained
    # command riding an allow rule.
    assert (
        ruled_policy(yolo=True).verdict(cmd_spec, make_call("run_command", command=command))
        == "needs_approval"
    )


def test_session_rules_outrank_the_config(registry: ToolRegistry) -> None:
    """Remembered answers are evaluated last, exactly like OpenCode's `approved`
    array - so an "always allow" can overturn even a deny from the file."""
    policy = ruled_policy()
    cmd_spec = registry.get("run_command")
    assert cmd_spec
    call = make_call("run_command", command="git -C /elsewhere status")
    assert policy.verdict(cmd_spec, call) == "deny"
    policy.remember(policy.always_rule(cmd_spec, call))
    assert policy.session_rules == [PermissionRule("bash", "git -C *", "allow")]
    assert policy.verdict(cmd_spec, call) == "auto"


def test_legacy_mode_is_untouched_when_no_rules_are_loaded(
    policy: ApprovalPolicy, registry: ToolRegistry
) -> None:
    assert policy.ruleset_mode is False
    cmd_spec = registry.get("run_command")
    list_spec = registry.get("list_dir")
    assert cmd_spec and list_spec
    assert policy.verdict(cmd_spec, make_call("run_command", command="pytest -q")) == "auto"
    assert policy.verdict(list_spec, make_call("list_dir", path=".")) == "auto"


# -- the MCP invoker under a ruleset ---------------------------------------------

# `mcp` needs no code in _ruleset_verdict: the shipped default rule plus normal
# evaluation already do the whole job. These pin that (docs/design/mcp.md section 4).

MCP_DENY_RULESET = """{"permission": {"mcp": {"github_*": "deny"}}}"""


def test_the_default_ask_rule_gates_every_mcp_call_under_a_ruleset() -> None:
    """A ruleset that says nothing about MCP still asks: the shipped default
    ("mcp", "*", "ask") sits after the BUILT-IN "*": "allow", so the defaults
    alone can never auto-approve an MCP call. (What a user's own blanket allow
    means is a different question - the test below.)"""
    policy = ruled_policy(ASK_ONLY_RULESET)
    assert PermissionRule("mcp", "*", "ask") in ruleset(ASK_ONLY_RULESET)
    call = mcp_call("github_create_issue")
    assert policy.verdict(MCP_SPEC, call) == "needs_approval"
    # YOLO is still allowed to answer that ask, exactly as for any other key.
    assert ruled_policy(ASK_ONLY_RULESET, yolo=True).verdict(MCP_SPEC, call) == "auto"


def test_a_user_written_blanket_allow_covers_mcp_too_as_it_does_in_opencode() -> None:
    """`{"permission": {"*": "allow"}}` written by the USER auto-approves MCP
    calls - pinned deliberately, not an accident: user rules load after the
    defaults and last match wins, exactly as in OpenCode, and the design's
    founding rule (a rule the user already trusts means HERE what it means
    THERE) outranks any instinct to special-case MCP out of it. The shipped
    default only guarantees the built-ins never do this on their own
    (docs/design/mcp.md section 4)."""
    policy = ruled_policy("""{"permission": {"*": "allow"}}""")
    assert policy.verdict(MCP_SPEC, mcp_call("github_create_issue")) == "auto"


def test_an_explicit_mcp_deny_beats_yolo() -> None:
    call = mcp_call("github_create_issue")
    assert ruled_policy(MCP_DENY_RULESET).verdict(MCP_SPEC, call) == "deny"
    assert ruled_policy(MCP_DENY_RULESET, yolo=True).verdict(MCP_SPEC, call) == "deny"
    # The glob is a server prefix here, so another server's tool is untouched.
    other = mcp_call("jira_search")
    assert ruled_policy(MCP_DENY_RULESET).verdict(MCP_SPEC, other) == "needs_approval"


def test_always_allow_on_an_mcp_call_remembers_that_one_tool() -> None:
    """Per tool, not per server: the user approved one tool's behaviour at the
    gate, not a server's whole surface."""
    policy = ruled_policy(ASK_ONLY_RULESET)
    call = mcp_call("github_create_issue")
    assert policy.always_rule(MCP_SPEC, call) == PermissionRule(
        "mcp", "github_create_issue", "allow"
    )

    policy.remember(policy.always_rule(MCP_SPEC, call))
    assert policy.verdict(MCP_SPEC, call) == "auto"
    assert policy.verdict(MCP_SPEC, mcp_call("github_delete_repo")) == (
        "needs_approval"
    )


# -- through the Engine ---------------------------------------------------------

DENY_MIDDLE_REPLY = """===CLIP:CALL id=1 tool=run_command===
command: git status
===CLIP:END===
===CLIP:CALL id=2 tool=run_command===
command: git -C /elsewhere status
===CLIP:END===
===CLIP:CALL id=3 tool=write_file===
path: after_deny.txt
content <<EOT
still ran
EOT
===CLIP:END===
===CLIP:EOM calls=3 chat=amber-falcon===
"""

TWO_COMMITS_AND_A_STRANGER = """===CLIP:CALL id=1 tool=run_command===
command: git commit -m one
===CLIP:END===
===CLIP:CALL id=2 tool=run_command===
command: git commit -m two
===CLIP:END===
===CLIP:CALL id=3 tool=run_command===
command: definitely-not-a-command --flag
===CLIP:END===
===CLIP:EOM calls=3 chat=amber-falcon===
"""


def transcript(engine: Engine) -> list[dict]:
    text = (engine.status().session_dir / "transcript.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_denied_call_is_pre_resolved_and_the_turn_carries_on(project, make_engine) -> None:
    write_ruleset(project)
    engine = make_engine()
    engine.start_task("t")
    assert isinstance(engine.ingest(DENY_MIDDLE_REPLY), NewTurn)
    assert engine.pending() == ()  # a deny is not a gate: there is nothing to ask

    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert (
        "The user has specified a rule which prevents you from using this specific"
        " tool call. Here are some of the relevant rules " in payload
    )
    denied_rules = json.loads(payload.split("relevant rules ", 1)[1].split("\n", 1)[0])
    assert {"permission": "bash", "pattern": "git -C*", "action": "deny"} in denied_rules
    # The denied call did not abort the ones after it (only a user rejection does).
    assert (project / "after_deny.txt").read_text(encoding="utf-8") == "still ran"

    decisions = [e for e in transcript(engine) if e["t"] == "decision"]
    denied = [e for e in decisions if e["verdict"] == "denied"]
    assert [(e["call_id"], e["source"]) for e in denied] == [(2, "rule")]
    autos = [e for e in decisions if e["verdict"] == "auto"]
    assert {e["call_id"] for e in autos} == {1, 3}
    assert all(e["source"] == "rule" for e in autos)


def test_approve_always_remembers_a_rule_and_cascades(project, make_engine) -> None:
    write_ruleset(project, ASK_ONLY_RULESET)
    engine = make_engine()
    engine.start_task("t")
    assert isinstance(engine.ingest(TWO_COMMITS_AND_A_STRANGER), NewTurn)
    pend = engine.pending()
    assert [p.call.id for p in pend] == [1, 2, 3]
    # The gate can say exactly what it would remember.
    assert pend[0].always_pattern == "git commit *"
    assert pend[2].always_pattern == "definitely-not-a-command *"

    engine.decide(1, Decision.APPROVE_ALWAYS)
    # The sibling commit is covered by the new rule; the unrelated command is not.
    assert [p.call.id for p in engine.pending()] == [3]
    cascaded = [e for e in transcript(engine) if e["t"] == "decision" and e["source"] == "rule"]
    assert [e["call_id"] for e in cascaded] == [2]

    # ...and it lasts the session: later commits never gate again.
    engine.decide(3, Decision.REJECT)
    engine.execute()
    next_turn = TWO_COMMITS_AND_A_STRANGER.replace("one", "three").replace("two", "four")
    assert isinstance(engine.ingest(next_turn), NewTurn)
    assert [p.call.id for p in engine.pending()] == [3]


def test_approve_all_edits_becomes_an_edit_rule_under_a_ruleset(project, make_engine) -> None:
    write_ruleset(project, ASK_ONLY_RULESET)
    engine = make_engine()
    engine.start_task("t")
    assert isinstance(engine.ingest(TWO_EDITS_REPLY), NewTurn)
    assert [p.call.id for p in engine.pending()] == [1, 2]

    engine.decide(1, Decision.APPROVE_ALL_EDITS)
    assert engine.pending() == ()
    step = engine.execute()
    assert isinstance(step, Send)
    assert (project / "notes_b.txt").read_text(encoding="utf-8") == "beta"
    # One mechanism, not two: the sticky flag stays off, a session rule does it.
    assert engine.status().auto_accept_edits is False
    assert isinstance(engine.ingest(THIRD_EDIT_REPLY), NewTurn)
    assert engine.pending() == ()


# -- the acceptance example ------------------------------------------------------

# A copy of a real-world opencode.json (the shape this feature was built
# against), inline so the suite never depends on a file outside it.
REAL_WORLD_JSON = """{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "*": "ask",
    "read": "allow",
    "grep": "allow",
    "glob": "allow",
    "edit": "allow",
    "todowrite": "allow",
    "bash": {
      "*": "ask",
      "git status*": "allow",
      "git commit*": "allow",
      "git log*": "allow",
      "git -C*": "deny",
      "ls*": "allow",
      "pwd": "allow",
      "cat *": "allow",
      "rg *": "allow",
      "cmake *": "allow",
      "Test-Path*": "allow"
    }
  },
  "agent": {
    "explore": {
      "mode": "subagent",
      "permission": {"*": "deny", "read": "allow"}
    }
  },
  "plugin": []
}
"""


@pytest.mark.parametrize(
    ("tool", "params", "expected"),
    [
        ("run_command", {"command": "git status"}, "auto"),
        ("run_command", {"command": "git commit -m x"}, "auto"),
        ("run_command", {"command": "cat foo.txt"}, "auto"),
        ("run_command", {"command": "pwd"}, "auto"),
        ("run_command", {"command": "git -C /x status"}, "deny"),
        ("run_command", {"command": "rm foo"}, "needs_approval"),
        # the backstop, not a rule: `git status*` would otherwise allow it
        ("run_command", {"command": "git status && rm -rf /"}, "needs_approval"),
        ("read_file", {"path": "src/utils.py"}, "auto"),
        ("glob", {"pattern": "**/*.py"}, "auto"),
        ("grep", {"pattern": "TODO"}, "auto"),
        ("write_file", {"path": "src/utils.py", "content": "x"}, "auto"),
        # no `list` rule anywhere, so their "*": "ask" catch-all wins over the
        # shipped default allow - the one place this config surprises people
        ("list_dir", {"path": "."}, "needs_approval"),
    ],
)
def test_real_world_config_verdicts(
    registry: ToolRegistry, tool: str, params: dict, expected: str
) -> None:
    policy = ruled_policy(REAL_WORLD_JSON)
    spec = registry.get(tool)
    assert spec
    assert policy.verdict(spec, make_call(tool, **params)) == expected


def test_an_agent_block_never_leaks_into_the_ruleset() -> None:
    """OpenCode's per-agent permissions name OpenCode agents; AgentClip has no
    equivalent, so guessing a mapping would grant or refuse things the user
    never decided. Only the top-level block is read."""
    rules = ruleset(REAL_WORLD_JSON)
    assert all(rule.action != "deny" or rule.permission == "bash" for rule in rules)
