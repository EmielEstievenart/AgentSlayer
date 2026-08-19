"""ApprovalPolicy: the gate every tool call passes before it runs.

ONE mechanism, always in force: the permission ruleset (config.py reads
AgentClip's permissions.json, which has OpenCode's shape; an install with no such
file runs on permissions.py's DEFAULT_CONFIG). The rules decide, last match wins:

    deny  -> "deny"           the call never runs and never gates - not even
                              under YOLO; a deny is the user's standing "no"
    allow -> "auto"           except a bash command carrying a deny token (see
                              below), which is downgraded to a gate
    ask   -> "needs_approval" unless YOLO, which answers every ask with "yes"

A permission nothing matches is an implicit "ask", which is what makes the rules
safe to be the only gate: the answer to "nobody decided about this" is to ask.

Rules the user approves with "always allow" are appended to `session_rules`,
which is evaluated LAST - so a remembered answer outranks the file, exactly as
OpenCode's `approved` array does. In-memory only: it dies with the process,
because "always" here means "stop asking me today", not "edit my config".

The DENY-TOKEN BACKSTOP is AgentClip's one deviation from OpenCode. OpenCode
parses a shell script with tree-sitter and evaluates every command node
separately, so `git status && rm -rf /` is judged on `rm -rf /` too. AgentClip
has no shell parser, so instead a command containing any configured deny token
(`;`, `&&`, `||`, `|`, backtick, `$(`, `>`, `<`, newline) can never silently
auto-run: allow becomes ask. Deny still wins outright, and a user who wants
chained commands to run unattended can still say yes once at the gate.

THE PERMISSION MODE picks WHICH ruleset the rules above are read from. `build`
and `plan` are OpenCode's two primary agents (permissions.py), each with its own
effective ruleset built at load time; switching modes swaps the ruleset, it does
not add a check on top of one. `plan`'s built-in overlay denies `edit`, `bash`,
`mcp` and `task`, which is how "exploration only" is expressed - as rules, so a
user who deliberately writes an `agent.plan` rule can overturn it, as in
OpenCode.

A plan refusal reports "deny_plan" rather than "deny" when the same call would
NOT have been denied under `build`, because the model is told a different thing
by each ("the rules forbid this" vs "the user is only exploring") and the audit
trail names a different source.

THE UNATTENDED TOGGLE (`ApprovalPolicy.unattended`) is orthogonal to all of it:
the user is away, so a call that would have opened a gate is denied
("deny_unattended") instead, because there is nobody there to answer it. Allow
rules still run and deny rules still deny. YOLO, being the user's explicit
"approve everything for me", still answers asks - the one thing it still does
not answer is the deny-token backstop, which therefore denies here rather than
gating.
"""

from __future__ import annotations

from typing import Literal

from agentclip.config import ApprovalConfig
from agentclip.executor.permissions import (
    PERMISSION_MODES,
    ModeRules,
    PermissionMode,
    PermissionRule,
    always_pattern,
    default_mode_rules,
    evaluate,
    matching_rules,
    normalize_mode,
    permission_target,
    rules_json,
)
from agentclip.executor.tools.registry import ToolSpec
from agentclip.protocol.types import ToolCall

Verdict = Literal["auto", "needs_approval", "deny", "deny_plan", "deny_unattended"]

# What "approve all edits" remembers. One rule, named once, because two places
# mint it: a session that starts with [approval] auto_accept_edits, and the
# Decision.APPROVE_ALL_EDITS button.
EDITS_RULE = PermissionRule("edit", "*", "allow")

# The three ways a call can be refused without ever reaching the user. Kept as a
# set so a consumer asks "was this denied?" once instead of listing the variants.
DENY_VERDICTS: frozenset[str] = frozenset({"deny", "deny_plan", "deny_unattended"})

# The mode vocabulary is re-exported here, and this is where the layers above
# take it from: it is DEFINED in permissions.py because config.py has to read it
# and cannot import this module (approval imports config), but app/ and tui/ have
# no business reaching into the rule model - the policy that applies a mode is
# the thing they talk to.
__all__ = [
    "DENY_VERDICTS",
    "EDITS_RULE",
    "PERMISSION_MODES",
    "ApprovalPolicy",
    "ModeRules",
    "PermissionMode",
    "Verdict",
    "normalize_mode",
]


