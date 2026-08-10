"""The permission rule model: the wildcard matcher, last-match-wins evaluation,
config parsing, and the "always allow" pattern.

These are the semantics AgentClip inherits from OpenCode wholesale (a rule the
user already wrote for OpenCode must mean the same thing here), so the tests are
written as statements about the PORT: `*` crosses spaces and slashes, a trailing
`" *"` makes arguments optional, precedence is positional, and an unmatched call
asks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentclip import permissions
from agentclip.permissions import (
    PermissionRule,
    always_pattern,
    default_rules,
    evaluate,
    expand,
    matching_rules,
    permission_target,
    rules_from_config,
    rules_json,
    wildcard_match,
)


@pytest.fixture
def case_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match as a POSIX machine would, whatever this one is."""
    monkeypatch.setattr(permissions, "case_insensitive", lambda: False)


@pytest.fixture
def case_folding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match as Windows does, whatever this machine is."""
    monkeypatch.setattr(permissions, "case_insensitive", lambda: True)


# -- wildcard_match -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "pattern", "expected"),
    [
        ("git status", "git status*", True),
        ("git status --short", "git status*", True),
        ("git commit -m 'wip'", "git commit*", True),
        ("git push", "git commit*", False),
        # `*` is not a glob: it crosses spaces and slashes alike.
        ("git -C /tmp/x status", "git -C*", True),
        ("src/deep/nested/file.py", "src/*", True),
        ("rm -rf /", "*", True),
        # `?` is exactly one character.
        ("cat a.txt", "cat ?.txt", True),
        ("cat ab.txt", "cat ?.txt", False),
        # anchored at both ends: no substring matches.
        ("git status", "status", False),
        ("sudo git status", "git status*", False),
        # regex metacharacters in the pattern are literal.
        ("a+b", "a+b", True),
        ("axb", "a.b", False),
        ("file.env", "*.env", True),
        ("fileXenv", "*.env", False),
    ],
)
def test_wildcard_match_cases(
    text: str, pattern: str, expected: bool, case_sensitive: None
) -> None:
    assert wildcard_match(text, pattern) is expected


def test_trailing_space_star_makes_arguments_optional(case_sensitive: None) -> None:
    """`ls *` must match a bare `ls` - the rule every hand-written allow list
    depends on (OpenCode rewrites the tail to `( .*)?`)."""
    assert wildcard_match("ls", "ls *")
    assert wildcard_match("ls -la", "ls *")
    assert not wildcard_match("lsof", "ls *")
    # without the space it is an ordinary prefix match, and `ls` alone still hits
    assert wildcard_match("lsof", "ls*")


def test_backslashes_normalize_on_both_sides(case_sensitive: None) -> None:
    assert wildcard_match(r"src\utils.py", "src/*")
    assert wildcard_match("src/utils.py", r"src\*")
    assert wildcard_match(r"C:\Users\me\notes", r"C:\Users\*")


def test_star_crosses_newlines(case_sensitive: None) -> None:
    # DOTALL: a chained command is one string, and `*` must not stop at the break.
    assert wildcard_match("pytest\nrm -rf /", "pytest*")


def test_case_sensitivity_follows_the_platform_switch(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(permissions, "case_insensitive", lambda: False)
    assert not wildcard_match("GIT status", "git status*")
    monkeypatch.setattr(permissions, "case_insensitive", lambda: True)
    assert wildcard_match("GIT status", "git status*")


# -- evaluate -----------------------------------------------------------------


def test_no_rule_matches_means_ask() -> None:
    rule = evaluate("bash", "rm -rf /", ())
    assert rule == PermissionRule("bash", "*", "ask")


def test_last_match_wins_both_directions(case_sensitive: None) -> None:
    specific_then_catchall = (
        PermissionRule("bash", "git status*", "allow"),
        PermissionRule("*", "*", "ask"),
    )
    catchall_then_specific = (
        PermissionRule("*", "*", "ask"),
        PermissionRule("bash", "git status*", "allow"),
    )
    # Positional, not specificity-ranked: the later rule wins either way round.
    assert evaluate("bash", "git status", specific_then_catchall).action == "ask"
    assert evaluate("bash", "git status", catchall_then_specific).action == "allow"


def test_later_ruleset_outranks_earlier_one(case_sensitive: None) -> None:
    config = (PermissionRule("bash", "*", "deny"),)
    session = [PermissionRule("bash", "npm *", "allow")]
    # Session ("always allow") rules are passed last, so they outrank the file -
    # the same way OpenCode's `approved` array does.
    assert evaluate("bash", "npm test", config, session).action == "allow"
    assert evaluate("bash", "rm -rf /", config, session).action == "deny"


def test_matching_rules_are_keyed_on_the_permission_only(case_sensitive: None) -> None:
    rules = (
        PermissionRule("bash", "git *", "allow"),
        PermissionRule("bash", "git -C*", "deny"),
        PermissionRule("read", "*", "allow"),
    )
    matched = matching_rules("bash", rules)
    assert [r.pattern for r in matched] == ["git *", "git -C*"]
    assert json.loads(rules_json(matched))[1] == {
        "permission": "bash",
        "pattern": "git -C*",
        "action": "deny",
    }


# -- rules_from_config --------------------------------------------------------


def test_bare_string_applies_to_everything() -> None:
    rules, warnings = rules_from_config("deny")
    assert rules == (PermissionRule("*", "*", "deny"),)
    assert warnings == ()


def test_config_order_is_preserved() -> None:
    rules, warnings = rules_from_config(
        {
            "*": "ask",
            "read": "allow",
            "bash": {"*": "ask", "git status*": "allow", "git -C*": "deny"},
        }
    )
    assert warnings == ()
    assert rules == (
        PermissionRule("*", "*", "ask"),
        PermissionRule("read", "*", "allow"),
        PermissionRule("bash", "*", "ask"),
        PermissionRule("bash", "git status*", "allow"),
        PermissionRule("bash", "git -C*", "deny"),
    )


def test_home_prefixes_expand_in_patterns() -> None:
    home = Path.home().as_posix()
    rules, _ = rules_from_config({"read": {"~/secrets/*": "deny", "$HOME/keys": "deny"}})
    assert [r.pattern for r in rules] == [f"{home}/secrets/*", f"{home}/keys"]
    # only a prefix, and only these two forms
    assert expand("src/~/x") == "src/~/x"


def test_malformed_entries_warn_and_are_skipped() -> None:
    rules, warnings = rules_from_config(
        {
            "read": "allow",
            "bash": "maybe",  # not an action
            "edit": ["nope"],  # not an action or a table
            "glob": {"*": "sometimes"},
        }
    )
    assert rules == (PermissionRule("read", "*", "allow"),)
    assert len(warnings) == 3
    assert any("maybe" in w for w in warnings)
    assert any("edit" in w for w in warnings)
    assert any("sometimes" in w for w in warnings)


def test_defaults_ask_about_dotenv_but_allow_its_example(case_sensitive: None) -> None:
    rules = default_rules()
    assert evaluate("read", ".env", rules).action == "ask"
    assert evaluate("read", ".env.local", rules).action == "ask"
    assert evaluate("read", ".env.example", rules).action == "allow"
    assert evaluate("read", "src/utils.py", rules).action == "allow"
    assert evaluate("bash", "rm -rf /", rules).action == "allow"  # a user config narrows this


# -- tool mapping and the always pattern --------------------------------------


def test_permission_target_maps_tools_to_keys_and_resources() -> None:
    assert permission_target("read_file", {"path": "./src/utils.py"}) == ("read", "src/utils.py")
    assert permission_target("write_file", {"path": r"src\a.py"}) == ("edit", "src/a.py")
    assert permission_target("list_dir", {"path": "."}) == ("list", ".")
    assert permission_target("grep", {"pattern": "TODO"}) == ("grep", "TODO")
    assert permission_target("run_command", {"command": "git status"}) == ("bash", "git status")
    assert permission_target("delegate", {"task": "explore"}) == ("task", "explore")
    # unknown tools fall back to their approval kind, then to their own name
    assert permission_target("mystery", {}, "command") == ("bash", "*")
    assert permission_target("mystery", {}, "auto") == ("mystery", "*")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git commit -m 'wip'", "git commit *"),
        ("git status", "git status *"),
        ("npm run build", "npm run build *"),  # the longer prefix wins over "npm"
        ("npm install left-pad", "npm install *"),
        ("docker compose up -d", "docker compose up *"),
        ("python -m pytest tests", "python -m pytest *"),
        ("pytest tests -q", "pytest *"),
        ("rm -rf /", "rm *"),
        ("", "*"),
    ],
)
def test_always_pattern_for_bash_uses_the_arity_table(command: str, expected: str) -> None:
    assert always_pattern("bash", command) == expected


def test_always_pattern_for_everything_else_is_the_whole_key() -> None:
    assert always_pattern("edit", "src/utils.py") == "*"
    assert always_pattern("read", ".env") == "*"
    assert always_pattern("task", "explore") == "*"
