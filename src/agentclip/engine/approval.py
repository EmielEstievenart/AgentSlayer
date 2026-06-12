"""ApprovalPolicy: the allowlist gate for commands and the edit-approval flag.

Allowlist matching is glob (fnmatch.fnmatchcase) against the FULL command
string - auditable at a glance, no regex backtracking. Hard backstop: a
command containing any configured deny token ALWAYS needs approval, even when
a glob matches (stops ``pytest tests; rm -rf ~`` riding ``pytest*``).
"""

from __future__ import annotations

import fnmatch
from typing import Literal

from agentclip.config import ApprovalConfig
from agentclip.protocol.types import ToolCall
from agentclip.tools.registry import ToolSpec

Verdict = Literal["auto", "needs_approval"]


class ApprovalPolicy:
    """Per-session approval state. auto_accept_edits is flipped (and sticks)
    when the user chooses Decision.APPROVE_ALL_EDITS; it never affects
    run_command."""

    def __init__(self, config: ApprovalConfig) -> None:
        self.auto_accept_edits: bool = config.auto_accept_edits
        self._allowlist: tuple[str, ...] = config.command_allowlist
        self._deny_tokens: tuple[str, ...] = config.command_deny_tokens

    def command_auto_allowed(self, command: str) -> str | None:
        """Return the matched allowlist glob (for transcript display), or None.

        Deny tokens override: a command containing any deny token returns None
        no matter what the allowlist says.
        """
        for token in self._deny_tokens:
            if token in command:
                return None
        for pattern in self._allowlist:
            if fnmatch.fnmatchcase(command, pattern):
                return pattern
        return None

    def verdict(self, spec: ToolSpec, call: ToolCall) -> Verdict:
        if spec.approval_kind == "auto":
            return "auto"
        if spec.approval_kind == "edit":
            return "auto" if self.auto_accept_edits else "needs_approval"
        # approval_kind == "command"
        command = call.params.get("command", "")
        return "auto" if self.command_auto_allowed(command) is not None else "needs_approval"
