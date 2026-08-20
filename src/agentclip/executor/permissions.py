"""Permission rules: OpenCode's allow/ask/deny model, ported.

A stdlib-only leaf (imported by config.py and engine/approval.py, importing
neither) so the same rules can be LOADED where config lives and APPLIED where
the approval verdict is made.

The model is deliberately OpenCode's, not a new one: AgentClip's own file
(``~/.config/agentclip/permissions.json``) has opencode.json's shape, so a rule
a user already trusts must mean here exactly what it means there - copied
across, it reads the same.

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

OpenCode's PER-AGENT permission blocks are ported too, restricted to the two
primary agents AgentClip has an equivalent of: `build` and `plan` are its
permission MODES (see PermissionMode), and a mode's effective ruleset is built
close to the way OpenCode builds an agent's - see :func:`build_mode_rules`,
whose docstring gives the layer order and the one place it deliberately differs.
Later still wins, so a rule written for `plan` can deliberately loosen what the
overlay denies.

This ruleset IS the permission system. There is no second gate behind it: an
install with no permissions.json runs on :data:`DEFAULT_CONFIG`, which is also
exactly what a config reset writes, so "the defaults" is a document a user can
read rather than behaviour they have to infer.

What is NOT ported: OpenCode's tree-sitter shell parsing (see
engine/approval.py's deny-token backstop, which stands in for it).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

Action = Literal["allow", "ask", "deny"]

ACTIONS: frozenset[str] = frozenset({"allow", "ask", "deny"})

# -- the session's permission mode --------------------------------------------

# OpenCode's PRIMARY AGENTS, under their own names: `build` executes, `plan`
# explores. They live here for this module's founding reason - the mode is read
# where config lives and applied where the verdict is made, and a leaf both
# sides already import is the only place that can be true of.
#
#   build  the default builder. Whatever the ruleset says, nothing more.
#   plan   exploration only: the built-in overlay below denies everything that
#          could change something (see MODE_PERMISSIONS).
#
# A mode is no longer a dial ABOVE the rules: it IS a ruleset, built by layering
# its overlay into the user's own (see ModeRules). "The user is away" is not a
# mode at all any more - it is the `unattended` toggle on ApprovalPolicy, which
# answers gates rather than changing what the rules say.
PermissionMode = Literal["build", "plan"]

# Cycle order (`/mode` with no argument, and the TUI's Shift+Tab): the default
# first, so a stray keypress lands back on the mode that works.
PERMISSION_MODES: tuple[PermissionMode, ...] = ("build", "plan")


def normalize_mode(value: object) -> PermissionMode | None:
    """``value`` as a permission mode, or None if it is not one. Case- and
    space-insensitive, because it arrives from a config file and a chat box.

    Nothing else is accepted - there is no migration table for the modes this
    replaced. An unreadable value falls back to `build` where it is read
    (config.py warns), which is the only sound answer: a mode is a ruleset, and
    guessing which ruleset a stale word meant would silently run a session under
    permissions nobody chose."""
    if not isinstance(value, str):
        return None
    wanted = value.strip().lower()
    return wanted if wanted in PERMISSION_MODES else None


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


# THE default permissions document - one object, two jobs. It is what governs an
# install with no permissions.json anywhere, and it is what `/config` serialises
# into a fresh one (config.default_permissions_document), so those two can never
# drift: creating the file writes down exactly what was already in force, and a
# user reading the file they just got is reading the rules they were running
# under a moment earlier.
#
# There is deliberately NO "*" key. An unlisted permission falls through to
# evaluate()'s implicit "ask", which is the answer a permission system must give
# about something nobody has decided; a blanket allow at the bottom would silently
# cover every tool added after this file was written.
#
# `mcp` asks rather than allows because an MCP tool can do anything
# (docs/design/mcp.md section 4), and `task` asks because a delegation is a second
# actor doing the same.
DEFAULT_CONFIG: dict[str, object] = {
    "permission": {
        # The dotenv carve-out, in OpenCode's own shape: reading source is free,
        # reading secrets is not.
        "read": {"*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow"},
        "list": "allow",
        "glob": "allow",
        "grep": "allow",
        "skill": "allow",
        # The MCP metadata listing, which reads the connect-time cache and
        # touches no server (docs/design/mcp.md section 4). Its own key rather
        # than `mcp`'s, so it stays cheap where `mcp` itself is locked down.
        "mcp_schema": "allow",
        # Re-reading output AgentClip already produced, out of its own in-memory
        # cache: no file is touched and no command is run, so gating it would
        # only make the model pay a prompt to finish reading a result the user
        # approved a turn ago. Same reasoning as mcp_schema's, and it stays a
        # separate key so a user can still say otherwise.
        "fetch_chunk": "allow",
        "edit": "ask",
        "task": "ask",
        "mcp": "ask",
        "bash": {
            "*": "ask",
            "git status *": "allow",
            "git diff *": "allow",
            "git log *": "allow",
            "ls *": "allow",
            "dir *": "allow",
        },
    },
    # The default PER-MODE blocks, layered over the built-in overlay below. Plan's
    # exists to buy back the two read-only git commands the overlay's blanket
    # `bash: deny` would otherwise take away - which is also the worked example of
    # how a user loosens plan mode deliberately.
    "agent": {
        "plan": {"permission": {"bash": {"git status *": "allow", "git diff *": "allow"}}},
        "build": {"permission": {}},
    },
}


def default_rules() -> tuple[PermissionRule, ...]:
    """The shared `permission` block of :data:`DEFAULT_CONFIG`."""
    rules, _ = rules_from_config(DEFAULT_CONFIG["permission"])
    return rules


def default_agent_rules(mode: PermissionMode) -> tuple[PermissionRule, ...]:
    """The default `agent.<mode>.permission` block of :data:`DEFAULT_CONFIG`."""
    agents = DEFAULT_CONFIG["agent"]
    assert isinstance(agents, dict)
    block = agents.get(mode) or {}
    rules, _ = rules_from_config(block.get("permission", {}))
    return rules


# The built-in overlay each mode adds on top of those shared defaults - OpenCode's
# per-agent overlay, in AgentClip's vocabulary.
#
# `plan` denies everything that could CHANGE something, by permission key rather
# than by tool: `edit` covers every write/delete (and any unknown edit-kind tool,
# which permission_target maps onto that key), `bash` every command line, `mcp`
# every MCP call - an MCP tool can do anything - and `task` delegation, because a
# sub-agent is a second actor that would not be in plan mode's ruleset if this
# key were missing. Reads, listings, globs, greps and skills are untouched, so
# they behave exactly as they do in `build`.
#
# `build` adds nothing: it IS the ruleset the user wrote.
MODE_PERMISSIONS: dict[PermissionMode, dict[str, object]] = {
    "build": {},
    "plan": {"edit": "deny", "bash": "deny", "mcp": "deny", "task": "deny"},
}


def mode_rules(mode: PermissionMode) -> tuple[PermissionRule, ...]:
    rules, _ = rules_from_config(MODE_PERMISSIONS[mode])
    return rules


@dataclass(frozen=True, slots=True)
class ModeRules:
    """One effective ruleset per permission mode - what a session actually runs.

    Never empty in practice: the built-in defaults and the mode overlay are
    always layered underneath whatever the user wrote, because the ruleset IS the
    permission system - there is no second gate behind it to fall back on.

    One field per mode rather than a mapping: `build` is read on its own by the
    deny_plan check (would this call have been denied anyway?), so the pair is
    not interchangeable and the type should say so.
    """

    build: tuple[PermissionRule, ...] = ()
    plan: tuple[PermissionRule, ...] = ()

    def for_mode(self, mode: PermissionMode) -> tuple[PermissionRule, ...]:
        return self.plan if mode == "plan" else self.build

    def __bool__(self) -> bool:
        return bool(self.build or self.plan)


def build_mode_rules(
    shared: Sequence[PermissionRule],
    per_mode: Mapping[str, Sequence[PermissionRule]] | None = None,
) -> ModeRules:
    """Layer a config's rules into one ruleset per mode:

        DEFAULT_CONFIG's `permission` block
        -> the user's shared `permission` block
        -> the mode's built-in overlay (MODE_PERMISSIONS)
        -> DEFAULT_CONFIG's `agent.<mode>.permission` block
        -> the user's `agent.<mode>.permission` block

    Last match wins, so each layer may overturn the one before it. ``shared`` and
    each ``per_mode`` entry are already in file order, global layer before
    project layer.

    Two properties fall out of that order, and both are the point:

    * a config with no permissions.json anywhere evaluates IDENTICALLY to one
      whose file was just created by `/config`. That file IS DEFAULT_CONFIG, so
      its blocks arrive a second time immediately after the built-in copies of
      themselves - and a layer repeated next to itself cannot change a
      last-match-wins answer;
    * the overlay outranks the block written for EVERY mode, and is outranked
      only by the block written FOR that mode. OpenCode merges the agent overlay
      before the user's shared block instead, which means a config saying
      ``{"edit": "allow"}`` switches plan mode's denials back off by accident -
      and would make DEFAULT_CONFIG's own `agent.plan` block dead weight. Here
      loosening plan mode is still possible and still OpenCode-shaped; it just
      has to be said under `agent.plan`, where it is unmistakably meant.
    """

    blocks = per_mode or {}

    def layer(mode: PermissionMode) -> tuple[PermissionRule, ...]:
        return (
            default_rules()
            + tuple(shared)
            + mode_rules(mode)
            + default_agent_rules(mode)
            + tuple(blocks.get(mode, ()))
        )

    return ModeRules(build=layer("build"), plan=layer("plan"))


def default_mode_rules() -> ModeRules:
    """What governs a session with no permissions.json anywhere."""
    return build_mode_rules(())


# -- tools -> (permission key, resource) --------------------------------------

# The OpenCode permission key each AgentClip tool answers to, and the call
# parameter that names the resource being acted on. ask_user/task_done are
# absent on purpose: they are AgentClip's own control flow, not access to the
# user's machine, and gating them would deadlock the turn that asks.
#
# `mcp_schema` is absent on purpose too, and for a different reason: with no
# entry here it falls back to its OWN NAME as the permission key (approval_kind
# "auto" is not in _KIND_KEYS, so permission_target's unknown-tool fallback
# returns ("mcp_schema", "*")). That keeps the cache-only metadata listing cheap
# - a user can write `{"mcp_schema": "allow"}` - even where `mcp` itself is
# locked down, which is exactly what docs/design/mcp.md section 4 asks for.
TOOL_PERMISSIONS: dict[str, tuple[str, str]] = {
    "read_file": ("read", "path"),
    "write_file": ("edit", "path"),
    "edit_file": ("edit", "path"),
    # The ranged edit answers to the same `edit` key as edit_file: it is the
    # same act on the same resource, and a rule the user wrote to allow edits
    # under src/ must not stop meaning that because a service turned
    # edit_by_lines on.
    "replace_lines": ("edit", "path"),
    "delete_file": ("edit", "path"),
    "list_dir": ("list", "path"),
    "glob": ("glob", "pattern"),
    "grep": ("grep", "pattern"),
    "run_command": ("bash", "command"),
    "skill": ("skill", "name"),
    # The resource is the chunk id, so a rule can be written per id at all - not
    # that anyone would, but the alternative (no entry) would resolve to a `*`
    # resource and make the key unable to say anything more specific later.
    "fetch_chunk": ("fetch_chunk", "id"),
    "delegate": ("task", "task"),
    # The MCP invoker's resource is the composite tool id (sanitize(server) + "_"
    # + sanitize(tool)), so a ruleset rule such as {"mcp": {"github_*": "allow"}}
    # gates per server prefix or per individual tool - the same glob meaning
    # OpenCode gives it (docs/design/mcp.md section 4).
    "mcp": ("mcp", "tool"),
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
    sandbox rejects them before a rule could allow them.

    With ONE exception, and it needs no special case: an absolute path into a
    discovered skill folder, which read_file accepts (sandbox.py's
    ``extra_read_roots``). It is passed through as written, and the dotenv
    carve-out still bites it, because a wildcard ``*`` crosses slashes here -
    ``*.env`` matches ``/home/u/.claude/skills/deploy/.env`` exactly as it
    matches ``config/.env``. Normalising it to something workspace-relative
    would be the dangerous move: it has no workspace-relative form, and any
    invented one would be a second spelling for a rule to miss."""
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


