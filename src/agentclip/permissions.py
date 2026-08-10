"""Permission rules: OpenCode's allow/ask/deny model, ported.

A stdlib-only leaf (imported by config.py and engine/approval.py, importing
neither) so the same rules can be LOADED where config lives and APPLIED where
the approval verdict is made.

The model is deliberately OpenCode's, not a new one: AgentClip reads the very
file OpenCode reads (``~/.config/opencode/opencode.json``), so a rule a user
already trusts must mean here exactly what it means there.

    rule = (permission key, resource pattern, action)

Two decisions carry the whole design and are ported verbatim:

- MATCHING is wildcard, not glob and not regex: ``*`` crosses spaces and
  slashes (``git *`` matches ``git -C /x status``), ``?`` is one character, and
  a pattern ending in ``" *"`` makes the arguments OPTIONAL (``ls *`` matches a
  bare ``ls``) - which is the only reason a hand-written allow list reads the
  way its author expects.
- PRECEDENCE is positional: the LAST rule that matches wins, with no
  specificity sorting. That is what lets a config say "everything asks" and
  then carve exceptions below it, and what lets the session's own remembered
  rules (appended last) outrank the file.

What is NOT ported: OpenCode's per-agent permission blocks (they name OpenCode
agents, which have no AgentClip equivalent) and its tree-sitter shell parsing
(see engine/approval.py's deny-token backstop, which stands in for it).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, cast

Action = Literal["allow", "ask", "deny"]

ACTIONS: frozenset[str] = frozenset({"allow", "ask", "deny"})


@dataclass(frozen=True, slots=True)
class PermissionRule:
    permission: str  # tool/permission key pattern, e.g. "bash", "read", "*"
    pattern: str  # resource pattern, e.g. "git status*", "src/**", "*"
    action: Action

    def as_json(self) -> dict[str, str]:
        return {"permission": self.permission, "pattern": self.pattern, "action": self.action}


# -- the matcher --------------------------------------------------------------

# The characters a wildcard pattern may contain literally. `*` and `?` are NOT
# here: they are rewritten after this pass, which is why escaping must come
# first (an escaped `\*` would otherwise be turned into `\.*`).
_ESCAPE = re.compile(r"[.+^${}()|\[\]\\]")


def case_insensitive() -> bool:
    """Whether matching ignores case - true on Windows, where neither paths nor
    executable names are case-sensitive and a user writing ``Test-Path*`` must
    not be defeated by ``test-path``. A function rather than a constant so tests
    can exercise both modes on one machine."""
    return sys.platform == "win32"


def wildcard_match(text: str, pattern: str) -> bool:
    """OpenCode's ``Wildcard.match``: ``*`` = any run of characters (newlines
    included), ``?`` = exactly one, whole-string anchored.

    Backslashes are normalized to ``/`` on BOTH sides, so a Windows path and a
    pattern written with either separator meet in the middle.
    """
    body = _ESCAPE.sub(lambda m: "\\" + m.group(0), pattern.replace("\\", "/"))
    body = body.replace("*", ".*").replace("?", ".")
    if body.endswith(" .*"):
        # A trailing " *" means "with or without arguments": `ls *` must match a
        # bare `ls`, or every allow list in the wild would be subtly wrong.
        body = body[:-3] + "( .*)?"
    flags = re.DOTALL | (re.IGNORECASE if case_insensitive() else 0)
    return re.fullmatch(body, text.replace("\\", "/"), flags) is not None


def evaluate(
    permission: str, pattern: str, *rulesets: Sequence[PermissionRule]
) -> PermissionRule:
    """The last rule (across the rulesets in order) matching both the permission
    key and the resource. No match is an implicit "ask" - the safe default that
    makes an empty config mean "confirm everything"."""
    found: PermissionRule | None = None
    for ruleset in rulesets:
        for rule in ruleset:
            if wildcard_match(permission, rule.permission) and wildcard_match(
                pattern, rule.pattern
            ):
                found = rule
    return found if found is not None else PermissionRule(permission, "*", "ask")


def matching_rules(permission: str, *rulesets: Sequence[PermissionRule]) -> tuple[PermissionRule, ...]:
    """Every rule whose permission key matches, in order - the "here are the
    relevant rules" payload a denied call reports back to the model."""
    return tuple(
        rule
        for ruleset in rulesets
        for rule in ruleset
        if wildcard_match(permission, rule.permission)
    )


def rules_json(rules: Iterable[PermissionRule]) -> str:
    return json.dumps([rule.as_json() for rule in rules])


# -- reading rules out of a config object -------------------------------------


def _home() -> str:
    return os.path.expanduser("~").replace("\\", "/")


def expand(pattern: str) -> str:
    """``~/`` and ``$HOME/`` prefixes -> an absolute forward-slash path.

    Only a PREFIX, and only these two forms: a pattern is matched, not resolved,
    so anything cleverer (env vars mid-string, ``..``) would change what a rule
    means depending on where AgentClip was started."""
    for prefix in ("~", "$HOME"):
        if pattern == prefix:
            return _home()
        if pattern.startswith(prefix + "/"):
            return _home() + pattern[len(prefix) :]
    return pattern


def _as_action(value: object) -> Action | None:
    return cast(Action, value) if value in ACTIONS else None


