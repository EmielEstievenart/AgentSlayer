"""GuiView: the pywebview adapter that implements the three UI ports.

The GUI's ``MainScreen``. It is the same arrangement, structurally and for the
same reasons (``docs/design/gui.md`` §0/§1): the session orchestration lives in
:class:`~agentclip.app.SessionController` and the browser automation in
:class:`~agentclip.automation.controller.AutomationController`, and this object
is the *view* - it owns both controllers, implements ``ChatView`` for the first,
``AutomationView`` + ``AutomationHost`` for the second, and turns every call
into an event on the JS bridge. State flows one way (the controller pushes a
``SessionView`` through ``render_state`` and the page repaints from it); user
input flows back through the ``js_api`` methods the runner marshals onto this
object's loop.

**Threading.** Everything below runs on one of three kinds of thread and knows
which: the GUI's asyncio loop (every ``async def``, every ``js_api``-originated
call after the runner has marshalled it), the engine's worker thread (the
``call_*`` family), and the automation's watcher/poller threads (every
``AutomationView`` paint). The last two are exactly the port methods with a
thread contract, and all of them do one thing: ``bridge.send``, which is
non-blocking and ordered by construction (``gui/bridge.py``). Nothing here
touches the page directly.

**What this slice reduces, honestly.** Slice 2 is the first live conversation,
not parity, so a handful of ``ChatView`` methods are implemented smaller than
the TUI's rather than left as a silent ``pass`` that would strand a controller
flow. Each one says so at its own definition and they are listed together in
``docs/design/gui.md`` §2: window tabs (one transcript with dividers), the
harness log pane, the ``/identify`` overlay, and the elements crops (kinds, not
pictures). Everything a *turn* passes through - the transcript, the gate, the
delivery, the watcher, the prompts - is the real thing.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from agentclip.app import SessionController, SessionSpec, SessionView
from agentclip.app.types import EngineRequest, SessionRef
from agentclip.app.view import RunCall, Severity
from agentclip.automation.controller import AutomationController, DetectorPoller
from agentclip.automation.harness_log import (
    KIND_ARMED,
    KIND_CLIPBOARD,
    KIND_SESSION,
    HarnessEntry,
)
from agentclip.automation.loop_state import LoopState
from agentclip.automation.ops import ElementClick
from agentclip.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.config import Config, ServicePreset, default_profile_dir
from agentclip.engine.engine import Decision, Engine, PendingAction
from agentclip.gui.bridge import Bridge
from agentclip.protocol.parser import looks_like_protocol
from agentclip.protocol.types import Outbound, ToolCall
from agentclip.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.screen.detector import ScreenDetector, build_detector
from agentclip.screen.focus import foreground_window
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.profile_store import load_profile
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, can_delegate, missing
from agentclip.screen.template import (
    RegionMatch,
    Template,
    find_all_in_region,
    match_rect,
    same_element,
)

# The finish-detector poll cadence, the TUI's own (tui/screens/main.py). Spelled
# here rather than imported: the two shells may not import each other, and this
# is a number the detector composition needs, not a shared decision.
_BUSY_POLL_S = 0.5

# How many matches of one appearance are worth collecting - the question a
# search answers is "one, or more than one?" (see ``find_all``).
_MAX_MATCHES = 8

# The browser windows the automation drives, as the AutomationController keys
# them. Opaque strings down there by design; the GUI has no tab bar yet, so it
# names the same two the TUI does and shows one transcript for both.
MASTER_WINDOW = "m1"
SUBAGENT_WINDOW = "m1-s1"

_WINDOW_SLOTS: dict[str, AgentSlot] = {
    MASTER_WINDOW: AgentSlot.MASTER,
    SUBAGENT_WINDOW: AgentSlot.SUBAGENT,
}

MASTER_VIEW = "master"


@dataclass(slots=True)
class LogEvent:
    """One transcript event, kept for ``render_log``'s export.

    The page holds the rendered DOM; this holds the text. Same split as the
    TUI's (``TranscriptPanel.event_log`` beside its widgets) and for the same
    reason: an export must survive whatever the display prunes, and must carry
    the verbatim payloads the rendered form collapses.
    """

    time: str
    headline: str
    body: str = ""
    fenced: bool = False


class McpStatusSource(Protocol):
    """What this view needs of the process-wide ``McpManager``.

    Structural, and stated with ``Any`` rather than ``McpServerStatus``, because
    ``agentclip.gui`` may not import ``agentclip.mcp`` (tests/test_layering.py):
    the GUI only ever reads a status row's ``name``/``state``/``detail`` and
    hands them to a toast. Displaying MCP properly is a later increment.
    """

    def statuses(self) -> Sequence[Any]: ...
    def set_status_hook(self, cb: Callable[[Any], None] | None) -> None: ...


def _fence(body: str) -> str:
    """A backtick fence longer than any backtick run inside ``body``."""
    longest = run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _call_target(call: ToolCall) -> str:
    """The one thing worth reading beside a tool name in the transcript."""
    return (
        call.params.get("path")
        or call.params.get("command")
        or call.params.get("pattern")
        or call.params.get("question")
        or ""
    )


def _gate_target(call: ToolCall) -> str:
    """The gate title's target: the param the VERDICT was computed from.

    Per tool, never "whatever params exist" - an ``mcp`` call carrying a decoy
    ``command:`` must not repaint the gate as a harmless shell line
    (docs/design/ui-briefs/main-chat.md §6, and ``ActionPanel.show_approval``).
    """
    if call.tool == "run_command":
        return call.params.get("command", "")
    if call.tool == "mcp":
        return call.params.get("tool", "")
    return call.params.get("path", "")


def _distinct_rects(
    region: ScreenRegion, found: list[tuple[Template, RegionMatch]]
) -> list[ScreenRegion]:
    """Scene-local matches as absolute rectangles, one per physical element.

    The TUI's ``_distinct_rects``, spelled again here rather than imported: the
    two shells may not import each other, and this is the fold that stops two
    IMAGES of one control reading as two windows of the same service. It moves
    down into ``agentclip.automation`` the moment the GUI grows calibration and
    there are two real callers of it (parity backlog, gui.md §2).
    """
    kept: list[ScreenRegion] = []
    for template, match in found:
        rect = match_rect(region, template, match)
        if not any(same_element(rect, other) for other in kept):
            kept.append(rect)
    return kept


class GuiView:
    """``ChatView`` + ``AutomationView`` + ``AutomationHost``, over the bridge."""

    def __init__(
        self,
        bridge: Bridge,
        *,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[EngineRequest], Engine],
        project_root: Path,
        profile_root: Path | None = None,
        mcp_manager: McpStatusSource | None = None,
        schedule: Callable[[Coroutine[Any, Any, Any]], None] | None = None,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._config = config
        self._provider = provider
        self._project_root = project_root
        self._profile_root = profile_root if profile_root is not None else default_profile_dir()
        self._profiles: dict[str, ServiceProfile] = {}
        self._mcp_manager = mcp_manager
        self._mcp_announced: set[tuple[str, str]] = set()
        # How a coroutine reaches the GUI's loop. Injected because the loop is
        # the RUNNER's (gui/runner.py) and this object must be constructible -
        # and drivable - without one.
        self._schedule = schedule if schedule is not None else _no_schedule
        self._on_exit = on_exit if on_exit is not None else _no_exit

        # -- transcript ------------------------------------------------------
        self._events: list[LogEvent] = []
        # Which session's output is being written. One transcript in this slice,
        # so this only decides whether a line is LABELLED as a sub-agent's - see
        # ``focus_session_view``.
        self._focused_session = MASTER_VIEW
        self._sessions: dict[str, SessionRef] = {}

        # -- session chrome mirrored off the last render_state ---------------
        self._last_view: SessionView | None = None
        self._awaiting_new_session = False
        self._new_session_future: asyncio.Future[SessionSpec | None] | None = None
        self._session_role = "master"
        self._session_title = ""
        self._gate_kind: str | None = None
        self._gate_always: str | None = None

        # -- blocking prompts (confirm / prompt_text / show_summary) ---------
        self._prompts: dict[str, asyncio.Future[Any]] = {}
        self._prompt_ids = itertools.count(1)

        # -- the automation core, exactly as MainScreen builds it ------------
        self._automation = AutomationController(
            view=self,
            host=self,
            services=self._initial_services(config),
            clipboard=provider,
            poll_interval_ms=config.clipboard.poll_interval_ms,
            accepts=looks_like_protocol,
            on_clipboard_captured=self._clipboard_captured,
            has_appearance=self._live_has,
            on_fire=self._fire_auto_copy,
        )
        # The detector the current poller run watches through, and the run
        # itself. Mirrors ``MainScreen._detector`` / ``_detector_worker``.
        self._detector: ScreenDetector | None = None
        self._detector_worker: DetectorPoller | None = None
        self._logged_session_active = False

        self._controller = SessionController(
            config,
            engine_factory,
            project_root,
            view=self,
            mcp_statuses=mcp_manager.statuses if mcp_manager is not None else None,
        )

    # == lifecycle =============================================================

    @property
    def controller(self) -> SessionController:
        """The session controller this view drives (the runner starts it)."""
        return self._controller

    @property
    def automation(self) -> AutomationController:
        """The automation core this view drives (the runner stops it)."""
        return self._automation

    def start(self) -> None:
        """Mount: paint the resting chrome, then let the session flow begin.

        The GUI's ``MainScreen.on_mount``. Order matters the same way: the
        window is recorded while the user is provably here (they just launched
        us), the detector composition runs once so the readout is truthful
        before any calibration exists, and the controller starts last because
        its first act is to park on ``prompt_new_session``.
        """
        self._push_status()
        self._push_state_event()
        self._remember_own_window()
        self._start_detector_worker()
        if self._mcp_manager is not None:
            self._mcp_manager.set_status_hook(self._mcp_status_hook)
        for warning in self._config.warnings:
            self.notify(warning, severity="warning", timeout=8)
        self._controller.start()

    def shutdown(self) -> None:
        """The window is closing: stop everything that touches the machine.

        The GUI's ``MainScreen.on_unmount`` - the watcher and the poller are
        plain threads the AutomationController owns, so they are stopped by
        name. Cancelling the session worker is the RUNNER's half (it owns the
        loop the flows run on), exactly as Textual's unmount cancels workers.
        """
        if self._mcp_manager is not None:
            self._mcp_manager.set_status_hook(None)
        self._automation.stop_input()
        self._stop_detector_worker()

    # == what the page asks for (js_api, already on the loop) ==================

    def page_ready(self) -> None:
        """The page installed its receiver: repaint everything it missed."""
        self._push_status()
        self._push_state_event()
        self._bridge.send("armed", armed=self._automation.os_armed)

    def submit_text(self, text: str) -> None:
        """One door for every composer send - ``MainScreen._submit_text``.

        While the start prompt is up the text IS the task, except for a slash
        line, which is dispatched as a command so ``/identify`` and friends stay
        reachable in exactly the state they are most needed (a task that really
        begins with a slash is written ``//...``). Otherwise the text goes to
        the controller, where an open ``ask_user`` gate wins over slash parsing
        unconditionally.
        """
        self._remember_own_window()  # typing here = our window has OS focus
        future = self._new_session_future
        if future is not None and not future.done():
            task = text.strip()
            if not task:
                self.notify("describe the task first", severity="warning")
                return
            if task.startswith("//"):
                task = task[1:]
            elif task.startswith("/"):
                self._controller.submit_message(task)
                return
            self.reset_composer()
            future.set_result(
                SessionSpec(
                    task=task,
                    service=self._service_for(AgentSlot.MASTER),
                    subagent_service=self._service_for(AgentSlot.SUBAGENT),
                )
            )
            return
        self._controller.submit_message(text)

    def submit_decision(self, choice: str, note: str = "") -> None:
        """The gate's three answers, mapped onto the controller's one call.

        ``approve_always`` resolves the way the TUI's ``a`` does: the ruleset's
        remembered pattern when the gate carries one, the legacy edits-only
        auto-accept at an edit gate, and nothing at all otherwise - a button the
        page should not have offered is refused rather than reinterpreted.
        """
        if choice == "approve":
            self._controller.submit_decision(Decision.APPROVE, None)
        elif choice == "reject":
            self._controller.submit_decision(Decision.REJECT, note.strip() or None)
        elif choice == "approve_always":
            if self._gate_always is not None:
                self._controller.submit_decision(Decision.APPROVE_ALWAYS, None)
            elif self._gate_kind == "edit":
                self._controller.submit_decision(Decision.APPROVE_ALL_EDITS, None)

    def cancel_execution(self) -> None:
        """ctrl+x's equivalent. The controller no-ops when nothing is running."""
        self._controller.cancel_execution()

    def answer_prompt(self, prompt_id: str, value: Any) -> None:
        """Resolve one blocking prompt. Unknown ids are ignored - a modal the
        user answered twice must not raise into the page's promise."""
        future = self._prompts.pop(prompt_id, None)
        if future is not None and not future.done():
            future.set_result(value)

    # == ChatView: transcript ==================================================

    async def add_user(self, text: str) -> None:
        self._record("you", text)
        self._bridge.send("transcript", kind="user", text=text, label=self._speaker_label("you"))

    async def add_prose(self, text: str) -> None:
        self._record("assistant", text)
        self._bridge.send(
            "transcript", kind="prose", text=text, label=self._speaker_label("assistant")
        )

    async def add_call(self, call: ToolCall) -> None:
        target = _call_target(call)
        raw = call.raw.strip("\n")
        self._record(f"tool call {call.id} - {call.tool} {target}".rstrip(), raw, fenced=True)
        self._bridge.send(
            "transcript",
            kind="call",
            call_id=call.id,
            tool=call.tool,
            target=target,
            summary=f"▶ call {call.id} {call.tool} {target}".rstrip(),
            raw=raw,
        )

    async def add_note(self, text: str) -> None:
        self._record(text)
        self._bridge.send("transcript", kind="note", text=text)

    async def add_error(self, text: str) -> None:
        self._record(f"ERROR: {text}")
        self._bridge.send("transcript", kind="error", text=text)

    async def add_outbound(self, outbound: Outbound, label: str) -> None:
        payload = outbound.chunks[0]
        note = f"→ {label} ({outbound.total_chars:,} chars)"
        self._record(f"{note} [outbound turn {outbound.turn}]", payload, fenced=True)
        self._bridge.send(
            "transcript",
            kind="outbound",
            note=note,
            turn=outbound.turn,
            chars=outbound.total_chars,
            parts=len(outbound.chunks),
            payload=payload,
        )

    async def clear_transcript(self) -> None:
        """The ``/new`` teardown, and the automation half of it in full.

        The calibrations SURVIVE (they describe where a service's windows are,
        not what the finished session said), the pointers go home to MASTER, and
        the poller is rebuilt against it - all exactly as ``MainScreen`` does it,
        because every one of those is a fact about the browser rather than about
        a widget.
        """
        self._events.clear()
        self._sessions.clear()
        self._focused_session = MASTER_VIEW
        self._automation.select_live_slot(AgentSlot.MASTER)
        self._automation.select_calibrating_slot(AgentSlot.MASTER)
        self._automation.reset_finish_trigger()
        self._automation.close_reply_gate()
        self._automation.forget_pending_insert()
        self._automation.set_loop_state(LoopState.IDLE, "session reset")
        self._automation.log_harness(
            KIND_SESSION,
            "session reset: the transcript is cleared, the calibrations and this log are not",
        )
        self._start_detector_worker()
        self.hide_paste_flash()
        self._bridge.send("transcript_clear")

    def has_transcript_events(self) -> bool:
        return bool(self._events)

    def render_log(self, meta_lines: list[str]) -> str:
        """The export, in the same markdown shape the TUI writes.

        One document rather than one per window: this shell has one transcript,
        and a delegated run appears in it under its divider, so slicing runs
        back apart (``MainScreen.render_log``) has nothing to slice yet.
        """
        lines = ["# AgentClip chat log", ""]
        lines += [f"- {m}" for m in meta_lines]
        lines += ["", "---", ""]
        body: list[str] = []
        for event in self._events:
            body.append(f"## [{event.time}] {event.headline}")
            body.append("")
            text = event.body.rstrip("\n")
            if text:
                if event.fenced:
                    fence = _fence(text)
                    body += [fence, text, fence, ""]
                else:
                    body += [text, ""]
        return ("\n".join(lines) + "\n" + "\n".join(body)).rstrip() + "\n"

    def _record(self, headline: str, body: str = "", *, fenced: bool = False) -> None:
        self._events.append(
            LogEvent(datetime.now().strftime("%H:%M:%S"), headline, body, fenced)
        )

    def _speaker_label(self, base: str) -> str:
        """Whose line this is. With one transcript, the label is the only thing
        that says a sub-agent wrote it."""
        if self._focused_session == MASTER_VIEW:
            return base
        ref = self._sessions.get(self._focused_session)
        title = ref.title if ref is not None else self._focused_session
        return f"{base} · sub-agent ‹{title}›"

    # == ChatView: session views ===============================================
    # REDUCED SCOPE (gui.md §2): the TUI mints a window tab per browser window
    # and routes each session's output into its own panel. This slice has ONE
    # transcript, so a delegated run opens with a divider, is labelled while it
    # runs, and closes with its note. Nothing can be misrouted because nothing
    # is routed - and the controller's contract (open -> focus -> ... -> finish,
    # single-flight) is satisfied exactly as written.

    async def open_session_view(self, session: SessionRef) -> None:
        self._sessions[session.id] = session
        await self.add_note(f"── task: {session.title} ──")
        self.focus_session_view(session.id)

    def focus_session_view(self, session_id: str) -> None:
        """Route every later ``add_*`` at that session. Unknown ids are recorded
        rather than refused: losing a transcript line is never worth taking a
        running session down with an exception."""
        self._focused_session = session_id
        ref = self._sessions.get(session_id)
        self._bridge.send(
            "focus_session",
            session_id=session_id,
            role=ref.role if ref is not None else "master",
        )

    async def finish_session_view(self, session_id: str, note: str, ok: bool) -> None:
        self._record(note)
        self._bridge.send("transcript", kind="note", text=note, ok=ok)

    # == ChatView: state + chrome ==============================================

    def render_state(self, view: SessionView) -> None:
        self._last_view = view
        self._session_role = view.session_role
        self._session_title = view.session_title
        if view.session_active != self._logged_session_active:
            self._logged_session_active = view.session_active
            self._automation.log_harness(
                KIND_SESSION, "session started" if view.session_active else "session ended"
            )
        # The two ways the loop settles home, read exactly as MainScreen reads
        # them: no session at all, or the turn finished interpreting and the
        # floor is back with the user (an open gate is still interpreting).
        if not view.session_active or (
            self._automation.loop_state is LoopState.INTERPRETING
            and (view.awaiting_answer or not (view.busy or view.pending_approval))
        ):
            self._automation.set_loop_state(
                LoopState.IDLE,
                "no session is running"
                if not view.session_active
                else "the turn finished and the floor is back with you",
            )
        self._push_state_event()

    def show_gate(self, action: PendingAction, position: str, queue: str) -> None:
        self._gate_kind = action.kind
        self._gate_always = action.always_pattern
        target = _gate_target(action.call)
        self._bridge.send(
            "gate",
            open=True,
            title=f"{self._gate_prefix()}APPROVE · call {position} · {action.call.tool} "
            f"{target}".rstrip(),
            position=position,
            queue=queue,
            tool=action.call.tool,
            target=target,
            kind=action.kind,
            preview=action.preview,
            auto_reason=action.auto_reason or "",
            always_pattern=action.always_pattern,
            # The third answer is offered on exactly the TUI's terms: a ruleset
            # pattern to remember, or an edit-kind gate in legacy mode. Never
            # for a run_command gate without a pattern - commands stay
            # allowlist-or-prompt (tui.md §2.4).
            always_label=self._always_label(action),
        )

    def hide_gate(self) -> None:
        self._gate_kind = None
        self._gate_always = None
        self._bridge.send("gate", open=False)

    def _gate_prefix(self) -> str:
        if self._session_role != "subagent":
            return ""
        return f"SUB-AGENT ‹{self._session_title}› · " if self._session_title else "SUB-AGENT · "

    @staticmethod
    def _always_label(action: PendingAction) -> str:
        if action.always_pattern is not None:
            what = "calls like this one" if action.always_pattern == "*" else action.always_pattern
            return f"Always: {what}"
        return "Approve + auto-edits" if action.kind == "edit" else ""

    def start_working(self, label: str, calls: Sequence[RunCall] = ()) -> None:
        self._bridge.send(
            "run",
            running=True,
            label=label,
            calls=[
                {
                    "call_id": row.call_id,
                    "tool": row.tool,
                    "detail": row.detail,
                    "streams": row.streams,
                    "glyph": row.glyph,
                }
                for row in calls
            ],
        )

    def stop_working(self) -> None:
        self._bridge.send("run", running=False)

    def reset_composer(self) -> None:
        self._bridge.send("composer_reset")

    # == ChatView: the turn executing (WORKER-THREAD callers) ==================
    # The GUI's half of the port's one thread contract. All three do exactly
    # what MainScreen's do - hand the fact to a thread-safe queue and return -
    # except that the queue is the JS bridge rather than Textual's message pump.

    def call_started(self, call_id: int, tool: str, detail: str) -> None:
        self._bridge.send("run_call", phase="started", call_id=call_id, tool=tool, detail=detail)

    def call_finished(self, call_id: int, glyph: str) -> None:
        self._bridge.send("run_call", phase="finished", call_id=call_id, glyph=glyph)

    def call_output(self, call_id: int, chunk: str) -> None:
        self._bridge.send("run_output", call_id=call_id, chunk=chunk)

    # == ChatView + AutomationView: notifications ==============================

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """A toast, from whichever thread asked for it.

        ``markup`` is accepted and ignored: it is Textual's console-markup
        switch, and the page escapes everything it renders anyway.
        """
        self._bridge.send(
            "toast",
            message=message,
            title=title,
            severity=severity,
            timeout=timeout,
        )

    def alert(self, message: str, severity: Severity = "information") -> None:
        """The attention-grabbing toast. No bell: ``config.notify.bell`` is a
        terminal escape the TUI writes and this shell has no equivalent for, so
        only the toast half is switchable here."""
        if self._config.notify.toast:
            self.notify(message, severity=severity)

    # == ChatView: clipboard / transport =======================================

    async def copy_outbound(self, text: str) -> None:
        """Deliver one outbound payload - the controller's ``copy_outbound``.

        With nothing calibrated (which is every GUI session in this slice) the
        delivery does the honest thing on its own: the payload is written to the
        real clipboard, ``chatbox_region`` answers None, no click and no
        synthetic Ctrl+V happen, and the loop lands on ``MANUAL_INSERT`` with
        the "paste it yourself" banner up. That is the existing manual path,
        reached without a second implementation of it.
        """
        await self._automation.copy_outbound(text)

    async def park_outbound(self, text: str) -> None:
        await self._automation.park_outbound(text)

    def redeliver_outbound(self, text: str) -> None:
        """The `c` double tap's second half. Scheduled rather than awaited: the
        controller is on the event loop and must not park for the seconds a
        delivery takes. The two refusals are the controller's."""
        if not self._automation.may_redeliver():
            return
        self._schedule(self._automation.copy_outbound(text))

    async def read_clipboard(self) -> str | None:
        # Deliberately not gated by the armed switch - this is the one-shot read
        # behind force-ingest, which is the user asking for their own clipboard.
        return await asyncio.to_thread(self._provider.read_text)

    def open_new_chat_now(self) -> None:
        """``/new``: click the browser's new-chat button, then reset the session.

        Best-effort by contract, exactly as in the TUI: AgentClip owns one half
        of a fresh chat and the user owns the other, so every outcome resets the
        session and a failed click spends its toast saying which half is left.
        """
        self._schedule(self._new_browser_chat(AgentSlot.MASTER))

    async def _new_browser_chat(self, slot: AgentSlot) -> None:
        outcome = await self._automation.click_profile_element(slot, TemplateKind.NEW_CHAT)
        restarted = self._controller.request_new_session() if self._session_running() else False
        if outcome is not ElementClick.CLICKED:
            tail = (
                " AgentClip is on a fresh session anyway - open a new browser chat yourself"
                if restarted
                else " Nothing on the tool side to renew - open a new browser chat yourself"
            )
            self.notify(
                f"the new-chat click did not land ({outcome.name.lower()}).{tail}",
                severity="warning",
            )
            return
        self.notify("new browser chat opened")

    def show_identify_overlay(self) -> None:
        """REDUCED SCOPE (gui.md §2): ``/identify`` draws a fullscreen overlay
        over the live chat window through a child process. The mechanism is
        shell-agnostic and carries over unchanged, but it needs a calibrated
        window to identify inside and this shell cannot draw one yet - so it
        says so rather than putting an empty overlay on the user's screen."""
        self.notify(
            "/identify is not wired into the GUI yet - it needs a drawn chat window,"
            " which lands with the calibration surface",
            severity="warning",
        )

    def toggle_harness_log(self) -> None:
        """REDUCED SCOPE (gui.md §2): the decision log exists and is being
        written (``AutomationController.harness_log``); the pane that shows it
        is a later increment. Entries also reach the page as ``harness`` events,
        so the pane is a renderer away."""
        self.notify("the harness log pane is not in the GUI yet (/log lands with the rail)")

    def set_os_armed(self, target: bool | None) -> None:
        """Arm or disarm everything that ACTS on the machine. The flag and the
        watcher are the controller's; what is left here is the chrome."""
        was_armed = self._automation.os_armed
        armed = self._automation.set_os_armed(target)
        self._automation.log_harness(
            KIND_ARMED,
            "ARMED - the tool may click, paste and watch the clipboard again"
            if armed
            else "DISARMED - watching only: no clicks, no paste, no clipboard watch",
        )
        self._push_status()
        if armed and not was_armed:
            self.notify("ARMED - automation restored")
        elif not armed:
            self.notify(
                "DISARMED - watching only: no clicks, no paste, no clipboard watch. "
                "Payloads still land on the clipboard.",
                severity="warning",
                timeout=8,
            )

    def start_input(self) -> None:
        self._automation.start_input()
        self._push_status()

    def stop_input(self) -> None:
        self._automation.stop_input()
        self._push_status()

    # == ChatView: sub-agent transport =========================================

    def delegation_available(self) -> bool:
        return can_delegate(
            self._automation.calibration(AgentSlot.SUBAGENT),
            self.profile_for(AgentSlot.SUBAGENT),
        )

    def delegation_missing(self) -> tuple[str, ...]:
        return missing(
            self._automation.calibration(AgentSlot.SUBAGENT),
            self.profile_for(AgentSlot.SUBAGENT),
        )

    async def start_chat(self, session: SessionRef) -> bool:
        slot = AgentSlot.SUBAGENT if session.role == "subagent" else AgentSlot.MASTER
        return await self._automation.start_browser_chat(slot)

    async def end_chat(self, session: SessionRef) -> None:
        self._automation.end_browser_chat()

    # == ChatView: scheduling + lifecycle ======================================

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Put a controller flow on the GUI's asyncio loop (gui/runner.py)."""
        self._schedule(coro)

    def exit_app(self) -> None:
        """Close the window, which ends ``webview.start()`` and the process."""
        self._on_exit()

    # == ChatView: blocking prompts ============================================

    async def prompt_new_session(self) -> SessionSpec | None:
        """Wait INLINE for the first message - no modal, exactly as the TUI.

        The composer switches into "describe the task" mode and this parks on a
        future the next send resolves. The controller may call it again (a
        budget-exceeded retry, ``/new``, the summary's "new session"), and each
        call re-arms the same surface.
        """
        future: asyncio.Future[SessionSpec | None] = asyncio.get_running_loop().create_future()
        self._new_session_future = future
        self._awaiting_new_session = True
        self._push_state_event()
        try:
            return await future
        finally:
            self._new_session_future = None
            self._awaiting_new_session = False
            self._push_state_event()

    async def confirm(self, title: str, body: str = "") -> bool:
        return bool(await self._modal("confirm", title=title, body=body))

    async def prompt_text(self, title: str, hint: str) -> str | None:
        answer = await self._modal("text", title=title, hint=hint)
        return answer if isinstance(answer, str) else None

    async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str:
        answer = await self._modal(
            "summary",
            title="Session summary",
            rows=[[label, value] for label, value in rows],
            summary=summary,
        )
        return answer if isinstance(answer, str) else ""

    async def _modal(self, modal: str, **fields: Any) -> Any:
        """Open one modal and park on the answer the page sends back.

        Keyed by id rather than by "the modal that is up", because the flows
        that open these are the ones an abort poisons: a stale answer must
        resolve nothing rather than resolve the next question.
        """
        prompt_id = f"p{next(self._prompt_ids)}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._prompts[prompt_id] = future
        self._bridge.send("modal", modal=modal, prompt_id=prompt_id, **fields)
        try:
            return await future
        finally:
            self._prompts.pop(prompt_id, None)
            self._bridge.send("modal_close", prompt_id=prompt_id)

    # == AutomationView ========================================================
    # Every method below may be called from the detector poller or the clipboard
    # watcher (the port's thread contract), and every one of them does the same
    # two things: build the event and queue it.

    def paint_loop_state(self, state: LoopState) -> None:
        self._bridge.send("status", loop=self._automation.loop_state.name)

    def paint_harness_entry(self, entry: HarnessEntry) -> None:
        self._bridge.send("harness", kind=entry.kind, time=entry.time, text=entry.text)

    def paint_detection(self, kind: TemplateKind, text: str) -> None:
        self._bridge.send("detection", kind=kind.name, text=text)

    def paint_stale(self, text: str) -> None:
        self._bridge.send("detection", kind="STALE", text=text)

    def paint_elements(self, crops: Mapping[TemplateKind, object]) -> None:
        """REDUCED SCOPE (gui.md §2): the kinds this tick recognised, not their
        pictures. No ``crop_elements`` is wired in, so what arrives here is the
        controller's uncut mapping of sightings - which is the honest thing to
        route while there is no elements panel to draw into. PNG data URIs per
        crop are the panel increment's job."""
        self._bridge.send(
            "elements", kinds=[kind.name for kind, seen in crops.items() if seen is not None]
        )

    def show_paste_flash(self, text: str, *, retry: bool = False) -> None:
        self._bridge.send("flash", show=True, text=text, retry=retry)

    def hide_paste_flash(self) -> None:
        self._bridge.send("flash", show=False)

    def paint_armed(self, armed: bool) -> None:
        self._bridge.send("armed", armed=self._automation.os_armed)

    # == AutomationHost ========================================================

    def live_preset(self) -> ServicePreset:
        return self._preset_for(self._automation.live_slot)

    def profile_for(self, slot: AgentSlot) -> ServiceProfile:
        return self._profile(self._service_for(slot))

    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        """Every place ``kind`` is on screen right now, in absolute coordinates.

        All of them rather than the first, and near-duplicate hits on one
        physical element folded away: an appearance belongs to the SERVICE, so a
        second window of the same service inside the drawn region carries the
        same button, and clicking whichever match came back first would click a
        different conversation's (``MainScreen._find_all``, whose implementation
        this is).

        Empty - never raised - for every way this comes up empty, which in this
        slice is always: the GUI has no calibration surface, so no chat region
        is ever drawn.
        """
        if scene is not None and slot is not None:
            raise ValueError("find_all takes a slot or a captured scene, never both")
        target = slot if slot is not None else self._automation.live_slot
        region = self._automation.calibration(target).chat_region
        if region is None:
            return []
        templates = self.profile_for(target).variants(kind)
        if not templates:
            return []
        if scene is None:
            try:
                scene = await asyncio.to_thread(capture_region, region)
            except CaptureError:
                return []
        tolerance, matcher = self._automation.live_search()
        found: list[tuple[Template, RegionMatch]] = []
        for template in templates:
            matches = await asyncio.to_thread(
                find_all_in_region,
                template,
                scene,
                tolerance=tolerance,
                max_diff=kind.max_diff,
                limit=_MAX_MATCHES,
                matcher=matcher,
            )
            found.extend((template, match) for match in matches)
        found.sort(key=lambda pair: (pair[1].y, pair[1].x))
        return _distinct_rects(region, found)

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        return await self._automation.verified_copy_click(target)

    async def ingest_harvest(self) -> None:
        """A verified copy click landed: show a non-protocol reply as prose if
        this service opted in (``ServicePreset.capture_prose``).

        Protocol-shaped harvests are left alone entirely - the watcher ingests
        those on its own, and reading them here too would ingest them twice.
        """
        if not self.live_preset().capture_prose:
            return
        try:
            text = await asyncio.to_thread(self._provider.read_text)
        except ClipboardUnavailable:
            return
        if not text or looks_like_protocol(text):
            return
        self.hide_paste_flash()
        self._automation.log_harness(
            KIND_CLIPBOARD,
            f"the harvested reply has no CLIP blocks ({len(text)} chars); "
            "capture_prose is on, so it goes to the transcript as prose",
        )
        self._automation.set_loop_state(
            LoopState.INTERPRETING, "the reply has no CLIP blocks - showing it as prose"
        )
        self._controller.submit_clipboard(text, accept_prose=True)

    def copy_seen_note(self) -> str:
        """What the always-running detector remembers about the copy button -
        the other half of a failed harvest's report."""
        detector = self._detector
        if detector is None or not detector.searches(TemplateKind.COPY):
            return ""
        ago = detector.seen_ago(TemplateKind.COPY)
        if ago is None:
            return "; the poller has never seen it in this window either"
        return f"; the poller last saw one {ago:.0f}s ago"

    def rebuild_detectors(self) -> None:
        self._start_detector_worker()

    def park_off_clipboard(self, text: str) -> None:
        """The clipboard provider refused the payload, and this shell has no
        second write channel.

        The TUI's answer is the terminal's OSC-52 escape; a WebView2 window has
        nothing like it, and writing the payload back into the page's own
        clipboard would be the same refused write one layer up. So the GUI's
        honest equivalent is to SHOW the payload where a human can select it -
        a plain block in the window - and say that the copy is theirs to make
        (docs/design/gui.md §2).
        """
        self._bridge.send("payload", text=text)
        self.notify(
            "no clipboard backend - the payload is shown in the window: select it and copy "
            "it by hand (or read it from .agentclip/sessions/<id>/outbound/)",
            severity="error",
            timeout=12,
        )

    # == the clipboard watcher's way in ========================================

    def _clipboard_captured(self, text: str) -> None:
        """The watcher thread's capture, marshalled onto the GUI's loop.

        Called from the AutomationController's watcher thread, so it does the
        one thing that is safe from there and the whole decision happens on the
        loop - the GUI's equivalent of MainScreen's ``post_message``.
        """
        self._schedule(self._ingest_capture(text))

    async def _ingest_capture(self, text: str) -> None:
        self.hide_paste_flash()
        self._automation.log_harness(
            KIND_CLIPBOARD, f"a protocol-shaped clipboard capture came in ({len(text)} chars)"
        )
        self._automation.set_loop_state(
            LoopState.INTERPRETING, "the reply arrived on the clipboard and is being parsed"
        )
        self._controller.submit_clipboard(text)

    # == the detector poller ===================================================

    def _start_detector_worker(self) -> None:
        """Compose and start one poll run against the LIVE window.

        ``MainScreen._start_detector_worker`` minus the sidebar it paints: what
        stays is every question about meaning - which detectors this composition
        runs, what a rebuild invalidates, and the run that replaces the last one.
        With no region drawn (every GUI session in this slice) it stops at the
        retarget, which is what keeps the outgoing run's in-flight probes from
        being read as the new one's.
        """
        self._stop_detector_worker()
        self._automation.retarget_detectors()
        self._automation.forget_verdicts()
        self._detector = None
        region = self._automation.live.chat_region
        if region is None:
            return
        preset = self.live_preset()
        detector = build_detector(
            region,
            self.profile_for(self._automation.live_slot),
            signals=preset.finish_signals,
            required_ticks=max(1, round(preset.stable_seconds / _BUSY_POLL_S)),
            tolerance=preset.tolerance,
            matcher=preset.matcher,
        )
        self._detector = detector
        self._automation.busy_tracker = detector.busy
        self._automation.idle_tracker = detector.idle
        self._automation.stale_tracker = detector.stale
        self._automation.active_detectors = detector.active_detectors
        if not detector.watching:
            return
        self._detector_worker = self._automation.start_detectors(
            self._automation.detector_loop(
                detector, region, capture=capture_region, poll_seconds=_BUSY_POLL_S
            )
        )

    def _stop_detector_worker(self) -> None:
        if self._detector_worker is not None:
            self._detector_worker.cancel()
            self._detector_worker = None
        self._automation.stop_detectors()

    def _live_has(self, kind: TemplateKind) -> bool:
        """Has the LIVE window's service a capture of ``kind``? Called on the
        POLLER thread, so it reads immutable state and nothing else."""
        return self.profile_for(self._automation.live_slot).has(kind)

    def _fire_auto_copy(self) -> None:
        """The finish decision says the model stopped: harvest the reply.

        Called on the poller thread, so it only SCHEDULES - the bracket that
        suspends evaluation for the flow's duration is the controller's.
        """
        self._schedule(self._automation.run_auto_copy_flow(self._automation.auto_copy_flow))

    # == MCP (a toast per transition; the readout is a later increment) ========

    def _mcp_status_hook(self, status: Any) -> None:
        """Called from the manager's loop thread. Non-blocking, never raises -
        the manager drops a listener that does, once, for good."""
        name = getattr(status, "name", "")
        state = getattr(status, "state", "")
        if state not in ("failed", "needs_auth", "connected"):
            return
        key = (name, state)
        if key in self._mcp_announced:
            return
        self._mcp_announced.add(key)
        if state == "connected":
            count = getattr(status, "tool_count", 0)
            self.notify(f"MCP server {name!r} connected · {count} tool{'' if count == 1 else 's'}")
            return
        what = "needs auth" if state == "needs_auth" else "failed"
        detail = getattr(status, "detail", "")
        self.notify(
            f"✗ MCP server {name!r} {what}{f' - {detail}' if detail else ''}",
            severity="warning",
            timeout=8,
        )

    # == chrome pushes =========================================================

    def _push_state_event(self) -> None:
        """The one state snapshot the page repaints from.

        Composed here rather than in JS because the composer's mode is the
        brief's precedence table (main-chat.md §3) and there must be exactly one
        implementation of it - the page renders what it is told.
        """
        view = self._last_view
        snap = view.snapshot if view is not None else None
        mode, placeholder, enabled = self._composer_mode(view)
        self._bridge.send(
            "state",
            session_active=bool(view and view.session_active),
            busy=bool(view and view.busy),
            pending_approval=bool(view and view.pending_approval),
            awaiting_answer=bool(view and view.awaiting_answer),
            awaiting_new_session=self._awaiting_new_session,
            has_outbound=bool(view and view.has_outbound),
            role=view.session_role if view is not None else "master",
            title=view.session_title if view is not None else "",
            phase=snap.phase.name if snap else "IDLE",
            turn=snap.turn if snap else 0,
            service=snap.service_key if snap else "",
            budget=snap.budget_chars if snap else 0,
            out_chars=snap.last_outbound_chars if snap else 0,
            permission_mode=snap.mode if snap else self._controller.permission_mode,
            yolo=bool(snap and snap.yolo),
            composer_mode=mode,
            composer_placeholder=placeholder,
            composer_enabled=enabled,
        )

    def _composer_mode(self, view: SessionView | None) -> tuple[str, str, bool]:
        """The brief's precedence table, first match wins (main-chat.md §3).

        The one deliberate divergence is the newline key: the TUI says Ctrl+J
        because Enter is its send key inside a ``TextArea``; the GUI says
        Shift+Enter, which is what every web composer means. A shell idiom, not
        drift - recorded in gui.md §2.
        """
        if self._awaiting_new_session:
            return "task", "Describe the task · Enter starts the session · Shift+Enter newline", True
        if view is None or not view.session_active:
            return "idle", "no session", False
        if view.awaiting_answer:
            return "answer", "Answer the model · Enter sends · Shift+Enter newline", True
        if view.session_role == "subagent" and not view.pending_approval:
            return "abort", "Sub-agent running · /abort ends it and tells the model", True
        if view.pending_approval:
            return "idle", "approve or reject the action above first", False
        phase = view.snapshot.phase.name if view.snapshot else "IDLE"
        if not view.busy and phase in ("AWAITING_REPLY", "DONE"):
            if phase == "DONE":
                return "done", "Task done · type a follow-up to continue", True
            return "message", "Message the model · Enter sends · Shift+Enter newline", True
        return "idle", "working - the chat box is paused", False

    def _push_status(self) -> None:
        """The status strip: where the automation loop is, and what it may do."""
        self._bridge.send(
            "status",
            loop=self._automation.loop_state.name,
            armed=self._automation.os_armed,
            watching=self._automation.watching,
            provider=self._provider.name,
            project=str(self._project_root),
        )

    # == services and profiles =================================================

    @staticmethod
    def _initial_services(config: Config) -> dict[str, str]:
        master = config.general.service
        if master not in config.services:
            master = next(iter(sorted(config.services)))
        sub = config.general.subagent_service
        return {
            MASTER_WINDOW: master,
            SUBAGENT_WINDOW: sub if sub in config.services else master,
        }

    def _service_for(self, slot: AgentSlot) -> str:
        window = next(win for win, known in _WINDOW_SLOTS.items() if known is slot)
        key = self._automation.service_of(window)
        return key if key in self._config.services else self._config.general.service

    def _preset_for(self, slot: AgentSlot) -> ServicePreset:
        return self._config.services.get(self._service_for(slot)) or self._config.preset()

    def _profile(self, key: str) -> ServiceProfile:
        """A service's captured appearances, read off disk once per app run."""
        profile = self._profiles.get(key)
        if profile is None:
            profile = load_profile(self._profile_root, key)
            self._profiles[key] = profile
        return profile

    # == small helpers =========================================================

    def _session_running(self) -> bool:
        view = self._last_view
        return bool(view and view.session_active)

    def _remember_own_window(self) -> None:
        """Record the foreground window at a moment the user is provably
        interacting with AgentClip. The HANDLE is OS state both shells snap
        focus back to, so it is kept below."""
        self._automation.set_own_window(foreground_window())


def _no_schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """The scheduler a view nobody wired a loop into gets: close the coroutine
    rather than leak it, and do nothing. Tests inject a recorder; the runner
    injects the real loop."""
    coro.close()


def _no_exit() -> None:
    """The exit a view with no window behind it gets."""