# The composite-id alphabet, mirrored from agentclip.executor.mcp.types.sanitize: this
# module is a stdlib-only leaf (it cannot import agentclip.executor.mcp), so the one
# regex is duplicated with its source named - the two must stay identical for
# the always_pattern neutering below to be sound.
_MCP_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def always_pattern(key: str, resource: str) -> str:
    """The resource pattern an "always allow" decision should remember.

    Everything but bash and mcp remembers the whole key (``*``): "always allow
    edits" is the decision users actually make, and it is the one today's
    approve-all-edits already means.
    """
    if key == "mcp":
        # Per TOOL, not per server, and never the whole key: the user approved
        # ONE tool's behaviour at the gate, not a server's entire surface
        # (docs/design/mcp.md section 4). REAL composite ids are sanitized to
        # [a-zA-Z0-9_-] and so read back as literal patterns - but the resource
        # here is the model's raw `tool:` param, not a cache id. A model that
        # sends `tool: github_*` would otherwise mint a remembered ALLOW rule
        # with a live wildcard - one gate press away from auto-approving every
        # github tool, and (because session rules evaluate last) from outranking
        # the user's own file-written denies. Re-sanitizing with the id
        # alphabet's own rule closes it: a resource that needed rewriting can
        # never equal a real id, so the remembered rule is inert; one that
        # didn't is the exact id, matching only itself.
        return _MCP_ID_RE.sub("_", resource)
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
    "DEFAULT_CONFIG",
    "MODE_PERMISSIONS",
    "PERMISSION_MODES",
    "ModeRules",
    "PermissionMode",
    "PermissionRule",
    "TOOL_PERMISSIONS",
    "always_pattern",
    "build_mode_rules",
    "case_insensitive",
    "default_agent_rules",
    "default_mode_rules",
    "default_rules",
    "evaluate",
    "expand",
    "matching_rules",
    "mode_rules",
    "normalize_mode",
    "permission_target",
    "rules_from_config",
    "rules_json",
    "wildcard_match",
]
