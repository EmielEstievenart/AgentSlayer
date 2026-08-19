"""ApprovalPolicy: the gate every tool call passes before it runs.

Two modes, chosen by whether a permission ruleset was loaded (config.py reads
AgentClip's permissions.json, which has OpenCode's shape; see permissions.py):

RULESET MODE (rules present). The rules decide, last match wins:

    deny  -> "deny"           the call never runs and never gates - not even
                              under YOLO; a deny is the user's standing "no"
    allow -> "auto"           except a bash command carrying a deny token (see
                              below), which is downgraded to a gate
    ask   -> "needs_approval" unless YOLO, which answers every ask with "yes"

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

LEGACY MODE (no ruleset). Unchanged: read-only tools are auto, edits gate until
auto_accept_edits, commands are matched against the glob allowlist
(fnmatch.fnmatchcase against the FULL command string - auditable at a glance,
no regex backtracking) with the same deny-token backstop, and YOLO bypasses
everything. One command-kind call never reaches the allowlist: `mcp`, whose
resource is a composite tool id rather than a shell command line, always gates
here (docs/design/mcp.md section 4) - the allowlist matches shell prefixes, and
an MCP call carrying a decoy `command` param must not be able to buy itself an
allowlist hit.

THE PERMISSION MODE (both of the above) is the session-scoped dial ABOVE all of
it - "what is the user doing right now" rather than "what is this call" - and it
can only ever REFUSE more, never allow more:

    ask         everything above, exactly as written. The default.
    plan        the user is exploring: every `edit`/`command` call is denied
                before anything else looks at it (YOLO included - a mode says
                what the user wants NOW, and it outranks a flag they set
                earlier). Read-only tools are untouched, so an `ask` or `deny`
                rule on a read still applies.
    unattended  the user is away: a call that would have opened a gate is denied
                instead, because there is nobody there to answer it. Allow rules
                still run and deny rules still deny. YOLO, being the user's
                explicit "approve everything for me", still answers asks - the
                one thing it still does not answer is the deny-token backstop,
                which therefore denies here rather than gating.

Both refusals are distinct verdicts ("deny_plan"/"deny_unattended") rather than
the rule-deny "deny", because the model is told a different thing by each and
the audit trail names a different source.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import Literal

from agentclip.config import ApprovalConfig
from agentclip.executor.permissions import (
    PERMISSION_MODES,
    PermissionMode,
    PermissionRule,
    always_pattern,
    evaluate,
    matching_rules,
    normalize_mode,
    permission_target,
    rules_json,
)
from agentclip.executor.tools.registry import ToolSpec
from agentclip.protocol.types import ToolCall

Verdict = Literal["auto", "needs_approval", "deny", "deny_plan", "deny_unattended"]

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
    "PERMISSION_MODES",
    "ApprovalPolicy",
    "PermissionMode",
    "Verdict",
    "normalize_mode",
]


class ApprovalPolicy:
    """Per-session approval state. auto_accept_edits is flipped (and sticks)
    when the user chooses Decision.APPROVE_ALL_EDITS; in legacy mode it never
    affects run_command. yolo is the bigger hammer: it answers every question
    with yes - in legacy mode that means EVERYTHING runs, in ruleset mode
    everything except what a rule explicitly denies. The /yolo chat command
    toggles it live. mode is the dial above both (see the module docstring); the
    /mode command sets it, also live."""

    def __init__(
        self, config: ApprovalConfig, rules: Sequence[PermissionRule] = ()
    ) -> None:
        self.auto_accept_edits: bool = config.auto_accept_edits
        self.yolo: bool = config.yolo
        # An unreadable value here means the session simply runs as "ask": a
        # permission dial has to fail towards the mode that asks the human.
        self.mode: PermissionMode = normalize_mode(config.mode) or "ask"
        self._allowlist: tuple[str, ...] = config.command_allowlist
        self._deny_tokens: tuple[str, ...] = config.command_deny_tokens
        self._rules: tuple[PermissionRule, ...] = tuple(rules)
        # "Always allow" answers, evaluated after the config so they outrank it.
        self.session_rules: list[PermissionRule] = []

    @property
    def ruleset_mode(self) -> bool:
        """True when a permission ruleset governs this session (it REPLACES the
        allowlist rather than adding to it)."""
        return bool(self._rules)

    # -- legacy allowlist ------------------------------------------------------

    def command_auto_allowed(self, command: str) -> str | None:
        """Return the matched allowlist glob (for transcript display), or None.

        Deny tokens override: a command containing any deny token returns None
        no matter what the allowlist says.
        """
        if self.has_deny_token(command):
            return None
        for pattern in self._allowlist:
            if fnmatch.fnmatchcase(command, pattern):
                return pattern
        return None

    def has_deny_token(self, command: str) -> bool:
        return any(token in command for token in self._deny_tokens)

    # -- ruleset ---------------------------------------------------------------

    def target(self, spec: ToolSpec, call: ToolCall) -> tuple[str, str]:
        return permission_target(call.tool, call.params, spec.approval_kind)

    def rule_for(self, spec: ToolSpec, call: ToolCall) -> PermissionRule:
        """The rule this call resolves to (implicit "ask" when none matches)."""
        key, resource = self.target(spec, call)
        return evaluate(key, resource, self._rules, self.session_rules)

    def always_rule(self, spec: ToolSpec, call: ToolCall) -> PermissionRule:
        """The rule an "always allow" answer to this call should remember."""
        key, resource = self.target(spec, call)
        return PermissionRule(key, always_pattern(key, resource), "allow")

    def remember(self, rule: PermissionRule) -> None:
        self.session_rules.append(rule)

    def denied_rules_json(self, spec: ToolSpec, call: ToolCall) -> str:
        """The rules relevant to a denied call, as OpenCode reports them."""
        key, _ = self.target(spec, call)
        return rules_json(matching_rules(key, self._rules, self.session_rules))

    # -- the verdict -----------------------------------------------------------

    def verdict(self, spec: ToolSpec, call: ToolCall) -> Verdict:
        if self.ruleset_mode:
            return self._ruleset_verdict(spec, call)
        if self._plan_denied(spec):
            return "deny_plan"
        if spec.approval_kind == "auto":
            return "auto"
        if self.yolo:
            return "auto"  # YOLO: nothing gates - edits AND commands run unattended
        if spec.approval_kind == "edit":
            return "auto" if self.auto_accept_edits else self._gate()
        # approval_kind == "command". Which command-kind calls the allowlist may
        # judge is decided by the PERMISSION KEY, not by the approval kind: the
        # allowlist matches shell command lines, so only a call whose key is
        # "bash" has a resource it can meaningfully match. That keeps unknown
        # command-kind tools on the bash path (their _KIND_KEYS fallback is
        # "bash" - unchanged behaviour) while `mcp`, whose key is "mcp" and whose
        # resource is a composite tool id, always gates. Reading the resource
        # from target() rather than call.params also closes the decoy: an mcp
        # call carrying `command: git status` would otherwise have matched the
        # allowlist and auto-approved itself (docs/design/mcp.md section 4).
        key, resource = self.target(spec, call)
        if key != "bash":
            return self._gate()
        return "auto" if self.command_auto_allowed(resource) is not None else self._gate()

    def _ruleset_verdict(self, spec: ToolSpec, call: ToolCall) -> Verdict:
        key, resource = self.target(spec, call)
        action = evaluate(key, resource, self._rules, self.session_rules).action
        if action == "deny":
            return "deny"  # the user's standing "no" outranks even the mode
        if self._plan_denied(spec):
            return "deny_plan"
        if action == "allow":
            if key == "bash" and self.has_deny_token(resource):
                return self._gate()  # backstop: no shell parser here
            return "auto"
        return "auto" if self.yolo else self._gate()

    def _plan_denied(self, spec: ToolSpec) -> bool:
        """Plan mode's whole rule: nothing that CHANGES anything may run.

        By approval kind rather than by permission key, so a tool the ruleset has
        never heard of is still covered by what it does."""
        return self.mode == "plan" and spec.approval_kind in ("edit", "command")

    def _gate(self) -> Verdict:
        """What a call that would ask the user resolves to. Unattended has nobody
        to ask, and a question nobody answers must not become a silent yes."""
        return "deny_unattended" if self.mode == "unattended" else "needs_approval"