class ApprovalPolicy:
    """Per-session approval state. yolo answers every question with yes -
    everything runs except what a rule explicitly denies - and the /yolo chat
    command toggles it live. mode picks which ruleset is in force (see the module
    docstring) and the /mode command sets it, also live; unattended is the third
    live switch - it answers gates with "no" instead of asking.

    auto_accept_edits is not a fourth: it is a shorthand for one remembered rule
    ("edit": "*": "allow"), seeded from config and set when the user chooses
    Decision.APPROVE_ALL_EDITS, so the status bar has a flag to paint while the
    rules stay the only thing deciding anything."""

    def __init__(self, config: ApprovalConfig, rules: ModeRules | None = None) -> None:
        self.auto_accept_edits: bool = config.auto_accept_edits
        self.yolo: bool = config.yolo
        # An unreadable value here means the session simply runs as "build": the
        # default builder, whose ruleset is exactly what the user wrote.
        self.mode: PermissionMode = normalize_mode(config.mode) or "build"
        # "Nobody is at the keyboard": a gate becomes a refusal rather than a
        # question. A flag, not a mode, because it is true of the USER and stays
        # true whichever ruleset they are running.
        self.unattended: bool = config.unattended
        self._deny_tokens: tuple[str, ...] = config.command_deny_tokens
        # No ruleset handed in means no permissions.json anywhere, which is not
        # "no rules" - it is the shipped defaults, in full.
        self._rules: ModeRules = rules if rules is not None else default_mode_rules()
        # "Always allow" answers, evaluated after the config so they outrank it.
        # Per session rather than per mode: the user answered a question about a
        # call, not about the ruleset they happened to be in.
        self.session_rules: list[PermissionRule] = []
        if config.auto_accept_edits:
            self.session_rules.append(EDITS_RULE)

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        """The ruleset the ACTIVE mode evaluates against."""
        return self._rules.for_mode(self.mode)

    def has_deny_token(self, command: str) -> bool:
        return any(token in command for token in self._deny_tokens)

    # -- ruleset ---------------------------------------------------------------

    def target(self, spec: ToolSpec, call: ToolCall) -> tuple[str, str]:
        return permission_target(call.tool, call.params, spec.approval_kind)

    def rule_for(self, spec: ToolSpec, call: ToolCall) -> PermissionRule:
        """The rule this call resolves to (implicit "ask" when none matches)."""
        key, resource = self.target(spec, call)
        return evaluate(key, resource, self.rules, self.session_rules)

    def always_rule(self, spec: ToolSpec, call: ToolCall) -> PermissionRule:
        """The rule an "always allow" answer to this call should remember."""
        key, resource = self.target(spec, call)
        return PermissionRule(key, always_pattern(key, resource), "allow")

    def remember(self, rule: PermissionRule) -> None:
        self.session_rules.append(rule)

    def denied_rules_json(self, spec: ToolSpec, call: ToolCall) -> str:
        """The rules relevant to a denied call, as OpenCode reports them."""
        key, _ = self.target(spec, call)
        return rules_json(matching_rules(key, self.rules, self.session_rules))

    # -- the verdict -----------------------------------------------------------

    def verdict(self, spec: ToolSpec, call: ToolCall) -> Verdict:
        key, resource = self.target(spec, call)
        action = evaluate(key, resource, self.rules, self.session_rules).action
        if action == "deny":
            # A deny is a deny either way; which SENTENCE the model is told
            # depends on whether the mode is the reason (see _only_plan_denies).
            return "deny_plan" if self._only_plan_denies(key, resource) else "deny"
        if action == "allow":
            if key == "bash" and self.has_deny_token(resource):
                return self._gate()  # backstop: no shell parser here
            return "auto"
        return "auto" if self.yolo else self._gate()

    def _only_plan_denies(self, key: str, resource: str) -> bool:
        """Whether plan mode is what refused this call - i.e. `build` would not
        have. The two refusals read differently to the model ("the user is only
        exploring, here is what still works" vs "a rule forbids this"), and only
        the ruleset knows which it is: plan's overlay and a user's own deny rule
        both arrive here as the same action."""
        if self.mode != "plan":
            return False
        return evaluate(key, resource, self._rules.build, self.session_rules).action != "deny"

    def _gate(self) -> Verdict:
        """What a call that would ask the user resolves to. An unattended session
        has nobody to ask, and a question nobody answers must not become a silent
        yes."""
        return "deny_unattended" if self.unattended else "needs_approval"
