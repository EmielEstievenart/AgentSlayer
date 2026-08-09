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

**A tab is a browser window** (``WindowTabs``, two rows: master windows over the
selected master's sub-agent windows). Not a session view - a window outlives
every session that runs in it, so the tabs are fixed furniture: one per window
AgentClip drives, each with its own service, its own drawn rectangle and its own
transcript panel that simply accumulates. Selecting a tab is what the AGENT SLOT
picker used to be: it points ``_calibrating`` (and the sidebar's service picker)
at that window and shows its transcript. It never touches ``_live`` - the
automation keeps driving the window it was retargeted at, whatever the user is
looking at.

Which panel the controller writes into is ``_focused_panel``, moved only by
``focus_session_view`` - never by the user clicking a tab. That split is the
point: the user can read the master's conversation while a sub-agent runs, and
live output still lands where it belongs instead of in whatever tab happens to
be visible. A delegation appends a divider and its run to the sub-agent window's
panel rather than minting a pane, so the window's whole history is one scroll -
and ``render_log`` slices it back apart per run, because an export wants one
heading per sub-task, not one over five.

Threading: the clipboard watcher is a ``run_worker(thread=True)`` that bridges
captures via the thread-safe ``post_message(ClipboardCaptured)`` -> the controller.
The controller's flow coroutines run via ``spawn`` (also ``run_worker``), so Textual
cancels everything on unmount. The finish detectors are the same shape: ONE
thread worker takes a single capture of the live chat region per tick and
hands it to whichever detectors the active service ASKS FOR and can run - its
``finish_signals`` checklist picks from busy/idle/stale, and the two icon
detectors additionally need their appearance captured - bridging ``BusyProbed``
/ ``IdleProbed`` / ``StaleProbed`` back to the UI. An empty set means no worker
at all and a sidebar line saying finish detection is off.

Their combined verdict drives the copy-button auto-click (``_evaluate_finish``):
the busy appearance is on screen only WHILE the model generates, so finding it
means "still generating"; the idle appearance is on screen only while the chat
is idle, so finding it means "finished"; and the stale detector needs no
appearance at all - the drawn chat region reading unchanged for the preset's
``stable_seconds`` means "finished". That last one is the service-agnostic
fallback for chats whose busy/idle cues are unreliable, and the reason one
drawn box is already a working finish detector. A busy/idle "generating"
verdict arms the trigger on the spot; a stale one has to be a sustained large
delta (``SEND_ARM_MIN_DIFF``/``SEND_ARM_TICKS``) before it counts, or the caret
blinking between AgentClip's paste and the user's Enter arms it and the still,
reply-less screen then fires it. Either way it fires only once EVERY running
detector says "finished" on two consecutive ticks - with more than one running,
that agreement is the whole point of having them. Firing runs
``_auto_copy_flow``: click the live chat input box, scroll to the bottom, find
the newest (lowest) copy-button icon anywhere in the drawn region - falling
back, for services whose preset opts into ``hover_scan``, to a hover scan for
chats that only render the icon under the pointer - click it, and let the
clipboard watcher ingest the copy. The flow itself scrolls and hover-scans the
very screen those detectors watch, so evaluation is suspended while it runs
(``_flow_running``) and every tracker forgets its history when the flow ends -
without that the flow would read as a fresh generation and re-fire itself
forever.

The chat input box is not a stored location at all any more: the two layouts a
service can show (a fresh chat centres its box, an ongoing one docks it at the
bottom) are *appearances* captured once per service (``screen.profile``) and
searched for INSIDE the drawn chat region on the spot (``_find_all``). Moving
or resizing the browser therefore costs nothing.
The browser's new-chat button works the same way: captured once per service,
found inside the chat region and clicked where it actually is
(``_click_profile_element``). Every such search asks for ALL the matches
rather than the first, because an appearance belongs to the service and not to
a window: two windows of the same service overlapping one drawn region make it
findable twice, and "click the first one" is exactly how a sub-agent's
new-chat click lands in the master's window. Two genuinely distinct matches
are therefore a refusal, never a coin toss.

Every one of those calibrations belongs to an *agent slot*
(:mod:`agentclip.screen.slot`), not to the screen: MASTER is the chat the
session runs in, SUBAGENT the second window a delegated sub-agent gets. The slot
is the storage key behind a window tab (``_WINDOW_SLOTS``) - that mapping is the
seam an N-window bar plugs into, and it is why the readiness rules and the
calibration dataclass never had to learn what a tab is. Two independent pointers
say what happens to which slot - ``_calibrating`` is the selected tab, ``_live``
is what the automation drives right now - because the user must be able to
calibrate (and watch) the sub-agent window while the master chat is mid-turn.
``start_browser_chat``/``end_browser_chat`` are the only things that move
``_live``, and ``start_browser_chat`` is all-or-nothing on purpose: it retargets
the automation *only* after a verified click landed, so a False return
guarantees nothing was clicked and nothing was retargeted - a sub-agent
bootstrap pasted into the master chat would corrupt that conversation
irrecoverably.

**A service per window, too.** Each tab carries its own service key
(``_services``), so the conversation the user steers can run on a big-context
chat while delegated sub-tasks go to a cheap fast one. Every "what does this
look like / how long is stillness / may I hover-scan" question therefore has to
name a slot: ``_slot_preset``/``_slot_profile`` answer it, ``_live_preset``/
``_live_profile`` are the automation's shorthand for the window it is driving,
and ``_active_preset``/``_active_profile`` are the sidebar's for the tab the
user has selected. The two coincide constantly and are never the same question.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
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
from textual.widgets import Button, Collapsible, Footer, Input
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
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import CaptureError, RegionImage, capture_region
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
from agentclip.screen.presence import PresenceTracker
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.profile_store import load_profile
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
    find_all_in_region,
    find_lowest_in_region,
    match_rect,
)
from agentclip.tui.messages import BusyProbed, ClipboardCaptured, IdleProbed, StaleProbed
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.summary import SummaryScreen
from agentclip.tui.screens.text_entry import TextEntryScreen
from agentclip.tui.widgets.action_panel import ActionPanel
from agentclip.tui.widgets.command_popup import CommandPopup
from agentclip.tui.widgets.composer import ChatComposer
from agentclip.tui.widgets.running_bar import RunningBar
from agentclip.tui.widgets.sidebar import (
    COPY_RESTING,
    ENTER_FLASH_TEXT,
    PASTE_FLASH_TEXT,
    PROBE_RESTING,
    PROBE_UNCAPTURED,
    STALE_CALIBRATED,
    STALE_OFF,
    STALE_UNSET,
    STALE_UNTICKED,
    Sidebar,
    slot_note,
)
from agentclip.tui.widgets.statusbar import StatusBar
from agentclip.tui.widgets.transcript import TranscriptPanel
from agentclip.tui.widgets.window_tabs import WindowSpec, WindowTabs

if TYPE_CHECKING:  # only for the action_settings hand-off; importing it for real would cycle
    from agentclip.tui.app import AgentClipApp

# Finish-detector poll cadence (tests monkeypatch this to something tiny).
_BUSY_POLL_S = 0.5
# What it takes for the STALE detector alone to arm the auto-copy trigger, i.e.
# to claim it has watched the user's message actually get sent.
#
# The busy/idle detectors arm on one frame, because a reasoning icon appearing
# is evidence nothing else produces. Frame-to-frame change is not: after
# AgentClip pastes the outbound text the user still has to press Enter, and in
# that window a blinking caret or a mouse-over highlight makes the region
# "change" by a handful of pixels. Arming on that, then reading the still
# pre-Enter screen as finished, fires the auto-copy at a chat with no reply in
# it at all - the exact bug these two constants exist to close.
#
# So a CHANGING verdict must be BIG and SUSTAINED: 2% of the sampled pixels
# (caret blink and hover tints are orders of magnitude below; a prompt landing
# in the transcript and the reasoning UI unfolding are far above) on
# SEND_ARM_TICKS consecutive stale probes - ~1.5 s at the 0.5 s cadence, longer
# than any repaint and shorter than any answer.
SEND_ARM_MIN_DIFF = 0.02
SEND_ARM_TICKS = 3
# Hover pause before clicking a calibrated element, for the same reason the copy
# click settles: web UIs paint their buttons on hover.
_ELEMENT_CLICK_SETTLE_S = 0.05
# Beat between opening a fresh browser chat and treating it as the live slot -
# the page still has to render its (centred) input box. Tests shrink this.
_NEW_CHAT_SETTLE_S = 0.4

# What the user is asked to draw for a slot. It is the ONLY thing they draw
# per window now, and it has to be generous rather than tight: everything else
# is recognised inside it, including the new-chat button, which most chat sites
# park in a sidebar outside the conversation column.
_CHAT_REGION_PROMPT = (
    "Drag a box around the WHOLE browser window hosting the chat - including its "
    "sidebar, so the New Chat button is inside it · Esc cancels"
)

# The session id the controller uses for the conversation the user started.
MASTER_VIEW = "master"

# The browser windows AgentClip drives, as the tab bar names them. One master
# and one sub-agent for now; the ids are shaped for the N x N bar (``m2``,
# ``m1-s2``) so nothing downstream has to be re-taught what a window id looks
# like when the second pair arrives.
MASTER_WINDOW = "m1"
SUBAGENT_WINDOW = "m1-s1"

# The seam between "a tab" and "the calibration store". Everything below the
# tab bar - SlotCalibration, can_delegate, start_browser_chat - speaks slots and
# knows nothing about tabs; this dict is the entire translation, and the place
# an N-window bar plugs in (a window id would become a slot *identity* rather
# than one of two enum members).
_WINDOW_SLOTS: dict[str, AgentSlot] = {
    MASTER_WINDOW: AgentSlot.MASTER,
    SUBAGENT_WINDOW: AgentSlot.SUBAGENT,
}
_SLOT_WINDOWS: dict[AgentSlot, str] = {slot: win for win, slot in _WINDOW_SLOTS.items()}

# What a window tab is called, before the state glyph and the service key.
_WINDOW_NAMES = {MASTER_WINDOW: "MASTER", SUBAGENT_WINDOW: "SUB-AGENT"}


def _panel_id(window: str) -> str:
    """The transcript panel's widget id. The master window keeps the pre-tabs
    ``#transcript`` so every existing selector - and every test that reaches for
    the transcript - resolves unchanged."""
    return "transcript" if window == MASTER_WINDOW else f"tr-{window}"


def _run_divider(title: str) -> str:
    """The line that separates one delegated run from the next in the sub-agent
    window's transcript. The window's panel is never cleared between runs, so
    this is the only thing saying where one sub-task ended and the next began."""
    return f"── task: {title} ──"


@dataclass(slots=True)
class _SubRun:
    """Where one delegated run lives inside the sub-agent window's transcript.

    The window's panel is persistent now, so a run is a *slice* of it rather
    than a panel of its own: ``start`` is the index of its first event in
    ``TranscriptPanel.event_log`` (just past the divider) and ``end`` is set
    when it finishes. The log is never pruned, so both stay valid for the life
    of the session - which is what lets ``render_log`` put one ``## sub-agent:``
    heading over each run instead of one over all of them.
    """

    ref: SessionRef
    start: int
    end: int | None = None


class ElementClick(Enum):
    """Outcome of the find-then-click primitive (``_click_profile_element``).

    Five states, not a bool, because the four failures are four different
    things to tell the user: nothing to look for or nowhere to look
    (NOT_CALIBRATED - go capture it), it is simply not on screen (MISMATCH -
    nothing was clicked, and clicking blind is never the answer), it is on
    screen in more than one place (AMBIGUOUS - which is not a search failure
    but a *drawing* failure, and the fix is to redraw the window), or we
    clicked and the OS refused (NOT_CLICKED - Windows-only input).
    """

    CLICKED = "clicked"
    MISMATCH = "mismatch"  # not on screen right now; refused to click
    AMBIGUOUS = "ambiguous"  # several of them in the region; refused to guess
    NOT_CLICKED = "not_clicked"  # found fine, but the click did not land
    NOT_CALIBRATED = "not_calibrated"  # no chat region drawn, or nothing captured


# How many matches of one appearance are worth collecting. The question the
# search answers is "one, or more than one?", so anything past a handful is the
# same answer - and every extra candidate is a full pixel comparison.
_MAX_MATCHES = 8


def _same_element(one: ScreenRegion, other: ScreenRegion) -> bool:
    """Are these two matches the same physical thing on screen?

    A template routinely matches its own element at several neighbouring
    origins - a pixel or two of drift is well inside the diff threshold - so a
    raw match count says more about anti-aliasing than about how many buttons
    are on screen. Overlapping rectangles are one element: a genuine second
    copy of a button cannot be drawn on top of the first.

    Per axis, not as one radius, because a template can be very lopsided. A
    chat input box is ~800x90, and "within max(width, height)" would fold two
    input boxes 400px apart - the two windows this whole check exists to tell
    apart - into one.
    """
    return (
        abs(one.left - other.left) < one.width and abs(one.top - other.top) < one.height
    )


def _distinct_rects(
    region: ScreenRegion, template: Template, matches: list[RegionMatch]
) -> list[ScreenRegion]:
    """Scene-local matches as absolute rectangles, one per physical element."""
    kept: list[ScreenRegion] = []
    for match in matches:
        rect = match_rect(region, template, match)
        if not any(_same_element(rect, other) for other in kept):
            kept.append(rect)
    return kept


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
        # One transcript panel per browser WINDOW, keyed by window id, both
        # mounted by compose and registered on mount. They are permanent: a
        # window's transcript accumulates every session and every delegated run
        # that happened in it, and only ``/new`` empties them.
        # ``_focused_panel`` is the window the CONTROLLER's output goes into -
        # deliberately NOT the tab the user is looking at, so reading the
        # master's conversation mid-delegation can never misroute the
        # sub-agent's output into it (see ``transcript``).
        self._panels: dict[str, TranscriptPanel] = {}
        self._focused_panel = MASTER_WINDOW
        # Which window tab is selected: what the sidebar configures and what the
        # user sees. Moves ``_calibrating`` with it; never ``_live``.
        self._selected_window = MASTER_WINDOW
        self._sessions: dict[str, SessionRef] = {}  # session id -> its identity
        # Every delegated run so far, in order, as slices of the sub-agent
        # window's transcript (see ``_SubRun``). Cleared with the session.
        self._sub_runs: list[_SubRun] = []
        # The service each window tab is pointed at. Two windows, two services:
        # a big-context chat for the conversation the user steers, something
        # cheap and fast for delegated sub-tasks. The sidebar's picker edits
        # whichever tab is selected; the automation reads whichever window it is
        # driving (``_live_preset`` / ``_live_profile``).
        self._services: dict[str, str] = self._initial_services(config)
        # Every user-drawn calibration, one set per agent slot - which since the
        # appearance model is exactly one thing: the chat window. That single
        # box is where every appearance is searched for, the click target of
        # last resort, and the whole calibration of the staleness detector.
        # It describes where a service's window IS, not what one conversation
        # said, so it survives /new; only the pointers below reset. What the
        # service LOOKS like lives in ``_profiles`` instead - captured once,
        # shared by both slots, persisted across runs.
        #
        # ``_calibrating`` is the slot behind the SELECTED window tab - what the
        # sidebar configures; ``_live`` is the slot the automation (paste click,
        # detector poller, auto-copy) drives. They move independently: the
        # sub-agent window is calibrated, and read, while the master chat is
        # mid-session.
        self._slots: dict[AgentSlot, SlotCalibration] = new_slots()
        self._calibrating: AgentSlot = AgentSlot.MASTER
        self._live: AgentSlot = AgentSlot.MASTER
        self._delegation_ready = False  # last-seen SUBAGENT can_delegate, for the one-shot toast
        self._region_click_warned = False
        self._detector_worker: Worker[None] | None = None
        # Which poller RUN a verdict belongs to. Bumped by every
        # ``_start_detector_worker``, stamped into every probe message the loop
        # posts, and checked on the way back in (``_ghost``). Cancelling a thread
        # worker only raises a flag - the loop it interrupts still finishes its
        # tick and posts - so without this a probe taken from the window the
        # automation was driving BEFORE a delegation started or ended arrives
        # after the retarget and arms the auto-copy against the new window.
        self._detector_generation = 0
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
        # The latest stale probe's frame-to-frame differing fraction, and the run
        # of consecutive stale probes whose diff cleared SEND_ARM_MIN_DIFF. Only
        # such a run may arm the auto-copy trigger on staleness alone - see
        # ``_evaluate_finish``.
        self._stale_diff: float | None = None
        self._stale_arm_streak = 0
        # The live detectors, kept on self so the auto-copy flow's finally can
        # reset them: the flow drives the very window they watch, so the frames
        # (and streaks) it leaves behind must not count as history.
        # ``_active_detectors`` is which of them the current worker posts, in
        # the fixed busy -> idle -> stale order - the seam that says which
        # message closes a tick (``_finish_tick_closed_by``).
        self._busy_tracker: PresenceTracker | None = None
        self._idle_tracker: PresenceTracker | None = None
        self._stale_tracker: StaleTracker | None = None
        self._active_detectors: tuple[str, ...] = ()
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

    # -- window tabs -> slots --------------------------------------------------

    @staticmethod
    def _initial_services(config: Config) -> dict[str, str]:
        """Which service each window tab starts on.

        The master's is the configured default; the sub-agent window's is
        ``[general] subagent_service`` when it names a real preset and the
        master's otherwise, which is what makes the second key optional for
        everybody running one service in both windows.
        """
        master = config.general.service
        if master not in config.services:
            master = next(iter(sorted(config.services)))
        sub = config.general.subagent_service
        return {
            MASTER_WINDOW: master,
            SUBAGENT_WINDOW: sub if sub in config.services else master,
        }

    @staticmethod
    def _window_of(slot: AgentSlot) -> str:
        return _SLOT_WINDOWS[slot]

    @staticmethod
    def _slot_of(window: str) -> AgentSlot:
        return _WINDOW_SLOTS[window]

    def _window_of_session(self, session_id: str) -> str | None:
        """Which browser window a session's output belongs in.

        The mapping the ChatView port is expressed in: a sub-agent session runs
        in the sub-agent window, anything else in the master's. Unknown ids
        answer None rather than guessing - losing a transcript line beats
        writing it into the wrong conversation's panel.
        """
        ref = self._sessions.get(session_id)
        if ref is not None:
            return SUBAGENT_WINDOW if ref.role == "subagent" else MASTER_WINDOW
        return MASTER_WINDOW if session_id == MASTER_VIEW else None

    # -- slots ----------------------------------------------------------------

    @property
    def calibrating(self) -> SlotCalibration:
        """The slot behind the selected window tab - what the sidebar edits."""
        return self._slots[self._calibrating]

    @property
    def live(self) -> SlotCalibration:
        """The slot the automation drives right now (paste click, finish
        detector, auto-copy). Only ``start_browser_chat``/``end_browser_chat``
        move it - everything else reads it."""
        return self._slots[self._live]

    # The last compatibility proxy onto the MASTER slot. The single-window
    # vocabulary predates slots and is what the older Pilot suites poke; only
    # ``_chat_region`` is left, because it is the only thing a slot still holds.
    @property
    def _chat_region(self) -> ScreenRegion | None:
        return self._slots[AgentSlot.MASTER].chat_region

    @_chat_region.setter
    def _chat_region(self, value: ScreenRegion | None) -> None:
        self._slots[AgentSlot.MASTER].chat_region = value

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Chat column on the left, settings sidebar on the right; the status bar
        # and the footer stay full width underneath both.
        with Horizontal(id="body"):
            with Vertical(id="main-col"):
                # One tab per browser window, in two rows: masters on top, the
                # selected master's sub-agent windows under it. Both windows'
                # panels are mounted here for good; only one is displayed.
                yield WindowTabs(
                    [
                        WindowSpec(
                            MASTER_WINDOW,
                            self._window_label(MASTER_WINDOW),
                            (WindowSpec(SUBAGENT_WINDOW, self._window_label(SUBAGENT_WINDOW)),),
                        )
                    ],
                    id="chats",
                )
                with Vertical(id="chat-panels"):
                    for window in _WINDOW_SLOTS:
                        yield TranscriptPanel(id=_panel_id(window))
                yield ActionPanel(id="action")
                yield RunningBar(id="running")
                # Directly above the box, so the list a keystroke is filtering
                # sits where the eye already is (§3.3a). Hidden until it isn't.
                yield CommandPopup(id="cmd-popup")
                yield ChatComposer(id="composer")
            yield Sidebar(self._config, self._project_root, id="sidebar")
        yield StatusBar(id="statusbar")
        yield Footer()

    @property
    def transcript(self) -> TranscriptPanel:
        """The panel the controller's output goes into right now.

        The *focused* window, not the selected tab: the user may be reading the
        master's conversation while a sub-agent runs, and output landing in the
        tab they happen to have open would look exactly like data loss. Falls
        back to the master panel (and raises ``NoMatches`` before the screen is
        mounted, which every ``add_*`` already suppresses).
        """
        panel = self._panels.get(self._focused_panel)
        if panel is not None and panel.is_mounted:
            return panel
        return self.query_one(f"#{_panel_id(MASTER_WINDOW)}", TranscriptPanel)

    @property
    def chat_tabs(self) -> WindowTabs:
        """The two-row window tab bar."""
        return self.query_one("#chats", WindowTabs)

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
    def command_popup(self) -> CommandPopup:
        """The slash-command list above the composer (the composer drives it)."""
        return self.query_one(CommandPopup)

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
        # The editor can delete a service's captured appearances (and a service
        # itself), so the per-run cache is no longer trustworthy - drop it and
        # let the next read come off disk.
        self._profiles.clear()
        # A deleted service can also be the one a window tab is pointed at. The
        # sidebar re-picks for the SELECTED tab on its own (refresh_services),
        # but the other tab has no widget to catch it, and a window pointed at a
        # preset that no longer exists would silently drive the automation off
        # ``Config.preset()``'s fallback.
        for window, key in list(self._services.items()):
            if key not in config.services:
                self._services[window] = self._initial_services(config)[window]
                self._relabel_window(window)
        self._paint_profile()
        self._after_calibration()
        # Everything the running poller was built from can have just changed:
        # the preset's ``stable_seconds`` (baked into the stale tracker's tick
        # count at start) and the busy/idle appearances behind it (the editor
        # can forget them). Without this restart an edited stillness window only
        # took effect on the next unrelated recalibration.
        self._start_detector_worker()

    def on_mount(self) -> None:
        for window in _WINDOW_SLOTS:
            self._panels[window] = self.query_one(f"#{_panel_id(window)}", TranscriptPanel)
        self._show_panel(MASTER_WINDOW)
        self._paint_status()
        self._update_composer()
        self._sync_sidebar()
        # Appearances captured on a previous run are already usable: show them.
        self._paint_profile()
        # Nothing is drawn yet, so this starts no worker - but it is the only
        # writer of the DETECTION block, and the block has to name the window it
        # is about (the master's) from the first frame rather than after the
        # first calibration.
        self._start_detector_worker()
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
        # calibrations SURVIVE for both windows: they describe where the
        # service's windows are, not what the finished session said, so /new
        # must not make the user re-draw every region. Nor do the TABS go
        # anywhere - a window outlives the sessions run in it; only their
        # transcripts are emptied. The pointers go home to MASTER (the next
        # session starts by driving the master window) and the detector worker
        # restarts against it so the surviving baselines keep being polled.
        await self._remove_session_views()
        self._live = AgentSlot.MASTER
        self._select_window(MASTER_WINDOW)
        # Re-derive rather than clear: the sub-agent slot is still calibrated,
        # and _after_calibration's one-shot toast must not re-fire after /new.
        self._delegation_ready = self.delegation_available()
        self._reset_finish_trigger()
        self._start_detector_worker()
        with suppress(NoMatches):
            self.sidebar.hide_paste_flash()
        for panel in self._panels.values():
            with suppress(NoMatches):
                await panel.clear_events()

    def has_transcript_events(self) -> bool:
        if not self._panels:
            return False
        return any(panel.event_log for panel in self._panels.values())

    def render_log(self, meta_lines: list[str]) -> str:
        """The master's log, then every sub-agent RUN under its own heading.

        One exported document for the whole delegation tree: the master's
        transcript reads end to end (the sub-runs appear in it as the delegate
        call and its result) and each delegated run's transcript follows, so
        nothing a sub-agent did is only visible in a tab.

        Per run, not per window, even though the runs now share one panel. Five
        sub-tasks under a single ``## sub-agent:`` heading is a wall; each one
        under its own title and chat name is the readable thing an export is
        for. ``_sub_runs`` remembers where each run's events start and end in
        the panel's (unpruned) log, which is all it takes to slice them back
        apart.
        """
        master = self._panels.get(MASTER_WINDOW)
        if master is None:
            master = self.query_one(f"#{_panel_id(MASTER_WINDOW)}", TranscriptPanel)
        parts = [master.render_log(meta_lines)]
        sub = self._panels.get(SUBAGENT_WINDOW)
        if sub is not None:
            for run in self._sub_runs:
                chat = f" ({run.ref.chat_name})" if run.ref.chat_name else ""
                body = sub.render_events(run.start, run.end)
                parts.append(f"## sub-agent: {run.ref.title}{chat}\n\n{body}".rstrip() + "\n")
        return "\n".join(parts)

    # == ChatView: session views (window transcripts) =========================
    # The port speaks session ids; this screen speaks windows. ``_window_of_session``
    # is the whole adapter - a sub-agent session's output belongs in the
    # sub-agent window's panel, whichever run it is.

    async def open_session_view(self, session: SessionRef) -> None:
        """Start ``session``'s transcript in its window and focus that window.

        No pane is minted: the sub-agent window's panel is permanent, so a run
        opens by appending a divider to whatever is already in it and recording
        where it began (``_SubRun``). That is what makes the window's history
        one scroll instead of a graveyard of tabs, and the divider is the only
        thing marking where the previous sub-task ended.

        Focus moves here, not on the user's next click: the controller writes
        the sub-agent's whole run through the ordinary ``add_*`` calls right
        after this returns.
        """
        self._sessions[session.id] = session
        window = self._window_of_session(session.id)
        panel = self._panels.get(window) if window is not None else None
        if panel is not None:
            await panel.add_note(_run_divider(session.title))
            if window == SUBAGENT_WINDOW:
                self._sub_runs.append(_SubRun(session, len(panel.event_log)))
                self._relabel_window(window)
        self.focus_session_view(session.id)

    def focus_session_view(self, session_id: str) -> None:
        """Route every later ``add_*`` into that session's window (and show it).

        Unknown ids are ignored rather than fatal: losing a transcript line is
        never worth taking a running session down with an exception.
        """
        window = self._window_of_session(session_id)
        if window is None:
            return
        self._focused_panel = window
        self._select_window(window)

    async def finish_session_view(self, session_id: str, note: str) -> None:
        """A sub-agent run ended: annotate its transcript and re-badge its tab.

        Nothing is disabled or removed - the panels are output-only and the
        composer always targets the controller's active session, so leaving the
        run readable costs nothing and is the whole point of keeping it. The
        tab drops its ``▶`` for a ``✓``: the label belongs to the WINDOW, so it
        reports whether that window is busy, and the run's own title lives in
        the divider above its transcript.
        """
        window = self._window_of_session(session_id)
        panel = self._panels.get(window) if window is not None else None
        if panel is None:
            return
        await panel.add_note(note)
        if window == SUBAGENT_WINDOW:
            for run in reversed(self._sub_runs):
                if run.ref.id == session_id:
                    run.end = len(panel.event_log)
                    break
            self._relabel_window(window)

    async def _remove_session_views(self) -> None:
        """The /new teardown: forget the runs, keep both window tabs.

        Windows are not session state - the browser is still open, still drawn,
        still pointed at its service - so nothing is unmounted here. Only the
        run bookkeeping goes, and with it the sub-agent tab's ``✓``; the
        transcripts themselves are emptied by ``clear_transcript``.
        """
        self._focused_panel = MASTER_WINDOW
        self._sessions.clear()
        self._sub_runs.clear()
        self._relabel_window(SUBAGENT_WINDOW)

    def action_next_chat_tab(self) -> None:
        """f6: select the next window tab.

        Browsing plus pointing: like clicking the tab, it moves what the user
        SEES and what the sidebar configures, and never where the controller
        writes (see ``transcript``) or which window the automation drives.
        """
        try:
            tabs = self.chat_tabs
        except NoMatches:
            return
        order = tabs.order()
        if len(order) < 2:
            return
        try:
            index = order.index(self._selected_window)
        except ValueError:
            index = -1
        self._select_window(order[(index + 1) % len(order)])

    # -- selecting a window tab ------------------------------------------------

    @on(WindowTabs.WindowSelected)
    def _on_window_selected(self, message: WindowTabs.WindowSelected) -> None:
        message.stop()
        self._select_window(message.window)

    def _select_window(self, window: str) -> None:
        """Make ``window`` the tab the user sees and the sidebar configures.

        This is what the AGENT SLOT picker used to do, plus the transcript: it
        moves ``_calibrating`` (so "Set chat region..." and the service picker
        write into this window), repaints the whole column from that window's
        state, and shows its transcript panel.

        It pointedly does NOT touch ``_live``. Looking at a window is not
        driving it: a click here while a sub-agent is mid-run must not send the
        next paste into the master's chat. Nor does it touch the DETECTION
        block, which reports on the live window and is the detectors' to write.

        Selecting the window that is already selected is a no-op. The tab bar
        re-posts ``WindowSelected`` for a click on the current tab, and every
        widget below would otherwise be rebuilt from state that did not change -
        which is only ever a chance to lose something (a live verdict, a
        readiness note) for no gain: ``_selected_window`` and the displayed
        panel move together, so there is never a stale view to correct.
        """
        if window not in _WINDOW_SLOTS or window == self._selected_window:
            return
        self._selected_window = window
        self._calibrating = self._slot_of(window)
        self._show_panel(window)
        with suppress(NoMatches):
            self.chat_tabs.select(window)
        with suppress(NoMatches):
            self.sidebar.show_service(self._selected_service())
            self.sidebar.show_slot(self.calibrating, self._slot_note())
        self._paint_profile()

    def _show_panel(self, window: str) -> None:
        """Display that window's transcript and hide the others."""
        for other, panel in self._panels.items():
            panel.display = other == window

    def _window_label(self, window: str) -> str:
        """A window tab's text: what it is, how it is doing, what it runs on.

        The service key is on the tab because it is per window now and the
        sidebar only ever shows the selected one's - without it, "which chat is
        the sub-agent going to open?" would be a question you answer by clicking
        around. The glyph is the sub-agent window's live state, derived from the
        runs rather than stored: none before it has ever run, ``▶`` while a run
        is in flight (its slice has no end yet), ``✓`` once one has finished.
        """
        name = _WINDOW_NAMES[window]
        service = self._services.get(window, "")
        glyph = ""
        if window == SUBAGENT_WINDOW and self._sub_runs:
            glyph = "▶ " if any(run.end is None for run in self._sub_runs) else "✓ "
        return f"{glyph}{name} · {service}" if service else f"{glyph}{name}"

    def _relabel_window(self, window: str) -> None:
        with suppress(NoMatches, KeyError):
            self.chat_tabs.set_label(window, self._window_label(window))

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

    async def _find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        """Every place ``kind`` is on screen right now, in absolute coordinates.

        The one primitive behind every "is it there / click it" question. It
        looks *inside the drawn chat region* for the appearance THAT WINDOW's
        own service captured, which is the whole point of the profile model: the
        user drew one box per window, and everything inside it is recognised
        rather than remembered, so moving or resizing the browser costs nothing.
        Per window rather than per app because the two windows can be pointed at
        two different services - a sub-agent chat whose copy icon looks nothing
        like the master's is exactly the case this supports.

        All of them rather than the first, because that is the question callers
        actually have to answer: an appearance is the SERVICE's, so a second
        window of the same service inside (or overlapping) the drawn region
        carries the same button, and clicking whichever match came back first
        would click a different conversation's. Near-duplicate hits on one
        physical element are folded away first (``_distinct_rects``), so a list
        longer than one really does mean two elements.

        ``scene`` lets a caller that already captured the chat region reuse the
        frame, so several appearances can be hunted in one picture of one
        instant - which is why it may not be combined with ``slot``: the frame
        was taken from one window, and translating its matches back through
        another slot's rectangle would put them anywhere at all.

        Empty - never raised - for every way this can come up empty: no chat
        region drawn, no such appearance captured, the capture failed, or it
        simply is not on screen.
        """
        if scene is not None and slot is not None:
            raise ValueError("_find_all takes a slot or a captured scene, never both")
        target = slot if slot is not None else self._live
        cal = self._slots[target]
        region = cal.chat_region
        if region is None:
            return []
        template = self._profile_for(target).get(kind)
        if template is None:
            return []
        if scene is None:
            try:
                scene = await asyncio.to_thread(capture_region, region)
            except CaptureError:
                return []
        matches = await asyncio.to_thread(
            find_all_in_region, template, scene, max_diff=kind.max_diff, limit=_MAX_MATCHES
        )
        return _distinct_rects(region, template, matches)

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

        Two input boxes of the same layout inside the region take that same
        fallback, and for a sharper reason: this click is what focuses the
        window a payload is about to be pasted into, so poking the wrong one
        pastes a whole turn into somebody else's conversation. The drawn region
        is the user's own answer to "where is this chat", so a click in the
        middle of it is the conservative move - and it is exactly what happens
        with no chat box captured at all.

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
            found = await self._find_all(kind, scene=scene)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                self.notify(
                    f"found {len(found)} things that look like the {kind.label} in the chat "
                    "window - clicking its centre instead; redraw the window so it contains "
                    "only this chat",
                    severity="warning",
                )
                return region
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
        command parsing, exactly like an ask_user answer), and it starts the
        session with a service PER WINDOW: the master's tab decides the
        conversation's budgets, the sub-agent tab's decides what any delegation
        will run on. Both are read here, once, because both are locked for the
        session's life. Otherwise the text goes to the controller.
        """
        self._remember_own_window()  # typing here = our terminal has OS focus
        future = self._new_session_future
        if future is not None and not future.done():
            task = text.strip()
            if not task:
                self.notify("describe the task first", severity="warning")
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

    # -- a service per window --------------------------------------------------
    #
    # Three ways to ask "which service?", and mixing them up is the bug this
    # split exists to prevent. ``_service_for(slot)`` is the general one; the
    # automation asks about the window it is DRIVING (``_live_*``) and the
    # sidebar about the tab the user has SELECTED (``_active_*``). They agree
    # almost always and are never the same question - mid-delegation the live
    # window is the sub-agent's while the user reads the master's tab.

    def _service_for(self, slot: AgentSlot) -> str:
        """The service key one window tab is pointed at."""
        key = self._services.get(self._window_of(slot), "")
        return key if key in self._config.services else self._config.general.service

    def _preset_for(self, slot: AgentSlot) -> ServicePreset:
        return self._config.services.get(self._service_for(slot)) or self._config.preset()

    def _profile_for(self, slot: AgentSlot) -> ServiceProfile:
        return self._profile(self._service_for(slot))

    def _live_preset(self) -> ServicePreset:
        """The preset of the window the automation is driving. The stale
        detector reads its ``stable_seconds`` at poller start and the auto-copy
        flow its ``hover_scan``: streaming cadence and icon rendering are
        properties of the service in THAT window, not of AgentClip and not of
        whatever tab is on screen."""
        return self._preset_for(self._live)

    def _live_profile(self) -> ServiceProfile:
        """What the window the automation is driving looks like."""
        return self._profile_for(self._live)

    def _selected_service(self) -> str:
        """The selected tab's service - what the sidebar's picker shows."""
        return self._service_for(self._calibrating)

    def _active_preset(self) -> ServicePreset:
        """The preset behind the sidebar's service picker: the SELECTED window
        tab's service, locked to it while a session runs."""
        return self._preset_for(self._calibrating)

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
        """What the SELECTED window tab's service looks like - the sidebar's
        appearance summary and readiness note, and nothing the automation does.
        """
        return self._profile_for(self._calibrating)

    # -- sidebar --------------------------------------------------------------

    def _slot_note(self) -> str:
        """The sidebar's readiness line for the selected window.

        Composed here, not in the sidebar, because it takes both halves of the
        answer: the window's drawn box and what THAT TAB's service looks like.
        """
        return slot_note(self.calibrating, self._active_profile())

    def _slot_prompt(self, prompt: str, slot: AgentSlot) -> str:
        """Both windows share the picker code, so the sub-agent's prompts have
        to say out loud which window the user is being asked to draw on."""
        if slot is AgentSlot.SUBAGENT:
            return f"SUB-AGENT window · {prompt}"
        return prompt

    def _after_calibration(self) -> None:
        """Repaint the window readiness line after anything readiness depends on
        changed, and tell the user once when the sub-agent window becomes usable
        - the delegate tool is baked into the bootstrap, so it only reaches the
        model on the next /new.

        Both kinds of event have to land here, which is easy to get wrong:
        readiness is composed from the window AND its service's profile, so
        capturing a copy button can flip delegation ON without any region being
        drawn."""
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

    @property
    def picker_open(self) -> bool:
        """Is a fullscreen draw-a-box overlay up right now?

        Read by the app before it opens the service editor: the editor has
        capture buttons of its own, and its guard and this one are separate
        flags on separate screens - satisfied at the same time, they would let
        two overlays stack, which no amount of worker cancellation can undo
        (the overlay is a child process).
        """
        return self._picker_open

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
        # The target slot is decided HERE, when the overlay opens, and travels
        # with the worker - see _pick_chat_region.
        self.run_worker(
            self._pick_chat_region(self._calibrating), group="regionpick", exclusive=True
        )

    async def _pick_chat_region(self, slot: AgentSlot) -> None:
        """Run the draw-a-box overlay (a child process - tkinter cannot live in
        this one) and adopt the drawn chatbot window as ``slot``'s.

        The slot is a PARAMETER rather than a read of ``_calibrating`` on the
        way out, because the overlay blocks for as long as the user takes to
        drag a box and ``_calibrating`` moves on its own in the meantime: the
        controller focusing a delegated run's transcript (``focus_session_view``
        -> ``_select_window``) selects the sub-agent tab. Re-reading it after the
        await filed the box the user drew around the MASTER's window under the
        SUBAGENT slot, and restarted the poller against a rectangle nothing is
        happening in. What was selected when the picker opened is what the user
        was answering.
        """
        try:
            region = await asyncio.to_thread(
                pick_region,
                prompt=self._slot_prompt(_CHAT_REGION_PROMPT, slot),
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        finally:
            self._picker_open = False
        if region is None:
            self.notify("chat region unchanged (selection cancelled)")
            return
        self._slots[slot].chat_region = region
        self._region_click_warned = False
        # Only when the tab it belongs to is still the one on screen: the
        # sidebar shows ONE window's calibration, and writing this one's box into
        # a column describing the other is the same mix-up in the other
        # direction.
        if slot is self._calibrating:
            with suppress(NoMatches):
                self.sidebar.update_region(region)
        self._after_calibration()
        # The drawn window is where every appearance is searched for AND the
        # staleness detector's whole calibration, so the poller has to be
        # rebuilt around it - but ONLY when the window just drawn is the one the
        # poller is watching. Drawing the sub-agent's window mid-session is the
        # normal way to reach delegation, and restarting there would throw away
        # the master's in-flight streaks (and its trackers' previous frames) for
        # a window the automation is not driving.
        if slot is self._live:
            self._start_detector_worker()
        self.notify(
            f"chat region set ({region.describe()}) - the chatbot window; "
            "everything is recognised inside it"
        )

    # -- what the selected tab's service LOOKS like ----------------------------

    def _paint_profile(self) -> None:
        """Repaint the sidebar's read-only appearance summary + probe lines.

        Capturing appearances is the service editor's job now (they are a
        per-service setting, and belong next to the service's other settings),
        so this screen only *reads* the profile: it caches it per run, paints
        this one-line summary of it, hunts for its templates, and drops the
        cache when ``update_config`` says the editor touched the store.
        """
        with suppress(NoMatches):
            self.sidebar.show_profile(self._active_profile())

    @on(Sidebar.ServiceChanged)
    def _on_service_changed(self, message: Sidebar.ServiceChanged) -> None:
        """The user pointed the SELECTED window tab at a different service.

        The picker edits one tab, so the key lands in that window's slot of
        ``_services`` and the tab's label follows. Everything downstream of
        "what does this service look like?" repaints: the appearance summary and
        the readiness note (half of which is the profile).

        The detector worker restarts only when the tab that changed is the one
        the automation is DRIVING. Re-pointing the sub-agent window mid-session
        is the normal way to set delegation up, and rebuilding the master's
        poller there would throw away its in-flight streaks and its trackers'
        previous frames on behalf of a window nothing is watching.
        """
        message.stop()
        self._services[self._selected_window] = message.key
        self._relabel_window(self._selected_window)
        self._paint_profile()
        self._after_calibration()
        if self._calibrating is self._live:
            self._start_detector_worker()

    # -- finish-detector polling -----------------------------------------------

    def _start_detector_worker(self) -> None:
        """Mirrors ``_start_watcher``: one thread worker watching the live
        slot's chat region and bridging each verdict back to the UI via
        ``post_message``. It replaces any previous run, so a recapture or a
        slot move mid-session cannot leave two loops watching two windows.

        ONE capture per tick, shared by every detector. That is not only
        cheaper: the three verdicts then describe the same instant of a moving
        screen rather than three moments of it, and a failed capture reaches
        all of them as the same ERROR instead of some seeing a frame and
        others not.

        What runs is composed from the drawn window, the checklist of the
        service THAT WINDOW is pointed at (never the selected tab's - the user
        reading the master's transcript mid-delegation must not re-aim the
        poller), and that service's captured appearances: a busy
        tracker if the checklist asks for one AND a busy indicator is captured,
        an idle tracker on the same terms, and the stale tracker if the
        checklist asks for it (it needs no capture - a drawn region is a finish
        detector all by itself). With no region drawn there is nothing to watch
        and no worker at all; with nothing runnable the worker is likewise not
        started.

        This is also the ONLY writer of the sidebar's DETECTION block
        (``_paint_detection``): those lines report what is being watched in the
        LIVE window, so every exit here - including the two that start nothing -
        leaves them saying what just became true. Nothing driven by the selected
        tab may touch them, because the tab and the live window part company for
        the whole of a delegation.

        ``_active_detectors`` records which of them will post, in the fixed
        busy -> idle -> stale order, which is what makes the last one the
        tick's closing message (see ``_finish_tick_closed_by``). Everything is
        read once here rather than per tick: restarting the worker is how the
        poller follows the live slot across a delegation, so an in-flight loop
        must keep watching the window it was started for. The trackers are
        likewise built once per run - they carry streaks and a previous frame,
        and all of that describes one window - with the "stable for N seconds"
        wish converted to ticks of the poll cadence here, from the live window's
        service preset. The run gets a fresh ``_detector_generation`` too, which
        every probe it posts carries back (see ``_ghost``).
        """
        self._stop_detector_worker()
        self._detector_generation += 1
        generation = self._detector_generation
        # Every tracker is rebuilt below, so the verdicts they produced belong
        # to detectors that no longer exist. The trigger's ARM survives: it
        # records that the model was generating, which recapturing a button
        # does not un-observe.
        self._busy_seen = self._idle_seen = self._stale_seen = False
        self._busy_finished = self._idle_finished = self._stale_finished = None
        # A half-built large-delta run belongs to the tracker that produced it.
        self._stale_diff = None
        self._stale_arm_streak = 0
        self._busy_tracker = None
        self._idle_tracker = None
        self._stale_tracker = None
        self._active_detectors = ()
        region = self.live.chat_region
        if region is None:
            self._paint_detection(STALE_UNSET)
            return
        profile = self._live_profile()
        preset = self._live_preset()
        signals = preset.finish_signals
        ticks = max(1, round(preset.stable_seconds / _BUSY_POLL_S))
        busy_template = profile.get(TemplateKind.BUSY) if "busy" in signals else None
        idle_template = profile.get(TemplateKind.IDLE) if "idle" in signals else None
        busy = (
            PresenceTracker(
                busy_template,
                found_is_busy=True,
                required_ticks=ticks,
                max_diff=TemplateKind.BUSY.max_diff,
            )
            if busy_template is not None
            else None
        )
        idle = (
            PresenceTracker(
                idle_template,
                found_is_busy=False,
                required_ticks=ticks,
                max_diff=TemplateKind.IDLE.max_diff,
            )
            if idle_template is not None
            else None
        )
        # No ``capture=``: the loop below takes the tick's single capture and
        # hands it to ``observe``, so this tracker never captures for itself.
        stale = StaleTracker(region, required_ticks=ticks) if "stale" in signals else None
        self._busy_tracker, self._idle_tracker, self._stale_tracker = busy, idle, stale
        self._active_detectors = tuple(
            name
            for name, tracker in (("busy", busy), ("idle", idle), ("stale", stale))
            if tracker is not None
        )
        if not self._active_detectors:
            # The service's checklist is empty, or asks only for appearances it
            # has none of. Say so where the stale verdict would go: an unexplained
            # silent readout is indistinguishable from a detector that is simply
            # never finding anything, and the consequence (auto-copy will never
            # fire) is invisible until the user waits for a copy that never comes.
            self._paint_detection(STALE_OFF)
            return
        # Whether the stale line is a live verdict or an explanation of its
        # silence: it is the one detector with no appearance behind it, so
        # "unticked" is otherwise indistinguishable from "not reporting yet".
        self._paint_detection(
            STALE_CALIBRATED if "stale" in self._active_detectors else STALE_UNTICKED
        )

        def loop() -> None:
            worker = get_current_worker()
            while not worker.is_cancelled:
                try:
                    scene: RegionImage | None = capture_region(region)
                except CaptureError:
                    scene = None  # every detector hears about it the same way
                if busy is not None:
                    self.post_message(BusyProbed(busy.observe(scene), generation))
                if idle is not None:
                    self.post_message(IdleProbed(idle.observe(scene), generation))
                if stale is not None:
                    self.post_message(StaleProbed(stale.observe(scene), generation))
                # Sleep in short increments so cancellation lands promptly.
                remaining = _BUSY_POLL_S
                while remaining > 0 and not worker.is_cancelled:
                    step = min(0.05, remaining)
                    time.sleep(step)
                    remaining -= step

        self._spawn_detector_worker(loop)

    def _spawn_detector_worker(self, loop: Callable[[], None]) -> None:
        """Run the composed poll loop as a thread worker.

        The seam between deciding *what* to watch and actually watching it, so
        a test can freeze the polling and still observe the composition - the
        live loop repaints the DETECTION block within milliseconds, which is
        exactly what makes its resting lines otherwise unassertable.
        """
        self._detector_worker = self.run_worker(
            loop, thread=True, group="busyprobe", exit_on_error=False
        )

    def _paint_detection(self, stale_line: str) -> None:
        """Repaint the sidebar's DETECTION block for the LIVE window.

        Owned by the detector machinery alone, and titled with the window it
        describes. Both halves of that matter. Nothing driven by the SELECTED
        tab may write here, because a user reading the master's transcript
        while a sub-agent runs would otherwise see the master's tab clobber a
        readout of the sub-agent's window with "watching the chat region" -
        the exact line that used to overwrite "finish detection off". And with
        the two pointers apart for the whole of a delegation, a block that does
        not name its window is read as the selected tab's.

        The busy/idle lines rest either at "no verdict yet" or, when the
        service's checklist ticks a signal whose appearance was never captured,
        at a line saying so: that combination runs nothing at all, and the only
        symptom is an auto-copy that never fires.
        """
        signals = self._live_preset().finish_signals
        profile = self._live_profile()
        with suppress(NoMatches):
            sidebar = self.sidebar
            sidebar.show_detection_window(_WINDOW_NAMES[self._window_of(self._live)])
            for name, kind in (("busy", TemplateKind.BUSY), ("idle", TemplateKind.IDLE)):
                ticked_but_blind = name in signals and not profile.has(kind)
                sidebar.update_template(
                    kind, PROBE_UNCAPTURED if ticked_but_blind else PROBE_RESTING
                )
            sidebar.update_template(TemplateKind.COPY, COPY_RESTING)
            sidebar.update_stale(stale_line)

    def _stop_detector_worker(self) -> None:
        if self._detector_worker is not None:
            self._detector_worker.cancel()
            self._detector_worker = None

    def suspend_detectors(self) -> None:
        """Stop polling (and disarm the trigger) while a modal owns the screen.

        The service editor is the case this exists for: capturing an appearance
        there throws the same fullscreen draw-a-box overlay up over the browser
        the detectors are watching, and an overlay appearing and disappearing is
        a sustained large delta - which is precisely what arms the auto-copy on
        staleness alone. Left running, closing the editor would then read the
        settled screen as a finished response and fire the copy flow at a chat
        nobody sent anything to. ``resume_detectors`` puts it back.
        """
        self._stop_detector_worker()
        self._reset_finish_trigger()

    def resume_detectors(self) -> None:
        """Restart polling after ``suspend_detectors``.

        A no-op when something already restarted it - adopting an edited Config
        does - so the guaranteed call in the caller's ``finally`` cannot cost a
        second rebuild of a poller that is already watching the right window.
        """
        if self._detector_worker is None:
            self._start_detector_worker()

    def _finish_tick_closed_by(self, detector: str) -> bool:
        """Is ``detector``'s message the tick's LAST, given what is running?

        The poller posts busy -> idle -> stale each tick, skipping whichever
        detector it was not built with, and the combined verdict must fold
        exactly once per tick - on the closing message - or a half-reported
        tick could arm or fire on one detector's word while another's is still
        in flight. ``_active_detectors`` is that build order, so the closer is
        simply its last entry.
        """
        active = self._active_detectors
        return bool(active) and detector == active[-1]

    def _ghost(self, detector: str, generation: int) -> bool:
        """Is this verdict left over from a poller run that is no longer live?

        Cancelling a thread worker only raises a flag: the loop it interrupts
        still finishes the tick it was in and posts its verdicts, which land
        AFTER ``_start_detector_worker`` rebuilt everything. Two ways that hurts,
        and the run's ``generation`` stamp is what catches both.

        The stamp is the load-bearing half. A probe is a reading of ONE browser
        window, and the poller is restarted precisely when the automation
        changes windows (``start_browser_chat`` / ``end_browser_chat``): /abort
        during a generating sub-run hands the master back the live slot, and the
        cancelled loop's in-flight "still generating" then arrives about the
        SUB-agent's window. Filtering by detector name alone let it through -
        both windows run a stale detector - so it armed the trigger and two
        quiet ticks later fired the copy flow at the master's chat. Same story
        for two runs of the same-named detector across a service switch.

        The name check is the older half: when the new detector set is SMALLER
        (a forgotten busy appearance, an unticked signal) the leftovers are
        verdicts about a detector that no longer exists, and a leaked
        "generating" one re-arms the trigger every time, wedging auto-copy shut.

        Dropping a verdict is always safe: a detector that is still running is
        simply refreshed by the next tick, a poll interval later.
        """
        if generation != self._detector_generation:
            return True
        return detector not in self._active_detectors

    def on_busy_probed(self, message: BusyProbed) -> None:
        message.stop()
        if self._ghost("busy", message.generation):
            return
        with suppress(NoMatches):
            self.sidebar.update_template(TemplateKind.BUSY, _format_busy_probe(message.probe))
        self._busy_seen = True
        self._busy_finished = _busy_verdict(message.probe)
        if self._finish_tick_closed_by("busy"):
            self._evaluate_finish()

    def on_idle_probed(self, message: IdleProbed) -> None:
        message.stop()
        if self._ghost("idle", message.generation):
            return
        with suppress(NoMatches):
            self.sidebar.update_template(TemplateKind.IDLE, _format_idle_probe(message.probe))
        self._idle_seen = True
        self._idle_finished = _idle_verdict(message.probe)
        if self._finish_tick_closed_by("idle"):
            self._evaluate_finish()

    def on_stale_probed(self, message: StaleProbed) -> None:
        message.stop()
        if self._ghost("stale", message.generation):
            return
        with suppress(NoMatches):
            self.sidebar.update_stale(_format_stale_probe(message.probe))
        self._stale_seen = True
        self._stale_finished = _stale_verdict(message.probe)
        self._stale_diff = message.probe.diff
        if self._finish_tick_closed_by("stale"):
            self._evaluate_finish()

    def _evaluate_finish(self) -> None:
        """Fold every live detector's latest verdict into one "the model
        stopped" decision, once per poll tick.

        * ANY detector saying "generating" breaks the finished-streak.
        * A busy/idle detector saying "generating" also ARMS the auto-copy
          trigger, immediately, and stops the paste nag - a reasoning icon on
          screen is evidence nothing else produces.
        * The STALE detector saying "generating" arms it only as part of a
          sustained large delta: ``SEND_ARM_TICKS`` consecutive probes whose
          diff clears ``SEND_ARM_MIN_DIFF``. A caret blinking in the composer,
          or a mouse-over highlight, is a CHANGING verdict too - and arming on
          one of those between AgentClip's paste and the user's Enter meant the
          still, reply-less pre-Enter screen then read as a finished response
          and fired the auto-copy at nothing.
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
        # Roll the large-delta run forward on every tick the stale detector
        # reported, so "consecutive" really means consecutive: a small-diff
        # CHANGING (and a STALE or an ERROR) breaks it.
        if self._stale_seen:
            big_delta = (
                self._stale_finished is False
                and self._stale_diff is not None
                and self._stale_diff >= SEND_ARM_MIN_DIFF
            )
            self._stale_arm_streak = self._stale_arm_streak + 1 if big_delta else 0
        if any(verdict is False for verdict in verdicts):
            self._copy_changed_streak = 0
            icon_evidence = (self._busy_seen and self._busy_finished is False) or (
                self._idle_seen and self._idle_finished is False
            )
            if icon_evidence or self._stale_arm_streak >= SEND_ARM_TICKS:
                self._copy_armed = True
                # The send demonstrably happened - the Ctrl+V landed and the
                # user pressed Enter, so stop nagging them to.
                with suppress(NoMatches):
                    self.sidebar.hide_paste_flash()
            return
        if not all(verdict is True for verdict in verdicts) or not self._copy_armed:
            self._copy_changed_streak = 0
            return
        self._copy_changed_streak += 1
        if self._copy_changed_streak < 2 or not self._live_profile().has(TemplateKind.COPY):
            return
        self._copy_armed = False
        self._copy_changed_streak = 0
        self._stale_arm_streak = 0
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
            # The flow clicks, scrolls and hover-scans the very window all
            # three detectors watch, so every streak it leaves behind describes
            # the flow's own mouse work rather than the model's.
            for tracker in (self._busy_tracker, self._idle_tracker, self._stale_tracker):
                if tracker is not None:
                    tracker.reset()
            # Same reasoning for the send-arming run: whatever large deltas the
            # flow's own scrolling produced say nothing about the user sending.
            self._stale_diff = None
            self._stale_arm_streak = 0

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
        self._stale_diff = None
        self._stale_arm_streak = 0
        self._copy_armed = False
        self._copy_changed_streak = 0

    # -- the copy button + auto-copy-click -------------------------------------

    def _copy_status(self, text: str) -> None:
        """Repaint the copy button's status line, keeping its captured size in
        front of whatever the flow has to report."""
        with suppress(NoMatches):
            template = self._live_profile().get(TemplateKind.COPY)
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
        template = self._live_profile().get(TemplateKind.COPY)
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
        if match is None and self._live_preset().hover_scan:
            # Nothing in the static frame: this service is one of the chats that
            # only paint the icon under the pointer, so try again while hovering
            # up the region. Opt-in per service (``hover_scan``) because the scan
            # drives the user's real mouse across the screen - worth it where it
            # is the only way to find the icon, gratuitous everywhere else, where
            # a static miss simply means the icon is not there.
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

    # -- profile elements: the reusable find-then-click -------------------------

    async def _click_profile_element(
        self, slot: AgentSlot, kind: TemplateKind, *, settle_s: float = _ELEMENT_CLICK_SETTLE_S
    ) -> ElementClick:
        """Find ``kind`` inside ``slot``'s chat region right now, and click it.

        The primitive every programmatic click on a service appearance goes
        through. It replaces "click where those pixels used to be" with "click
        where they *are*", which is both safer and the reason the browser may
        move: a page that re-laid itself out, scrolled, or opened a dialog
        simply reads as not-on-screen and gets no click at all. Refusing is
        always the safe answer - the user can click it themselves.

        Finding it TWICE is refused just as firmly. An appearance belongs to
        the service, so a second window of the same service sitting inside the
        drawn region carries an identical button; picking one of them is a coin
        toss between two conversations, and the loser is a chat that gets
        clicked - reset, even - on behalf of the other.
        """
        if self._slots[slot].chat_region is None or not self._profile_for(slot).has(kind):
            return ElementClick.NOT_CALIBRATED
        found = await self._find_all(kind, slot)
        if not found:
            return ElementClick.MISMATCH
        if len(found) > 1:
            return ElementClick.AMBIGUOUS
        clicked = await asyncio.to_thread(click_region, found[0], settle_s=settle_s)
        return ElementClick.CLICKED if clicked else ElementClick.NOT_CLICKED

    # -- the browser's new-chat button ------------------------------------------
    #
    # No sidebar status line: the new-chat button is found on demand rather than
    # polled, so there is no verdict to keep on screen between presses, and
    # every outcome below already says what happened as a toast.

    @on(Button.Pressed, "#newchat-btn")
    def _on_newchat(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_worker(self._new_browser_chat(), group="newchat", exclusive=True)

    async def _new_browser_chat(self) -> None:
        """Click the browser's new-chat button, then hand focus back here.

        The *calibrating* slot's window, so the user can test either one from
        the same place the sidebar is pointed at. It never moves the live slot -
        that is ``start_browser_chat``'s job alone.

        Located first: if the button is not on screen nothing is clicked, because
        the alternative is a blind click somewhere in a browser window."""
        outcome = await self._click_profile_element(self._calibrating, TemplateKind.NEW_CHAT)
        if outcome is ElementClick.NOT_CALIBRATED:
            self.notify(
                'capture the browser\'s new-chat button first (F2 > "Capture new-chat '
                'button...") and draw the chat window it lives in',
                severity="warning",
            )
            return
        if outcome is ElementClick.MISMATCH:
            self.notify(
                "the new-chat button is not on screen in the chat window - nothing "
                "was clicked; recapture it or redraw the window",
                severity="warning",
            )
            return
        if outcome is ElementClick.AMBIGUOUS:
            self.notify(
                "found several things that look like the new-chat button in the chat "
                "window - nothing was clicked; redraw the window so it contains only "
                "this chat",
                severity="warning",
            )
            return
        if outcome is ElementClick.NOT_CLICKED:
            self.notify(
                "the new-chat click did not land (it is Windows-only) - start the chat yourself",
                severity="warning",
            )
            return
        self.notify("new browser chat opened")
        # Same beat as the auto-copy flow: let the click register before focus
        # moves away, then bring the user back to AgentClip.
        if self._own_window is not None:
            await asyncio.sleep(0.15)
            await asyncio.to_thread(focus_window, self._own_window)

    # -- sub-agent transport: opening a chat and retargeting the automation ----

    def delegation_available(self) -> bool:
        """Is the sub-agent window calibrated well enough to run a delegation?

        The single source of truth the controller asks before it even builds a
        sub-agent engine. Deliberately strict (see ``SlotCalibration``): a
        half-calibrated window must read as unavailable rather than strand a
        sub-run halfway through.

        Against the SUB-AGENT tab's own service profile, which is the whole
        point of a service per window: the copy and new-chat buttons the run
        will click are the ones in the chat it is going to open, and the master
        tab having captured its own says nothing about them.
        """
        return can_delegate(
            self._slots[AgentSlot.SUBAGENT], self._profile_for(AgentSlot.SUBAGENT)
        )

    def delegation_missing(self) -> tuple[str, ...]:
        """The calibrations still standing between here and ``can_delegate``.

        Handed to the controller as data so the error the *model* gets when it
        calls ``delegate`` against an uncalibrated host names the actual gaps -
        the controller cannot import ``screen`` to ask, and should not have to
        know what a "new-chat button" is.
        """
        return missing(self._slots[AgentSlot.SUBAGENT], self._profile_for(AgentSlot.SUBAGENT))

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
        outcome = await self._click_profile_element(slot, TemplateKind.NEW_CHAT)
        if outcome is ElementClick.NOT_CALIBRATED:
            self.notify(
                f"the {slot.label} chat's new-chat button is not calibrated - "
                "nothing was clicked",
                severity="error",
            )
            return False
        if outcome is not ElementClick.CLICKED:
            # AMBIGUOUS is the one worth spelling out: nothing is broken, the
            # drawn region simply holds two chats, and the fix is a redraw
            # rather than a recapture.
            reasons = {
                ElementClick.MISMATCH: "is not on screen",
                ElementClick.AMBIGUOUS: (
                    "was found in several places in the drawn window - redraw it so it "
                    "contains only this chat"
                ),
            }
            reason = reasons.get(outcome, "could not be clicked (it is Windows-only)")
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
        """Enable/disable the chat box, set its prompt, and say whether the next
        send is verbatim - the three things that all follow from the same phase.

        ``verbatim`` is the slash-command popup's suppression switch (§3.3a).
        The two modes that consume the box's text literally are exactly the two
        the controller already tells us about: waiting for the task that starts a
        session, and an open ``ask_user`` gate (``SessionView.awaiting_answer``).
        A leading slash means nothing there, so offering to complete it would be
        a lie about what Enter is going to do.
        """
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
        # Last, so the popup is re-decided against the mode we just settled on
        # (the setter re-syncs it, and a disabled box never shows one).
        composer.verbatim = self.awaiting_new_session or self.awaiting_answer

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
