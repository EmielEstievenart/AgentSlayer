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
    TOOL_PERMISSIONS,
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


# -- MCP: the two tools' permission wiring ------------------------------------
#
# docs/design/mcp.md section 4. `mcp` invokes a server tool and can do anything;
# `mcp_schema` only reads the connect-time cache. The wiring below is what keeps
# those two facts apart in the rule model.


def test_the_mcp_invoker_is_keyed_on_its_composite_tool_id() -> None:
    """The resource is the composite id (`sanitize(server)_sanitize(tool)`), so a
    ruleset rule like {"mcp": {"github_*": "allow"}} gates per server prefix or
    per individual tool - the same glob meaning OpenCode gives it."""
    assert TOOL_PERMISSIONS["mcp"] == ("mcp", "tool")
    assert permission_target("mcp", {"tool": "github_create_issue"}, "command") == (
        "mcp",
        "github_create_issue",
    )


def test_mcp_schema_takes_the_unknown_tool_fallback_on_purpose() -> None:
    """It has no TOOL_PERMISSIONS entry by design: approval kind "auto" is not in
    _KIND_KEYS, so the metadata reader falls back to its OWN NAME as the key and
    a user can keep the listing cheap ("mcp_schema": "allow") even where `mcp`
    itself is locked down."""
    assert "mcp_schema" not in TOOL_PERMISSIONS
    assert permission_target("mcp_schema", {}, "auto") == ("mcp_schema", "*")


def test_the_defaults_ask_before_every_mcp_call(case_sensitive: None) -> None:
    """The rule that must come LAST in DEFAULT_PERMISSIONS: without it the
    built-in "*": "allow" above it would silently auto-approve every MCP call in
    ruleset mode - the one outcome docs/design/mcp.md must make impossible."""
    rules = default_rules()
    assert evaluate("mcp", "anything_at_all", rules).action == "ask"
    assert evaluate("mcp", "github_create_issue", rules).action == "ask"
    # ...while the blanket allow still stands for every other key.
    assert evaluate("bash", "ls", rules).action == "allow"
    assert evaluate("edit", "src/utils.py", rules).action == "allow"


def test_a_user_mcp_rule_still_outranks_the_default_ask(case_sensitive: None) -> None:
    """The default is a floor, not a ceiling: the user's own rules load after it
    and the last match wins, exactly as for every other key."""
    rules = default_rules() + (PermissionRule("mcp", "github_*", "allow"),)
    assert evaluate("mcp", "github_create_issue", rules).action == "allow"
    assert evaluate("mcp", "jira_search", rules).action == "ask"


def test_always_pattern_for_mcp_is_the_exact_tool_id() -> None:
    """Per tool, not per server and not the whole key: the user approved ONE
    tool's behaviour at the gate, not a server's entire surface. Sanitized ids
    hold no `*`/`?`, so the id read back as a pattern matches only itself."""
    assert always_pattern("mcp", "github_create_issue") == "github_create_issue"
    assert always_pattern("mcp", "jira_search") == "jira_search"
    # contrast: every other non-bash key remembers the key, and bash the arity.
    assert always_pattern("edit", "src/utils.py") == "*"
    assert always_pattern("bash", "git commit -m 'wip'") == "git commit *"
