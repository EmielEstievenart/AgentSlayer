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

...and one method family with a thread contract of its own: ``call_started`` /
``call_finished`` / ``call_output`` are pushed from the engine's worker thread
while a turn executes (see them below).

Clipboard I/O (the read-watcher and the outbound write) is deliberately a view/transport
concern - it lives behind ``copy_outbound`` / ``read_clipboard`` / ``start_input`` /
``stop_input`` so the controller stays free of any ``clip`` dependency. Delegation is
the same shape: the controller decides *that* a sub-agent runs, the view knows *where*
(``delegation_available`` / ``start_chat`` / ``end_chat``) and *how it is shown*
(``open_session_view`` / ``focus_session_view`` / ``finish_session_view``).
"""

from __future__ import annotations

from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agentclip.engine.engine import PendingAction, StatusSnapshot
from agentclip.protocol.types import Outbound, ToolCall
from agentclip.shell.app.types import Role, SessionRef, SessionSpec

Severity = Literal["information", "warning", "error"]


@dataclass(frozen=True, slots=True)
class RunCall:
    """One row of the run panel: a call this turn is about to make (or made).

    Handed over as a whole list when the turn starts executing, so the panel can
    show what is QUEUED and not merely what is running - the question a user
    staring at a five-minute build actually has is "and what comes after this?".
    ``detail`` is the one thing worth reading next to the tool name (the command
    line, the path), already flattened and clipped by the controller: the view
    renders, it does not decide what matters. ``glyph`` is the row's state as of
    now, which is only ever non-pending when a parked turn resumes (an ask_user
    answered mid-plan re-shows the panel with the earlier calls already done).
    ``streams`` marks the rows that can carry live output - run_command's.
    """

    call_id: int
    tool: str
    detail: str
    streams: bool = False
    glyph: str = "•"


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
    def start_working(self, label: str, calls: Sequence[RunCall] = ()) -> None: ...
    def stop_working(self) -> None: ...
    def reset_composer(self) -> None: ...

    # -- the turn executing, call by call -------------------------------------
    # THREAD CONTRACT, and it is the only place in this port with one: these
    # three are called from the engine's WORKER THREAD, mid-``execute()``, not
    # from the loop the rest of the port lives on. They are the only way the
    # user learns anything before the whole batch finishes, and the engine
    # cannot report from anywhere else. So an implementation must be non-
    # blocking and thread-safe - the Textual front-end posts a message and
    # returns - and must tolerate an id it has never heard of (a call the
    # controller did not plan) and one it has already resolved (a parked turn
    # resuming). Everything they say is redundant with what the results payload
    # will say at the end; nothing may depend on them having arrived.
    def call_started(self, call_id: int, tool: str, detail: str) -> None: ...
    def call_finished(self, call_id: int, glyph: str) -> None: ...
    def call_output(self, call_id: int, chunk: str) -> None: ...

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
    # ``copy_outbound`` is the whole DELIVERY of a payload, not a clipboard
    # write: the view parks the text, clicks the chat box, pastes it and - for a
    # service that opted in - taps Enter. Every composed outbound goes out this
    # way and there is exactly one implementation of it.
    async def copy_outbound(self, text: str) -> None: ...
    async def read_clipboard(self) -> str | None: ...

    # The two halves of the `c` re-copy (tui.md 3.4a), split precisely because
    # the call above does so much. A user pressing `c` has asked for their
    # payload back on the CLIPBOARD; they have not asked for the mouse to move
    # into the browser and paste a second copy on top of whatever is in the box.
    # So the first press parks the text and stops there...
    async def park_outbound(self, text: str) -> None: ...

    # ...and only a second press inside the double-tap window escalates to the
    # real thing, which is `copy_outbound` again and nothing else - a re-delivery
    # that skipped the stream mode or the auto-submit tap would be a second,
    # drifting send path. Never blocking (it returns as soon as the work is
    # scheduled), for the same reason ``open_new_chat_now`` is not: the delivery
    # clicks, settles, pastes and may stream for several seconds, and the
    # controller must not be parked on it. It refuses - with its own toast - in
    # the states only the view can see (disarmed, an auto-copy flow already
    # driving the mouse), exactly as the sidebar's retry button does.
    def redeliver_outbound(self, text: str) -> None: ...

    # /new wants the next conversation in a FRESH browser chat, and only the
    # view knows what a new-chat button is - so the controller states the intent
    # and the view does the whole thing, right now: find the control, click it,
    # hand focus back. Never blocking (it returns as soon as the work is
    # scheduled). The view calls ``request_new_session`` itself, and does so
    # whether or not the click landed: the browser half needs a calibrated,
    # armed, findable button and the tool half needs nothing, so tying the one
    # AgentClip can always do to the one it often cannot left /new inert in the
    # states it is reached for (tui.md section 3.3a). The toast there is what
    # tells the user the browser chat is still theirs to open.
    # That is the same path the sidebar's "New browser chat" takes, deliberately:
    # two ways to ask for one thing, one implementation of it.
    def open_new_chat_now(self) -> None: ...

    # /identify's whole implementation, for the same reason: the controller can
    # say "show the user what you can see", and nothing more - where the tool is
    # looking, what it is looking for and how a rectangle gets drawn on a real
    # screen are all the view's. Never blocking: the overlay it puts up owns the
    # screen for a few seconds, and the controller must not be parked on it.
    def show_identify_overlay(self) -> None: ...

    # /log's whole implementation, for the third time and the same reason: the
    # controller can say "show the user why you did what you did", and the
    # decisions themselves are all the view's - the paste attempt, the send
    # gate, the finish detectors and the auto-copy flow live on the far side of
    # this port, so the log they write does too. Never blocking: it flips a pane
    # along the bottom of the screen, which is also why it TOGGLES - the same
    # command puts it away, and F8 is the same call.
    def toggle_harness_log(self) -> None: ...

    # The global ARMED switch (`/armed`, F5). DISARMED means the tool stops
    # ACTING on the world - no clicks, no synthetic paste, no cursor moves, no
    # focus stealing, no clipboard watching - while every read-only half
    # (capture, the finish detectors, the whole sidebar readout) stays live.
    #
    # Not the session's, unlike `/yolo`: the engine and the session have nothing
    # to hold here, so this side of the port only ever FORWARDS the intent. The
    # flag itself lives in ``AutomationController`` - one armed switch below
    # every shell rather than one per frontend (docs/design/gui.md section 1) -
    # which is why this still takes a target and returns nothing: the session
    # controller does not mirror the flag and cannot get it out of step.
    # ``None`` means toggle, and it is the bare `/armed` (and F5).
    def set_os_armed(self, target: bool | None) -> None: ...

    def start_input(self) -> None: ...
    def stop_input(self) -> None: ...

    # -- session views (transcript tabs) --------------------------------------
    # open -> focus -> ... -> finish is the sub-agent run's whole view lifecycle.
    # ``finish_session_view`` only annotates and relabels: the view stays mounted
    # and readable (the panels are output-only anyway), so the user can go back
    # and read what a sub-agent did after the master has moved on.
    #
    # ``ok`` is that run's outcome, and it is a parameter because the view
    # cannot work it out: every ending - a result handed back, a refused chat, a
    # bootstrap over budget, an abort, a crash - arrives through the controller's
    # one `finally`, so a view left to guess labels failures as successes. Plain
    # data, like every other argument here: the view decides what a failed run
    # LOOKS like (a glyph, a colour), the controller decides what happened.
    async def open_session_view(self, session: SessionRef) -> None: ...
    def focus_session_view(self, session_id: str) -> None: ...
    async def finish_session_view(self, session_id: str, note: str, ok: bool) -> None: ...

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