def rules_from_config(obj: object) -> tuple[tuple[PermissionRule, ...], tuple[str, ...]]:
    """OpenCode's ``fromConfig``: a permission object -> flat rules in file order.

    Accepts the three shapes OpenCode accepts - a bare action string (applies to
    everything), ``key: action``, and ``key: {pattern: action}`` - and returns
    ``(rules, warnings)``: a malformed entry is skipped and reported, never
    raised, because a typo in a permission file must not stop AgentClip from
    starting (it just leaves that entry doing nothing).
    """
    rules: list[PermissionRule] = []
    warnings: list[str] = []
    if isinstance(obj, str):
        action = _as_action(obj)
        if action is None:
            return (), (f"permission: unknown action {obj!r}; ignored",)
        return (PermissionRule("*", "*", action),), ()
    if not isinstance(obj, dict):
        return (), (f"permission: expected a table or an action string, got {type(obj).__name__}",)
    for key, value in obj.items():
        if isinstance(value, str):
            action = _as_action(value)
            if action is None:
                warnings.append(f"permission: [{key}] unknown action {value!r}; ignored")
                continue
            rules.append(PermissionRule(key, "*", action))
            continue
        if isinstance(value, dict):
            for pattern, raw in value.items():
                action = _as_action(raw)
                if action is None:
                    warnings.append(
                        f"permission: [{key}] {pattern!r}: unknown action {raw!r}; ignored"
                    )
                    continue
                rules.append(PermissionRule(key, expand(pattern), action))
            continue
        warnings.append(f"permission: [{key}] must be an action string or a table; ignored")
    return tuple(rules), tuple(warnings)


# OpenCode's engine defaults, trimmed to the keys AgentClip has. Loaded BEFORE
# the user's rules so anything they write outranks these (last match wins).
DEFAULT_PERMISSIONS: dict[str, object] = {
    "*": "allow",
    "read": {"*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow"},
}


def default_rules() -> tuple[PermissionRule, ...]:
    rules, _ = rules_from_config(DEFAULT_PERMISSIONS)
    return rules


# -- tools -> (permission key, resource) --------------------------------------

# The OpenCode permission key each AgentClip tool answers to, and the call
# parameter that names the resource being acted on. ask_user/task_done are
# absent on purpose: they are AgentClip's own control flow, not access to the
# user's machine, and gating them would deadlock the turn that asks.
TOOL_PERMISSIONS: dict[str, tuple[str, str]] = {
    "read_file": ("read", "path"),
    "write_file": ("edit", "path"),
    "edit_file": ("edit", "path"),
    "delete_file": ("edit", "path"),
    "list_dir": ("list", "path"),
    "glob": ("glob", "pattern"),
    "grep": ("grep", "pattern"),
    "run_command": ("bash", "command"),
    "skill": ("skill", "name"),
    "delegate": ("task", "task"),
}

# What a tool the table has never heard of is treated as, by its approval kind.
_KIND_KEYS: dict[str, str] = {"edit": "edit", "command": "bash"}


def permission_target(tool: str, params: dict[str, str], approval_kind: str = "auto") -> tuple[str, str]:
    """The (permission key, resource) pair a call is evaluated as.

    An unknown tool falls back to its approval kind (a custom edit tool is still
    governed by the ``edit`` rules) and, failing that, to its own name with a
    ``*`` resource - so a rule can always be written for it, and the implicit
    "ask" covers it until one is."""
    entry = TOOL_PERMISSIONS.get(tool)
    if entry is None:
        return _KIND_KEYS.get(approval_kind, tool), "*"
    key, param = entry
    resource = params.get(param, "")
    if key != "bash":
        resource = _relative(resource)
    return key, resource


def _relative(resource: str) -> str:
    """Forward slashes, no leading ``./`` - the worktree-relative form OpenCode
    matches paths in. Paths that escape the workspace never get here: the
    sandbox rejects them before a rule could allow them."""
    text = resource.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


# -- the "always allow" pattern -----------------------------------------------

# How many leading words of a command identify "this kind of command" - so
# remembering `git commit -m "wip"` remembers `git commit *`, not every `git`
# and not that one message. Ported from OpenCode's arity table; a command not
# listed keeps its first word only.
ARITY: dict[str, int] = {
    "git": 2,
    "docker": 2,
    "docker compose": 3,
    "npm": 2,
    "npm run": 3,
    "pnpm": 2,
    "yarn": 2,
    "cargo": 2,
    "pip": 2,
    "uv": 2,
    "python -m": 3,
    "poetry": 2,
    "go": 2,
    "dotnet": 2,
    "make": 1,
    "cmake": 1,
    "ruff": 2,
    "pytest": 1,
}


def always_pattern(key: str, resource: str) -> str:
    """The resource pattern an "always allow" decision should remember.

    Everything but bash remembers the whole key (``*``): "always allow edits"
    is the decision users actually make, and it is the one today's
    approve-all-edits already means.
    """
    if key != "bash":
        return "*"
    try:
        tokens = shlex.split(resource, posix=False)
    except ValueError:
        tokens = resource.split()
    if not tokens:
        return "*"
    arity, matched = 1, 0
    for prefix, count in ARITY.items():
        words = prefix.split(" ")
        if len(words) > matched and [t.lower() for t in tokens[: len(words)]] == words:
            arity, matched = count, len(words)
    return " ".join(tokens[:arity]) + " *"


__all__ = [
    "ACTIONS",
    "ARITY",
    "Action",
    "DEFAULT_PERMISSIONS",
    "PermissionRule",
    "TOOL_PERMISSIONS",
    "always_pattern",
    "case_insensitive",
    "default_rules",
    "evaluate",
    "expand",
    "matching_rules",
    "permission_target",
    "rules_from_config",
    "rules_json",
    "wildcard_match",
]
