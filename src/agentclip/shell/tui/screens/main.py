"""MainScreen: the Textual adapter that implements the ChatView port.

The session orchestration lives in :class:`agentclip.shell.app.SessionController` (UI-
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

Threading: the clipboard watcher and the detector poller are plain
``threading.Thread``s the AutomationController owns (docs/design/gui.md §1) and
this screen only starts, stops and mirrors; captures come back through
``_clipboard_captured``, which bridges them via the thread-safe
``post_message(ClipboardCaptured)`` -> the controller. The screen's own workers -
the flow coroutines ``spawn`` starts - are ``run_worker``s Textual cancels on
unmount, where both controller threads are stopped by name instead. The
detectors are the same shape as the watcher: ONE poll loop takes a single
capture of the live chat region per tick and hands it to one
``screen.detector.ScreenDetector`` - a plain, Textual-free object that searches
for every appearance the live window's service is CALIBRATED for and answers
with a snapshot - and the loop feeds that snapshot straight into the
controller's own ``consume_*`` calls, on the poller thread, in the busy -> idle
-> stale -> send-ready -> elements order, all in one call stack per tick. What a
probe MEANS never reaches this screen at all: the bookkeeping, both gates, the
loop's narration and the combined verdict are one object below both shells.

What comes back up is paint, and ONLY paint. Every ``AutomationView`` method
this screen implements - ``paint_detection``/``paint_stale``/``paint_elements``/
``paint_loop_state``/``paint_harness_entry``/``paint_armed``, the two banner
calls, ``notify`` - is now two lines: build the typed message that says what to
draw and ``post_message`` it, because the caller is usually the poller thread and
a widget may only be touched on this one. The handlers underneath do the writes.
``on_fire`` crosses the same way (``AutoCopyRequested``), since starting a
Textual worker is this thread's alone. The one thing still asked of this screen
DURING a tick is ``_crop_elements`` - cutting the matched pixels down to panel
size, which happens on the poller thread so the queue carries an icon rather
than a chat window - and ``_live_has``, which reads immutable state only.

The OS-ACTING SEQUENCES went the same way (gui.md §1, slice 6): the auto-copy
harvest, the find-then-click primitive under every programmatic click, the hover
scan and the two calls that move the automation between browser windows are
coroutines on the AutomationController now, reaching ``agentclip.driver.screen``
directly. What is left here is the two things a shell really owns - SCHEDULING
(``run_worker``, still the same group and exclusivity) and the handful of
answers only this screen has, which cross as ``AutomationHost``: which service a
window is on and what it looks like, the seam the search for an appearance is
asked through (``_find_all``, a one-liner onto the controller that the suites
stub), handing a prose reply to the SESSION, and rebuilding the detector set
after a retarget. The primitives themselves are reached through
``_MainScreenOps``, whose only job is to resolve THIS module's names per call so
the suites' patches still bite.

That split is deliberate and load-bearing: **the detector detects, the state
machine consumes**. Nothing about the send gate, the auto-copy flow or the
session can reach into what a tick searches for, which is what lets the ELEMENTS
column show what the tool can see at any moment rather than only during the two
windows the searches used to be gated to. Which FINISH detectors the service
asks for (``finish_signals``, with busy/idle additionally needing their
appearance captured) still decides what can ever fold into a verdict - an empty
set means finish detection is off and the sidebar says so - but a captured send
or copy button is still watched and still shown.

Their combined verdict drives the copy-button auto-click
(``AutomationController.evaluate_finish``):
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
that agreement is the whole point of having them. And none of it happens at
all unless a reply is genuinely outstanding (``_awaiting_pasted_reply``, opened
by ``copy_outbound`` and shut by the harvest): the poller runs off the
CALIBRATION, so it reports on a resting chat too, but only a pasted outbound
lets a verdict reach for the mouse. Riding on top of that gate, for services
whose profile has a ``SEND_READY`` appearance, is the READY-TO-SEND gate
(``AutomationController.open_send_gate``): while the send button is on screen the outbound is
sitting in the composer UNSENT, so finish detection is held back entirely until
the button is seen and then seen to vanish - which is the user's Enter. No
capture means no gate and the behaviour that shipped before it. Delaying a
session is allowed and deadlocking one is not, so nothing about the gate is
open-ended: a busy/idle detector saying the model is GENERATING overrides it on
the spot (nothing answers a message that was never sent), and each phase of it
is on a clock as well - ``SEND_GATE_TIMEOUT_TICKS`` for a button that never
appears, ``SEND_GATE_SEEN_TIMEOUT_TICKS`` for one that appears and then never
goes. Firing runs
``AutomationController.auto_copy_flow``: click the live chat input box, scroll to the bottom, find
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
bottom) are *appearances* captured once per service (``driver.screen.profile``) and
searched for INSIDE the drawn chat region on the spot (``_find_all``). Moving
or resizing the browser therefore costs nothing.
The browser's new-chat button works the same way: captured once per service,
found inside the chat region and clicked where it actually is
(``AutomationController.click_profile_element``). Every such search asks for ALL the matches
rather than the first, because an appearance belongs to the service and not to
a window: two windows of the same service overlapping one drawn region make it
findable twice, and "click the first one" is exactly how a sub-agent's
new-chat click lands in the master's window. Two genuinely distinct matches
are therefore a refusal, never a coin toss.

Every one of those calibrations belongs to an *agent slot*
(:mod:`agentclip.driver.screen.slot`), not to the screen: MASTER is the chat the
session runs in, SUBAGENT the second window a delegated sub-agent gets. The slot
is the storage key behind a window tab (``_WINDOW_SLOTS``) - that mapping is the
seam an N-window bar plugs into, and it is why the readiness rules and the
calibration dataclass never had to learn what a tab is. Two independent pointers
say what happens to which slot - *calibrating* is the selected tab, *live* is
what the automation drives right now - because the user must be able to
calibrate (and watch) the sub-agent window while the master chat is mid-turn.
Both of them, and the calibrations they point at, are the
:class:`~agentclip.driver.automation.controller.AutomationController`'s (``_automation``
below): they are shared automation state, not one shell's, and this screen
reaches them through it. ``start_browser_chat``/``end_browser_chat`` - the
controller's since slice 6, delegated to from here - are the
only things that move the live pointer, and ``start_browser_chat`` is
all-or-nothing on purpose: it retargets the automation *only* after a verified
click landed, so a False return guarantees nothing was clicked and nothing was
retargeted - a sub-agent bootstrap pasted into the master chat would corrupt
that conversation irrecoverably.

**A service per window, too.** Each tab carries its own service key (also the
controller's - ``_automation.service_of``), so the conversation the user steers
can run on a big-context chat while delegated sub-tasks go to a cheap fast one.
What that key RESOLVES to stays here: the preset comes off this screen's
``Config`` and the profile out of its ``_profiles`` cache. Every "what does this
look like / how long is stillness / may I hover-scan" question therefore has to
name a slot: ``_slot_preset``/``_slot_profile`` answer it, ``_live_preset``/
``_live_profile`` are the automation's shorthand for the window it is driving,
and ``_active_preset``/``_active_profile`` are the sidebar's for the tab the
user has selected. The two coincide constantly and are never the same question.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.notifications import SeverityLevel
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Footer, Input

from agentclip.config import (
    DEFAULT_THEME,
    Config,
    ServicePreset,
    save_active_services,
)
from agentclip.driver.automation import delivery as _delivery
from agentclip.driver.automation import flow as _flow
from agentclip.driver.automation.controller import AutomationController, DetectorPoller
from agentclip.driver.automation.finish import (
    SEND_ARM_MIN_DIFF,
    SEND_ARM_TICKS,
    SEND_GATE_SEEN_TIMEOUT_TICKS,
    SEND_GATE_TIMEOUT_TICKS,
    SendGate,
)
from agentclip.driver.automation.harness_log import (
    HARNESS_LOG_MAX,
    KIND_ARMED,
    KIND_CLIPBOARD,
    KIND_SESSION,
    HarnessEntry,
)
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.ops import ElementClick, ScreenOps
from agentclip.driver.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.driver.clip.chunking import STREAM_CHUNK_CHARS
from agentclip.driver.clip.watcher import SelfWriteSet
from agentclip.driver.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.driver.screen.detector import ScreenDetector, Sighting, build_detector
from agentclip.driver.screen.focus import (
    click_region,
    focus_window_verified,
    foreground_window,
    move_cursor,
    scroll_region,
    send_enter,
    send_paste,
    send_scroll_key,
)
from agentclip.driver.screen.hover import STEP_DELAY_S as _HOVER_STEP_DELAY_S
from agentclip.driver.screen.identify import IdentifiedElement, identify_elements, summarise
from agentclip.driver.screen.picker import ScreenPickError, draw_identify_overlay, pick_region
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.profile_store import load_profile
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import (
    AgentSlot,
    SlotCalibration,
    can_delegate,
    missing,
)
from agentclip.driver.screen.stale import StaleTracker
from agentclip.driver.screen.template import (
    CandidateSource,
    RegionMatch,
    Template,
    find_all_in_region,
    find_lowest_with_best_miss,
)
from agentclip.engine.engine import Decision, PendingAction, StatusSnapshot
from agentclip.engine.link.factory import EngineRequest
from agentclip.executor.mcp.types import McpServerStatus
from agentclip.protocol.parser import looks_like_protocol
from agentclip.protocol.types import Outbound, ToolCall
from agentclip.shell.app import SessionController, SessionSpec, SessionView
from agentclip.shell.app.link import Link
from agentclip.shell.app.types import SessionRef
from agentclip.shell.app.view import RunCall, Severity
from agentclip.shell.tui.messages import (
    AutoCopyRequested,
    CallFinished,
    CallOutput,
    CallStarted,
    ClipboardCaptured,
    ElementCrop,
    HidePasteFlash,
    McpStatusChanged,
    NotifyRequested,
    PaintArmed,
    PaintDetection,
    PaintElements,
    PaintHarnessEntry,
    PaintLoopState,
    PaintStale,
    ShowPasteFlash,
)
from agentclip.shell.tui.pixels import crop
from agentclip.shell.tui.screens.confirm import ConfirmScreen
from agentclip.shell.tui.screens.settings import THEME_CHOICES
from agentclip.shell.tui.screens.summary import SummaryScreen
from agentclip.shell.tui.screens.text_entry import TextEntryScreen
from agentclip.shell.tui.widgets.action_panel import ActionPanel
from agentclip.shell.tui.widgets.command_popup import CommandPopup
from agentclip.shell.tui.widgets.composer import ChatComposer
from agentclip.shell.tui.widgets.elements import (
    ElementsPanel,
    element_crop_image,
)
from agentclip.shell.tui.widgets.log_pane import HarnessLogPane
from agentclip.shell.tui.widgets.run_panel import RUN_OUTPUT_LINES, RunPanel
from agentclip.shell.tui.widgets.running_bar import RunningBar
from agentclip.shell.tui.widgets.sidebar import (
    COPY_RESTING,
    PROBE_RESTING,
    PROBE_UNCAPTURED,
    STALE_CALIBRATED,
    STALE_OFF,
    STALE_UNSET,
    STALE_UNTICKED,
    Sidebar,
    slot_note,
)
from agentclip.shell.tui.widgets.statusbar import StatusBar
from agentclip.shell.tui.widgets.transcript import TranscriptPanel
from agentclip.shell.tui.widgets.window_tabs import WindowSpec, WindowTabs

if TYPE_CHECKING:  # only for the action_settings hand-off; importing it for real would cycle
    from agentclip.shell.tui.app import AgentClipApp

# Finish-detector poll cadence (tests monkeypatch this to something tiny).
_BUSY_POLL_S = 0.5
# The finish decision's four tunables are the AutomationController's now
# (agentclip.driver.automation.finish) and are imported above rather than spelled here.
# They stay reachable under this module's names on purpose: the Pilot suites read
# them to size a probe sequence and patch one of them to shrink a two-minute gate
# budget, and this screen hands the values it can see to the controller at
# construction, so patching this module's name still moves the clock.
#
# Beat between opening a fresh browser chat and treating it as the live slot -
# the page still has to render its (centred) input box. Tests shrink this, which
# is why ``_MainScreenOps`` reads it per call rather than at import.
_NEW_CHAT_SETTLE_S = 0.4
# The delivery's beats moved down with the sequence that paces itself by them
# (agentclip.driver.automation.delivery, which documents what each one is FOR). They
# stay reachable - and PATCHABLE - under this module's names, because that is
# where the Pilot suites shrink them: ``_MainScreenOps`` reads all three per
# call, so a monkeypatch here still bites exactly as it did when the paste path
# lived on this screen.
#
# The first is the one worth naming here too: the click is what gives the browser
# window the OS focus, focus is granted ASYNCHRONOUSLY, and a Ctrl+V that
# overtakes the activation is delivered to whatever held focus a moment ago.
PASTE_SETTLE_DELAY = _delivery.PASTE_SETTLE_DELAY
_SUBMIT_SETTLE_S = _delivery.SUBMIT_SETTLE_S
_STREAM_CHUNK_SETTLE_S = _delivery.STREAM_CHUNK_SETTLE_S
# ...and so are the activation wait in front of that first beat and the beat
# before a snap back, for exactly the same reason: a suite that waited out a
# real foreground poll on every delivery is a suite nobody runs.
_ACTIVATION_ATTEMPTS = _delivery.ACTIVATION_ATTEMPTS
_ACTIVATION_POLL_S = _delivery.ACTIVATION_POLL_S
_SNAP_BACK_SETTLE_S = _delivery.SNAP_BACK_SETTLE_S
# The auto-copy harvest's own tuning numbers moved down with the sequence that
# reads them (agentclip.driver.automation.flow). They stay reachable under this
# module's names because the Pilot suites size their assertions off them - how
# many snap rounds a miss gets, how big the flick is, where the keyboard snap's
# focus click lands.
_SNAP_WHEEL_DETENTS = _flow.SNAP_WHEEL_DETENTS
_PAGE_DOWN_TAPS = _flow.PAGE_DOWN_TAPS
_COPY_SNAP_ROUNDS = _flow.COPY_SNAP_ROUNDS
_ABOVE_CHATBOX_PX = _flow.ABOVE_CHATBOX_PX

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
    # How it ended, recorded when ``end`` is. False for every way a run can fail
    # before it produces a deliverable (the fresh chat could not be opened, the
    # bootstrap did not fit, the user aborted, it crashed) - which is what stops
    # the window's tab claiming a ``✓`` for a run that handed nothing back.
    ok: bool = True


def _element_crop(scene: RegionImage, sighting: Sighting | None) -> ElementCrop | None:
    """Cut a verified match out of the frame it was found in, panel-sized.

    The cut runs in the WORKER that captured the scene, never on the UI thread,
    for the same reason the old whole-region thumbnail did (tui.pixels): the
    message queue should carry an icon, not a chat window. What the cut is
    sized to is the panel's business, because it depends on which renderer the
    terminal can drive - ``elements.element_crop_image`` decides.

    ``None`` in, ``None`` out - "nothing matched" and "the match is too
    degenerate to draw" are the same row in the panel, and there is nothing
    useful to tell apart.
    """
    if sighting is None:
        return None
    template, match = sighting.template, sighting.match
    image = element_crop_image(crop(scene, match.x, match.y, template.width, template.height))
    return None if image is None else ElementCrop(image, match.diff)


def _poll_capture(region: ScreenRegion) -> RegionImage:
    """The detector poller's capture, resolved HERE at every tick.

    The loop lives in the AutomationController now, so the function it captures
    with has to be handed in - and handing in ``capture_region`` itself would
    freeze whatever this module's name pointed at when the run started. The
    Pilot suites patch that name (``main_mod.capture_region``), some of them
    while a poller is already running, so the indirection keeps the late binding
    the in-line loop had for free.
    """
    return capture_region(region)


class _MainScreenOps(ScreenOps):
    """``ScreenOps`` with THIS module's names resolved at every call.

    ``_poll_capture``'s reason, generalised to the whole hand the automation
    puts on the machine. The OS-acting sequences live in the
    AutomationController now and reach ``agentclip.driver.screen`` through
    :class:`~agentclip.driver.automation.ops.ScreenOps`; the default implementation
    calls those functions directly, which is right for the app and wrong for
    this shell's test suites - they patch every one of them at
    ``agentclip.shell.tui.screens.main``'s scope, because that is where the code used
    to live, and several of them re-patch mid-run. Overriding here keeps the
    late binding those stubs have always relied on: each method below is a name
    lookup in this module, per call, and nothing else.

    The two beats are here for the same reason and no other: the suites shrink
    them so a test does not sleep its way up a chat region or wait out a fresh
    chat's render.
    """

    def capture(self, region: ScreenRegion) -> RegionImage:
        return capture_region(region)

    def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        return click_region(region) if settle_s is None else click_region(region, settle_s=settle_s)

    def move_cursor(self, x: int, y: int) -> bool:
        return move_cursor(x, y)

    def scroll(self, region: ScreenRegion, detents: int) -> bool:
        return scroll_region(region, detents)

    def scroll_key(self, key: str, taps: int = 1) -> bool:
        return send_scroll_key(key, taps)

    def focus_window(self, handle: int) -> bool:
        return focus_window_verified(handle)

    def foreground_window(self) -> int | None:
        return foreground_window()

    def send_paste(self) -> bool:
        return send_paste()

    def send_enter(self) -> bool:
        return send_enter()

    def lowest_match(
        self,
        template: Template,
        scene: RegionImage,
        *,
        tolerance: int,
        max_diff: float,
        matcher: CandidateSource | None,
    ) -> tuple[RegionMatch | None, float | None]:
        return find_lowest_with_best_miss(
            template, scene, tolerance=tolerance, max_diff=max_diff, matcher=matcher
        )

    def all_matches(
        self,
        template: Template,
        scene: RegionImage,
        *,
        tolerance: int,
        max_diff: float,
        limit: int,
        matcher: CandidateSource | None,
    ) -> list[RegionMatch]:
        return find_all_in_region(
            template,
            scene,
            tolerance=tolerance,
            max_diff=max_diff,
            limit=limit,
            matcher=matcher,
        )

    def hover_step_delay(self) -> float:
        return _HOVER_STEP_DELAY_S

    def new_chat_settle(self) -> float:
        return _NEW_CHAT_SETTLE_S

    def activation_attempts(self) -> int:
        return _ACTIVATION_ATTEMPTS

    def activation_poll(self) -> float:
        return _ACTIVATION_POLL_S

    def paste_settle(self) -> float:
        return PASTE_SETTLE_DELAY

    def snap_back_settle(self) -> float:
        return _SNAP_BACK_SETTLE_S

    def submit_settle(self) -> float:
        return _SUBMIT_SETTLE_S

    def stream_chunk_settle(self) -> float:
        return _STREAM_CHUNK_SETTLE_S

    def stream_chunk_chars(self) -> int:
        return STREAM_CHUNK_CHARS


def _fmt_k(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


# Leading state glyphs the watcher segment prefixes its text with; a sub-agent
# run replaces them with its own, so they are stripped before rebadging.
_STATE_GLYPHS = "●○■✓✗"


def _strip_glyph(text: str) -> str:
    return text.lstrip(_STATE_GLYPHS).lstrip()


class McpStatusSource(Protocol):
    """What this screen needs of the process-wide ``McpManager``: the status
    tuple and the one listener slot (docs/design/mcp.md sections 3 and 6).

    A Protocol rather than the class so a Pilot test can hand in a three-line
    stub with neither the SDK nor a loop thread behind it - the screen only
    ever paints from ``statuses()`` and bridges the hook; connecting is the
    manager's business and stays untested here.
    """

    def statuses(self) -> tuple[McpServerStatus, ...]: ...
    def set_status_hook(self, cb: Callable[[McpServerStatus], None] | None) -> None: ...


class MainScreen(Screen[None]):
    BINDINGS = [
        Binding("y", "approve", "approve"),
        Binding("n", "reject", "reject"),
        Binding("a", "auto_edits", "auto-edits"),
        Binding("u", "undo", "undo"),
        # Two words longer than every other label here, and they buy the only
        # thing the footer can say about a double tap: that there IS one. The
        # escalation is otherwise invisible until the first press toasts it, and
        # a key whose second press moves the mouse has to announce itself.
        Binding("c", "recopy", "re-copy · cc pastes"),
        Binding("i", "force_ingest", "ingest"),
        # Hidden until the active service actually carries extra instructions
        # (check_action), because on every other service there is nothing to
        # re-inject and the footer must not offer a key that only ever toasts.
        Binding("r", "reinstruct", "re-instruct"),
        Binding("w", "toggle_watch", "watcher"),
        Binding("t", "follow_up", "type message"),
        Binding("e", "end_session", "summary"),
        Binding("l", "export_log", "export log"),
        Binding("x", "toggle_last", "expand last", show=False),
        # f3 is priority so it works while the composer (a TextArea) holds focus.
        Binding("f3", "toggle_sidebar", "sidebar", priority=True),
        # The ELEMENTS column's own F3. f5 is spoken for, so f7 - and show=False
        # for the same reason as f6: the footer is already full of the loop's
        # one-key answers, and the two columns name each other's keys in their
        # own hint lines.
        Binding("f7", "toggle_elements", "elements", priority=True, show=False),
        # The harness log pane (§3.3b), the same trade one row down: f1-f7 are
        # all spoken for, so f8. Priority for the third time and the same
        # reason - the composer is a focused TextArea - and show=False because
        # `/log` is how it is discovered (the popup lists it, the help screen
        # names the key) and the footer is already full.
        Binding("f8", "toggle_harness_log", "log", priority=True, show=False),
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
        # Its neighbour, and for the same reason: while a command is grinding
        # away the screen is otherwise inert, and this is the key that opens
        # what it is printing (§8a). Priority because the composer is a TextArea
        # and ctrl+o is one of the few chords it does not already claim.
        Binding("ctrl+o", "toggle_run_output", "output", priority=True, show=False),
        # The permission mode (§2.6a), one key, always live - including mid-turn
        # and with a gate up, because "stop asking me / stop changing things" is
        # exactly the thought a user has while watching a turn run. Priority for
        # the composer's sake like the rest, but here it also overrides a
        # binding of Textual's own: Screen binds shift+tab to focus_previous,
        # and a focused TextArea would hand the key straight to it. Backwards
        # focus navigation is the accepted price - the app has four focusables
        # and tab still cycles them forwards.
        Binding("shift+tab", "cycle_permission_mode", "mode", priority=True, show=False),
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
    # Does the LIVE session's preset carry extra_instructions? Mirrors the
    # engine's snapshot, not the config: a service edited mid-session has not
    # reached the running engine, and `r` may only offer what that engine would
    # actually send.
    has_extra_instructions: reactive[bool] = reactive(False, bindings=True)
    # True for the whole of a delegated sub-agent run. The master's flow is busy
    # throughout, which would normally disable the composer - but /abort is the
    # only way out of a sub-run, so the box has to stay reachable.
    sub_running: reactive[bool] = reactive(False, bindings=True)

    def __init__(
        self,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[EngineRequest], Link],
        project_root: Path,
        profile_root: Path,
        *,
        mcp_manager: McpStatusSource | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._provider = provider
        self._project_root = project_root
        # The process-wide MCP runtime, or None when MCP is unconfigured - which
        # keeps every MCP surface off the screen entirely (no sidebar block, no
        # statusbar segment, no hook). Handed down from AgentClipApp; this
        # screen only READS statuses and listens, the lifecycle stays cli.py's.
        self._mcp_manager = mcp_manager
        # Which (server, state) pairs have already been announced - the
        # once-per-server-per-state memory behind the failed/needs_auth
        # transcript notes and the connected toast, so reconnect churn cannot
        # spam either channel. Never reset: MCP state is app-level and connects
        # happen once per process, so "already said" stays said across /new
        # (which empties the transcript but changes nothing about the servers).
        self._mcp_announced: set[tuple[str, str]] = set()
        # Where each service's captured appearances live on disk, and the
        # per-run cache of the ones already read back (see ``_profile``).
        self._profile_root = profile_root
        self._profiles: dict[str, ServiceProfile] = {}
        self._snap: StatusSnapshot | None = None  # mirrors SessionView.snapshot (read by tests)
        self._gate_kind: str | None = None  # the in-flight gate's kind, for a/check_action
        # The pattern `a` would remember at the in-flight gate, or None in legacy
        # mode (where `a` is the edits-only auto-accept).
        self._gate_always: str | None = None
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
        # The screen-automation core, driven through the AutomationView port
        # this screen implements structurally - the same arrangement as the
        # SessionController/ChatView pair below (docs/design/gui.md §1). It owns
        # the state the loop is read against and this screen no longer does:
        #
        # * the global ARMED switch (F5, `/armed`) - see ``set_os_armed``, whose
        #   remaining consequences (three of the four chokepoints, the status
        #   bar, the toasts) stay here while the decision itself, and the
        #   clipboard watcher it stops, live down there;
        # * the clipboard watcher thread: the provider is handed in (cli.py
        #   picks the backend at startup and it comes down through this screen),
        #   the protocol pre-filter is handed in (the automation layer may not
        #   import ``agentclip.protocol``), and captures come back out through
        #   ``_clipboard_captured`` below;
        # * every user-drawn calibration, one per agent slot, plus the two
        #   pointers into them - *calibrating* is the slot behind the SELECTED
        #   window tab (what the sidebar configures), *live* is the slot the
        #   automation (paste click, detector poller, auto-copy) drives. They
        #   move independently: the sub-agent window is calibrated, and read,
        #   while the master chat is mid-session;
        # * the service each window tab is pointed at - the KEY only. What a
        #   service LOOKS like lives in ``_profiles`` here instead, captured
        #   once, shared by both slots and persisted across runs.
        self._automation = AutomationController(
            view=self,
            # The other half of the seam (agentclip.driver.automation.host): what the
            # OS-acting sequences down there still have to ASK this shell -
            # which service the live window is on and what it looks like, where
            # an appearance is on screen, and the two acts a harvest ends in
            # that cannot live below (handing prose to the SESSION, rebuilding
            # the detector set). Structural, like the two view ports.
            host=self,
            # ...and the hand they put on the machine, resolved at this module's
            # scope so the suites' patches still bite (see ``_MainScreenOps``).
            ops=_MainScreenOps(),
            services=self._initial_services(config),
            clipboard=provider,
            poll_interval_ms=config.clipboard.poll_interval_ms,
            accepts=looks_like_protocol,
            on_clipboard_captured=self._clipboard_captured,
            # The one thing a tick still asks this screen for on its way into the
            # consumer: cut this tick's matches down to panel-sized pictures.
            # Sizing one depends on which renderer this terminal can drive, so
            # the CUT is the shell's - and it runs on the poller thread that
            # captured the frame, because what then crosses to the UI is an icon
            # rather than a chat window.
            crop_elements=self._crop_elements,
            # What the fold cannot work out for itself. The profile cache is
            # this screen's (a service key resolves against its Config), and
            # launching a coroutine is Textual's - so "does the live window's
            # service have a capture of X?" and "start the auto-copy flow" cross
            # back as callbacks, and everything between them is decided below.
            # BOTH are called on the poller thread now: ``_live_has`` answers
            # from an immutable snapshot and ``_fire_auto_copy`` posts.
            has_appearance=self._live_has,
            on_fire=self._fire_auto_copy,
            # The tunables are read off THIS module rather than the automation
            # package's, because that is the name the Pilot suites patch (see
            # the constants block at the top).
            send_arm_ticks=SEND_ARM_TICKS,
            send_arm_min_diff=SEND_ARM_MIN_DIFF,
            send_gate_timeout_ticks=SEND_GATE_TIMEOUT_TICKS,
            send_gate_seen_timeout_ticks=SEND_GATE_SEEN_TIMEOUT_TICKS,
        )
        self._delegation_ready = False  # last-seen SUBAGENT can_delegate, for the one-shot toast
        self._region_click_warned = False
        # Which detector RUN the DETECTION block and the ELEMENTS column are
        # currently showing. Bumped by ``_paint_detection`` (the rebuild's
        # reset), stamped into every run-scoped paint the controller asks for,
        # and compared by the handlers - the ghost filter on the paint side, now
        # that the probe it used to ride on never leaves the poller thread.
        self._paint_epoch = 0
        # Harness-log entries waiting for the pane, in the order the controller
        # logged them - see ``paint_harness_entry``. Bounded like the log
        # itself, because before the pane exists there is nowhere to drain to.
        self._log_pending: deque[HarnessEntry] = deque(maxlen=HARNESS_LOG_MAX)
        # This screen's mirror of the controller's poller: set by
        # ``_spawn_detector_worker``, dropped by ``_stop_detector_worker``, and
        # the thing ``resume_detectors`` asks "is one already up?". A mirror
        # rather than a read-through, because a test freezes the spawn and puts
        # its own stand-in here.
        self._detector_worker: DetectorPoller | None = None
        # What is looking at the live window: one ``ScreenDetector``, rebuilt
        # per poller run, searching for every appearance that window's service
        # is calibrated for and for nothing else. It is a SOURCE, never a
        # participant - everything below consumes it, and nothing below may
        # decide what it looks for. Its three finish trackers, what they have
        # said, the trigger they arm, both gates, the loop's state and the
        # harness log are all the AutomationController's now (the compatibility
        # proxies further down are what the older suites still poke).
        self._detector: ScreenDetector | None = None
        # One running command's output, per call id, as complete LINES - the run
        # panel's tail is a view of this and nothing else (widgets/run_panel.py).
        # Bounded and per-turn: ``stop_working`` empties both, because the
        # model's copy of the output has by then reached the transcript and this
        # was only ever the live view of it. ``_run_output_partial`` holds each
        # call's unfinished last line until its newline arrives.
        self._run_output: dict[int, deque[str]] = {}
        self._run_output_partial: dict[int, str] = {}
        # Whether the last status push said a session was running, so the log
        # can mark the two boundaries the controller never announces directly.
        self._logged_session_active = False
        # One overlay at a time, across ALL pickers: cancelling an exclusive
        # worker cannot kill the blocking child overlay process it spawned, so
        # extra button presses are refused up front instead.
        self._picker_open = False
        self._controller = SessionController(
            config,
            engine_factory,
            project_root,
            view=self,
            # A bound method, not the manager: the controller is UI- and
            # MCP-agnostic (layering), so /mcp takes a supplier of duck-typed
            # status rows and None means "say MCP is not configured".
            mcp_statuses=mcp_manager.statuses if mcp_manager is not None else None,
        )

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
        return self._automation.calibrating

    @property
    def selected_service(self) -> str:
        """The selected window tab's service key.

        What the sidebar's picker shows right now - master tab selected means
        the master's service, sub tab selected means the sub-agent's (already
        resolved to the master's when the sub key is blank). The service editor
        opens preselected on this (tui.md §1.4).
        """
        return self._selected_service()

    @property
    def live(self) -> SlotCalibration:
        """The slot the automation drives right now (paste click, finish
        detector, auto-copy). Only ``start_browser_chat``/``end_browser_chat``
        move it - everything else reads it."""
        return self._automation.live

    # Compatibility proxies onto the state the AutomationController now owns.
    # The underscored names predate the extraction and are what the Pilot suites
    # poke (and what a monkeypatched ``_find_all`` stand-in reads); they are
    # spelled once, here, so the screen's own code can go straight to the
    # controller without a rewrite of every test that reaches in.
    @property
    def _slots(self) -> dict[AgentSlot, SlotCalibration]:
        return self._automation.slots

    @property
    def _calibrating(self) -> AgentSlot:
        return self._automation.calibrating_slot

    @_calibrating.setter
    def _calibrating(self, value: AgentSlot) -> None:
        self._automation.select_calibrating_slot(value)

    @property
    def _live(self) -> AgentSlot:
        return self._automation.live_slot

    @_live.setter
    def _live(self, value: AgentSlot) -> None:
        self._automation.select_live_slot(value)

    @property
    def _os_armed(self) -> bool:
        return self._automation.os_armed

    @property
    def _detector_generation(self) -> int:
        """Which poller RUN a verdict belongs to.

        The counter and the comparison both belong to the controller now
        (``AutomationController.is_ghost``); this stays as the read a test uses
        to stamp an injected probe so it speaks as the current poller
        (``AutomationController.feed_probe``).
        """
        return self._automation.detector_generation

    # ...and the same proxies onto the probe CONSUMER's state, which moved down
    # with the decisions made out of it. Every name here is one the Pilot suites
    # read or write to drive a tick sequence without a poller thread; the
    # settable ones are settable because a test says "this detector has reported
    # nothing" the same way a rebuild does.
    @property
    def _active_detectors(self) -> tuple[str, ...]:
        return self._automation.active_detectors

    @_active_detectors.setter
    def _active_detectors(self, names: tuple[str, ...]) -> None:
        self._automation.active_detectors = names

    @property
    def _busy_tracker(self) -> PresenceTracker | None:
        return self._automation.busy_tracker

    @_busy_tracker.setter
    def _busy_tracker(self, tracker: PresenceTracker | None) -> None:
        self._automation.busy_tracker = tracker

    @property
    def _idle_tracker(self) -> PresenceTracker | None:
        return self._automation.idle_tracker

    @_idle_tracker.setter
    def _idle_tracker(self, tracker: PresenceTracker | None) -> None:
        self._automation.idle_tracker = tracker

    @property
    def _stale_tracker(self) -> StaleTracker | None:
        return self._automation.stale_tracker

    @_stale_tracker.setter
    def _stale_tracker(self, tracker: StaleTracker | None) -> None:
        self._automation.stale_tracker = tracker

    @property
    def _busy_seen(self) -> bool:
        return self._automation.busy_seen

    @_busy_seen.setter
    def _busy_seen(self, value: bool) -> None:
        self._automation.busy_seen = value

    @property
    def _idle_seen(self) -> bool:
        return self._automation.idle_seen

    @_idle_seen.setter
    def _idle_seen(self, value: bool) -> None:
        self._automation.idle_seen = value

    @property
    def _stale_seen(self) -> bool:
        return self._automation.stale_seen

    @_stale_seen.setter
    def _stale_seen(self, value: bool) -> None:
        self._automation.stale_seen = value

    @property
    def _busy_finished(self) -> bool | None:
        return self._automation.busy_finished

    @_busy_finished.setter
    def _busy_finished(self, value: bool | None) -> None:
        self._automation.busy_finished = value

    @property
    def _idle_finished(self) -> bool | None:
        return self._automation.idle_finished

    @_idle_finished.setter
    def _idle_finished(self, value: bool | None) -> None:
        self._automation.idle_finished = value

    @property
    def _stale_finished(self) -> bool | None:
        return self._automation.stale_finished

    @_stale_finished.setter
    def _stale_finished(self, value: bool | None) -> None:
        self._automation.stale_finished = value

    @property
    def _stale_diff(self) -> float | None:
        return self._automation.stale_diff

    @property
    def _stale_arm_streak(self) -> int:
        return self._automation.stale_arm_streak

    @property
    def _copy_armed(self) -> bool:
        return self._automation.copy_armed

    @_copy_armed.setter
    def _copy_armed(self, value: bool) -> None:
        self._automation.copy_armed = value

    @property
    def _copy_changed_streak(self) -> int:
        return self._automation.copy_changed_streak

    @_copy_changed_streak.setter
    def _copy_changed_streak(self, value: int) -> None:
        self._automation.copy_changed_streak = value

    @property
    def _awaiting_pasted_reply(self) -> bool:
        return self._automation.awaiting_pasted_reply

    @property
    def _self_writes(self) -> SelfWriteSet:
        """Every clipboard write the automation made, so the watcher cannot read
        our own outbound back as a reply. The controller's since the delivery
        path came down with it (slice 7) - both ends are one object."""
        return self._automation.self_writes

    @property
    def _pending_insert(self) -> str | None:
        """What the sidebar's retry button would re-deliver (the controller's -
        see ``AutomationController.pending_insert``)."""
        return self._automation.pending_insert

    @property
    def _flow_running(self) -> bool:
        return self._automation.flow_running

    @_flow_running.setter
    def _flow_running(self, value: bool) -> None:
        self._automation.flow_running = value

    @property
    def _send_gate(self) -> SendGate | None:
        return self._automation.send_gate

    @property
    def _send_gate_ticks(self) -> int:
        return self._automation.send_gate_ticks

    @property
    def _loop_state(self) -> LoopState:
        return self._automation.loop_state

    @property
    def _harness_log(self) -> deque[HarnessEntry]:
        return self._automation.harness_log

    # The last compatibility proxy onto the MASTER slot. The single-window
    # vocabulary predates slots and is what the older Pilot suites poke; only
    # ``_chat_region`` is left, because it is the only thing a slot still holds.
    @property
    def _chat_region(self) -> ScreenRegion | None:
        return self._automation.calibration(AgentSlot.MASTER).chat_region

    @_chat_region.setter
    def _chat_region(self, value: ScreenRegion | None) -> None:
        self._automation.set_calibration(AgentSlot.MASTER, value)

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
                yield RunPanel(id="running")
                # Directly above the box, so the list a keystroke is filtering
                # sits where the eye already is (§3.3a). Hidden until it isn't.
                yield CommandPopup(id="cmd-popup")
                yield ChatComposer(id="composer")
            yield Sidebar(
                self._config,
                self._project_root,
                # The MCP block exists exactly when a manager does; the tuple
                # is the initial paint, so pending/disabled states show from
                # the first frame (docs/design/mcp.md section 6).
                mcp_statuses=(
                    self._mcp_manager.statuses() if self._mcp_manager is not None else None
                ),
                id="sidebar",
            )
            # The pictures the sidebar's DETECTION lines are words about, in a
            # column of their own rather than at the bottom of one that already
            # overflows (tui.md 1.3/1.7). F7 hides it, F3 hides its neighbour.
            yield ElementsPanel(id="elements")
        # Full width, under all three columns and above the status bar: an entry
        # is a whole sentence of reason, and nothing narrower than the terminal
        # can hold one. Mounted for good and hidden by CSS; F8 and `/log` show
        # it, and the columns above give up ~30% of their height while it is up.
        yield HarnessLogPane(id="log-pane")
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
    def run_panel(self) -> RunPanel:
        """The whole executing-turn region: spinner, call rows, live output (§8a)."""
        return self.query_one(RunPanel)

    @property
    def running_bar(self) -> RunningBar:
        """The run panel's spinner line - the `ctrl+x to cancel` advertisement."""
        return self.query_one(RunningBar)

    @property
    def sidebar(self) -> Sidebar:
        return self.query_one(Sidebar)

    @property
    def elements_panel(self) -> ElementsPanel:
        """The ELEMENTS column - the LIVE window's recognised crops (§1.7)."""
        return self.query_one(ElementsPanel)

    @property
    def harness_log_pane(self) -> HarnessLogPane:
        """The full-width live tail of the decision log (`/log`, F8; §3.3b)."""
        return self.query_one(HarnessLogPane)

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
        for window, key in self._automation.services().items():
            if key not in config.services:
                self._automation.set_service(window, self._initial_services(config)[window])
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
        self._paint_state_rail()
        # Appearances captured on a previous run are already usable: show them.
        self._paint_profile()
        # Nothing is drawn yet, so this starts no worker - but it is the only
        # writer of the DETECTION block, and the block has to name the window it
        # is about (the master's) from the first frame rather than after the
        # first calibration.
        self._start_detector_worker()
        if self._mcp_manager is not None:
            # Hook first, paint second, so no transition can fall in the gap: one
            # landing between the two lines posts a message that arrives after
            # this paint, and both read a fresh statuses(). The screen mounts
            # once per process (the app pushes it at its own mount and never
            # pops it), so this IS app-mount wiring; connects are lazy
            # (eager-on-arm), which is why the paint below usually shows
            # pending/disabled - the states that exist before any transition
            # fires.
            self._mcp_manager.set_status_hook(self._mcp_status_hook)
            self._paint_mcp()
        self._remember_own_window()  # the user just launched us - focus is our terminal
        self._controller.start()

    def on_unmount(self) -> None:
        """Quitting must not leave a thread polling the user's clipboard - or
        capturing their screen twice a second.

        This is the same seam Textual's own teardown used when both were thread
        WORKERS: ``Widget._on_unmount`` cancels every worker the node started,
        and this screen's unmount is dispatched on app shutdown. Now that the
        threads belong to the AutomationController, that teardown has to be
        asked for by name - once per thread, because "stopped" is a flag each
        loop reads and not a group cancel. Both loops notice between ticks and
        both threads are daemons besides, so nothing here can hang the exit;
        this is what makes them stop *promptly*, and what keeps a test run from
        accumulating one live watcher and one live poller per app it booted.
        """
        self._automation.stop_input()
        self._stop_detector_worker()

    def _remember_own_window(self) -> None:
        """Record the foreground window at a moment the user is provably
        interacting with AgentClip (launch, composer send, a sidebar press).

        Reading the foreground window is this shell's - only it knows when the
        user is demonstrably here - but the HANDLE is OS state both shells snap
        focus back to, so it is kept below (``set_own_window``, which ignores a
        None reading and keeps the last good handle)."""
        self._automation.set_own_window(foreground_window())

    @property
    def _own_window(self) -> int | None:
        """The handle the automation snaps focus back to. The name predates the
        extraction and is what the Pilot suites read."""
        return self._automation.own_window

    # Snapping focus back is the controller's whole and only
    # (``snap_back_after_click``, beat included): this screen used to keep a
    # one-line wrapper around it, and there is no shell decision left inside it
    # to justify one.

    # -- dynamic bindings -----------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("approve", "reject"):
            return True if self.pending_approval else None
        if action == "auto_edits":
            offered = self._gate_always is not None or self._gate_kind == "edit"
            return True if (self.pending_approval and offered) else None
        if action in ("undo", "end_session"):
            ok = (
                self.session_active
                and not self.busy
                and self.phase_name in ("AWAITING_REPLY", "DONE")
            )
            return True if ok else None
        if action == "recopy":
            return True if self.has_outbound else None
        if action == "reinstruct":
            # Hidden outright, not dimmed, on a service with nothing to
            # re-inject: unlike `undo` or `ingest` this is not a key that
            # becomes available in a moment - on this service it never does.
            if not self.has_extra_instructions:
                return False
            return True if self.session_active else None
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
            # Hidden outright while disarmed, exactly as in manual-clipboard
            # mode: in both there is no state in which the watcher may run, so a
            # dimmed-but-present key would be advertising a lie. F5 is the way
            # back, and the DISARMED badge and banner are what say so.
            if self._provider.name == "manual" or not self._automation.os_armed:
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
        if action == "toggle_run_output":
            # Hidden outright rather than dimmed: there is no output to show
            # unless a command is running THIS INSTANT, so a dimmed key in the
            # footer would be advertising something the user cannot reach.
            return self.executing
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
        self._automation.select_live_slot(AgentSlot.MASTER)
        self._select_window(MASTER_WINDOW)
        # Re-derive rather than clear: the sub-agent slot is still calibrated,
        # and _after_calibration's one-shot toast must not re-fire after /new.
        self._delegation_ready = self.delegation_available()
        self._reset_finish_trigger()
        self._close_reply_gate()  # whatever was pasted, no reply to it is due now
        # ...and the last outbound belongs to the session being torn down, so
        # there is nothing left for the retry button to re-deliver.
        self._automation.forget_pending_insert()
        # A reset, not a transition - and the log says so in those words, because
        # every other road to IDLE is the loop finishing something. This entry is
        # also the boundary marker that lets the log survive /new intact: the
        # tail above it describes the session the user just tore down, which is
        # usually the reason they tore it down.
        self._set_loop_state(LoopState.IDLE, "session reset")
        self._log_harness(
            KIND_SESSION,
            # Not "(/new)": the summary screen's "new session" and the
            # budget-exceeded retry reach this same teardown, and a log that
            # named a command the user did not type would be the one kind of
            # lie this whole feature exists to stop telling.
            "session reset: the transcript is cleared, the calibrations and "
            "this log are not",
        )
        self._start_detector_worker()
        self.hide_paste_flash()
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

    async def finish_session_view(self, session_id: str, note: str, ok: bool) -> None:
        """A sub-agent run ended: annotate its transcript and re-badge its tab.

        Nothing is disabled or removed - the panels are output-only and the
        composer always targets the controller's active session, so leaving the
        run readable costs nothing and is the whole point of keeping it. The
        tab drops its ``▶`` for a ``✓`` or a ``✗``: the label belongs to the
        WINDOW, so it reports what happened in it, and the run's own title lives
        in the divider above its transcript.

        ``ok`` is the outcome, and it is a parameter rather than an inference
        because the caller is the only one who knows: a run that was refused a
        fresh chat, blew its paste budget or crashed reaches here exactly like a
        run that finished, through the same ``finally``. Without it the tab
        showed a success glyph over a failure, directly under a note claiming a
        result had been handed back.
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
                    run.ok = ok
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
        self._automation.select_calibrating_slot(self._slot_of(window))
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
        is in flight (its slice has no end yet), and otherwise the LAST run's
        outcome - ``✓`` when it handed a result back, ``✗`` when it did not.
        The last one, because the tab is a status light for the window and the
        thing a user wants to know at a glance is how the most recent attempt
        went; the earlier runs are still readable in the transcript below it.
        """
        name = _WINDOW_NAMES[window]
        service = self._automation.service_of(window)
        glyph = ""
        if window == SUBAGENT_WINDOW and self._sub_runs:
            if any(run.end is None for run in self._sub_runs):
                glyph = "▶ "
            else:
                glyph = "✓ " if self._sub_runs[-1].ok else "✗ "
        return f"{glyph}{name} · {service}" if service else f"{glyph}{name}"

    def _relabel_window(self, window: str) -> None:
        with suppress(NoMatches, KeyError):
            self.chat_tabs.set_label(window, self._window_label(window))

    # == ChatView: state + chrome =============================================

    def _set_loop_state(self, state: LoopState, reason: str) -> None:
        """Move the browser-automation loop to ``state``, giving the reason.

        The controller's, and still the one door - a transition cannot reach the
        rail without reaching the log. This screen keeps the name because it is
        the door two dozen of its own call sites already knock on.
        """
        self._automation.set_loop_state(state, reason)

    def _log_harness(self, kind: str, text: str) -> None:
        """Append one decision to the harness log (`/log`), through the
        controller that owns the deque."""
        self._automation.log_harness(kind, text)

    # -- AutomationView: the loop's narration ----------------------------------

    def paint_loop_state(self, state: LoopState) -> None:
        """Repaint the sidebar's STATE rail (``AutomationView``).

        Called on every loop-state change, plus once on mount so the rail is
        never blank before the first one - and, since the consumer moved onto
        the poller thread, usually from there.
        """
        self.post_message(PaintLoopState(state))

    @on(PaintLoopState)
    def _on_paint_loop_state(self, message: PaintLoopState) -> None:
        """The message is a TICK; the controller is the state.

        Deliberately painting ``loop_state`` rather than ``message.state``, on
        the ``McpStatusChanged`` precedent: two threads move the loop now, and
        Textual delivers a cross-thread post through ``call_soon_threadsafe``,
        so two transitions taken in one order can be delivered in the other. The
        rail would then rest on the older one for good. Reading the controller
        here makes the LAST message painted the CURRENT truth whatever order
        they arrive in - and the payload stays on the message so the pump still
        says which transition asked.
        """
        message.stop()
        with suppress(NoMatches):
            self.sidebar.show_loop(self._automation.loop_state)

    def paint_harness_entry(self, entry: HarnessEntry) -> None:
        """Mirror one appended decision into the log pane (``AutomationView``).

        The deque in the controller is the log; the pane is a view of it, fed
        one entry at a time so an open pane shows a decision as it is taken
        (§3.3b). The entry is queued HERE, in append order, and the message only
        says "there is something to mirror" - because order is the whole meaning
        of a log and the message queue cannot promise it across threads
        (``call_soon_threadsafe`` again). ``log_harness`` appends under the
        controller's tick lock, so this queue is filled in exactly the order the
        deque was, and the handler drains it in that order.
        """
        self._log_pending.append(entry)
        self.post_message(PaintHarnessEntry(entry))

    @on(PaintHarnessEntry)
    def _on_paint_harness_entry(self, message: PaintHarnessEntry) -> None:
        """A hidden pane paints nothing and refills itself from the deque when it
        is next revealed, and before the screen is mounted there is no pane at
        all - which is why the query is suppressed, and why the pending queue is
        bounded like the log itself."""
        message.stop()
        with suppress(NoMatches):
            pane = self.harness_log_pane
            while self._log_pending:
                pane.append(self._log_pending.popleft())

    def _paint_state_rail(self) -> None:
        """The mount-time paint of the STATE rail, from wherever it rests."""
        self.paint_loop_state(self._automation.loop_state)

    # -- MCP status (docs/design/mcp.md section 6) -----------------------------

    def _mcp_status_hook(self, status: McpServerStatus) -> None:
        """The manager's status listener: hand off to the UI loop, nothing else.

        Called from the manager's loop thread (and, for missing_sdk, from
        whichever thread called ensure_started - possibly Textual's own, which
        is why this is post_message and not call_from_thread). The contract is
        the run-panel hooks' (`_on_call_output`): non-blocking, thread-safe,
        and it must never raise - the manager silently drops a listener that
        does, once, for good.
        """
        self.post_message(McpStatusChanged(status))

    def _paint_mcp(self) -> None:
        """Repaint both MCP readouts - the sidebar block and the statusbar
        segment - from a fresh ``statuses()``. A no-op without a manager."""
        if self._mcp_manager is None:
            return
        with suppress(NoMatches):
            self.sidebar.show_mcp(self._mcp_manager.statuses())
        self._paint_status()

    @on(McpStatusChanged)
    async def _on_mcp_status_changed(self, message: McpStatusChanged) -> None:
        """One server moved: repaint, and announce the transitions worth words.

        The repaint reads statuses() rather than patching one row in - the
        message is a tick, and a connect can change a NEIGHBOUR's line too
        (shadowed tool ids). The announcements are once per server PER STATE
        (``_mcp_announced``): failed/needs_auth land in the transcript (they
        need the user) and toast as warnings; connected only toasts, quietly -
        a working server is not worth a permanent transcript line.

        The note goes to the MASTER window's panel directly, not through
        ``self.transcript``: MCP state is app-level - sessions come and go
        under it, and mid-delegation the focused panel is the sub-agent's,
        where an infrastructure note would read as part of that run. The
        panels are mounted for the app's whole life (only /new empties them),
        so the channel exists pre-session too and nothing has to be parked for
        the next session start.
        """
        self._paint_mcp()
        status = message.status
        if status.state not in ("failed", "needs_auth", "connected"):
            return
        key = (status.name, status.state)
        if key in self._mcp_announced:
            return
        self._mcp_announced.add(key)
        if status.state == "connected":
            self.notify(
                f"MCP server {status.name!r} connected"
                f" · {status.tool_count} tool{'' if status.tool_count == 1 else 's'}",
                severity="information",
            )
            return
        what = "needs auth" if status.state == "needs_auth" else "failed"
        text = f"✗ MCP server {status.name!r} {what}"
        if status.detail:
            text += f" - {status.detail}"
        panel = self._panels.get(MASTER_WINDOW)
        if panel is not None:
            with suppress(NoMatches):
                await panel.add_note(text)
        self.notify(text, severity="warning", timeout=8)

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
        self.has_extra_instructions = bool(
            view.snapshot and view.snapshot.has_extra_instructions
        )
        self.pending_approval = view.pending_approval
        self.awaiting_answer = view.awaiting_answer
        self.busy = view.busy
        self.phase_name = view.snapshot.phase.name if view.snapshot else "IDLE"
        # The two ways the loop settles back to idle, both visible only from a
        # status push: the session ended (or none has started), or the reply's
        # turn finished interpreting and is now waiting on the user - an open
        # approval gate is still "interpreting" (the reply is being acted on),
        # while an ask_user question hands the floor back like any other prompt.
        # The two session boundaries, which arrive only as a changed flag on a
        # status push: worth an entry because half the log's other lines mean
        # something different on either side of one. Logged BEFORE the idle
        # transition below, so the log reads cause then effect - the session
        # ending is why the loop goes home.
        if view.session_active != self._logged_session_active:
            self._logged_session_active = view.session_active
            self._log_harness(
                KIND_SESSION,
                "session started" if view.session_active else "session ended",
            )
            if view.session_active:
                # The one moment the MCP rows can have moved with no hook to say
                # so: in remote mode the settle rides ``build_session`` and there
                # is no push over the wire (docs/design/remote-executor.md
                # section 2.9), so a session start is when the target's runtime
                # first has anything to report. Local mode repaints an unchanged
                # block for the price of one ``statuses()`` read.
                self._paint_mcp()
        if not view.session_active or (
            self._loop_state is LoopState.INTERPRETING
            and (view.awaiting_answer or not (view.busy or view.pending_approval))
        ):
            self._set_loop_state(
                LoopState.IDLE,
                "no session is running"
                if not view.session_active
                else "the turn finished and the floor is back with you",
            )
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
        self._gate_always = action.always_pattern
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
        self._gate_always = None
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.action_panel.hide_panel()

    def start_working(self, label: str, calls: Sequence[RunCall] = ()) -> None:
        # The run panel and the cancel binding share one lifetime: the panel
        # advertises ctrl+x, so ctrl+x must work exactly while it is up.
        self.executing = True
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.run_panel.start(label, calls)

    def stop_working(self) -> None:
        self.executing = False
        # The turn is over: its output belonged to the panel that is going away,
        # and the model's copy of it is in the transcript.
        self._run_output.clear()
        self._run_output_partial.clear()
        if not self.is_mounted:
            return
        with suppress(NoMatches):
            self.run_panel.stop()

    # -- ChatView: the turn executing, call by call (WORKER-THREAD callers) ----
    #
    # The engine calls these three from the thread it is executing the plan on
    # (see ChatView), so all three do exactly one thing: post. Everything that
    # touches a widget happens in the handlers below, on the UI thread - the
    # same bridge the clipboard watcher uses.

    def call_started(self, call_id: int, tool: str, detail: str) -> None:
        self.post_message(CallStarted(call_id, tool, detail))

    def call_finished(self, call_id: int, glyph: str) -> None:
        self.post_message(CallFinished(call_id, glyph))

    def call_output(self, call_id: int, chunk: str) -> None:
        self.post_message(CallOutput(call_id, chunk))

    def on_call_started(self, message: CallStarted) -> None:
        message.stop()
        with suppress(NoMatches):
            self.run_panel.call_started(message.call_id, message.tool, message.detail)

    def on_call_finished(self, message: CallFinished) -> None:
        message.stop()
        with suppress(NoMatches):
            self.run_panel.call_finished(message.call_id, message.glyph)

    def on_call_output(self, message: CallOutput) -> None:
        """Append one command's newest characters to its buffer, then repaint.

        The deque is the truth (log_pane.py's division of labour): it fills
        whether or not anyone is looking, and the panel paints only while its
        tail is expanded - which is the usual case of NOT, so a chatty command
        costs a couple of list operations per poll slice and no render at all.
        """
        message.stop()
        self._append_run_output(message.call_id, message.chunk)
        with suppress(NoMatches):
            panel = self.run_panel
            if panel.expanded and panel.streaming_call == message.call_id:
                panel.show_output(self._run_output_lines(message.call_id))

    def on_run_panel_output_toggle_requested(self, message: RunPanel.OutputToggleRequested) -> None:
        message.stop()
        self.action_toggle_run_output()

    def action_toggle_run_output(self) -> None:
        """ctrl+o: show/hide the running command's live output (§8a)."""
        with suppress(NoMatches):
            panel = self.run_panel
            call_id = panel.streaming_call
            panel.toggle_output(self._run_output_lines(call_id) if call_id is not None else ())

    def _append_run_output(self, call_id: int, chunk: str) -> None:
        """Fold one delta into the call's line deque, holding the partial line.

        A command's output arrives mid-line as often as not (a progress line, a
        prompt), and a tail that only showed completed lines would sit one line
        behind the thing the user opened it to watch. So the unfinished tail is
        kept aside and shown as the last line until its newline arrives.
        """
        buffer = self._run_output.setdefault(call_id, deque(maxlen=RUN_OUTPUT_LINES))
        text = self._run_output_partial.pop(call_id, "") + chunk.replace("\r\n", "\n")
        lines = text.replace("\r", "\n").split("\n")
        self._run_output_partial[call_id] = lines.pop()
        buffer.extend(lines)

    def _run_output_lines(self, call_id: int) -> list[str]:
        """One call's buffered output, the unfinished last line included."""
        lines = list(self._run_output.get(call_id, ()))
        partial = self._run_output_partial.get(call_id, "")
        if partial:
            lines.append(partial)
        return lines

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

    def open_new_chat_now(self) -> None:
        """Open a fresh browser chat immediately, for ``/new`` (tui.md 3.3a).

        The ChatView half of the command: the controller has said *this
        conversation is over*, and the browser is the view's to drive. It runs
        the very flow the sidebar's "New browser chat" button runs - find the
        control, click it, hand focus back, and reset the session whether or not
        the click landed - so the two ways of asking cannot drift apart.

        Always the MASTER window, never ``_calibrating``: /new is a command
        typed into the master's session, and the sidebar happening to point at
        the sub-agent tab must not send the master's fresh chat there.
        """
        self.run_worker(self._new_browser_chat(AgentSlot.MASTER), group="newchat", exclusive=True)

    async def copy_outbound(self, text: str) -> None:
        """ChatView: deliver one outbound payload (the controller's - see
        ``AutomationController.copy_outbound``). Park it, click the chat box,
        settle, paste or stream, and tap Enter for a service that opted in."""
        await self._automation.copy_outbound(text)

    async def park_outbound(self, text: str) -> None:
        """ChatView: put the last outbound back on the clipboard and stop there.

        Stage one of the `c` re-copy (tui.md 3.4a) - the clipboard half of
        ``copy_outbound`` and none of the rest. The controller's.
        """
        await self._automation.park_outbound(text)

    def redeliver_outbound(self, text: str) -> None:
        """ChatView: send the last outbound again, the way a normal send sends.

        Stage two of the `c` re-copy - the double tap (tui.md 3.4a). It is
        ``copy_outbound`` and nothing else, so the re-delivery cannot drift from
        the delivery it repeats: the same park, the same verified focus click,
        the same burst-or-stream choice and the same opt-in Enter tap, chosen by
        the same live preset. The two refusals (and their wording) are the
        controller's ``may_redeliver``; what is left here is the SCHEDULING,
        which is the one thing a UI framework really owns.

        Scheduled rather than awaited, in the retry button's exclusive worker
        group: the session controller is on the event loop and must not park for
        the seconds a streamed delivery takes, and two inserts racing into one
        chat box is exactly what the group exists to prevent.
        """
        if not self._automation.may_redeliver():
            return
        self.run_worker(self.copy_outbound(text), group="insert", exclusive=True)

    def park_off_clipboard(self, text: str) -> None:
        """AutomationHost: the clipboard provider refused the payload, so send it
        out over the terminal's OSC-52 escape instead (write-only).

        The one delivery step that could not come down with the rest of the paste
        path: it is ``App.copy_to_clipboard``, a Textual terminal escape, and no
        other shell has anything like it (docs/design/gui.md §0). The controller
        reports what it means for the delivery through ``deliver``'s
        ``clipboard_ok`` - a stream has no clipboard to walk chunks through.
        """
        self.app.copy_to_clipboard(text)

    # -- re-running an insert that did not land ---------------------------------

    @on(Button.Pressed, "#retry-insert-btn")
    def _on_retry_insert(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_worker(self.retry_insert(), group="insert", exclusive=True)

    async def retry_insert(self) -> None:
        """Do the insert again (the controller's - see
        ``AutomationController.retry_insert``): park the last payload back on the
        clipboard, click the chat box, settle, paste, and auto-submit if the
        service does.

        The name stays here because it is what the sidebar's button runs and what
        the suites drive; the three refusals and the sequence itself are one
        thing, done once, below both shells.
        """
        await self._automation.retry_insert()

    async def _find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        """Every place ``kind`` is on screen right now, in absolute coordinates.

        The one primitive behind every "is it there / click it" question: it
        looks *inside the drawn chat region* for the appearance THAT WINDOW's
        own service captured, answers with ALL the matches rather than the
        first, and comes up empty rather than raising for every way it can
        (``AutomationController.find_all``, whose implementation this was until
        the GUI turned out to have spelled the identical search out - and a
        shell may not import another shell).

        The NAME stays here because it is what this shell's suites stub: a
        stand-in ``_find_all`` is how a Pilot test puts a copy button on an
        imaginary screen, and every sequence below still asks through the host.
        """
        return await self._automation.find_all(kind, slot, scene=scene)

    async def _chatbox_region(self) -> ScreenRegion | None:
        """Which chat input box to poke right now, or None if none is known
        (the controller's - see ``chatbox_region``)."""
        return await self._automation.chatbox_region()

    async def read_clipboard(self) -> str | None:
        # Deliberately NOT gated by the armed switch: this is the one-shot read
        # behind `i` (force-ingest), which is the user asking, once, for what is
        # on their own clipboard right now. Disarming stops the *watching* - a
        # background poll of a clipboard the user has not offered us - and it is
        # what makes the manual path completable while disarmed.
        return await asyncio.to_thread(self._provider.read_text)

    # == ChatView: the ARMED switch ===========================================

    def set_os_armed(self, target: bool | None) -> None:
        """Arm or disarm every part of this app that ACTS on the machine.

        DISARMED is a promise about the OS, not a pause button: no click, no
        scroll, no cursor move, no synthetic Ctrl+V, no focus stealing, and no
        clipboard watching. Everything that only LOOKS keeps running exactly as
        before - the capture loop, all three finish detectors, the send gate, the
        DETECTION lines, the ELEMENTS crops and the STATE rail - because the
        state a user reaches for this switch in ("what is it about to do, and
        why?") is the state where turning the instruments off too would be the
        opposite of helpful.

        It is enforced at four chokepoints rather than sprinkled through the
        callers, because the acting primitives are five screen-layer functions
        (``click_region``, ``scroll_region``, ``move_cursor``, ``send_paste``,
        ``focus_window_verified``) and every one of them is reached through
        exactly one of these doors - none of which is still this screen's:

        1. the paste path (``AutomationController.deliver``, reached by
           ``copy_outbound`` and by the sidebar's retry button alike), which
           stops clicking and pasting but still WRITES the payload to the
           clipboard, so the user can paste it by hand - that is the existing
           MANUAL_INSERT fallback, and disarmed mode simply routes into it;
        2. ``AutomationController.click_profile_element``, the one find-then-click
           primitive, which refuses with ``ElementClick.DISARMED`` before it
           touches anything - covering /new's new-chat click and a delegation's
           chat-open alike;
        3. the finish decision (``AutomationController.evaluate_finish``), which
           keeps every scrap of its bookkeeping and simply lands on MANUAL_COPY
           where it would have launched the auto-copy flow;
        4. the clipboard watcher: the thread belongs to the AutomationController,
           so the stop and the restart happen inside its ``set_os_armed``.

        In-flight work is left alone on purpose. The detectors' state
        (``_awaiting_pasted_reply``, ``_send_gate``, ``_copy_armed``, the
        streaks) is pure bookkeeping fed by live detection, so resetting it would
        only make the sidebar lie; and an ``_auto_copy_flow`` already running is
        allowed to finish, because it is a sequence of clicks with a half-done
        middle and cancelling it between two of them is worse than either end.
        What disarming guarantees is that no NEW one starts.

        The watcher rule, of the two on offer: disarming forces it off and
        remembers what it was, and re-arming puts *that* back - so a user who had
        paused it with `w`, disarmed, and re-armed does not get a watcher they
        turned off themselves. That rule (and the transition-only bookkeeping
        behind it) is the controller's now; what is left here is `w` itself,
        refused outright while disarmed because there is no state in which a
        watcher may run - which is why ``check_action`` dims it exactly as it
        does in manual-clipboard mode.

        ``target`` is the wanted state; ``None`` toggles (bare `/armed`, F5).
        Painting is synchronous and unconditional - both indicators and the
        toast repaint even when the state did not change, so an explicit
        `/armed off` typed twice confirms itself rather than looking ignored.

        The FLAG itself is the AutomationController's - one armed switch below
        every shell rather than one per frontend (docs/design/gui.md §1) - and
        so, since this slice, is the clipboard watcher the disarm stops. Both
        have moved by the time the call returns, which is what lets the chrome
        below simply read the result: ``watch_paused`` mirrors what the
        controller ended up doing with the watcher rather than deciding it.
        What is left here is the screen's own consequences: that status segment,
        the footer's bindings, and the toast.
        """
        was_armed = self._automation.os_armed
        armed = self._automation.set_os_armed(target)
        if armed and not was_armed:
            self._mirror_watcher()
        elif was_armed and not armed:
            self.watch_paused = True  # truthful: nothing is polling the clipboard
        self._log_harness(
            KIND_ARMED,
            "ARMED - the tool may click, paste and watch the clipboard again"
            if armed
            else "DISARMED - watching only: no clicks, no paste, no clipboard watch",
        )
        self._paint_status()
        # The `w` binding's availability just changed and ``watch_paused`` may
        # not have (disarming an already-paused watcher moves no reactive), so
        # the footer is re-asked by hand.
        self.refresh_bindings()
        if armed:
            self.notify(
                "ARMED - automation restored: the tool may click, paste and watch "
                "the clipboard again",
            )
        else:
            self.notify(
                "DISARMED - watching only: no clicks, no paste, no clipboard watch. "
                "Payloads still land on the clipboard; press i to ingest a reply.",
                severity="warning",
                timeout=8,
            )

    # == ChatView: /theme ======================================================
    # F4's picker and `/theme` are two doors onto one setting, so they share the
    # list (``SettingsScreen.THEME_CHOICES``, in the order that screen offers
    # them) and the save path (``AgentClipApp.remember_theme``). A second list
    # here would be a `/theme` that could set something F4 cannot show.

    def theme_choices(self) -> tuple[str, ...]:
        return tuple(name for name, _label in THEME_CHOICES)

    def current_theme(self) -> str:
        # ``App.theme`` is a reactive with a default, so this only falls back for
        # a screen mounted outside the real app (unit tests do that).
        return str(getattr(self.app, "theme", DEFAULT_THEME) or DEFAULT_THEME)

    def apply_theme(self, name: str) -> None:
        """Wear ``name`` now and remember it, exactly as Save on F4 does.

        The preview and the persistence are deliberately the two halves the
        settings screen already splits: ``app.theme`` is app-wide and applies on
        assignment, ``remember_theme`` is the write. The host app is duck-typed
        for ``_persist_services``'s reason - ``AgentClipApp`` is a
        TYPE_CHECKING-only import here - so a screen mounted outside the real app
        still applies the theme and simply has nowhere to save it.
        """
        self.app.theme = name
        remember = getattr(self.app, "remember_theme", None)
        if remember is not None:
            remember(name)

    # == AutomationView: the ARMED switch =====================================

    def paint_armed(self, armed: bool) -> None:
        """Put the standing DISARMED banner up or take it down (sidebar half).

        The controller's half of ``set_os_armed``, called on every set and not
        only on a change - which is why it may not do anything conditional on
        state the transition has not reached yet. The status bar's DISARMED
        segment is repainted by ``set_os_armed`` instead, because it also
        reports the clipboard watcher and must run after the watcher moved.
        """
        self.post_message(PaintArmed(armed))

    @on(PaintArmed)
    def _on_paint_armed(self, message: PaintArmed) -> None:
        """Painted from the controller rather than the payload, for the reason
        ``_on_paint_loop_state`` gives: the flag is the truth and the message is
        only the ask, so a delivery order the switch was not toggled in cannot
        leave the banner disagreeing with ``os_armed``."""
        message.stop()
        with suppress(NoMatches):
            self.sidebar.show_armed_state(self._automation.os_armed)

    # == ChatView + AutomationView: notifications =============================

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """A transient toast, from whichever thread asked for it.

        Textual's own ``notify`` already ends in a ``post_message``, so it is
        thread-safe as it stands and this override buys no safety. What it buys
        is UNIFORMITY: every other thing this screen is asked for from the poller
        thread is a typed message with a handler, and a toast that took a
        different road would be the one call nobody could find in the pump. It
        also keeps a toast behind the paints of the same decision when both were
        asked for on the same thread - the gate timing out paints its DETECTION
        line, appends its log entry and toasts about it in one call stack.

        The signature is Textual's, unchanged, because everything that already
        called ``self.notify`` on the UI thread still does.
        """
        self.post_message(
            NotifyRequested(
                message, title=title, severity=severity, timeout=timeout, markup=markup
            )
        )

    @on(NotifyRequested)
    def _on_notify_requested(self, message: NotifyRequested) -> None:
        message.stop()
        if message.timeout is None:
            super().notify(
                message.message,
                title=message.title,
                severity=message.severity,
                markup=message.markup,
            )
            return
        super().notify(
            message.message,
            title=message.title,
            severity=message.severity,
            timeout=message.timeout,
            markup=message.markup,
        )

    def start_input(self) -> None:
        """ChatView: the session wants the clipboard watched.

        Every rule behind that (disarmed defers, manual mode explains itself
        instead, and the toast that says so) is the controller's; this end only
        mirrors what it did into the chrome.
        """
        self._automation.start_input()
        self._mirror_watcher()

    def stop_input(self) -> None:
        self._automation.stop_input()

    def _mirror_watcher(self) -> None:
        """Bring ``watch_paused`` in line after something tried to START one.

        The reactive is presentation - the status bar's "paused" and the `w`
        binding read it - so it follows the controller rather than leading it.
        Only the True->False direction lives here, because that is the only one
        a start can cause; the pauses (the `w` key, the disarm) say so at their
        own site, exactly as they always did.
        """
        if self._automation.watching:
            self.watch_paused = False

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
        # No render_state is coming to trigger it (the controller has nothing to
        # push while parked here), so the status bar has to be repainted by hand.
        self._paint_status()
        try:
            return await future
        finally:
            self._new_session_future = None
            self.awaiting_new_session = False
            self._sync_sidebar()
            self._update_composer()
            self._paint_status()

    async def confirm(self, title: str, body: str = "") -> bool:
        return await self.app.push_screen_wait(ConfirmScreen(title, body))

    async def prompt_text(self, title: str, hint: str) -> str | None:
        return await self.app.push_screen_wait(TextEntryScreen(title, hint))

    async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str:
        return await self.app.push_screen_wait(SummaryScreen(_stats_table(rows), summary))

    # -- clipboard watcher ----------------------------------------------------

    @property
    def _watch_worker(self) -> threading.Thread | None:
        """The watcher, as a truthiness this screen (and its tests) can read.

        Compatibility surface: it was a Textual thread worker owned here and is
        now a ``threading.Thread`` owned by the AutomationController, but every
        reader only ever asked whether one exists.
        """
        return self._automation.watcher_thread

    def _start_watcher(self) -> None:
        """Resume the watcher and mirror it into the chrome (the `w` key's other
        half). The start rules - manual mode, already running - are the
        controller's; this is the two-line shell wrapper around them."""
        self._automation.start_watching()
        self._mirror_watcher()

    def _clipboard_captured(self, text: str) -> None:
        """The watcher thread's way in: post, return, decide on the UI thread.

        Called from the AutomationController's watcher thread, so it does the one
        thing that is safe from there - ``post_message`` is Textual's thread-safe
        bridge - and the whole decision lives in the handler below.
        """
        self.post_message(ClipboardCaptured(text))

    def on_clipboard_captured(self, message: ClipboardCaptured) -> None:
        message.stop()
        # A new capture means the conversation moved on without the paste
        # (manual copy, no busy region) - stop nagging either way.
        self.hide_paste_flash()
        # The reply is in hand, however it got there (the flow's click or the
        # user's own copy): the loop's last leg is doing something with it.
        self._log_harness(
            KIND_CLIPBOARD,
            f"a protocol-shaped clipboard capture came in ({len(message.text)} chars)",
        )
        self._set_loop_state(
            LoopState.INTERPRETING, "the reply arrived on the clipboard and is being parsed"
        )
        self._controller.submit_clipboard(message.text)

    # -- key actions / events -> controller -----------------------------------

    def action_approve(self) -> None:
        self._controller.submit_decision(Decision.APPROVE, None)

    def action_auto_edits(self) -> None:
        """The gate's third answer: "stop asking me about these". Under a
        permission ruleset it remembers a rule for calls like this one; in legacy
        mode it is the edits-only auto-accept, offered at edit gates alone."""
        if self._gate_always is not None:
            self._controller.submit_decision(Decision.APPROVE_ALWAYS, None)
        elif self._gate_kind == "edit":
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
        # Through the widget's own ``submit`` rather than straight to
        # ``_submit_text``: this key sends WITHOUT focusing the box, and the
        # composer is what remembers a send for its up/down history. Reading the
        # text out from under it would leave a message the user just watched
        # leave unreachable by `up`. The extra hop is the ``Submitted`` message,
        # which lands back here a moment later.
        try:
            composer = self.composer
        except NoMatches:
            return
        composer.submit()

    def _submit_text(self, text: str) -> None:
        """One door for every composer send.

        While waiting for a new session the text IS the task, and it starts the
        session with a service PER WINDOW: the master's tab decides the
        conversation's budgets, the sub-agent tab's decides what any delegation
        will run on. Both are read here, once, because both are locked for the
        session's life. Otherwise the text goes to the controller.

        A slash line is not a task even here. `/identify` is the one command with
        no session gate precisely because the states it is most needed in are the
        ones with nothing armed - and this prompt is that state, so a task prompt
        that swallowed it would eat the command exactly where it matters and post
        it to the model as the opening message. Slash lines are therefore
        dispatched, and each command's own gate answers: /identify runs, a
        session-gated one toasts its refusal, an unknown one says so, and the
        prompt is still waiting either way. A task that genuinely starts with a
        slash is written `//...` - the same escape the follow-up path uses
        (``SessionController._handle_command``), one slash stripped.

        The ``ask_user`` gate is deliberately NOT like this: there the typed text
        is the answer, verbatim, so an answer like `/etc/hosts` or `/no` is
        delivered rather than parsed (the §3.3a precedence rule, owned by
        ``submit_message``).
        """
        self._remember_own_window()  # typing here = our terminal has OS focus
        future = self._new_session_future
        if future is not None and not future.done():
            task = text.strip()
            if not task:
                self.notify("describe the task first", severity="warning")
                return
            if task.startswith("//"):
                task = task[1:]  # literal-slash task: the escape hatch, unescaped
            elif task.startswith("/"):
                self._controller.submit_message(task)  # a command; the prompt stays up
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
        key = self._automation.service_of(self._window_of(slot))
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
        return self._preset_for(self._automation.live_slot)

    def _live_profile(self) -> ServiceProfile:
        """What the window the automation is driving looks like."""
        return self._profile_for(self._automation.live_slot)

    def _live_has(self, kind: TemplateKind) -> bool:
        """Has the LIVE window's service a capture of ``kind``?

        The one question the finish decision cannot answer for itself, so it is
        handed down as a callback (``has_appearance``): resolving a service key
        against a Config and a cache of profiles read off disk is this screen's,
        and re-read on every call rather than snapshotted because the service
        editor can forget an appearance mid-session.

        **Called on the POLLER thread** since slice 5b, so everything it touches
        has to be answerable without the UI loop:

        * no widget, at any depth - it reads pointers and dicts and nothing else;
        * ``Config`` and ``ServicePreset`` are frozen dataclasses, and
          ``update_config`` REBINDS ``self._config`` rather than editing it, so a
          reader either sees the whole old config or the whole new one;
        * a cached ``ServiceProfile`` is never edited in place. The service
          editor loads its OWN copy off disk, writes PNGs to the store, and the
          cache here is DROPPED afterwards rather than patched - so an entry a
          reader is holding stays exactly as it was read;
        * ``self._profiles`` is a plain dict used as a cache of exactly that.
          The UI thread clears it (``update_config``) while this thread may be
          reading it, and both are single bytecode-level dict operations, so the
          worst case is a redundant ``load_profile`` off disk - which is what a
          cache miss already costs and which ``load_profile`` never raises for.
          A lock here would buy consistency nobody can use: the question is
          "what did the service look like when this tick was taken", and a
          capture forgotten one microsecond ago is not a wrong answer to it.
        """
        return self._live_profile().has(kind)

    def _live_search(self) -> tuple[int, CandidateSource]:
        """How the live window's service wants its appearances hunted for (the
        controller's - see ``live_search``). Every search outside the poller has
        to use the same two settings it was built with, or the ELEMENTS column
        and the thing about to click would answer with different rulers."""
        return self._automation.live_search()

    def _selected_service(self) -> str:
        """The selected tab's service - what the sidebar's picker shows."""
        return self._service_for(self._automation.calibrating_slot)

    def _active_preset(self) -> ServicePreset:
        """The preset behind the sidebar's service picker: the SELECTED window
        tab's service, locked to it while a session runs."""
        return self._preset_for(self._automation.calibrating_slot)

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
        return self._profile_for(self._automation.calibrating_slot)

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

    def action_toggle_elements(self) -> None:
        """Hide/show the ELEMENTS column - same trade as F3, one column over.

        A whole-column toggle rather than four collapsible rows: what a user
        reclaims here is horizontal room for a diff, and folding the pictures
        away one at a time gives back none of it. Hiding it does not stop the
        detectors - the crops keep arriving and keep being painted into a
        column nobody is looking at, which costs a few dozen cells a tick and
        means unhiding it shows *now* rather than a poll interval ago.
        """
        with suppress(NoMatches):
            panel = self.elements_panel
            panel.display = not panel.display

    def action_toggle_harness_log(self) -> None:
        """F8: the same show/hide as `/log`, one keystroke instead of five.

        Deliberately the same call and not a parallel one: two ways to ask for
        one thing, one implementation of it - and the key exists because the
        pane is a thing you flick open mid-run to watch a decision land, which
        is not a thing you type a command for.
        """
        self.toggle_harness_log()

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
            self._pick_chat_region(self._automation.calibrating_slot),
            group="regionpick",
            exclusive=True,
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

        The detectors are suspended for the whole visit, exactly as the service
        editor's capture buttons do it (``AgentClipApp._open_service_editor``,
        §3.4e): this overlay is the same translucent fullscreen child process,
        thrown over the very browser window they are watching, and an overlay
        appearing and vanishing is precisely the sustained large delta that
        arms the trigger on staleness alone. The suspension covers the poller
        whichever slot is being drawn - the overlay spans the whole virtual
        desktop, so the LIVE window is behind it either way, and the restart the
        sub-agent's window used to be spared costs one poll interval against a
        mouse click in a conversation nobody sent anything to.
        """
        self.suspend_detectors()
        try:
            region = await asyncio.to_thread(
                pick_region,
                prompt=self._slot_prompt(_CHAT_REGION_PROMPT, slot),
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        else:
            if region is None:
                self.notify("chat region unchanged (selection cancelled)")
                return
            self._automation.set_calibration(slot, region)
            self._region_click_warned = False
            # Only when the tab it belongs to is still the one on screen: the
            # sidebar shows ONE window's calibration, and writing this one's box
            # into a column describing the other is the same mix-up in the other
            # direction.
            if slot is self._automation.calibrating_slot:
                with suppress(NoMatches):
                    self.sidebar.update_region(region)
            self._after_calibration()
            # The drawn window is where every appearance is searched for AND the
            # staleness detector's whole calibration, so the poller has to be
            # rebuilt around it - but ONLY when the window just drawn is the one
            # the poller is watching. Drawing the sub-agent's window mid-session
            # is the normal way to reach delegation, and rebuilding *around it*
            # would re-aim a poller at a window the automation is not driving.
            if slot is self._automation.live_slot:
                self._start_detector_worker()
            self.notify(
                f"chat region set ({region.describe()}) - the chatbot window; "
                "everything is recognised inside it"
            )
        finally:
            self._picker_open = False
            # After the adoption above, so the common case (the live window was
            # the one drawn) restarted the poller already and this is the no-op
            # ``resume_detectors`` is written to be. Setting a region is pure
            # configuration: the resumed poller reports, and nothing more, until
            # an outbound re-opens the reply gate.
            self.resume_detectors()

    # == ChatView: /log ========================================================

    def toggle_harness_log(self) -> None:
        """`/log` (and F8): show/hide why the harness moved through its states.

        The rail says WHERE the loop is; this says how it got there, and it is
        the only place the reasons survive - a toast is gone in eight seconds
        and half these decisions never raise one at all.

        A pane rather than a modal, and LIVE: the moment a user asks this is the
        moment something is going wrong in front of them, and a snapshot they
        have to close and re-open to see the next entry is the wrong instrument
        for watching a loop. The pane is handed the deque itself and follows the
        tail only while the reader is already at it (widgets/log_pane.py).

        No session gate, for `/identify`'s reason: the log is most wanted
        exactly when a run has gone sideways or ended.
        """
        with suppress(NoMatches):
            pane = self.harness_log_pane
            if pane.display:
                pane.display = False
            else:
                pane.reveal(self._automation.harness_log)

    # == ChatView: /identify ===================================================

    def show_identify_overlay(self) -> None:
        """`/identify`: box every part of the live chat window we can recognise.

        The debug view of the whole recognition model. Everything the automation
        does is "find this captured appearance inside that drawn rectangle", and
        until now the only report of it was the consequence - a click that landed
        somewhere odd, a copy that never fired. This draws the search's actual
        answer on the actual screen, next to the actual buttons.

        The LIVE window, not the selected tab: what is boxed has to be what the
        automation would act on, so mid-delegation this identifies the sub-agent's
        chat while the user is reading the master's transcript. Nothing is
        clicked, moved or typed - the overlay is read-only and takes itself down.
        """
        if self._refuse_second_picker():
            return
        # Same worker group as the region picker: both put a fullscreen child
        # process over the desktop, and two of those at once is unusable.
        self.run_worker(self._identify_live_window(), group="regionpick", exclusive=True)

    async def _identify_live_window(self) -> None:
        """Capture the live chat region, work out what is in it, draw the answer.

        The capture happens FIRST and exactly once, before any overlay exists:
        the overlay covers the browser, so a frame taken with it up would be
        identified as part of the chat window. The detectors are then suspended
        for the drawing, exactly as ``_pick_chat_region`` does it and for exactly
        the same reason - a fullscreen window appearing and vanishing over the
        window they are watching is the sustained large delta that arms the
        auto-copy trigger on staleness alone.
        """
        try:
            region = self.live.chat_region
            if region is None:
                self.notify(
                    'no chat window drawn for this tab - use "Set chat region..." first; '
                    "there is nothing to identify inside yet",
                    severity="warning",
                )
                return
            try:
                scene = await asyncio.to_thread(capture_region, region)
            except CaptureError as exc:
                self.notify(f"could not capture the chat window: {exc}", severity="error")
                return
            tolerance, matcher = self._live_search()
            elements: list[IdentifiedElement] = await asyncio.to_thread(
                identify_elements,
                region,
                self._live_profile(),
                scene,
                tolerance=tolerance,
                matcher=matcher,
            )
            self.suspend_detectors()
            try:
                await asyncio.to_thread(draw_identify_overlay, elements)
            except ScreenPickError as exc:
                self.notify(str(exc), severity="error")
                return
            finally:
                self.resume_detectors()
            # After the overlay is down, so the summary is readable rather than
            # painted behind it.
            self.notify(summarise(elements))
        finally:
            self._picker_open = False

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
        the controller's ``services`` map and the tab's label follows. Everything downstream of
        "what does this service look like?" repaints: the appearance summary and
        the readiness note (half of which is the profile).

        The detector worker restarts only when the tab that changed is the one
        the automation is DRIVING. Re-pointing the sub-agent window mid-session
        is the normal way to set delegation up, and rebuilding the master's
        poller there would throw away its in-flight streaks and its trackers'
        previous frames on behalf of a window nothing is watching.

        The pick also outlives the run: it is written straight back to the
        global config.toml, so the next launch comes up on the service the user
        last worked in rather than on whatever the file was first seeded with.
        """
        message.stop()
        self._automation.set_service(self._selected_window, message.key)
        self._relabel_window(self._selected_window)
        self._persist_active_services()
        self._paint_profile()
        self._after_calibration()
        if self._automation.calibrating_slot is self._automation.live_slot:
            self._start_detector_worker()

    def _persist_active_services(self) -> None:
        """Write both window tabs' services to the global config.toml.

        Both, not just the one that changed: ``save_active_services`` decides
        whether the sub-agent window needs its own key at all by comparing it
        against the master's, which it can only do with the pair in hand.

        Remembering the pick is a convenience, never the point of the press -
        so every way the write can fail (read-only config dir, a locked file, a
        full disk) degrades to a warning toast and a session that carries on
        with the switch the user actually asked for. Only the PERSISTENCE is
        lost; the controller's ``services`` map above is already updated
        either way.

        A screen mounted outside the real app (unit tests do this) has no config
        path to write to, and no business inventing the user's one, so it simply
        skips. The host app is duck-typed rather than isinstance-checked because
        ``AgentClipApp`` is a TYPE_CHECKING-only import here (importing it for
        real would cycle - it imports this module).
        """
        target = getattr(self.app, "global_config_path", None)
        if target is None:
            return
        try:
            save_active_services(
                self._automation.service_of(MASTER_WINDOW),
                self._automation.service_of(SUBAGENT_WINDOW),
                target,
            )
        except OSError as exc:
            self.notify(f"couldn't remember the service: {exc}", severity="warning")

    # -- detector polling ------------------------------------------------------

    def _start_detector_worker(self) -> None:
        """Mirrors ``_start_watcher``: one thread worker watching the live
        slot's chat region and bridging each tick back to the UI via
        ``post_message``. It replaces any previous run, so a recapture or a
        slot move mid-session cannot leave two loops watching two windows.

        ONE capture per tick, handed to ONE ``ScreenDetector``. That is not only
        cheaper: every verdict then describes the same instant of a moving
        screen rather than four moments of it, and a failed capture reaches all
        of them as the same ERROR instead of some seeing a frame and others not.

        What that detector searches for is entirely
        ``screen.detector.build_detector``'s business, and it decides from
        calibration alone: the drawn window, the checklist of the service THAT
        WINDOW is pointed at (never the selected tab's - the user reading the
        master's transcript mid-delegation must not re-aim the poller), and that
        service's captured appearances. This method knows nothing about which
        kinds those are, and deliberately: the loop below is a bridge, not a
        policy. With no region drawn there is nothing to watch and no worker at
        all; with nothing calibrated at all (``ScreenDetector.watching``) the
        worker is likewise not started.

        This is also the ONLY writer of the sidebar's DETECTION block
        (``_paint_detection``): those lines report what is being watched in the
        LIVE window, so every exit here - including the two that start nothing -
        leaves them saying what just became true. Nothing driven by the selected
        tab may touch them, because the tab and the live window part company for
        the whole of a delegation.

        ``_active_detectors`` records which of the FINISH detectors will
        report, in the fixed busy -> idle -> stale order, which is what makes
        the last one the tick's closing probe (see ``finish_tick_closed_by`` on
        the controller). It is
        not the same question as whether a worker runs: a service with a
        captured send button and an empty checklist has nothing that can decide
        a response finished, and still has something to show the user in the
        ELEMENTS column every half second.

        Everything is read once here rather than per tick: restarting the worker
        is how the poller follows the live slot across a delegation, so an
        in-flight loop must keep watching the window it was started for. The
        detector is likewise built once per run - its trackers carry streaks and
        a previous frame, and all of that describes one window - with the
        "stable for N seconds" wish converted to ticks of the poll cadence here,
        from the live window's service preset.

        The THREAD is the AutomationController's, and so is the run's generation
        stamp: ``retarget_detectors`` below ends whatever was polling and opens a
        new run, which is why it is called before the two exits that start
        nothing at all - a rebuild that finds no window still has to invalidate
        the probes the old one has in flight (see ``is_ghost`` on the
        controller). What stays here is
        every question about MEANING: which detectors this composition runs, what
        the sidebar says about them, and every verdict they will produce.
        """
        self._stop_detector_worker()
        self._automation.retarget_detectors()
        # Every tracker is rebuilt below, so the verdicts they produced belong
        # to detectors that no longer exist. The trigger's ARM survives: it
        # records that the model was generating, which recapturing a button
        # does not un-observe.
        self._automation.forget_verdicts()
        self._detector = None
        region = self.live.chat_region
        if region is None:
            self._paint_detection(STALE_UNSET)
            return
        preset = self._live_preset()
        ticks = max(1, round(preset.stable_seconds / _BUSY_POLL_S))
        detector = build_detector(
            region,
            self._live_profile(),
            signals=preset.finish_signals,
            required_ticks=ticks,
            tolerance=preset.tolerance,
            matcher=preset.matcher,
        )
        self._detector = detector
        # The trackers stay reachable under their own names: the flow, the paste
        # and the slot move all reset the DEBOUNCE without touching what the
        # detector has seen, and that is a per-tracker act.
        self._busy_tracker = detector.busy
        self._idle_tracker = detector.idle
        self._stale_tracker = detector.stale
        self._active_detectors = detector.active_detectors
        if not self._active_detectors:
            # The service's checklist is empty, or asks only for appearances it
            # has none of. Say so where the stale verdict would go: an unexplained
            # silent readout is indistinguishable from a detector that is simply
            # never finding anything, and the consequence (auto-copy will never
            # fire) is invisible until the user waits for a copy that never comes.
            self._paint_detection(STALE_OFF)
        else:
            # Whether the stale line is a live verdict or an explanation of its
            # silence: it is the one detector with no appearance behind it, so
            # "unticked" is otherwise indistinguishable from "not reporting yet".
            self._paint_detection(
                STALE_CALIBRATED if "stale" in self._active_detectors else STALE_UNTICKED
            )
        if not detector.watching:
            # Nothing calibrated at all: no tracker to feed and no picture to
            # look for, so a loop would be pure cost. Note this is NOT the same
            # test as the one above - a captured send or copy button with an
            # empty checklist still has something to show, even though nothing
            # can decide a response finished.
            return

        # ONE search pass over the frame per tick, for everything the live
        # window's service is calibrated for, folded by the controller's own
        # consumer in the same call stack, in the fixed busy -> idle -> stale
        # order the tick-closing rule reads. The loop belongs to the controller;
        # what it watches, how fast, and with what capture are decided here.
        self._spawn_detector_worker(
            self._automation.detector_loop(
                detector,
                region,
                capture=_poll_capture,
                poll_seconds=_BUSY_POLL_S,
            )
        )

    def _spawn_detector_worker(self, loop: Callable[[], None]) -> None:
        """Run the composed poll loop on the controller's poller thread.

        The seam between deciding *what* to watch and actually watching it, so
        a test can freeze the polling and still observe the composition - the
        live loop repaints the DETECTION block within milliseconds, which is
        exactly what makes its resting lines otherwise unassertable. It stayed a
        method of this screen for that reason alone: the thread it starts is the
        controller's now, and this is only where the screen learns of it.
        """
        self._detector_worker = self._automation.start_detectors(loop)

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

        The send-gate line is the exception that is NOT reset here: a rebuild
        (a config adopted, an appearance recaptured) does not un-paste the
        outbound the gate is holding for, so it is re-derived from the gate's
        own state instead.

        The ELEMENTS column IS reset, for the opposite reason: its crops are
        pictures cut out of one window, the headings have just been repointed,
        and a rebuild that leaves the old window's send button under the new
        window's name is a straightforward lie. It refills itself on the new
        run's first tick.

        **It opens a new paint epoch**, which is what keeps the reset from being
        undone a moment after it lands. The poller thread's verdicts reach these
        same widgets through the message queue now, and Textual routes a
        cross-thread ``post_message`` through ``call_soon_threadsafe`` - so a
        paint the outgoing run asked for BEFORE this rebuild can still be sitting
        in flight and be delivered AFTER it. The last tick of a cancelled run is
        exactly that paint: a live verdict about a window this block no longer
        describes, which used to be dropped by the ghost filter because the
        PROBE crossed the thread boundary and got filtered on arrival. Now the
        probe never crosses and the paint does, so the same filter has to sit
        here: every ``paint_*`` below stamps the epoch it was asked in, and a
        handler ignores anything older than the current one (``_paint_epoch``).
        """
        self._paint_epoch += 1
        signals = self._live_preset().finish_signals
        profile = self._live_profile()
        window_name = _WINDOW_NAMES[self._window_of(self._automation.live_slot)]
        with suppress(NoMatches):
            panel = self.elements_panel
            panel.show_window(window_name)
            panel.clear()
        with suppress(NoMatches):
            sidebar = self.sidebar
            sidebar.show_detection_window(window_name)
            sidebar.update_template(TemplateKind.SEND_READY, self._send_gate_line())
            for name, kind in (("busy", TemplateKind.BUSY), ("idle", TemplateKind.IDLE)):
                ticked_but_blind = name in signals and not profile.has(kind)
                sidebar.update_template(
                    kind, PROBE_UNCAPTURED if ticked_but_blind else PROBE_RESTING
                )
            sidebar.update_template(TemplateKind.COPY, COPY_RESTING)
            sidebar.update_stale(stale_line)

    def _stop_detector_worker(self) -> None:
        """Cancel the mirrored run, and tell the controller the same thing.

        Both halves, because they are not always the same object: a test freezes
        the spawn and leaves its own stand-in in ``_detector_worker``, and the
        controller is the one that actually holds a thread.
        """
        if self._detector_worker is not None:
            self._detector_worker.cancel()
            self._detector_worker = None
        self._automation.stop_detectors()

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

    # -- the poller thread's one question --------------------------------------

    def _crop_elements(
        self,
        scene: RegionImage,
        sightings: Mapping[TemplateKind, Sighting | None],
    ) -> Mapping[TemplateKind, object]:
        """One tick's recognitions, cut down to pictures before they cross.

        The only thing the poll loop still asks this screen for on its way into
        the consumer, and it does work rather than routing: the crop runs HERE,
        on the poller thread that captured the frame, because the message queue
        should carry an icon and not a chat window (see ``_element_crop``). That
        is why the controller hands over the raw frame and its sightings instead
        of crops - sizing a crop is a question about which renderer this
        terminal can drive, and that is the shell's to answer.

        Touches no widget and reads nothing mutable, which is what makes it safe
        off the UI thread: ``_element_crop`` is a module function over the frame
        it is handed, and the renderer choice inside ``element_crop_image`` is
        settled at import.
        """
        return {kind: _element_crop(scene, sighting) for kind, sighting in sightings.items()}

    def _finish_tick_closed_by(self, detector: str) -> bool:
        """Is ``detector``'s message the tick's LAST, given what is running?

        The controller's rule, kept reachable under this name because it is how
        the Pilot suites check that the poller's build order and the fold's
        tick-closing agree.
        """
        return self._automation.finish_tick_closed_by(detector)

    # -- AutomationView: what the detectors are seeing -------------------------
    # Every method below is called from the POLLER thread (the consumer runs
    # there since slice 5b), so every one of them does the same two things and
    # nothing else: build the typed message that says what to paint, and post
    # it. The handler underneath does the widget writes - including the
    # ``NoMatches`` guards, which are the pre-mount case: a probe can be
    # consumed before the sidebar exists.
    #
    # The three below carry the PAINT EPOCH they were asked in, because they
    # are the run-scoped ones: a rebuild resets this whole block, and a verdict
    # the outgoing run asked for can still be in flight when it does (Textual
    # routes a cross-thread post through ``call_soon_threadsafe``, so it can be
    # overtaken by anything the UI thread posts - or writes - afterwards). The
    # stamp is the ghost filter, moved to the paint side because the probe no
    # longer crosses; see ``_paint_detection``.

    def paint_detection(self, kind: TemplateKind, text: str) -> None:
        """One appearance's line in the sidebar's DETECTION block."""
        self.post_message(PaintDetection(kind, text, self._paint_epoch))

    @on(PaintDetection)
    def _on_paint_detection(self, message: PaintDetection) -> None:
        message.stop()
        if message.epoch != self._paint_epoch:
            return
        with suppress(NoMatches):
            self.sidebar.update_template(message.kind, message.text)

    def paint_stale(self, text: str) -> None:
        """The stale detector's line, which has no appearance behind it."""
        self.post_message(PaintStale(text, self._paint_epoch))

    @on(PaintStale)
    def _on_paint_stale(self, message: PaintStale) -> None:
        message.stop()
        if message.epoch != self._paint_epoch:
            return
        with suppress(NoMatches):
            self.sidebar.update_stale(message.text)

    def paint_elements(self, crops: Mapping[TemplateKind, object]) -> None:
        """Paint one tick's recognised crops into the ELEMENTS column.

        The cast is the port's opacity being cashed in: a crop is sized for
        whatever renderer will draw it, so the automation layer routes the
        mapping without a type for it and this end knows what it cut - it cut
        them itself, in ``_crop_elements``, on the thread now calling this.
        """
        self.post_message(
            PaintElements(
                cast("Mapping[TemplateKind, ElementCrop | None]", crops), self._paint_epoch
            )
        )

    @on(PaintElements)
    def _on_paint_elements(self, message: PaintElements) -> None:
        """Together with ``_paint_detection``'s reset, the only writer of the
        ELEMENTS column - the poller's crops and the harvest's one-shot picture
        of the frame it aimed at both arrive here now. Which keeps it inside the
        DETECTION block's ownership rule (tui.md 3.4e): the crops are cut from
        the LIVE window, so a tab click - which may be showing the OTHER
        window's transcript for the whole of a delegation - must never repaint
        them."""
        message.stop()
        if message.epoch != self._paint_epoch:
            return
        with suppress(NoMatches):
            self.elements_panel.show_matches(message.crops)

    def show_paste_flash(self, text: str, *, retry: bool = False) -> None:
        """Put the ">>> PRESS ... <<<" banner up (``AutomationView``)."""
        self.post_message(ShowPasteFlash(text, retry))

    @on(ShowPasteFlash)
    def _on_show_paste_flash(self, message: ShowPasteFlash) -> None:
        message.stop()
        with suppress(NoMatches):
            self.sidebar.show_paste_flash(message.text, retry=message.retry)

    def hide_paste_flash(self) -> None:
        """Take it down - the send is proven, so the nag is over."""
        self.post_message(HidePasteFlash())

    @on(HidePasteFlash)
    def _on_hide_paste_flash(self, message: HidePasteFlash) -> None:
        message.stop()
        with suppress(NoMatches):
            self.sidebar.hide_paste_flash()

    def _fire_auto_copy(self) -> None:
        """The controller decided the model stopped: harvest the reply.

        The one action the finish decision still hands back, because SCHEDULING
        is what a UI framework really owns - and since 5b the decision is taken
        on the POLLER thread, where ``run_worker`` may not be called at all. So
        this posts, and the handler launches.

        The deferral costs nothing the fire-once rule needed: ``evaluate_finish``
        sets ``flow_running`` synchronously *before* asking, so every tick that
        lands in the hop between this post and its handler is suspended and
        cannot ask again.
        """
        self.post_message(AutoCopyRequested())

    @on(AutoCopyRequested)
    def _on_auto_copy_requested(self, message: AutoCopyRequested) -> None:
        """Put the harvest on a worker: same group, same exclusivity as ever.

        The bracket around it - the flow-suspension the ``finally`` lifts - is
        the controller's (``run_auto_copy_flow``); what crosses back up is the
        body, as this screen's own stubbable seam, resolved when the handler
        runs so a suite that replaced ``_auto_copy_flow`` is still the one that
        runs.
        """
        message.stop()
        self.run_worker(
            self._automation.run_auto_copy_flow(self._auto_copy_flow),
            group="copyflow",
            exclusive=True,
        )

    # -- the gates, as this screen's own call sites still name them ------------
    # Every one of them is the AutomationController's decision now; these are
    # the doors the paste path, the teardown and the slot moves already knock on.

    def _reset_finish_trigger(self) -> None:
        """Forget every detector verdict and the auto-copy arm."""
        self._automation.reset_finish_trigger()

    def _open_reply_gate(self) -> None:
        """An outbound just went into the chat: a reply is now due, so the
        detectors may arm and fire until it has been harvested."""
        self._automation.open_reply_gate()

    def _close_reply_gate(self) -> None:
        """No reply is outstanding any more, so nothing may move the mouse."""
        self._automation.close_reply_gate()

    def _send_gate_line(self) -> str:
        """The sidebar's send line, re-derived from the gate rather than stored."""
        return self._automation.send_gate_line()

    def _copy_last_seen_note(self) -> str:
        """What the always-running detector remembers about the copy button.

        The other half of a failed harvest's report, and the reason it is still
        here: the ``ScreenDetector`` is built and held by this screen. ``how_close``
        says how close THIS frame came; this says whether the poller has ever
        seen the icon in this window at all - which separates "the capture no
        longer matches anything, ever" from "it was there thirty seconds ago and
        this response has not drawn one yet". Read-only, and never a coordinate:
        the harvest re-searches for what it clicks.
        """
        detector = self._detector
        if detector is None or not detector.searches(TemplateKind.COPY):
            return ""
        ago = detector.seen_ago(TemplateKind.COPY)
        if ago is None:
            return "; the poller has never seen it in this window either"
        return f"; the poller last saw one {ago:.0f}s ago"

    async def _auto_copy_flow(self) -> None:
        """The harvest, one delegation below (``auto_copy_flow``).

        It stays a method of this screen because it is the seam the Pilot suites
        stub: half a dozen of them replace the whole harvest with a recorder to
        test what FIRES it, and the fire path reaches the flow through this
        name. Everything the harvest DOES - the focus click, the snap rounds,
        the hunt, the hover scan, the verified click, the snap-back - is the
        controller's now.
        """
        await self._automation.auto_copy_flow()

    async def _ingest_prose_harvest(self) -> None:
        """Hand a verified copy click's harvest to the session even when it has
        no CLIP blocks, if this service opted in (``ServicePreset.capture_prose``).

        The one loosening of protocol.md 1.4 tolerance #11, and deliberately
        scoped to the flow's own click rather than to the watcher: THIS
        clipboard text is known to be the model's reply - the flow just watched
        the copy button put it there - while the watcher sees every copy the
        user makes and must keep ignoring the non-protocol ones. Protocol-shaped
        harvests are left alone entirely; the watcher ingests those on its own,
        exactly as before, and reading them here too would ingest them twice.

        The prose is only ever DISPLAYED (the controller shows it in the
        transcript and invites a follow-up); nothing in it executes, and the
        engine still counts it as noise.
        """
        if not self._live_preset().capture_prose:
            return
        try:
            text = await asyncio.to_thread(self._provider.read_text)
        except ClipboardUnavailable:
            return
        if not text or looks_like_protocol(text):
            return  # protocol traffic: the watcher's job, untouched
        self.hide_paste_flash()
        self._log_harness(
            KIND_CLIPBOARD,
            f"the harvested reply has no CLIP blocks ({len(text)} chars); "
            "capture_prose is on, so it goes to the transcript as prose",
        )
        self._set_loop_state(
            LoopState.INTERPRETING, "the reply has no CLIP blocks - showing it as prose"
        )
        self._controller.submit_clipboard(text, accept_prose=True)

    async def _verified_copy_click(self, target: ScreenRegion) -> bool:
        """Click the matched copy-button rect at slightly offset points until the
        clipboard actually changes (the controller's - see
        ``verified_copy_click``).

        A named seam rather than a straight call from inside the harvest,
        because it is the one the Pilot suites stub: verifying a click means
        three clicks and up to six clipboard reads a fifth of a second apart,
        and none of that is about where the transcript scrolled to.
        """
        return await self._automation.verified_copy_click(target)

    # -- the browser's new-chat button ------------------------------------------
    #
    # No sidebar status line: nothing here is DECIDED from the new-chat button -
    # it is clicked, and every outcome below already says what happened as a
    # toast. Its live picture is in the ELEMENTS column with every other kind
    # (the poller searches for it on every tick, §1.7); this click still
    # re-searches a fresh capture, because a polled corner is up to half an
    # interval old and this one moves the mouse.

    @on(Button.Pressed, "#newchat-btn")
    def _on_newchat(self, event: Button.Pressed) -> None:
        event.stop()
        # One refusal left, and it is not about the master's turn: a mid-run
        # press on the SUB-AGENT tab would empty the chat a delegated run is
        # still talking to, destroying the run's conversation without ending the
        # run. On the master tab there is nothing to refuse - the press ends the
        # session, and request_new_session aborts the turn in flight to do it
        # (§1.3).
        if self._mid_turn() and self._automation.calibrating_slot is not AgentSlot.MASTER:
            self.notify(
                "the sub-agent window's chat belongs to the run in flight - /abort ends "
                "it, or start the new chat from the master tab",
                severity="warning",
            )
            return
        # A mouse press on OUR sidebar means our terminal has the OS focus right
        # now, so this reading is trustworthy - and it is the one moment before
        # the flow's snap-back where the handle can still be learned. Without it
        # a run whose composer was never used (calibrate, press, done) has no
        # window to come back to, and the click leaves the user in the browser.
        self._remember_own_window()
        self.run_worker(
            self._new_browser_chat(self._automation.calibrating_slot),
            group="newchat",
            exclusive=True,
        )

    def _mid_turn(self) -> bool:
        """Is a turn actually in flight right now?

        NOT simply ``busy``: while the inline start flow waits for the first
        message the session worker is technically busy, and there is no turn
        there to lose - the same distinction ``AgentClipApp.action_quit`` draws
        before it warns about quitting.

        One caller left, and one meaning with it: the sub-agent tab's new-chat
        refusal above. The master tab used to ask this too and refuse; it now
        goes ahead and the controller aborts the turn for it, which is the
        difference between "you cannot leave this conversation" and "leaving it
        ends what it was doing".
        """
        if self.awaiting_new_session:
            return False
        return self.busy or self.pending_approval or self.awaiting_answer

    # Why the browser's button went unclicked, one reason per outcome, each with
    # the way out of it. The toast is the only place the user learns that the
    # half AgentClip could not do is now theirs, so none of them may stop at
    # "it failed" - and none of them may claim a reset that did not happen,
    # which is what the two tails below are for.
    _NO_CLICK_REASONS = {
        ElementClick.DISARMED: (
            "disarmed - no new chat was opened in the browser (nothing was clicked); "
            "press F5 to arm"
        ),
        ElementClick.NOT_CALIBRATED: (
            'capture the browser\'s new-chat button first (F2 > "New-chat button" > '
            "Capture) and draw the chat window it lives in - nothing was clicked"
        ),
        ElementClick.MISMATCH: (
            "the new-chat button is not on screen in the chat window - nothing "
            "was clicked; recapture it or redraw the window"
        ),
        ElementClick.AMBIGUOUS: (
            "found several things that look like the new-chat button in the chat "
            "window - nothing was clicked; redraw the window so it contains only "
            "this chat"
        ),
        ElementClick.NOT_CLICKED: "the new-chat click did not land (it is Windows-only)",
    }
    _RESTARTED_TAIL = ". AgentClip is on a fresh session anyway - open a new browser chat yourself"
    _NOT_RESTARTED_TAIL = ". Nothing on the tool side to renew - open a new browser chat yourself"

    async def _new_browser_chat(self, slot: AgentSlot) -> None:
        """Click ``slot``'s browser new-chat button, then hand focus back here.

        The one implementation behind both ways of asking for a fresh chat: the
        sidebar button (which drives the *calibrating* slot, so the user can
        test either window from the place the sidebar is pointed at) and ``/new``
        (always the master's). It never moves the live slot - that is
        ``start_browser_chat``'s job alone.

        Located first: if the button is not on screen nothing is clicked, because
        the alternative is a blind click somewhere in a browser window. But the
        click is BEST-EFFORT, not a precondition. AgentClip owns one half of a
        fresh chat and the user owns the other, and refusing the half it can do
        because it could not do the half it cannot made ``/new`` useless in
        precisely the situation it was needed - a window not calibrated, or not
        calibrated any more. So every outcome resets the session, and a failed
        click spends its toast saying which half is left to the user.

        Which slot this is runs as an ARGUMENT rather than a re-read, because it
        is decided once before the click - the same rule the region picker
        follows (§3.4a). There are awaits either side of the click, the user can
        select another tab across them, and re-reading ``_calibrating``
        afterwards would credit the master with a chat that was opened in the
        sub-agent's window - and end the master's session for it.
        """
        outcome = await self._automation.click_profile_element(slot, TemplateKind.NEW_CHAT)
        if outcome is not ElementClick.CLICKED:
            # Reset first, only so the toast can say truthfully whether it
            # happened: on the sub-agent tab, and with no session running, there
            # is no tool side to renew and promising one would be a lie.
            # No refocus either - the browser is where the user has to finish
            # the job, so it keeps whatever focus it already has.
            restarted = self._reset_after_new_browser_chat(slot)
            tail = self._RESTARTED_TAIL if restarted else self._NOT_RESTARTED_TAIL
            self.notify(self._NO_CLICK_REASONS[outcome] + tail, severity="warning")
            return
        self.notify("new browser chat opened")
        # Same beat as the auto-copy flow's, and the same one call:
        # ``snap_back_after_click`` lets the click register before focus moves
        # away, then brings the user back to AgentClip.
        await self._automation.snap_back_after_click()
        self._reset_after_new_browser_chat(slot)

    def _reset_after_new_browser_chat(self, slot: AgentSlot) -> bool:
        """Start a fresh SESSION too, when the chat just emptied was the master's.

        A new browser chat on the master tab means the conversation this session
        is having no longer exists, so leaving the session running would paste
        its next turn into a chat with none of its history in it. Only on the
        master tab: the sub-agent window hosts delegated runs, which are the
        controller's to start and end, never the user's.

        Whether the click landed is deliberately not a condition. The user asked
        for a new conversation, and the tool side is the part AgentClip can
        always deliver; withholding it when the browser could not be reached
        leaves the user with neither half and no way to get the first one. So a
        refused click still lands here - ``_new_browser_chat``'s toast is what
        tells them the browser is still showing the old chat.

        This is also the *whole* tool-side of ``/new``: the command asks the
        view to open the chat, and the reset it wanted arrives here.

        Whether a turn is in flight is not a condition either, and not this
        side's business: ``request_new_session`` aborts one if it finds one
        (poisoning whatever park it is on, cancelling an executing step) and
        resets when it has unwound. It still returns True for that - the fresh
        session is coming, a beat later than usual.

        With no session running there is nothing to reset - the start screen
        just got itself a clean chat to start in - and that is not an error.
        Returns whether a fresh session actually started, which is what lets a
        failed click's toast say so without guessing.

        ``slot`` is the window the click actually went to, read before the
        click rather than after it - see ``_new_browser_chat``.
        """
        if slot is not AgentSlot.MASTER:
            return False
        if not self.session_active:
            return False
        return self._controller.request_new_session()

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
            self._automation.calibration(AgentSlot.SUBAGENT),
            self._profile_for(AgentSlot.SUBAGENT),
        )

    def delegation_missing(self) -> tuple[str, ...]:
        """The calibrations still standing between here and ``can_delegate``.

        Handed to the controller as data so the error the *model* gets when it
        calls ``delegate`` against an uncalibrated host names the actual gaps -
        the controller cannot import ``screen`` to ask, and should not have to
        know what a "new-chat button" is.
        """
        return missing(
            self._automation.calibration(AgentSlot.SUBAGENT),
            self._profile_for(AgentSlot.SUBAGENT),
        )

    # -- AutomationHost: what the sequences below still ask this shell ---------
    # The counterpart of the AutomationView block: those are paints going down
    # to up, these are the four questions and the two acts the OS-acting
    # sequences in the AutomationController cannot answer for themselves
    # (agentclip/driver/automation/host.py). Each is one line onto a private this
    # screen already had, which is also what keeps the late binding the Pilot
    # suites rely on: they stub ``_find_all``, ``_verified_copy_click`` and
    # ``_start_detector_worker`` on the CLASS, and the lookup happens here, per
    # call, rather than at construction.

    def live_preset(self) -> ServicePreset:
        """The preset of the window the automation is driving: how it scrolls,
        whether it wants a hover scan, how its appearances are hunted for."""
        return self._live_preset()

    def profile_for(self, slot: AgentSlot) -> ServiceProfile:
        """What one window's service looks like."""
        return self._profile_for(slot)

    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        """Every place ``kind`` is on screen right now, in absolute coordinates."""
        return await self._find_all(kind, slot, scene=scene)

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        """Click the copy button until the clipboard changes."""
        return await self._verified_copy_click(target)

    async def ingest_harvest(self) -> None:
        """A verified copy click landed: show a non-protocol reply as prose if
        this service opted in. The SESSION is ``agentclip.shell.app``'s, which the
        automation layer may not import - hence the ask."""
        await self._ingest_prose_harvest()

    def copy_seen_note(self) -> str:
        """What the always-running detector remembers about the copy button."""
        return self._copy_last_seen_note()

    def rebuild_detectors(self) -> None:
        """The automation moved to another window: rebuild the detector set
        around what THAT window's calibration says now, and repoint the
        readout."""
        self._start_detector_worker()

    async def start_browser_chat(self, slot: AgentSlot) -> bool:
        """Open a fresh browser chat in ``slot`` and make it the live one (the
        controller's - see ``start_browser_chat``).

        All-or-nothing by contract: a False return means nothing was clicked and
        nothing was retargeted, so the caller can abort the delegation before a
        character is pasted.
        """
        return await self._automation.start_browser_chat(slot)

    def end_browser_chat(self) -> None:
        """Hand the automation back to the master chat when a delegation ends
        (the controller's - see ``end_browser_chat``). Unconditional and never
        fails: the master window is where the session lives."""
        self._automation.end_browser_chat()

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

    def action_cycle_permission_mode(self) -> None:
        """shift+tab: ask -> plan -> unattended -> ask (§2.6a).

        Deliberately ungated in ``check_action`` (it falls through to the
        default ``True``), unlike every single-letter key on this screen: the
        moment a user reaches for this is the moment the app is busy doing the
        thing they want it to stop doing. Without a live session the controller
        says so and changes nothing - the status bar is showing the configured
        default there, which is not a session's state to change.
        """
        self._controller.cycle_permission_mode()

    def action_undo(self) -> None:
        self._controller.undo()

    def action_recopy(self) -> None:
        """`c`: re-copy the last outbound - and, on a second press inside the
        double-tap window, re-deliver it. The controller owns both the payload
        and the window, so the whole decision is made there; this screen only
        supplies the two halves it can act on (``park_outbound`` /
        ``redeliver_outbound``)."""
        self._controller.recopy()

    def action_reinstruct(self) -> None:
        """`r`: arm/disarm the service's extra instructions for the next payload
        (tui.md 3.4h). The controller owns the whole decision - including both
        refusals - because the engine is the only thing that knows whether there
        is a session and what its preset actually says."""
        self._controller.reinstruct()

    def action_force_ingest(self) -> None:
        # The user says the reply is on the clipboard right now. If the parse
        # then fails, the settled status push walks this back to idle.
        self._set_loop_state(
            LoopState.INTERPRETING, "you pressed i: ingesting the clipboard by hand"
        )
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
        if not self._automation.os_armed:
            # check_action already hides the key; this is the palette/rebind door
            # into the same action, and a resumed watcher would be a hole in the
            # promise the switch makes.
            self.notify(
                "disarmed - the clipboard watcher stays off until F5 arms the tool",
                severity="warning",
            )
            return
        if self._automation.watching:
            self._automation.stop_input()
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

        ``verbatim`` is the slash-command popup's suppression switch (§3.3a),
        and the rule behind it is "does a leading slash mean anything here?" -
        offering to complete one where it does not would be a lie about what
        Enter is going to do. That is now exactly ONE mode: an open ``ask_user``
        gate (``SessionView.awaiting_answer``), where the text is the answer and
        `/no` is an answer. At the task prompt Enter dispatches commands
        (``_submit_text``), so the popup belongs there like anywhere else.
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
                # "Esc clears / shortcuts" is the two-stage key (§3.3c) in the
                # width a border title has: Esc empties the box, and Esc on an
                # already-empty box frees the single-key shortcuts. The old
                # "Esc for shortcuts" now describes only the second press, and
                # a title that promises the wrong thing about a key that can
                # throw away a paragraph is worse than a shorter one. The full
                # story, including the ctrl+z that gives the text back, is on
                # the help screen.
                "Task done · type a follow-up to continue · Esc clears / shortcuts"
                if self.phase_name == "DONE"
                else "Message the model  ·  Enter sends · Ctrl+J newline · Esc clears / shortcuts"
            )
        else:  # no session, executing, at a gate, etc.
            composer.disabled = True
            composer.border_title = self._composer_idle_title()
        # Last, so the popup is re-decided against the mode we just settled on
        # (the setter re-syncs it, and a disabled box never shows one).
        composer.verbatim = self.awaiting_answer

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
        if self.awaiting_new_session:
            # _busy is technically True here too (the session worker is parked
            # on the inline prompt) but there is no turn in flight - nothing for
            # the user to wait on - so the bar must not say "working".
            return "○ idle", "st-dim"
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
        # The snapshot is the authority once there is a session (it is the
        # engine's own policy, and during a delegation it is the SUB-AGENT's,
        # like every other field here); before one, the controller's mirror of
        # the configured default is what shift+tab would be changing.
        mode = snap.mode if snap else self._controller.permission_mode
        mode_class = {"plan": "st-plan", "unattended": "st-unattended"}.get(mode, "st-dim")
        service = f"{snap.service_key} {_fmt_k(snap.budget_chars)}" if snap else "no session"
        out = (
            f"out {_fmt_k(snap.last_outbound_chars)}/{_fmt_k(snap.budget_chars)} (1/1)"
            if snap
            else "out -"
        )
        turn = f"turn {snap.turn}" if snap else "turn -"
        # The edits slot follows the same rule, so a `/yolo` armed at the start
        # prompt is visible before the session it will govern exists.
        if snap.yolo if snap else self._controller.yolo:
            edits, edits_class = "⚡ YOLO", "st-yolo"
        elif snap and snap.auto_accept_edits:
            edits, edits_class = "EDITS:auto", ""
        else:
            edits, edits_class = "EDITS:ask", ""
        try:
            root = str(Path("~") / self._project_root.relative_to(Path.home()))
        except ValueError:
            root = str(self._project_root)
        # "mcp 2/3" = connected servers over enabled ones (disabled entries are
        # a config statement, not a runtime hope, so they are out of both
        # numbers' way). Empty - which hides the segment - exactly when the app
        # has no manager: an install without MCP gets the bar it always had.
        mcp = ""
        if self._mcp_manager is not None:
            statuses = self._mcp_manager.statuses()
            if statuses:
                connected = sum(1 for s in statuses if s.state == "connected")
                enabled_total = sum(1 for s in statuses if s.state != "disabled")
                mcp = f"mcp {connected}/{enabled_total}"
        bar.update_segments(
            mode=f"MODE:{mode}",
            mode_class=mode_class,
            watch=watch_text,
            watch_class=watch_class,
            # Its own segment rather than a word folded into the watch one: that
            # segment says what the app wants FROM the user, and this says what
            # the app may do TO their machine - and the YOLO badge two along
            # cannot borrow the slot either, since a disarmed YOLO session is a
            # real and worth-seeing pair.
            armed="" if self._automation.os_armed else "⛔ DISARMED",
            service=service,
            out=out,
            turn=turn,
            # Lit only between the `r` press and the payload that spends it.
            instr="✎ INSTR" if snap and snap.instructions_armed else "",
            edits=edits,
            edits_class=edits_class,
            mcp=mcp,
            root=root,
        )


def _stats_table(rows: list[tuple[str, str]]) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    for label, value in rows:
        table.add_row(label, value)
    return table
