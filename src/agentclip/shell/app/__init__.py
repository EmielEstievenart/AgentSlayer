"""UI-agnostic application layer: the session orchestrator and its view port.

Imports engine/engine.store/protocol/config only - never Textual, the Driver's
``clip``, or ``shell.tui`` (enforced by tests/test_layering.py), so any UI can
drive a session by
implementing :class:`ChatView` and feeding the controller events.
"""

from __future__ import annotations

from agentclip.shell.app.controller import SessionController
from agentclip.shell.app.types import SessionSpec, SessionStats
from agentclip.shell.app.view import ChatView, SessionView

__all__ = ["ChatView", "SessionController", "SessionSpec", "SessionStats", "SessionView"]
