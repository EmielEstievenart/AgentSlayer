"""The ``ChatView`` port: the narrow interface the session controller drives.

This is the seam that decouples the UI from the orchestration. ``SessionController``
holds a ``ChatView`` and never imports Textual; the Textual ``MainScreen`` implements
``ChatView`` (structurally - it does not subclass it, to avoid a metaclass clash with
Textual's ``Screen``). A future web/GUI front-end only has to implement this Protocol
and feed the controller events (``submit_clipboard``/``submit_message``/...).

Two method families:

- *Display / chrome* calls the controller makes to update what the user sees
  (transcript adds, ``render_state`` snapshot push, gate show/hide, status, toasts).
- *Blocking modal prompts* the controller awaits for a user decision
  (``prompt_new_session`` / ``confirm`` / ``prompt_text`` / ``show_summary``).

Clipboard I/O (the read-watcher and the outbound write) is deliberately a view/transport
concern - it lives behind ``copy_outbound`` / ``read_clipboard`` / ``start_input`` /
``stop_input`` so the controller stays free of any ``clip`` dependency.
"""

from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agentclip.app.types import SessionSpec
from agentclip.engine.engine import PendingAction, StatusSnapshot
from agentclip.protocol.types import Outbound, ToolCall

Severity = Literal["information", "warning", "error"]


@dataclass(frozen=True)
class SessionView:
    """Immutable snapshot of session state the controller pushes to the view.

    The view maps this onto its own widgets/reactives and repaints; it is the
    single, unidirectional channel for "the orchestration state changed".
    ``snapshot`` is the engine's ``StatusSnapshot`` (None before a session arms).
    """

    session_active: bool
    busy: bool
    pending_approval: bool
    awaiting_answer: bool
    has_outbound: bool
    snapshot: StatusSnapshot | None


class ChatView(Protocol):
    # -- transcript -----------------------------------------------------------
    async def add_user(self, text: str) -> None: ...
    async def add_prose(self, text: str) -> None: ...
    async def add_call(self, call: ToolCall) -> None: ...
    async def add_note(self, text: str) -> None: ...
    async def add_error(self, text: str) -> None: ...
    async def add_outbound(self, outbound: Outbound, label: str) -> None: ...
    async def clear_transcript(self) -> None: ...
    def has_transcript_events(self) -> bool: ...
    def render_log(self, meta_lines: list[str]) -> str: ...

    # -- state + chrome -------------------------------------------------------
    def render_state(self, view: SessionView) -> None: ...
    def show_gate(self, action: PendingAction, position: str, queue: str) -> None: ...
    def hide_gate(self) -> None: ...
    def start_working(self, label: str) -> None: ...
    def stop_working(self) -> None: ...
    def reset_composer(self) -> None: ...

    # -- notifications --------------------------------------------------------
    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None: ...
    def alert(self, message: str, severity: Severity = "information") -> None: ...

    # -- clipboard / transport ------------------------------------------------
    async def copy_outbound(self, text: str) -> None: ...
    async def read_clipboard(self) -> str | None: ...
    def start_input(self) -> None: ...
    def stop_input(self) -> None: ...

    # -- scheduling + lifecycle ----------------------------------------------
    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None: ...
    def exit_app(self) -> None: ...

    # -- blocking modal prompts ----------------------------------------------
    async def prompt_new_session(self) -> SessionSpec | None: ...
    async def confirm(self, title: str, body: str = "") -> bool: ...
    async def prompt_text(self, title: str, hint: str) -> str | None: ...
    async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str: ...
