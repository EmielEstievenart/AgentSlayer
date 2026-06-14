"""UI-agnostic application layer: the session orchestrator and its view port.

Imports engine/protocol/store/config only - never Textual, ``clip``, or ``tui``
(enforced by tests/test_layering.py), so any UI can drive a session by
implementing :class:`ChatView` and feeding the controller events.
"""

from __future__ import annotations

from agentclip.app.controller import SessionController
from agentclip.app.types import SessionSpec, SessionStats
from agentclip.app.view import ChatView, SessionView

__all__ = ["ChatView", "SessionController", "SessionSpec", "SessionStats", "SessionView"]
