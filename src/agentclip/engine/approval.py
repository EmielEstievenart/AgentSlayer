"""ApprovalPolicy: the gate every tool call passes before it runs.

Two modes, chosen by whether a permission ruleset was loaded (config.py reads
OpenCode's opencode.json; see permissions.py):

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
everything.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import Literal

from agentclip.config import ApprovalConfig
from agentclip.permissions import (
    PermissionRule,
    always_pattern,
    evaluate,
    matching_rules,
    permission_target,
    rules_json,
)
from agentclip.protocol.types import ToolCall
from agentclip.tools.registry import ToolSpec

Verdict = Literal["auto", "needs_approval", "deny"]


class ApprovalPolicy:
    """Per-session approval state. auto_accept_edits is flipped (and sticks)
    when the user chooses Decision.APPROVE_ALL_EDITS; in legacy mode it never
    affects run_command. yolo is the bigger hammer: it answers every question
    with yes - in legacy mode that means EVERYTHING runs, in ruleset mode
    everything except what a rule explicitly denies. The /yolo chat command
    toggles it live."""

    def __init__(
        self, config: ApprovalConfig, rules: Sequence[PermissionRule] = ()
    ) -> None:
        self.auto_accept_edits: bool = config.auto_accept_edits
        self.yolo: bool = config.yolo
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
        if spec.approval_kind == "auto":
            return "auto"
        if self.yolo:
            return "auto"  # YOLO: nothing gates - edits AND commands run unattended
        if spec.approval_kind == "edit":
            return "auto" if self.auto_accept_edits else "needs_approval"
        # approval_kind == "command"
        command = call.params.get("command", "")
        return "auto" if self.command_auto_allowed(command) is not None else "needs_approval"

    def _ruleset_verdict(self, spec: ToolSpec, call: ToolCall) -> Verdict:
        key, resource = self.target(spec, call)
        action = evaluate(key, resource, self._rules, self.session_rules).action
        if action == "deny":
            return "deny"
        if action == "allow":
            if key == "bash" and self.has_deny_token(resource):
                return "needs_approval"  # backstop: no shell parser here
            return "auto"
        return "auto" if self.yolo else "needs_approval"
