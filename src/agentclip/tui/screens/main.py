"""MainScreen: the Textual adapter that implements the ChatView port.

The session orchestration lives in :class:`agentclip.app.SessionController` (UI-
agnostic, no Textual). This screen is the *view*: it owns the layout, widgets,
key bindings, the clipboard watcher + outbound copy (clipboard is a transport
concern), and implements every ``ChatView`` method the controller calls. State
flows one way - the controller pushes a :class:`SessionView` snapshot via
``render_state`` and this screen maps it onto its reactives and repaints; user
input flows back as controller events (``submit_clipboard`` / ``submit_message`` /
``submit_decision`` / the ``action_*`` delegations).

Session start is *inline*, not modal: ``prompt_new_session`` arms the composer and
the sidebar's service picker and parks on an ``asyncio.Future`` that the first
composer send resolves into a ``SessionSpec``. The same path serves ``/new`` and
the summary screen's "new session" choice, so there is exactly one way to start.

The transcript is a ``TabbedContent``: one pane per *session view*. The master's
is always there; a delegated sub-agent run gets its own (``open_session_view``)
and keeps it, ticked, once it finishes. Which pane the controller writes into is
``_focused_panel``, moved only by ``focus_session_view`` - never by the user
clicking a tab. That split is the point: the user can read a finished
sub-agent's transcript while the master keeps working, and live output still
lands where it belongs instead of in whatever tab happens to be visible.

Threading: the clipboard watcher is a ``run_worker(thread=True)`` that bridges
captures via the thread-safe ``post_message(ClipboardCaptured)`` -> the controller.
The controller's flow coroutines run via ``spawn`` (also ``run_worker``), so Textual
cancels everything on unmount. The finish detectors (sidebar's "Set busy
region..." / "Set idle button..." / "Set response region...") are the same
shape: one thread worker polling whichever detectors are calibrated and
bridging ``BusyProbed`` / ``IdleProbed`` / ``StaleProbed`` to the sidebar.

Their combined verdict drives the copy-button auto-click (``_evaluate_finish``):
the busy element was calibrated mid-generation so MATCH there means "still
generating", the idle element was calibrated while idle so MATCH there means
"finished", and the stale detector needs no calibration pixels at all - the
response region reading unchanged for the preset's ``stable_seconds`` means
"finished" (the service-agnostic fallback for chats whose busy/idle cues are
unreliable). Any detector saying "generating" arms the trigger; the trigger
fires only once EVERY calibrated detector says "finished" on two consecutive
polls - with several calibrated that agreement is the whole point of having
more than one. Firing runs ``_auto_copy_flow``: click the live chat input box,
scroll to the bottom, find the newest (lowest) copy-button icon in a vertical
band - falling back to a hover scan for chats that only render the icon under
the pointer - click it, and let the clipboard watcher ingest the copy. The
flow itself scrolls and hover-scans the very screen the stale detector
watches, so evaluation is suspended while it runs (``_flow_running``) and the
stale tracker forgets its history when the flow ends - without that the flow
would read as a fresh generation and re-fire itself forever.

The chat input box is not a stored location at all any more: the two layouts a
service can show (a fresh chat centres its box, an ongoing one docks it at the
bottom) are *appearances* captured once per service (``screen.profile``) and
searched for INSIDE the drawn chat region on the spot (``_find``). Moving or
resizing the browser therefore costs nothing. The new-chat button is still a
``CalibratedElement`` (region + snapshot) - "is this still the thing I was
pointed at?" - until it moves the same way.

Every one of those calibrations belongs to an *agent slot*
(:mod:`agentclip.screen.slot`), not to the screen: MASTER is the chat the
session runs in, SUBAGENT the second window a delegated sub-agent gets. Two
independent pointers say what happens to which slot - ``_calibrating`` is what
the sidebar's pickers write into, ``_live`` is what the automation drives right
now - because the user must be able to calibrate the sub-agent window while the
master chat is mid-turn. ``start_browser_chat``/``end_browser_chat`` are the
only things that move ``_live``, and ``start_browser_chat`` is all-or-nothing on
purpose: it retargets the automation *only* after a verified click landed, so a
False return guarantees nothing was clicked and nothing was retargeted - a
sub-agent bootstrap pasted into the master chat would corrupt that conversation
irrecoverably.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Footer, Input, TabbedContent, TabPane
from textual.worker import Worker, get_current_worker

from agentclip.app import SessionController, SessionSpec, SessionView
from agentclip.app.types import EngineRequest, SessionRef
from agentclip.app.view import Severity
from agentclip.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.clip.watcher import SelfWriteSet, watch, write_via
from agentclip.config import Config, ServicePreset
from agentclip.engine.engine import Decision, Engine, PendingAction, StatusSnapshot
from agentclip.protocol.parser import looks_like_protocol
from agentclip.protocol.types import Outbound, ToolCall
from agentclip.screen.busy import BusyProbe, BusyState, probe_busy
from agentclip.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.screen.element import CalibratedElement, probe_element
from agentclip.screen.focus import (
    click_region,
    focus_window,
    foreground_window,
    move_cursor,
    scroll_region,
    send_paste,
)
from agentclip.screen.hover import STEP_DELAY_S as _HOVER_STEP_DELAY_S
from agentclip.screen.hover import hover_scan_points
from agentclip.screen.picker import ScreenPickError, pick_region
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.profile_store import ProfileStoreError, load_profile, save_template
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import (
    AgentSlot,
    SlotCalibration,
    can_delegate,
    missing,
    new_slots,
)
from agentclip.screen.stale import StaleProbe, StaleState, StaleTracker
from agentclip.screen.template import (
    RegionMatch,
    Template,
    find_in_region,
    find_lowest_in_region,
    match_rect,
)
from agentclip.tui.messages import BusyProbed, ClipboardCaptured, IdleProbed, StaleProbed
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.summary import SummaryScreen
from agentclip.tui.screens.text_entry import TextEntryScreen
from agentclip.tui.widgets.action_panel import ActionPanel
from agentclip.tui.widgets.composer import ChatComposer
from agentclip.tui.widgets.running_bar import RunningBar
from agentclip.tui.widgets.sidebar import (
    BUSY_CALIBRATED,
    ENTER_FLASH_TEXT,
    IDLE_CALIBRATED,
    PASTE_FLASH_TEXT,
    STALE_CALIBRATED,
    TEMPLATE_UNSET,
    Sidebar,
    slot_note,
)
from agentclip.tui.widgets.statusbar import StatusBar
from agentclip.tui.widgets.transcript import TranscriptPanel

if TYPE_CHECKING:  # only for the action_settings hand-off; importing it for real would cycle
    from agentclip.tui.app import AgentClipApp

# Finish-detector poll cadence (tests monkeypatch this to something tiny).
_BUSY_POLL_S = 0.5
# Hover pause before clicking a calibrated element, for the same reason the copy
# click settles: web UIs paint their buttons on hover.
_ELEMENT_CLICK_SETTLE_S = 0.05
# Beat between opening a fresh browser chat and treating it as the live slot -
# the page still has to render its (centred) input box. Tests shrink this.
_NEW_CHAT_SETTLE_S = 0.4

# The session view that always exists: the conversation the user started. Its
# widget ids are the pre-tabs ones (``#transcript``) so every existing selector
# - and every test that reaches for the transcript - keeps resolving.
MASTER_VIEW = "master"


def _pane_id(view_id: str) -> str:
    return f"tab-{view_id}"


def _panel_id(view_id: str) -> str:
    return "transcript" if view_id == MASTER_VIEW else f"tr-{view_id}"


class ElementClick(Enum):
    """Outcome of the verify-then-click primitive (``_click_calibrated_element``).

    Three states, not a bool: "the element no longer looks like its snapshot"
    (nothing was clicked - the user must recalibrate) is a different story to
    tell than "we clicked and the OS refused" (Windows-only input).
    """

    CLICKED = "clicked"
    MISMATCH = "mismatch"  # verified against the snapshot and refused to click
    NOT_CLICKED = "not_clicked"  # verified fine, but the click did not land


def _fmt_k(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


# Leading state glyphs the watcher segment prefixes its text with; a sub-agent
# run replaces them with its own, so they are stripped before rebadging.
_STATE_GLYPHS = "●○■✓✗"


def _strip_glyph(text: str) -> str:
    return text.lstrip(_STATE_GLYPHS).lstrip()


def _format_busy_probe(probe: BusyProbe) -> str:
    """Unmistakable readout for the sidebar - this is the whole deliverable."""
    if probe.state is BusyState.ERROR:
        return "✗ capture failed"
    pct = f"{(probe.diff or 0.0) * 100:.1f}%"
    if probe.state is BusyState.MATCH:
        return f"● GENERATING · match (diff {pct})"
    return f"○ response ready · changed (diff {pct})"


def _format_idle_probe(probe: BusyProbe) -> str:
    """Same readout for the idle element, with the polarity flipped: it was
    calibrated while the chat was idle, so MATCH is the *finished* verdict."""
    if probe.state is BusyState.ERROR:
        return "✗ capture failed"
    pct = f"{(probe.diff or 0.0) * 100:.1f}%"
    if probe.state is BusyState.MATCH:
        return f"○ response ready · match (diff {pct})"
    return f"● GENERATING · changed (diff {pct})"


def _format_stale_probe(probe: StaleProbe) -> str:
    """Same unmistakable readout for the stale detector: the response region
    still moving means the model is still typing; long enough unchanged means
    the answer is done. ``still ×N`` shows the streak building toward STALE."""
    if probe.state is StaleState.ERROR:
        return "✗ capture failed"
    if probe.state is StaleState.STALE:
        return f"○ response ready · stale (still ×{probe.stable_ticks})"
    pct = f"{(probe.diff or 0.0) * 100:.2f}%"
    return f"● GENERATING · changing (diff {pct} · still ×{probe.stable_ticks})"


def _busy_verdict(probe: BusyProbe) -> bool | None:
    """The busy element's probe as a finish verdict: True = finished,
    False = generating, None = no verdict (capture error).

    It was calibrated WHILE the model was generating, so a MATCH means the
    generation is still going.
    """
    if probe.state is BusyState.ERROR:
        return None
    return probe.state is BusyState.CHANGED


def _idle_verdict(probe: BusyProbe) -> bool | None:
    """The idle element's probe as a finish verdict, same three values.

    It was calibrated while the chat was IDLE, so a MATCH means the response
    has finished - the exact inverse of the busy element.
    """
    if probe.state is BusyState.ERROR:
        return None
    return probe.state is BusyState.MATCH


def _stale_verdict(probe: StaleProbe) -> bool | None:
    """The stale tracker's probe as a finish verdict, same three values.

    STALE (unchanged long enough) means finished; CHANGING - including the
    settling polls before the streak completes - means generating.
    """
    if probe.state is StaleState.ERROR:
        return None
    return probe.state is StaleState.STALE


class MainScreen(Screen[None]):
    BINDINGS = [
        Binding("y", "approve", "approve"),
        Binding("n", "reject", "reject"),
        Binding("a", "auto_edits", "auto-edits"),
        Binding("u", "undo", "undo"),
        Binding("c", "recopy", "re-copy"),
        Binding("i", "force_ingest", "ingest"),
        Binding("w", "toggle_watch", "watcher"),
        Binding("t", "follow_up", "type message"),
        Binding("e", "end_session", "summary"),
        Binding("l", "export_log", "export log"),
        Binding("x", "toggle_last", "expand last", show=False),
        # f3 is priority so it works while the composer (a TextArea) holds focus.
        Binding("f3", "toggle_sidebar", "sidebar", priority=True),
        # Browse the transcript tabs by keyboard. f4/f2/f1 are the app's, f3 is
        # the sidebar's, so f6 it is. Priority for the same reason as f3 (the
        # composer is a TextArea and would otherwise eat it), and show=False
        # because it is only meaningful once a sub-agent tab exists.
        Binding("f6", "next_chat_tab", "next chat", priority=True, show=False),
        # Priority for the same reason, and because the whole point is to reach
        # it while a long tool call has the screen otherwise inert. ctrl+x is
        # free here (f1-f4 are taken; the TextArea's own ctrl+x cut would
        # otherwise swallow it, hence priority).
        Binding("ctrl+x", "cancel_execution", "cancel run", priority=True),
        Binding("ctrl+s", "submit_composer", "send", priority=True, show=False),
        Binding("ctrl+enter", "submit_composer", "send", priority=True, show=False),
        Binding("escape", "cancel_entry", "cancel", show=False),
    ]

    pending_approval: reactive[bool] = reactive(False, bindings=True)
    awaiting_answer: reactive[bool] = reactive(False, bindings=True)
    busy: reactive[bool] = reactive(False, bindings=True)
    executing: reactive[bool] = reactive(False, bindings=True)  # tool calls in flight
    session_active: reactive[bool] = reactive(False, bindings=True)
    awaiting_new_session: reactive[bool] = reactive(False, bindings=True)
    phase_name: reactive[str] = reactive("IDLE", bindings=True)
    watch_paused: reactive[bool] = reactive(False, bindings=True)
    reject_open: reactive[bool] = reactive(False, bindings=True)
    has_outbound: reactive[bool] = reactive(False, bindings=True)
    # True for the whole of a delegated sub-agent run. The master's flow is busy
    # throughout, which would normally disable the composer - but /abort is the
    # only way out of a sub-run, so the box has to stay reachable.
    sub_running: reactive[bool] = reactive(False, bindings=True)

    def __init__(
        self,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[EngineRequest], Engine],
        project_root: Path,
        profile_root: Path,
    ) -> None:
        super().__init__()
        self._config = config
        self._provider = provider
        self._project_root = project_root
        # Where each service's captured appearances live on disk, and the
        # per-run cache of the ones already read back (see ``_profile``).
        self._profile_root = profile_root
        self._profiles: dict[str, ServiceProfile] = {}
        self._self_writes = SelfWriteSet()
        self._watch_worker: Worker[None] | None = None
        self._snap: StatusSnapshot | None = None  # mirrors SessionView.snapshot (read by tests)
        self._gate_kind: str | None = None  # the in-flight gate's kind, for a/check_action
        # Mirrors SessionView.session_role/title: whose session the chrome is
        # currently describing (see render_state).
        self._session_role = "master"
        self._session_title = ""
        # Resolved by the first composer send while waiting for a new session.
        self._new_session_future: asyncio.Future[SessionSpec | None] | None = None
        # One transcript panel per session view, keyed by SessionRef.id: the
        # master's is always present (mounted by compose, registered on mount),
        # each sub-agent run adds and keeps one. ``_focused_panel`` is where the
        # CONTROLLER's output goes - deliberately NOT the tab the user is
        # looking at, so reading an old sub-agent tab can never misroute live
        # output into it (see ``transcript``).
        self._panels: dict[str, TranscriptPanel] = {}
        self._focused_panel = MASTER_VIEW
        self._sessions: dict[str, SessionRef] = {}  # view id -> its identity, for labels/export
        # Every user-drawn calibration, one set per agent slot: the chat window
        # itself (where appearances are searched for, the last-resort click
        # target, and the vertical span of the copy-button search band), the
        # two finish detectors, the copy button and the new-chat button.
        # Calibrations describe where the service's windows are, not what one
        # conversation said, so they survive /new; only the pointers below
        # reset. What the service LOOKS like lives in ``_profiles`` instead -
        # captured once, shared by both slots, persisted across runs.
        #
        # ``_calibrating`` is the slot the sidebar's pickers write into;
        # ``_live`` is the slot the automation (paste click, detector poller,
        # auto-copy) drives. They move independently: the sub-agent window is
        # calibrated while the master chat is mid-session.
        self._slots: dict[AgentSlot, SlotCalibration] = new_slots()
        self._calibrating: AgentSlot = AgentSlot.MASTER
        self._live: AgentSlot = AgentSlot.MASTER
        self._delegation_ready = False  # last-seen SUBAGENT can_delegate, for the one-shot toast
        self._region_click_warned = False
        self._detector_worker: Worker[None] | None = None
        # Latest verdict per detector: True = finished, False = generating,
        # None = capture error. ``_seen`` is what makes a detector count toward
        # the combined verdict, so a detector that has never reported cannot
        # veto (or fake) a finish.
        self._busy_seen = False
        self._idle_seen = False
        self._stale_seen = False
        self._busy_finished: bool | None = None
        self._idle_finished: bool | None = None
        self._stale_finished: bool | None = None
        # The live stale tracker, kept on self so the auto-copy flow's finally
        # can reset it: the flow scrolls the very region the tracker watches,
        # so the frame (and streak) it leaves behind must not count as history.
        self._stale_tracker: StaleTracker | None = None
        # True from the moment _evaluate_finish fires the auto-copy flow until
        # the flow's finally: evaluation is suspended meanwhile, because the
        # flow's own scrolling/hover-scanning reads as "generating" to the
        # stale detector and would re-arm and re-fire the trigger forever.
        self._flow_running = False
        # ``_copy_armed``/``_copy_changed_streak`` track the busy-probe sequence
        # that fires the auto-copy flow - see ``_evaluate_finish``. Trigger
        # state, not calibration, so it lives on the screen and is reset
        # whenever the live slot moves.
        self._copy_armed = False
        self._copy_changed_streak = 0
        # Our own terminal window, refreshed while the user is demonstrably
        # typing here - the auto-copy flow snaps focus back to it after
        # clicking the browser's copy button. Not session-scoped: the terminal
        # outlives /new.
        self._own_window: int | None = None
        # One overlay at a time, across ALL pickers: cancelling an exclusive
        # worker cannot kill the blocking child overlay process it spawned, so
        # extra button presses are refused up front instead.
        self._picker_open = False
        self._controller = SessionController(config, engine_factory, project_root, view=self)

    # -- slots ----------------------------------------------------------------

    @property
    def calibrating(self) -> SlotCalibration:
        """The slot the sidebar's calibration buttons write into."""
        return self._slots[self._calibrating]

    @property
    def live(self) -> SlotCalibration:
        """The slot the automation drives right now (paste click, finish
        detector, auto-copy). Only ``start_browser_chat``/``end_browser_chat``
        move it - everything else reads it."""
        return self._slots[self._live]

    # Compatibility proxies onto the MASTER slot. The single-window vocabulary
    # (``_chat_region``, ``_copy_template``, ...) predates slots and is what the
    # region/chatbox/copy/newchat Pilot tests poke; keeping it as read/write
    # views of MASTER means the migration to slots changed no test at all.
    @property
    def _chat_region(self) -> ScreenRegion | None:
        return self._slots[AgentSlot.MASTER].chat_region

    @_chat_region.setter
    def _chat_region(self, value: ScreenRegion | None) -> None:
        self._slots[AgentSlot.MASTER].chat_region = value

    @property
    def _busy_region(self) -> ScreenRegion | None:
        return self._slots[AgentSlot.MASTER].busy_region

    @_busy_region.setter
    def _busy_region(self, value: ScreenRegion | None) -> None:
        self._slots[AgentSlot.MASTER].busy_region = value

    @property
    def _busy_baseline(self) -> RegionImage | None:
        return self._slots[AgentSlot.MASTER].busy_baseline

    @_busy_baseline.setter
    def _busy_baseline(self, value: RegionImage | None) -> None:
        self._slots[AgentSlot.MASTER].busy_baseline = value

    @property
    def _idle_region(self) -> ScreenRegion | None:
        return self._slots[AgentSlot.MASTER].idle_region

    @_idle_region.setter
    def _idle_region(self, value: ScreenRegion | None) -> None:
        self._slots[AgentSlot.MASTER].idle_region = value

    @property
    def _idle_baseline(self) -> RegionImage | None:
        return self._slots[AgentSlot.MASTER].idle_baseline

    @_idle_baseline.setter
    def _idle_baseline(self, value: RegionImage | None) -> None:
        self._slots[AgentSlot.MASTER].idle_baseline = value

    @property
    def _stale_region(self) -> ScreenRegion | None:
        return self._slots[AgentSlot.MASTER].stale_region

    @_stale_region.setter
    def _stale_region(self, value: ScreenRegion | None) -> None:
        self._slots[AgentSlot.MASTER].stale_region = value

    @property
    def _newchat(self) -> CalibratedElement | None:
        return self._slots[AgentSlot.MASTER].new_chat

    @_newchat.setter
    def _newchat(self, value: CalibratedElement | None) -> None:
        self._slots[AgentSlot.MASTER].new_chat = value

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Chat column on the left, settings sidebar on the right; the status bar
        # and the footer stay full width underneath both.
        with Horizontal(id="body"):
            with Vertical(id="main-col"):
                # One tab per session view. A single tab (the master's) is the
                # normal case and reads as a plain title bar; a delegation adds
                # a second one and the user can flip between them.
                with (
                    TabbedContent(id="chats"),
                    TabPane("master", id=_pane_id(MASTER_VIEW)),
                ):
                    yield TranscriptPanel(id=_panel_id(MASTER_VIEW))
                yield ActionPanel(id="action")
                yield RunningBar(id="running")
                yield ChatComposer(id="composer")
            yield Sidebar(self._config, self._project_root, id="sidebar")
        yield StatusBar(id="statusbar")
        yield Footer()

    @property
    def transcript(self) -> TranscriptPanel:
        """The panel the controller's output goes into right now.

        The *focused* view, not the visible tab: the user may be reading a
        finished sub-agent's transcript while the master keeps working, and
        output landing in the tab they happen to have open would look exactly
        like data loss. Falls back to the master panel (and raises ``NoMatches``
        before the screen is mounted, which every ``add_*`` already suppresses).
        """
        panel = self._panels.get(self._focused_panel)
        if panel is not None and panel.is_mounted:
            return panel
        return self.query_one(f"#{_panel_id(MASTER_VIEW)}", TranscriptPanel)

    @property
    def chat_tabs(self) -> TabbedContent:
        return self.query_one("#chats", TabbedContent)

    @property
    def action_panel(self) -> ActionPanel:
        return self.query_one(ActionPanel)

    @property
    def status_bar(self) -> StatusBar:
        return self.query_one(StatusBar)

    @property
    def composer(self) -> ChatComposer:
        return self.query_one(ChatComposer)

    @property
    def running_bar(self) -> RunningBar:
        return self.query_one(RunningBar)

    @property
    def sidebar(self) -> Sidebar:
        return self.query_one(Sidebar)

    def update_config(self, config: Config) -> None:
        """Adopt a freshly-edited Config (service editor save) for everything
        this screen reads directly - the controller is updated too, so the
        NEXT session (and any /new) picks it up. The Sidebar is refreshed
        separately by the caller (it needs the same Config)."""
        self._config = config
        self._controller.update_config(config)

    def on_mount(self) -> None:
        self._panels[MASTER_VIEW] = self.query_one(f"#{_panel_id(MASTER_VIEW)}", TranscriptPanel)
        self._paint_status()
        self._update_composer()
        self._sync_sidebar()
        # Appearances captured on a previous run are already usable: show them.
        self._paint_profile()
        self._remember_own_window()  # the user just launched us - focus is our terminal
        self._controller.start()

    def _remember_own_window(self) -> None:
        """Record the foreground window at a moment the user is provably
        interacting with AgentClip (launch, composer send), so the auto-copy
        flow knows which window "back to the tool" means. A None reading (mid
        focus switch, non-Windows) keeps the last good handle."""
        handle = foreground_window()
        if handle is not None:
            self._own_window = handle

    # -- dynamic bindings -----------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("approve", "reject"):
            return True if self.pending_approval else None
        if action == "auto_edits":
            return True if (self.pending_approval and self._gate_kind == "edit") else None
        if action in ("undo", "end_session"):
            ok = (
                self.session_active
                and not self.busy
                and self.phase_name in ("AWAITING_REPLY", "DONE")
            )
            return True if ok else None
        if action == "recopy":
            return True if self.has_outbound else None
        if action == "force_ingest":  # ingest only parses in AWAITING_REPLY
            ok = self.session_active and not self.busy and self.phase_name == "AWAITING_REPLY"
            return True if ok else None
        if action == "follow_up":  # also after task_done: a follow-up reopens the session
            ok = (
                self.session_active
                and not self.busy
                and self.phase_name in ("AWAITING_REPLY", "DONE")
            )
            return True if ok else None
        if action == "toggle_watch":
            if self._provider.name == "manual":
                return False
            return True if self.session_active else None
        if action == "export_log":
            return True if self.session_active else None
        if action == "submit_composer":
            # ...and during a sub-agent run, where the box exists so /abort can
            # be typed even though the master's flow is busy throughout.
            if self.awaiting_answer or self.awaiting_new_session:
                return True
            if self.sub_running and not self.pending_approval:
                return True
            return (
                self.session_active
                and not self.busy
                and self.phase_name in ("AWAITING_REPLY", "DONE")
            )
        if action == "cancel_entry":
            return self.reject_open
        if action == "cancel_execution":  # only while tool calls are actually running
            return True if self.executing else None
        return True

    # == ChatView: transcript =================================================

    async def add_user(self, text: str) -> None:
        with suppress(NoMatches):
            await self.transcript.add_user(text)

    async def add_prose(self, text: str) -> None:
        with suppress(NoMatches):
            await self.transcript.add_prose(text)

    async def add_call(self, call: ToolCall) -> None:
        with suppress(NoMatches):
            await self.transcript.add_call(call)

    async def add_note(self, text: str) -> None:
        with suppress(NoMatches):
            await self.transcript.add_note(text)

    async def add_error(self, text: str) -> None:
        with suppress(NoMatches):
            await self.transcript.add_error(text)

    async def add_outbound(self, outbound: Outbound, label: str) -> None:
        with suppress(NoMatches):
            await self.transcript.add_outbound(outbound, label)

    async def clear_transcript(self) -> None:
        # Only the session-reset path (/new, the summary's "new session") clears
        # the transcript, so this doubles as the session teardown hook. The
        # calibrations SURVIVE for both slots: they describe where the service's
        # windows are, not what the finished session said, so /new must not make
        # the user re-draw every region. Only the pointers go home to MASTER -
        # the next session starts by driving the master window - and the
        # detector worker restarts against it so the surviving baselines keep
        # being polled. The sub-agent tabs do die with the session: the next one
        # numbers its runs from sub-1 again.
        await self._remove_session_views()
        self._calibrating = AgentSlot.MASTER
        self._live = AgentSlot.MASTER
        # Re-derive rather than clear: the sub-agent slot is still calibrated,
        # and _after_calibration's one-shot toast must not re-fire after /new.
        self._delegation_ready = self.delegation_available()
        self._reset_finish_trigger()
        self._start_detector_worker()
        with suppress(NoMatches):
            self.sidebar.show_slot(self.calibrating, self._slot_note())
            self.sidebar.hide_paste_flash()
        with suppress(NoMatches):
            await self.transcript.clear_events()

    def has_transcript_events(self) -> bool:
        if not self._panels:
            return False
        return any(panel.event_log for panel in self._panels.values())

    def render_log(self, meta_lines: list[str]) -> str:
        """The master's log, then every sub-agent's under its own heading.

        One exported document for the whole delegation tree: the master's
        transcript reads end to end (the sub-runs appear in it as the delegate
        call and its result) and each sub-agent's full transcript follows, so
        nothing a sub-agent did is only visible in a tab.
        """
        master = self._panels.get(MASTER_VIEW)
        if master is None:
            master = self.query_one(f"#{_panel_id(MASTER_VIEW)}", TranscriptPanel)
        parts = [master.render_log(meta_lines)]
        for view_id, panel in self._panels.items():
            if view_id == MASTER_VIEW:
                continue
            ref = self._sessions.get(view_id)
            title = ref.title if ref is not None else view_id
            chat = f" ({ref.chat_name})" if ref is not None and ref.chat_name else ""
            parts.append(f"## sub-agent: {title}{chat}\n\n{panel.render_events()}".rstrip() + "\n")
        return "\n".join(parts)

    # == ChatView: session views (transcript tabs) ============================

    async def open_session_view(self, session: SessionRef) -> None:
        """Mount a transcript tab for ``session`` and make it the focused one.

        Focus moves here, not on the user's next click: the controller writes
        the sub-agent's whole run through the ordinary ``add_*`` calls right
        after this returns.
        """
        panel = TranscriptPanel(id=_panel_id(session.id))
        await self.chat_tabs.add_pane(
            TabPane(f"▶ {session.title}", panel, id=_pane_id(session.id))
        )
        self._panels[session.id] = panel
        self._sessions[session.id] = session
        self.focus_session_view(session.id)

    def focus_session_view(self, session_id: str) -> None:
        """Route every later ``add_*`` into ``session_id``'s panel (and show it).

        Unknown ids are ignored rather than fatal: losing a transcript line is
        never worth taking a running session down with an exception.
        """
        if session_id not in self._panels and session_id != MASTER_VIEW:
            return
        self._focused_panel = session_id
        with suppress(NoMatches):
            self.chat_tabs.active = _pane_id(session_id)

    async def finish_session_view(self, session_id: str, note: str) -> None:
        """A sub-agent run ended: annotate its panel and tick its tab.

        Nothing is disabled or removed - the panels are output-only and the
        composer always targets the controller's active session, so leaving the
        tab readable costs nothing and is the whole point of keeping it.
        """
        panel = self._panels.get(session_id)
        if panel is None:
            return
        await panel.add_note(note)
        ref = self._sessions.get(session_id)
        title = ref.title if ref is not None else session_id
        with suppress(NoMatches, ValueError):
            self.chat_tabs.get_tab(_pane_id(session_id)).label = f"✓ {title}"

    async def _remove_session_views(self) -> None:
        """Drop every sub-agent tab, leaving the master's - the /new teardown."""
        self._focused_panel = MASTER_VIEW
        stale = [view_id for view_id in self._panels if view_id != MASTER_VIEW]
        self._sessions.clear()
        for view_id in stale:
            del self._panels[view_id]
            with suppress(NoMatches):
                await self.chat_tabs.remove_pane(_pane_id(view_id))
        with suppress(NoMatches):
            self.chat_tabs.active = _pane_id(MASTER_VIEW)

    def action_next_chat_tab(self) -> None:
        """f6: show the next transcript tab. Browsing only - it moves what the
        user SEES, never where the controller writes (see ``transcript``)."""
        try:
            tabs = self.chat_tabs
        except NoMatches:
            return
        order = [_pane_id(view_id) for view_id in self._panels]
        if len(order) < 2:
            return
        try:
            index = order.index(tabs.active)
        except ValueError:
            index = -1
        tabs.active = order[(index + 1) % len(order)]

    # == ChatView: state + chrome =============================================

    def render_state(self, view: SessionView) -> None:
        if not self.is_mounted:
            return
        self._snap = view.snapshot
        # Whose state the rest of this snapshot describes. During a delegation
        # every field below is the SUB-AGENT's, so the status bar, the gate
        # title and the composer all have to say so - a magenta status segment
        # is the one glance that tells the user which conversation is asking.
        self._session_role = view.session_role
        self._session_title = view.session_title
        self.sub_running = view.session_role == "subagent"
        self.session_active = view.session_active
        self.has_outbound = view.has_outbound
        self.pending_approval = view.pending_approval
        self.awaiting_answer = view.awaiting_answer
        self.busy = view.busy
        self.phase_name = view.snapshot.phase.name if view.snapshot else "IDLE"
        if not self.pending_approval and self.reject_open:
            self.reject_open = False
            with suppress(NoMatches):
                self.action_panel.close_reject_input()
        self._paint_status()
        self._update_composer()
        self._sync_sidebar()
        # Focus the composer when it is actionable: while answering or waiting for a
        # new session's first message, or the moment a flow ends (busy clears) and the
        # session is armed. _focus_composer no-ops if the composer is disabled or a
        # modal owns the screen.
        if self.awaiting_answer or self.awaiting_new_session or not self.busy:
            self._focus_composer()

    def show_gate(self, action: PendingAction, position: str, queue: str) -> None:
        self._gate_kind = action.kind
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.action_panel.show_approval(action, position, queue, prefix=self._gate_prefix())
            self.action_panel.focus_default()  # focus Approve so y/n/a bubble to the screen

    def _gate_prefix(self) -> str:
        """Whose call this gate is for. The user approving an edit mid-delegation
        is approving a SUB-agent's edit, and nothing else on screen would say so:
        the diff and the transcript around it look the same either way."""
        if self._session_role != "subagent":
            return ""
        title = self._session_title
        return f"SUB-AGENT ‹{title}› · " if title else "SUB-AGENT · "

    def hide_gate(self) -> None:
        self._gate_kind = None
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.action_panel.hide_panel()

    def start_working(self, label: str) -> None:
        # The running bar and the cancel binding share one lifetime: the bar
        # advertises ctrl+x, so ctrl+x must work exactly while it is up.
        self.executing = True
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.running_bar.start(label)

    def stop_working(self) -> None:
        self.executing = False
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.running_bar.stop()

    def reset_composer(self) -> None:
        with suppress(NoMatches):
            self.composer.reset()

    # == ChatView: notifications ==============================================
    # notify() is inherited from Textual's Screen and satisfies the port.

    def alert(self, message: str, severity: Severity = "information") -> None:
        """bell + toast, each switchable in config: the user is staring at the browser."""
        if self._config.notify.bell:
            self.app.bell()
        if self._config.notify.toast:
            self.notify(message, severity=severity)

    # == ChatView: clipboard / transport ======================================

    async def copy_outbound(self, text: str) -> None:
        try:
            await asyncio.to_thread(write_via, self._provider, self._self_writes, text)
        except ClipboardUnavailable:
            self.app.copy_to_clipboard(text)  # OSC-52, write-only
            self.notify(
                "no clipboard backend - sent via the terminal's OSC-52 escape; if pasting "
                "fails, copy from .agentclip/sessions/<id>/outbound/",
                severity="warning",
            )
        clicked = await self._click_after_response()
        # Only paste when the click actually landed - focus could be on any
        # window otherwise, and pasting into an unknown app is the one
        # unforgivable failure mode here.
        pasted = False
        if clicked:
            await asyncio.sleep(0.15)  # let focus settle before typing into it
            pasted = await asyncio.to_thread(send_paste)
        # The payload now waits on the user's Enter (pasted) or Ctrl+V+Enter
        # (not pasted) - nag until the busy region reports the model chewing
        # (or a new capture/reset happens).
        with suppress(NoMatches):
            self.sidebar.show_paste_flash(ENTER_FLASH_TEXT if pasted else PASTE_FLASH_TEXT)

    async def _find(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> ScreenRegion | None:
        """Where ``kind`` is on screen right now, in absolute coordinates.

        The one primitive behind every "is it there / click it" question. It
        looks *inside the drawn chat region* for the ACTIVE SERVICE's captured
        appearance, which is the whole point of the profile model: the user
        drew one box per window, and everything inside it is recognised rather
        than remembered, so moving or resizing the browser costs nothing.

        ``scene`` lets a caller that already captured the chat region reuse the
        frame, so several appearances can be hunted in one picture of one
        instant. None is returned - never raised - for every way this can come
        up empty: no chat region drawn, no such appearance captured, the
        capture failed, or it simply is not on screen.
        """
        cal = self._slots[slot] if slot is not None else self.live
        region = cal.chat_region
        if region is None:
            return None
        template = self._active_profile().get(kind)
        if template is None:
            return None
        if scene is None:
            try:
                scene = await asyncio.to_thread(capture_region, region)
            except CaptureError:
                return None
        match = await asyncio.to_thread(
            find_in_region, template, scene, max_diff=kind.max_diff
        )
        if match is None:
            return None
        return match_rect(region, template, match)

    async def _chatbox_region(self) -> ScreenRegion | None:
        """Which chat input box to poke right now, or None if none is known.

        A fresh chat centres its input box and an ongoing one docks it at the
        bottom, so both appearances are hunted in ONE capture of the live chat
        region - the two layouts are mutually exclusive, so whichever is found
        is the one on screen. Ongoing goes first: mid-session it is the common
        case, and the search stops at the first hit.

        When neither is found (the page is mid-transition, a dialog covers it,
        or the service has no chat box captured at all) the whole chat window
        is the answer rather than nothing: clicking a window is recoverable,
        not clicking at all means the paste never lands.

        Always the LIVE slot: mid-delegation this is the sub-agent's window.
        """
        region = self.live.chat_region
        if region is None:
            return None
        try:
            scene: RegionImage | None = await asyncio.to_thread(capture_region, region)
        except CaptureError:
            return region
        for kind in (TemplateKind.CHATBOX_ONGOING, TemplateKind.CHATBOX_INITIAL):
            found = await self._find(kind, scene=scene)
            if found is not None:
                return found
        return region

    async def _click_after_response(self) -> bool:
        """The payload is on the clipboard - poke the chat (when something is
        calibrated) so the browser has focus and the paste lands without
        alt-tab. Returns True only when a target was known AND the click landed
        - the signal callers use to decide whether it is safe to send Ctrl+V."""
        region = await self._chatbox_region()
        if region is None:
            return False
        clicked = await asyncio.to_thread(click_region, region)
        if not clicked and not self._region_click_warned:
            self._region_click_warned = True  # once, not on every copy
            self.notify(
                "the focus click did not land (it is Windows-only) - alt-tab to the chat instead",
                severity="warning",
            )
        return clicked

    async def read_clipboard(self) -> str | None:
        return await asyncio.to_thread(self._provider.read_text)

    def start_input(self) -> None:
        if self._provider.name == "manual":
            self.notify(
                "manual clipboard mode: press i and paste the model's reply into the box; "
                "outbound payloads go out via the terminal's OSC-52 copy",
                severity="warning",
                timeout=10,
            )
            return
        self._start_watcher()

    def stop_input(self) -> None:
        if self._watch_worker is not None:
            self._watch_worker.cancel()
            self._watch_worker = None

    # == ChatView: scheduling + lifecycle =====================================

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.run_worker(coro, group="flow")

    def exit_app(self) -> None:
        self.app.exit()

    # == ChatView: blocking prompts ===========================================

    async def prompt_new_session(self) -> SessionSpec | None:
        """Wait *inline* for the first message - no modal (tui.md section 1.3).

        The composer is switched into "describe the task" mode and the sidebar's
        service picker unlocks; the send resolves the future with a SessionSpec
        carrying whatever the sidebar has selected at that moment. The controller
        may call this again (a budget-exceeded retry, /new, or "new" from the
        summary screen) - each call re-arms the same inline surface.
        """
        future: asyncio.Future[SessionSpec | None] = asyncio.get_running_loop().create_future()
        self._new_session_future = future
        self.awaiting_new_session = True
        self._update_composer()
        self._sync_sidebar()
        self._focus_composer()
        try:
            return await future
        finally:
            self._new_session_future = None
            self.awaiting_new_session = False
            self._sync_sidebar()
            self._update_composer()

    async def confirm(self, title: str, body: str = "") -> bool:
        return await self.app.push_screen_wait(ConfirmScreen(title, body))

    async def prompt_text(self, title: str, hint: str) -> str | None:
        return await self.app.push_screen_wait(TextEntryScreen(title, hint))

    async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str:
        return await self.app.push_screen_wait(SummaryScreen(_stats_table(rows), summary))

    # -- clipboard watcher ----------------------------------------------------

    def _start_watcher(self) -> None:
        if self._provider.name == "manual" or self._watch_worker is not None:
            return
        provider = self._provider
        self_writes = self._self_writes
        interval = self._config.clipboard.poll_interval_ms

        def capture(text: str) -> None:
            self.post_message(ClipboardCaptured(text))  # thread-safe bridge to the UI

        def loop() -> None:
            worker = get_current_worker()
            watch(
                provider,
                interval,
                should_stop=lambda: worker.is_cancelled,
                accepts=looks_like_protocol,
                on_capture=capture,
                self_writes=self_writes,
            )

        self._watch_worker = self.run_worker(
            loop, thread=True, group="clipwatch", exit_on_error=False
        )
        self.watch_paused = False

    def on_clipboard_captured(self, message: ClipboardCaptured) -> None:
        message.stop()
        # A new capture means the conversation moved on without the paste
        # (manual copy, no busy region) - stop nagging either way.
        with suppress(NoMatches):
            self.sidebar.hide_paste_flash()
        self._controller.submit_clipboard(message.text)

    # -- key actions / events -> controller -----------------------------------

    def action_approve(self) -> None:
        self._controller.submit_decision(Decision.APPROVE, None)

    def action_auto_edits(self) -> None:
        if self._gate_kind == "edit":
            self._controller.submit_decision(Decision.APPROVE_ALL_EDITS, None)

    def action_reject(self) -> None:
        if not self.pending_approval:
            return
        self.reject_open = True
        self.action_panel.open_reject_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "reject-reason":
            return
        event.stop()
        self.reject_open = False
        self.action_panel.close_reject_input()
        self._controller.submit_decision(Decision.REJECT, event.value.strip() or None)

    def action_cancel_entry(self) -> None:
        if self.reject_open:
            self.reject_open = False
            self.action_panel.close_reject_input()

    @on(ActionPanel.Decision)
    def _on_action_decision(self, message: ActionPanel.Decision) -> None:
        message.stop()
        if message.choice == "approve":
            self.action_approve()
        elif message.choice == "approve_edits":
            self.action_auto_edits()
        elif message.choice == "reject":
            self.action_reject()

    @on(ChatComposer.Submitted)
    def _on_composer_submitted(self, message: ChatComposer.Submitted) -> None:
        message.stop()
        self._submit_text(message.text)

    def action_submit_composer(self) -> None:
        try:
            composer = self.composer
        except NoMatches:
            return
        self._submit_text(composer.text)

    def _submit_text(self, text: str) -> None:
        """One door for every composer send.

        While waiting for a new session the text IS the task (verbatim - no slash
        command parsing, exactly like an ask_user answer), and it starts the session
        with the sidebar's selected service. Otherwise it goes to the controller.
        """
        self._remember_own_window()  # typing here = our terminal has OS focus
        future = self._new_session_future
        if future is not None and not future.done():
            task = text.strip()
            if not task:
                self.notify("describe the task first", severity="warning")
                return
            self.reset_composer()
            future.set_result(SessionSpec(task=task, service=self._selected_service()))
            return
        self._controller.submit_message(text)

    def _selected_service(self) -> str:
        try:
            return self.sidebar.service
        except NoMatches:  # sidebar never mounted (shouldn't happen): configured default
            return self._config.general.service

    def _active_preset(self) -> ServicePreset:
        """The preset behind the sidebar's service picker - locked to the
        running session's service while one is active, so mid-session this is
        the session's preset. The stale detector reads its ``stable_seconds``
        from here at poller start: streaming cadence is a property of the
        service being driven, not of AgentClip."""
        return self._config.services.get(self._selected_service()) or self._config.preset()

    # -- service profiles (what the service LOOKS like) ------------------------

    def _profile(self, key: str) -> ServiceProfile:
        """``key``'s captured appearances, read from disk once per app run.

        Cached because a profile is a handful of decoded PNGs plus their anchor
        tables, and every capture button, every detector restart and every
        readiness question asks for it again. ``load_profile`` never raises, so
        an unreadable profile simply caches as an empty one.
        """
        profile = self._profiles.get(key)
        if profile is None:
            profile = load_profile(self._profile_root, key)
            self._profiles[key] = profile
        return profile

    def _active_profile(self) -> ServiceProfile:
        """The profile of the service currently being driven.

        Resolved exactly like ``_active_preset``: through the sidebar's picker,
        which a running session locks to that session's service - so mid-session
        this is the session's profile and between sessions it is whatever the
        user has selected to calibrate against.
        """
        return self._profile(self._selected_service())

    # -- sidebar --------------------------------------------------------------

    @on(Sidebar.SlotChanged)
    def _on_slot_changed(self, message: Sidebar.SlotChanged) -> None:
        """Point the calibration buttons at another slot and repaint the column
        from that slot's stored state. The *live* slot is untouched: switching
        the picker mid-run must not retarget a click at a different window."""
        message.stop()
        self._calibrating = message.slot
        with suppress(NoMatches):
            self.sidebar.show_slot(self.calibrating, self._slot_note())

    def _slot_note(self) -> str:
        """The sidebar's readiness line for the slot being calibrated.

        Composed here, not in the sidebar, because it takes both halves of the
        answer: the slot's drawn window and the active service's appearances.
        """
        return slot_note(self.calibrating, self._active_profile())

    def _slot_prompt(self, prompt: str) -> str:
        """Both slots share the picker code, so the sub-agent's prompts have to
        say out loud which window the user is being asked to draw on."""
        if self._calibrating is AgentSlot.SUBAGENT:
            return f"SUB-AGENT window · {prompt}"
        return prompt

    def _after_calibration(self) -> None:
        """Repaint the slot readiness line after any single calibration landed,
        and tell the user once when the sub-agent slot becomes usable - the
        delegate tool is baked into the bootstrap, so it only reaches the model
        on the next /new."""
        ready = self.delegation_available()
        with suppress(NoMatches):
            self.sidebar.update_slot_note(self._slot_note())
        if ready and not self._delegation_ready:
            self.notify("sub-agent slot ready - /new to give the model the delegate tool")
        self._delegation_ready = ready

    def action_toggle_sidebar(self) -> None:
        """Hide/show the settings column - diffs and command output want the room."""
        with suppress(NoMatches):
            sidebar = self.sidebar
            sidebar.display = not sidebar.display

    def _refuse_second_picker(self) -> bool:
        """True (and toast) when an overlay is already up. Worker cancellation
        cannot kill the blocking child overlay process, so the only safe
        guard against stacked fullscreen overlays is refusing the press."""
        if self._picker_open:
            self.notify("a region picker is already open - finish it or press Esc first")
            return True
        self._picker_open = True
        return False

    @on(Button.Pressed, "#set-region-btn")
    def _on_set_region(self, event: Button.Pressed) -> None:
        event.stop()
        if self._refuse_second_picker():
            return
        self.run_worker(self._pick_chat_region(), group="regionpick", exclusive=True)

    async def _pick_chat_region(self) -> None:
        """Run the draw-a-box overlay (a child process - tkinter cannot live in
        this one) and adopt the drawn chatbot window for the rest of the session."""
        try:
            region = await asyncio.to_thread(
                pick_region,
                prompt=self._slot_prompt(
                    "Drag a box around the window that hosts the AI chatbot · Esc cancels"
                ),
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        finally:
            self._picker_open = False
        if region is None:
            self.notify("chat region unchanged (selection cancelled)")
            return
        self.calibrating.chat_region = region
        self._region_click_warned = False
        with suppress(NoMatches):
            self.sidebar.update_region(region)
        self._after_calibration()
        self.notify(
            f"chat region set ({region.describe()}) - the chatbot window; "
            "outbound copies click it until a chatbox is calibrated"
        )

    # -- capturing what a service LOOKS like -----------------------------------

    @on(Button.Pressed, "#set-chatbox-initial-btn")
    def _on_capture_chatbox_initial(self, event: Button.Pressed) -> None:
        event.stop()
        self._start_capture(TemplateKind.CHATBOX_INITIAL)

    @on(Button.Pressed, "#set-chatbox-ongoing-btn")
    def _on_capture_chatbox_ongoing(self, event: Button.Pressed) -> None:
        event.stop()
        self._start_capture(TemplateKind.CHATBOX_ONGOING)

    def _start_capture(self, kind: TemplateKind) -> None:
        """Run one capture worker, unless an overlay is already up."""
        if self._refuse_second_picker():
            return
        self.run_worker(self._capture_template(kind), group="regionpick", exclusive=True)

    async def _capture_template(self, kind: TemplateKind) -> None:
        """Draw a box around ``kind`` and file the pixels under the SERVICE.

        The one capture path for every appearance. The prompt comes from the
        kind (screen.profile) rather than from here, because what makes a good
        capture is a fact about the appearance and must read identically
        wherever it is asked for; and there is no slot prefix, because the
        answer is the same whichever window the user drew it in.

        A save failure is reported but the template is kept in memory: the
        appearance works for this run, it just will not survive a restart -
        losing the capture as well would be strictly worse.
        """
        try:
            region = await asyncio.to_thread(pick_region, prompt=kind.prompt)
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        finally:
            self._picker_open = False
        if region is None:
            self.notify(f"{kind.label} unchanged (selection cancelled)")
            return
        try:
            image = await asyncio.to_thread(capture_region, region)
        except CaptureError as exc:
            self.notify(f"could not capture the {kind.label}: {exc}", severity="error")
            return
        profile = self._active_profile()
        try:
            profile.put(kind, image)
        except ValueError as exc:
            self.notify(f"that {kind.label} cannot be searched for: {exc}", severity="error")
            return
        try:
            await asyncio.to_thread(save_template, self._profile_root, profile.key, kind, image)
        except ProfileStoreError as exc:
            self.notify(
                f"{kind.label} captured, but not saved for next time: {exc}", severity="error"
            )
        self._region_click_warned = False
        self._paint_profile()
        self.notify(f"{kind.label} captured for {profile.key} ({region.describe()})")
        self._after_calibration()

    # Appearances that have moved off the slot and into the service profile.
    # Grows as each detector is migrated; the sidebar block follows last.
    _PROFILE_KINDS: tuple[TemplateKind, ...] = (
        TemplateKind.CHATBOX_INITIAL,
        TemplateKind.CHATBOX_ONGOING,
        TemplateKind.COPY,
    )

    def _paint_profile(self) -> None:
        """Repaint every migrated appearance's status from the active profile."""
        profile = self._active_profile()
        with suppress(NoMatches):
            sidebar = self.sidebar
            for kind in self._PROFILE_KINDS:
                template = profile.get(kind)
                sidebar.update_template(
                    kind,
                    f"{template.width}×{template.height} · captured"
                    if template is not None
                    else TEMPLATE_UNSET,
                )

    @on(Button.Pressed, "#set-busy-btn")
    def _on_set_busy_region(self, event: Button.Pressed) -> None:
        event.stop()
        if self._refuse_second_picker():
            return
        self.run_worker(self._pick_busy_region(), group="busyregionpick", exclusive=True)

    async def _pick_busy_region(self) -> None:
        """Draw-a-box overlay around the chat's busy/stop indicator, calibrated
        WHILE the model is generating - the drawn region's pixels right now
        become the baseline every later poll is compared against."""
        try:
            region = await asyncio.to_thread(
                pick_region,
                prompt=self._slot_prompt(
                    "Drag a box around the busy/stop indicator WHILE the model "
                    "is generating · Esc cancels"
                ),
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        finally:
            self._picker_open = False
        if region is None:
            self.notify("busy region unchanged (cancelled)")
            return
        try:
            baseline = await asyncio.to_thread(capture_region, region)
        except CaptureError as exc:
            self.notify(f"could not capture the busy region: {exc}", severity="error")
            return
        self.calibrating.busy_region = region
        self.calibrating.busy_baseline = baseline
        self._busy_seen = False
        self._busy_finished = None
        with suppress(NoMatches):
            self.sidebar.update_busy(BUSY_CALIBRATED)
        self.notify(f"busy region calibrated ({region.describe()})")
        self._after_calibration()
        self._start_detector_worker()

    @on(Button.Pressed, "#set-idle-btn")
    def _on_set_idle_region(self, event: Button.Pressed) -> None:
        event.stop()
        if self._refuse_second_picker():
            return
        self.run_worker(self._pick_idle_region(), group="busyregionpick", exclusive=True)

    async def _pick_idle_region(self) -> None:
        """Draw-a-box overlay around an element that looks DIFFERENT while the
        model generates (the send/voice button is the usual one), calibrated
        while the chat is IDLE - so a later match means "finished".

        The mirror image of the busy region, and useful on its own for chats
        that have no visible stop indicator. Calibrating both is the point of
        the feature: the auto-copy only fires when they agree."""
        try:
            region = await asyncio.to_thread(
                pick_region,
                prompt=self._slot_prompt(
                    "Drag a box around an element that CHANGES while generating "
                    "(e.g. the send button), WHILE the chat is idle · Esc cancels"
                ),
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        finally:
            self._picker_open = False
        if region is None:
            self.notify("idle element unchanged (cancelled)")
            return
        try:
            baseline = await asyncio.to_thread(capture_region, region)
        except CaptureError as exc:
            self.notify(f"could not capture the idle element: {exc}", severity="error")
            return
        self.calibrating.idle_region = region
        self.calibrating.idle_baseline = baseline
        self._idle_seen = False
        self._idle_finished = None
        with suppress(NoMatches):
            self.sidebar.update_idle(IDLE_CALIBRATED)
        self.notify(f"idle element calibrated ({region.describe()})")
        self._after_calibration()
        self._start_detector_worker()

    @on(Button.Pressed, "#set-stale-btn")
    def _on_set_stale_region(self, event: Button.Pressed) -> None:
        event.stop()
        if self._refuse_second_picker():
            return
        self.run_worker(self._pick_stale_region(), group="busyregionpick", exclusive=True)

    async def _pick_stale_region(self) -> None:
        """Draw-a-box overlay around the response area itself, for the third
        finish detector: "unchanged for stable_seconds" means the model is
        done. Unlike the busy/idle pickers there is NO baseline capture here -
        the tracker's first polled frame is the baseline, and every later frame
        is compared to the one before it - which is what makes this detector
        service-agnostic: it trusts no particular pixel cue, only stillness."""
        try:
            region = await asyncio.to_thread(
                pick_region,
                prompt=self._slot_prompt(
                    "Drag a box around the RESPONSE area (where the answer "
                    "text appears) · Esc cancels"
                ),
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        finally:
            self._picker_open = False
        if region is None:
            self.notify("response region unchanged (cancelled)")
            return
        self.calibrating.stale_region = region
        self._stale_seen = False
        self._stale_finished = None
        with suppress(NoMatches):
            self.sidebar.update_stale(STALE_CALIBRATED)
        self.notify(f"response region calibrated ({region.describe()})")
        self._after_calibration()
        self._start_detector_worker()

    # -- finish-detector polling -----------------------------------------------

    def _start_detector_worker(self) -> None:
        """Mirrors ``_start_watcher``: one thread worker polling whichever
        detectors are calibrated and bridging each verdict back to the UI via
        ``post_message``. It replaces any previous run, so a recalibration
        mid-session cannot leave two loops polling different regions.

        The probe order within a tick is fixed - busy, then idle, then stale -
        which is what makes the LAST calibrated one the tick's closing message
        (see ``_finish_tick_closed_by``).

        It always polls the LIVE slot, and the regions are read once here rather
        than per tick: restarting the worker is how the poller follows the live
        slot across a delegation, so an in-flight loop must keep watching the
        window it was started for. The stale tracker is likewise built once per
        run - it carries the previous frame and the stillness streak, and both
        describe one window - with its "stable for N seconds" wish converted to
        ticks of the poll cadence here, from the active service preset (kept on
        ``self._stale_tracker`` so the auto-copy flow can reset it)."""
        self._stop_detector_worker()
        live = self.live
        busy_region, busy_baseline = live.busy_region, live.busy_baseline
        idle_region, idle_baseline = live.idle_region, live.idle_baseline
        stale_region = live.stale_region
        tracker: StaleTracker | None = None
        if stale_region is not None:
            stable_seconds = self._active_preset().stable_seconds
            tracker = StaleTracker(
                stale_region,
                required_ticks=max(1, round(stable_seconds / _BUSY_POLL_S)),
                capture=capture_region,
            )
        self._stale_tracker = tracker
        if busy_baseline is None and idle_baseline is None and stale_region is None:
            return

        def loop() -> None:
            worker = get_current_worker()
            while not worker.is_cancelled:
                if busy_region is not None and busy_baseline is not None:
                    self.post_message(BusyProbed(probe_busy(busy_baseline, busy_region)))
                if idle_region is not None and idle_baseline is not None:
                    self.post_message(IdleProbed(probe_busy(idle_baseline, idle_region)))
                if tracker is not None:
                    self.post_message(StaleProbed(tracker.poll()))
                # Sleep in short increments so cancellation lands promptly.
                remaining = _BUSY_POLL_S
                while remaining > 0 and not worker.is_cancelled:
                    step = min(0.05, remaining)
                    time.sleep(step)
                    remaining -= step

        self._detector_worker = self.run_worker(
            loop, thread=True, group="busyprobe", exit_on_error=False
        )

    def _stop_detector_worker(self) -> None:
        if self._detector_worker is not None:
            self._detector_worker.cancel()
            self._detector_worker = None

    def _finish_tick_closed_by(self, detector: str) -> bool:
        """Is ``detector``'s message the tick's LAST, given what is calibrated?

        The poller posts busy -> idle -> stale each tick, skipping whatever is
        uncalibrated, and the combined verdict must fold exactly once per tick
        - on the closing message - or a half-reported tick could arm or fire
        on one detector's word while another's is still in flight. So each
        handler closes the tick only when no later-in-order detector is
        calibrated to close it instead.
        """
        live = self.live
        if detector == "busy":
            return live.idle_baseline is None and live.stale_region is None
        if detector == "idle":
            return live.stale_region is None
        return True  # stale is last in the order; nothing can follow it

    def on_busy_probed(self, message: BusyProbed) -> None:
        message.stop()
        with suppress(NoMatches):
            self.sidebar.update_busy(_format_busy_probe(message.probe))
        self._busy_seen = True
        self._busy_finished = _busy_verdict(message.probe)
        if self._finish_tick_closed_by("busy"):
            self._evaluate_finish()

    def on_idle_probed(self, message: IdleProbed) -> None:
        message.stop()
        with suppress(NoMatches):
            self.sidebar.update_idle(_format_idle_probe(message.probe))
        self._idle_seen = True
        self._idle_finished = _idle_verdict(message.probe)
        if self._finish_tick_closed_by("idle"):
            self._evaluate_finish()

    def on_stale_probed(self, message: StaleProbed) -> None:
        message.stop()
        with suppress(NoMatches):
            self.sidebar.update_stale(_format_stale_probe(message.probe))
        self._stale_seen = True
        self._stale_finished = _stale_verdict(message.probe)
        if self._finish_tick_closed_by("stale"):
            self._evaluate_finish()

    def _evaluate_finish(self) -> None:
        """Fold every live detector's latest verdict into one "the model
        stopped" decision, once per poll tick.

        * ANY detector saying "generating" arms the auto-copy trigger and stops
          the paste nag - the payload demonstrably went in.
        * The trigger fires only when EVERY live detector says "finished" on
          two consecutive ticks. With one detector that is today's
          MATCH-then-two-CHANGED rule; with both it is the agreement the second
          detector exists for.
        * A capture error (no verdict) breaks the streak but leaves the arm
          alone: one bad frame must not silently cancel an in-flight finish.

        Firing disarms, so the flow cannot repeat until the model generates
        again. A detector that has never reported is ignored entirely - it can
        neither veto nor fake a finish.

        Suspended while the auto-copy flow runs (``_flow_running``): the flow
        scrolls and hover-scans the browser, which the stale detector reads as
        the response region changing - a fresh generation - so evaluating
        mid-flow would re-arm the trigger against the flow's own mouse work
        and re-fire it forever. The flow's finally lifts the suspension and
        resets the tracker (``_run_auto_copy_flow``).
        """
        if self._flow_running:
            return
        verdicts: list[bool | None] = []
        if self._busy_seen:
            verdicts.append(self._busy_finished)
        if self._idle_seen:
            verdicts.append(self._idle_finished)
        if self._stale_seen:
            verdicts.append(self._stale_finished)
        if not verdicts:
            return
        if any(verdict is False for verdict in verdicts):
            self._copy_armed = True
            self._copy_changed_streak = 0
            # The model is generating again - the Ctrl+V landed, stop nagging.
            with suppress(NoMatches):
                self.sidebar.hide_paste_flash()
            return
        if not all(verdict is True for verdict in verdicts) or not self._copy_armed:
            self._copy_changed_streak = 0
            return
        self._copy_changed_streak += 1
        if self._copy_changed_streak < 2 or not self._active_profile().has(TemplateKind.COPY):
            return
        self._copy_armed = False
        self._copy_changed_streak = 0
        self._flow_running = True
        self.run_worker(self._run_auto_copy_flow(), group="copyflow", exclusive=True)

    async def _run_auto_copy_flow(self) -> None:
        """``_auto_copy_flow`` inside the flow-suspension bracket.

        A wrapper rather than a try/finally inside the flow itself so the
        guard's mechanics hold even when tests stub the flow out: whatever the
        flow body does (return, raise, or get cancelled), the suspension lifts
        and the stale tracker forgets the frames the flow's own scrolling and
        hover-scanning produced - polling resumes from a clean post-flow
        baseline instead of reading the flow's mouse work as a new generation.
        """
        try:
            await self._auto_copy_flow()
        finally:
            self._flow_running = False
            if self._stale_tracker is not None:
                self._stale_tracker.reset()

    def _reset_finish_trigger(self) -> None:
        """Forget every detector verdict and the auto-copy arm.

        Called whenever the live slot moves (a delegation starting or ending)
        and on session teardown: verdicts describe a window, so carrying them
        across a retarget could fire the auto-copy against the wrong chat.
        ``_flow_running`` is deliberately NOT cleared here - only the flow's
        own finally lifts the suspension, so a slot move while the flow still
        runs cannot let the trigger fire against its in-flight mouse work."""
        self._busy_seen = False
        self._idle_seen = False
        self._stale_seen = False
        self._busy_finished = None
        self._idle_finished = None
        self._stale_finished = None
        self._copy_armed = False
        self._copy_changed_streak = 0

    # -- the copy button + auto-copy-click -------------------------------------

    @on(Button.Pressed, "#set-copy-btn")
    def _on_capture_copy(self, event: Button.Pressed) -> None:
        event.stop()
        self._start_capture(TemplateKind.COPY)

    def _copy_status(self, text: str) -> None:
        """Repaint the copy button's status line, keeping its captured size in
        front of whatever the flow has to report."""
        with suppress(NoMatches):
            template = self._active_profile().get(TemplateKind.COPY)
            size = f"{template.width}×{template.height} · " if template is not None else ""
            self.sidebar.update_template(TemplateKind.COPY, f"{size}{text}")

    def _hover_scan_for_copy(self, region: ScreenRegion, template: Template) -> RegionMatch | None:
        """Walk the real cursor up ``region`` and stop at the FIRST frame the
        copy icon appears in, or None if it never does.

        Claude's chat only renders a response's copy button while the pointer is
        over that response, so the cheap static capture finds nothing there no
        matter how good the template is. Bottom-up (screen.hover picks the
        stops) because the newest response - the one we want - is at the bottom,
        so the usual answer is one or two stops in.

        Blocking by design: a cursor move, a settle pause and a capture + region
        scan per stop. Runs in a worker thread, never on the UI thread. Any
        failure (unsupported platform, a capture that fails) ends the scan,
        which the caller reports the same way as "not found" - a scan that
        cannot see is not a scan that found nothing.
        """
        for x, y in hover_scan_points(region):
            if not move_cursor(x, y):
                return None
            time.sleep(_HOVER_STEP_DELAY_S)
            try:
                scene = capture_region(region)
            except CaptureError:
                return None
            match = find_lowest_in_region(
                template, scene, max_diff=TemplateKind.COPY.max_diff
            )
            if match is not None:
                return match
        return None

    async def _auto_copy_flow(self) -> None:
        """Fired once by ``_evaluate_finish`` when the detectors agree reasoning
        finished: focus the browser, snap the transcript to the bottom, then look
        for the newest (lowest) copy-button icon anywhere in the chat region and
        click it - the clipboard watcher ingests the resulting copy on its own.

        The search is the whole chat region, not a same-width band beneath a
        remembered icon: the icon appears once per response down the transcript,
        and *lowest inside the window the user drew* is the same answer without
        anyone having to remember a column.
        """
        region = self.live.chat_region
        template = self._active_profile().get(TemplateKind.COPY)
        if region is None or template is None:
            return

        await self._click_after_response()  # the live chat box, else the chat region
        await asyncio.sleep(0.15)

        await asyncio.to_thread(scroll_region, region, -40)
        await asyncio.sleep(0.4)  # let the page settle/render after the flick

        try:
            scene = await asyncio.to_thread(capture_region, region)
        except CaptureError as exc:
            self.notify(f"could not capture the chat region: {exc}", severity="error")
            self._copy_status("capture failed")
            return
        match = await asyncio.to_thread(
            find_lowest_in_region, template, scene, max_diff=TemplateKind.COPY.max_diff
        )
        if match is None:
            # Nothing in the static frame: the chat may only paint the icon
            # under the pointer, so try again while hovering up the region.
            self._copy_status("hover-scanning")
            match = await asyncio.to_thread(self._hover_scan_for_copy, region, template)
        if match is None:
            self.notify("copy button not found on screen", severity="warning")
            self._copy_status("not found")
            return

        target = match_rect(region, template, match)
        clicked = await self._verified_copy_click(target)
        if clicked:
            self.notify(f"copy button clicked (diff {match.diff:.2f})")
            self._copy_status(f"clicked (diff {match.diff:.2f})")
            # The response is on its way to the clipboard - hand focus back to
            # AgentClip so the user watches the ingest here, not the browser. A
            # short beat first so the click registers before focus moves away.
            if self._own_window is not None:
                await asyncio.sleep(0.15)
                await asyncio.to_thread(focus_window, self._own_window)
            return

        # Every attempt clicked but the clipboard never changed - leave the
        # browser focused so the user can click the copy button themselves.
        self.notify(
            "copy click did not take - click the response's copy button yourself",
            severity="warning",
        )
        self._copy_status("click did not take")

    # Small offsets from the matched rect, still inside a ~24 px icon.
    _COPY_CLICK_OFFSETS = ((0, 0), (-3, -3), (3, 3))
    _COPY_VERIFY_READS = 6
    _COPY_VERIFY_INTERVAL_S = 0.2

    async def _verified_copy_click(self, target: ScreenRegion) -> bool:
        """Click the matched copy-button rect, retrying at slightly offset
        points (still inside the icon) until the clipboard actually changes.

        Sometimes the click lands on the right spot but nothing is copied (a
        hover-rendered button that hadn't quite settled). Each attempt polls
        the clipboard for a change instead of trusting the click return value,
        since ``click_region`` only reports whether the OS accepted the input,
        not whether the target app reacted to it.

        Returns True once a change is observed (or, when the clipboard can't
        be read at all, after one unverified click - retrying blind would
        just spam clicks with no way to tell if any of them worked).
        """
        try:
            before = await asyncio.to_thread(self._provider.read_text)
        except ClipboardUnavailable:
            await asyncio.to_thread(click_region, target, settle_s=0.05)
            return True

        for dx, dy in self._COPY_CLICK_OFFSETS:
            shifted = ScreenRegion(target.left + dx, target.top + dy, target.width, target.height)
            await asyncio.to_thread(click_region, shifted, settle_s=0.05)
            for _ in range(self._COPY_VERIFY_READS):
                await asyncio.sleep(self._COPY_VERIFY_INTERVAL_S)
                try:
                    after = await asyncio.to_thread(self._provider.read_text)
                except ClipboardUnavailable:
                    after = None
                if after != before:
                    return True
        return False

    # -- calibrated elements: the reusable verify-then-click -------------------

    async def _click_calibrated_element(
        self, element: CalibratedElement, *, settle_s: float = _ELEMENT_CLICK_SETTLE_S
    ) -> ElementClick:
        """Check the element still looks like its calibration snapshot, and only
        then click its centre.

        The primitive every programmatic click on a ``CalibratedElement`` goes
        through (the new-chat button today, the sub-agent slots later): a
        browser that re-laid itself out, scrolled, or opened a dialog would
        otherwise get a click wherever those pixels used to be. Refusing is
        always the safe answer - the user can click it themselves.
        """
        if not await asyncio.to_thread(probe_element, element):
            return ElementClick.MISMATCH
        clicked = await asyncio.to_thread(click_region, element.region, settle_s=settle_s)
        return ElementClick.CLICKED if clicked else ElementClick.NOT_CLICKED

    # -- the browser's new-chat button ------------------------------------------

    @on(Button.Pressed, "#set-newchat-btn")
    def _on_set_newchat(self, event: Button.Pressed) -> None:
        event.stop()
        if self._refuse_second_picker():
            return
        self.run_worker(self._pick_newchat(), group="regionpick", exclusive=True)

    async def _pick_newchat(self) -> None:
        """Draw-a-box overlay around the browser's "new chat" control, snapshotted
        so every later click can verify it is still that control."""
        try:
            region = await asyncio.to_thread(
                pick_region,
                prompt=self._slot_prompt(
                    "Drag a TIGHT box around the browser's NEW CHAT button · Esc cancels"
                ),
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        finally:
            self._picker_open = False
        if region is None:
            self.notify("new-chat button unchanged (selection cancelled)")
            return
        try:
            template = await asyncio.to_thread(capture_region, region)
        except CaptureError as exc:
            self.notify(f"could not capture the new-chat button: {exc}", severity="error")
            return
        self.calibrating.new_chat = CalibratedElement(region, template)
        with suppress(NoMatches):
            self.sidebar.update_newchat(f"{region.describe()} · set")
        self.notify(f"new-chat button calibrated ({region.describe()})")
        self._after_calibration()

    @on(Button.Pressed, "#newchat-btn")
    def _on_newchat(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_worker(self._new_browser_chat(), group="newchat", exclusive=True)

    async def _new_browser_chat(self) -> None:
        """Click the browser's new-chat button, then hand focus back here.

        The *calibrating* slot's button, so the user can test either window's
        control from the same place the sidebar is pointed at. It never moves
        the live slot - that is ``start_browser_chat``'s job alone.

        Verified first: on a mismatch nothing is clicked and the user is told to
        recalibrate, because the alternative is a blind click somewhere in a
        browser window."""
        element = self.calibrating.new_chat
        if element is None:
            self.notify(
                'calibrate the browser\'s new-chat button first ("Set new-chat button...")',
                severity="warning",
            )
            return
        outcome = await self._click_calibrated_element(element)
        if outcome is ElementClick.MISMATCH:
            self.notify(
                "the new-chat button no longer looks like its calibration - nothing "
                "was clicked; redraw it",
                severity="warning",
            )
            with suppress(NoMatches):
                self.sidebar.update_newchat(f"{element.describe()} · mismatch - not clicked")
            return
        if outcome is ElementClick.NOT_CLICKED:
            self.notify(
                "the new-chat click did not land (it is Windows-only) - start the chat yourself",
                severity="warning",
            )
            with suppress(NoMatches):
                self.sidebar.update_newchat(f"{element.describe()} · click did not land")
            return
        with suppress(NoMatches):
            self.sidebar.update_newchat(f"{element.describe()} · clicked")
        self.notify("new browser chat opened")
        # Same beat as the auto-copy flow: let the click register before focus
        # moves away, then bring the user back to AgentClip.
        if self._own_window is not None:
            await asyncio.sleep(0.15)
            await asyncio.to_thread(focus_window, self._own_window)

    # -- sub-agent transport: opening a chat and retargeting the automation ----

    def delegation_available(self) -> bool:
        """Is the sub-agent slot calibrated well enough to run a delegation?

        The single source of truth the controller asks before it even builds a
        sub-agent engine. Deliberately strict (see ``SlotCalibration``): a
        half-calibrated slot must read as unavailable rather than strand a
        sub-run halfway through.
        """
        return can_delegate(self._slots[AgentSlot.SUBAGENT], self._active_profile())

    def delegation_missing(self) -> tuple[str, ...]:
        """The calibrations still standing between here and ``can_delegate``.

        Handed to the controller as data so the error the *model* gets when it
        calls ``delegate`` against an uncalibrated host names the actual gaps -
        the controller cannot import ``screen`` to ask, and should not have to
        know what a "new-chat button" is.
        """
        return missing(self._slots[AgentSlot.SUBAGENT], self._active_profile())

    async def start_browser_chat(self, slot: AgentSlot) -> bool:
        """Open a fresh browser chat in ``slot`` and make it the live one.

        All-or-nothing, and that is the whole point. A True return means the
        new-chat button verified against its snapshot, the click landed, and the
        automation (paste click, finish detector, auto-copy) now targets that
        window. A False return means **nothing happened at all**: no click, no
        retarget, no trigger reset - so the caller can abort the delegation
        before anything is pasted. Pasting a sub-agent's bootstrap into the
        master chat would corrupt that conversation irrecoverably, so every
        failure here is a refusal rather than a best effort.
        """
        element = self._slots[slot].new_chat
        if element is None:
            self.notify(
                f"the {slot.label} chat's new-chat button is not calibrated - "
                "nothing was clicked",
                severity="error",
            )
            return False
        outcome = await self._click_calibrated_element(element)
        if outcome is not ElementClick.CLICKED:
            reason = (
                "no longer looks like its calibration"
                if outcome is ElementClick.MISMATCH
                else "could not be clicked (it is Windows-only)"
            )
            self.notify(
                f"the {slot.label} chat's new-chat button {reason} - nothing was "
                "clicked and nothing was pasted",
                severity="error",
            )
            return False
        self._live = slot
        self._reset_finish_trigger()
        with suppress(NoMatches):
            self.sidebar.hide_paste_flash()
        self._start_detector_worker()  # baseline + regions from the new live slot
        await asyncio.sleep(_NEW_CHAT_SETTLE_S)  # let the fresh chat render its input box
        return True

    def end_browser_chat(self) -> None:
        """Hand the automation back to the master chat when a delegation ends.

        Unconditional and never fails: the master window is where the session
        lives, so returning to it must work even after the sub-run blew up.
        """
        self._live = AgentSlot.MASTER
        self._reset_finish_trigger()
        with suppress(NoMatches):
            self.sidebar.hide_paste_flash()
        self._start_detector_worker()

    # -- ChatView: sub-agent transport (thin adapters over the slot primitives) -
    # The port speaks SessionRefs (it must not know what a screen slot is); the
    # primitives above speak slots (they must not know what a session is). The
    # mapping is the whole adapter: role "subagent" drives the SUBAGENT window,
    # everything else the master's.

    async def start_chat(self, session: SessionRef) -> bool:
        slot = AgentSlot.SUBAGENT if session.role == "subagent" else AgentSlot.MASTER
        return await self.start_browser_chat(slot)

    async def end_chat(self, session: SessionRef) -> None:
        # The ref is not consulted: whatever ran, the automation goes back to the
        # master window. It runs in the controller's ``finally``, so it must be
        # unconditional (see ``end_browser_chat``).
        self.end_browser_chat()

    @on(Button.Pressed, "#edit-services-btn")
    def _on_edit_services(self, event: Button.Pressed) -> None:
        # The service editor lands on the app's settings action (F2); this button is
        # its discoverable door. Nothing else about the sidebar needs to change when
        # that screen ships - it only has to call sidebar.refresh_services(config).
        event.stop()
        cast("AgentClipApp", self.app).action_settings()

    def _sync_sidebar(self) -> None:
        """The service is fixed for the life of a session: unlocked only between them."""
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.sidebar.set_locked(not self.awaiting_new_session)

    def action_cancel_execution(self) -> None:
        """ctrl+x while the running bar is up: stop the tool call in flight. The
        controller no-ops if nothing is executing, so a stray press is safe."""
        self._controller.cancel_execution()

    def action_undo(self) -> None:
        self._controller.undo()

    def action_recopy(self) -> None:
        self._controller.recopy()

    def action_force_ingest(self) -> None:
        self._controller.force_ingest()

    def action_end_session(self) -> None:
        self._controller.end_session()

    def action_export_log(self) -> None:
        self._controller.export_log()

    def action_follow_up(self) -> None:
        if not self.session_active:
            return
        self._focus_composer()

    def action_toggle_watch(self) -> None:
        if self._provider.name == "manual" or not self.session_active:
            return
        if self._watch_worker is not None:
            self._watch_worker.cancel()
            self._watch_worker = None
            self.watch_paused = True
            self._paint_status()
            self.notify("clipboard watcher paused - w resumes, i ingests manually")
        else:
            self._start_watcher()
            self._paint_status()
            self.notify("clipboard watcher resumed")

    def action_toggle_last(self) -> None:
        try:
            last = self.transcript.query(Collapsible).last()
        except NoMatches:
            return
        last.collapsed = not last.collapsed

    # -- composer enable/disable + focus (presentation) -----------------------

    def _update_composer(self) -> None:
        """Enable/disable the chat box and set its prompt to match the phase."""
        if not self.is_mounted:
            return
        try:
            composer = self.composer
        except NoMatches:
            return
        if self.awaiting_new_session:
            composer.disabled = False
            composer.border_title = (
                "Describe the task  ·  Enter starts the session · Ctrl+J newline"
            )
        elif self.awaiting_answer:
            composer.disabled = False
            composer.border_title = "Answer the model  ·  Enter sends · Ctrl+J newline"
        elif self.sub_running and not self.pending_approval:
            # The master's flow is busy for the whole delegation, so the usual
            # "armed and idle" rule would lock the box - but /abort is the only
            # way to end a sub-run, and it is typed here.
            composer.disabled = False
            composer.border_title = "Sub-agent running  ·  /abort ends it and tells the model"
        elif (
            self.session_active
            and not self.busy
            and not self.pending_approval
            and self.phase_name in ("AWAITING_REPLY", "DONE")
        ):  # armed and idle, or completed: ready for a follow-up (DONE reopens it)
            composer.disabled = False
            composer.border_title = (
                "Task done · type a follow-up to continue · Esc for shortcuts"
                if self.phase_name == "DONE"
                else "Message the model  ·  Enter sends · Ctrl+J newline · Esc for shortcuts"
            )
        else:  # no session, executing, at a gate, etc.
            composer.disabled = True
            composer.border_title = self._composer_idle_title()

    def _composer_idle_title(self) -> str:
        if not self.session_active:
            return "no session"
        if self.busy:
            return "working - the chat box is paused"
        if self.pending_approval:
            return "approve or reject the action above first"
        return ""

    def _focus_composer(self) -> None:
        if not self.is_mounted or self.app.screen is not self:
            return  # a modal (summary, confirm, new-session) owns focus right now
        try:
            composer = self.composer
        except NoMatches:
            return
        if not composer.disabled:
            composer.focus()

    # -- status bar -----------------------------------------------------------

    def _watch_segment(self) -> tuple[str, str]:
        """The leftmost status segment: what the app wants from the user next.

        While a delegated run is live the whole segment is rebadged magenta and
        prefixed ``◆ SUB-AGENT``, because everything it reports - the phase, the
        approval, the question - is that sub-agent's, not the conversation the
        user is watching.
        """
        text, style = self._base_watch_segment()
        if self._session_role == "subagent":
            return f"◆ SUB-AGENT · {_strip_glyph(text)}", "st-sub"
        return text, style

    def _base_watch_segment(self) -> tuple[str, str]:
        if self.phase_name == "DONE":
            return "✓ done - reply to continue", "st-done"
        if self.pending_approval:
            return "■ APPROVE NEEDED", "st-attn"
        if self.awaiting_answer:
            return "■ ANSWER NEEDED", "st-attn"
        if self.busy:
            return "● working...", "st-busy"
        if self._provider.name == "manual":
            return "✗ manual paste", "st-err"
        if self.watch_paused:
            return "○ paused", "st-dim"
        if self.session_active and self.phase_name == "AWAITING_REPLY":
            return "● ready - paste the reply", "st-armed"
        return "○ idle", "st-dim"

    def _paint_status(self) -> None:
        if not self.is_mounted:
            return
        try:
            bar = self.status_bar
        except NoMatches:
            return
        watch_text, watch_class = self._watch_segment()
        snap = self._snap
        service = f"{snap.service_key} {_fmt_k(snap.budget_chars)}" if snap else "no session"
        out = (
            f"out {_fmt_k(snap.last_outbound_chars)}/{_fmt_k(snap.budget_chars)} (1/1)"
            if snap
            else "out -"
        )
        turn = f"turn {snap.turn}" if snap else "turn -"
        if snap and snap.yolo:
            edits, edits_class = "⚡ YOLO", "st-yolo"
        elif snap and snap.auto_accept_edits:
            edits, edits_class = "EDITS:auto", ""
        else:
            edits, edits_class = "EDITS:ask", ""
        try:
            root = str(Path("~") / self._project_root.relative_to(Path.home()))
        except ValueError:
            root = str(self._project_root)
        bar.update_segments(
            watch=watch_text,
            watch_class=watch_class,
            service=service,
            out=out,
            turn=turn,
            edits=edits,
            edits_class=edits_class,
            root=root,
        )


def _stats_table(rows: list[tuple[str, str]]) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    for label, value in rows:
        table.add_row(label, value)
    return table
