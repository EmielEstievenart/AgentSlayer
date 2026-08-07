"""The ``ChatView`` port: the narrow interface the session controller drives.

This is the seam that decouples the UI from the orchestration. ``SessionController``
holds a ``ChatView`` and never imports Textual; the Textual ``MainScreen`` implements
``ChatView`` (structurally - it does not subclass it, to avoid a metaclass clash with
Textual's ``Screen``). A future web/GUI front-end only has to implement this Protocol
and feed the controller events (``submit_clipboard``/``submit_message``/...).

Two method families:

- *Display / chrome* calls the controller makes to update what the user sees
  (transcript adds, ``render_state`` snapshot push, gate show/hide, status, toasts).
- *Blocking prompts* the controller awaits for a user decision
  (``prompt_new_session`` / ``confirm`` / ``prompt_text`` / ``show_summary``). How the
  view asks is its own business: the Textual front-end serves ``prompt_new_session``
  inline (composer + sidebar, no modal) and the rest as modal screens.

Clipboard I/O (the read-watcher and the outbound write) is deliberately a view/transport
concern - it lives behind ``copy_outbound`` / ``read_clipboard`` / ``start_input`` /
``stop_input`` so the controller stays free of any ``clip`` dependency. Delegation is
the same shape: the controller decides *that* a sub-agent runs, the view knows *where*
(``delegation_available`` / ``start_chat`` / ``end_chat``) and *how it is shown*
(``open_session_view`` / ``focus_session_view`` / ``finish_session_view``).
"""

from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agentclip.app.types import Role, SessionRef, SessionSpec
from agentclip.engine.engine import PendingAction, StatusSnapshot
from agentclip.protocol.types import Outbound, ToolCall

Severity = Literal["information", "warning", "error"]


@dataclass(frozen=True)
class SessionView:
    """Immutable snapshot of session state the controller pushes to the view.

    The view maps this onto its own widgets/reactives and repaints; it is the
    single, unidirectional channel for "the orchestration state changed".
    ``snapshot`` is the engine's ``StatusSnapshot`` (None before a session arms).

    The three ``session_*`` fields say *whose* state this is - the master's or a
    sub-agent's. They are additive with master-shaped defaults so a view that
    predates delegation (or a test that builds a SessionView by hand) keeps
    working unchanged; a view that cares uses them to label the gate and the
    status bar, because during a sub-run every other field describes the
    sub-agent rather than the conversation the user started.
    """

    session_active: bool
    busy: bool
    pending_approval: bool
    awaiting_answer: bool
    has_outbound: bool
    snapshot: StatusSnapshot | None
    session_id: str = "master"
    session_role: Role = "master"
    session_title: str = ""


class ChatView(Protocol):
    # -- transcript -----------------------------------------------------------
    # INVARIANT: every add_* writes to the view the controller last FOCUSED (see
    # focus_session_view), never to whichever tab the user happens to be looking
    # at. Delegation is single-flight - the master is parked inside `delegate`
    # while a sub-agent runs - so exactly one session produces output at a time
    # and a session_id parameter on nine methods would buy nothing. The
    # controller's contract is the other half: focus a session view before
    # writing to it, and refocus the master when the sub-run ends.
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

    # -- session views (transcript tabs) --------------------------------------
    # open -> focus -> ... -> finish is the sub-agent run's whole view lifecycle.
    # ``finish_session_view`` only annotates and relabels: the view stays mounted
    # and readable (the panels are output-only anyway), so the user can go back
    # and read what a sub-agent did after the master has moved on.
    async def open_session_view(self, session: SessionRef) -> None: ...
    def focus_session_view(self, session_id: str) -> None: ...
    async def finish_session_view(self, session_id: str, note: str) -> None: ...

    # -- sub-agent transport --------------------------------------------------
    # ``delegation_available`` is asked BEFORE a sub-agent engine is built, so an
    # uncalibrated host answers the model with an error result instead of
    # stranding a half-started run. ``start_chat`` is all-or-nothing: False must
    # mean nothing was clicked and nothing was retargeted, because pasting a
    # sub-agent's bootstrap into the master's chat would corrupt it irrecoverably.
    def delegation_available(self) -> bool: ...

    # Why the gaps come back as data rather than a rendered sentence: the model
    # is told exactly what is missing when it calls `delegate` against an
    # uncalibrated host, and only the view knows what a "new-chat button" is -
    # the controller must not import the screen layer to find out.
    def delegation_missing(self) -> tuple[str, ...]: ...
    async def start_chat(self, session: SessionRef) -> bool: ...
    async def end_chat(self, session: SessionRef) -> None: ...

    # -- scheduling + lifecycle ----------------------------------------------
    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None: ...
    def exit_app(self) -> None: ...

    # -- blocking prompts -----------------------------------------------------
    # prompt_new_session returns the spec for the session to start, or None if the
    # user wants out (the controller then exits the app).
    async def prompt_new_session(self) -> SessionSpec | None: ...
    async def confirm(self, title: str, body: str = "") -> bool: ...
    async def prompt_text(self, title: str, hint: str) -> str | None: ...
    async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str: ...
