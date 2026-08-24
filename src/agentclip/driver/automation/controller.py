"""AutomationController: the UI-agnostic screen-automation core.

Sibling of :class:`~agentclip.shell.app.controller.SessionController` and the same
kind of object: the state and the decisions behind what AgentClip does *to* the
browser chat window, lifted out of the Textual ``MainScreen`` so a second shell
can drive the identical loop. It talks to the UI only through the
:class:`~agentclip.driver.automation.view.AutomationView` port and therefore imports no
Textual (docs/design/gui.md §1).

Since phase 6.2 of docs/design/ui-monitor.md it owns no thread, no pixels and no
counting. Everything on the far side of the screen is one object below it, the
:class:`~agentclip.driver.monitor.protocol.UIMonitor`, handed in at construction,
and the list is now the whole list:

* the poll loop, the trackers and their swap discipline, the generation stamp;
* the mouse, the keyboard, the clipboard and its watcher;
* every SEARCH - where an appearance is, whether there are several, how close a
  miss came, the hover walk that finds a button only drawn under the pointer,
  and the scroll that snaps a transcript to its bottom (§2.3);
* and the two consecutive-tick COUNTS - the stale-arm run and the run of ticks
  every detector agreed on - which ride in on the tick because a count is a
  statement about a screen, and a brain keeping its own totals would lose them
  on every reconnect (§2.2, §2.9).

What is left here is *meaning*: what a tick means, what may act on it, and what
the shell is told about it. Every OS-acting sequence below is still choreography
- click, settle, snap, look, click, hand back - but each of its steps is now one
named verb across the seam rather than a frame this object holds.

**The armed flag.** ``/armed`` and F5. DISARMED means the tool stops ACTING on
the machine - no clicks, no synthetic paste, no cursor moves, no focus stealing,
no clipboard watching - while every read-only half (the monitor's own polling,
the whole sidebar readout) stays live. It lives *below* both shells rather than
inside one, because with two of them a view-owned flag is a flag that drifts.
The consequence it owns *itself* is the one made of state down here: whether the
monitor is watching the clipboard, and the memory of what that watcher was doing
when a disarm took it away. The rest - the three remaining chokepoints, the
toasts, the status bar - is still the shell's, which is why ``set_os_armed``
returns the state now in force.

**The clipboard, at arm's length.** The watcher thread, the poll interval, the
provider and the self-write register are all the monitor's (ui-monitor.md
§2.11); this object asks for one with ``watch_clipboard`` and hears what it
caught through the ``on_clip`` hook it registers at construction. Captures leave
that hook through the ``on_clipboard_captured`` callback the shell hands in
(the Textual shell posts a message from it; the GUI enqueues onto its bridge),
which carries the same non-blocking, thread-safe contract every
``AutomationView`` method has.

**The probe consumer**, which is what a tick MEANS. One ``subscribe`` hook
unpacks the :class:`~agentclip.driver.monitor.protocol.Tick` and calls the five
``consume_*`` methods in the fixed busy -> idle -> stale -> send-ready ->
elements order the message pump used to serialize; the per-detector
bookkeeping, the readout, the ready-to-send gate, the loop's narration and the
combined finish verdict are what they do. One call stack per tick, so the
tick-closing rule is trivially true: there is no queue between the producer and
the consumer to reorder anything (docs/design/gui.md §1).

Two things the fold cannot answer for itself stay the shell's, and cross as
callbacks handed in at construction: ``has_appearance`` (what the LIVE window's
service has a capture of - a question about a profile cache keyed off the
shell's Config) and ``on_fire`` (what to launch when the verdict says
"finished"). Everything else it needs it owns, which is why the trigger, both
gates, ``LoopState`` and the harness log all live here now.

**Ghosts.** A tick carries the ``generation`` it was captured under, the monitor
bumps that on every ``configure`` and drops anything older before a subscriber
ever sees it (ui-monitor.md §4.2). The stamp is still compared here, because the
five ``consume_*`` methods are a public seam a shell may call with a stamp of
its own - a verdict about the window the automation was driving before a
delegation started must not arm the trigger against the new one.

**The slot pointers.** Which chat window is being configured and which one is
being driven, plus the drawn box and the service key behind each. Two pointers,
independent on purpose: *calibrating* is the slot behind the selected window tab
- what the sidebar's region picker writes into - and *live* is the slot the
automation (paste click, monitor, auto-copy) is driving right now. The user must
be able to draw the sub-agent's window while the master chat is mid-turn, and a
delegation must be able to retarget the automation without dragging the user's
view along with it.

What is emphatically NOT here is the *content* behind those pointers. A
:class:`~agentclip.driver.screen.profile.ServiceProfile` - what a service LOOKS like -
is loaded from disk and cached by the shell, which hands resolved values in;
this object holds the service KEY per window and nothing about what that key
resolves to. Same rule for the calibration: the controller owns which slot is
which, the shell owns what it does with the rectangle.

**The OS-acting sequences.** The auto-copy harvest, the find-then-click
primitive every programmatic click goes through, the hover scan, and the two
calls that move the automation between browser windows. They are coroutines,
because that is what they were on the screen and what a harvest IS - a
choreography of clicks, scrolls and settles with awaits between them - and this
object is allowed an event-loop-facing surface for exactly the reason
``SessionController`` is. What a shell keeps is the SCHEDULING (Textual puts the
harvest on a ``run_worker``) and the handful of answers only it has, which cross
as :class:`~agentclip.driver.automation.host.AutomationHost`. Every one of those
acts is now ``await self._monitor.click(...)`` and its siblings - and since phase
6.2 that includes the pixel work those sequences used to do for themselves. The
copy-icon hunt, the chat-box verification, the hover scan and the snap to the
bottom are ``locate`` / ``find_all`` / ``hover_scan`` / ``snap_to_bottom`` on the
monitor now (ui-monitor.md §2.3): nothing here captures a frame, compares a
template or names a tolerance any more. What is left of each of them is the
POLICY around the verdict - which kinds a match may not be, whether an ambiguous
answer may be clicked, how many rounds a hunt is worth, and what the user is told
when it comes up empty.

**Threading, as of this phase.** Two threads still write the state below: the UI
thread (a paste, a slot move, a modal, ``/new``) and whichever thread the
monitor pushes a tick from. ``_tick_lock`` is what keeps them from interleaving.
It is a ``RLock`` because consumption is re-entrant - ``consume_stale_probe``
reaches ``evaluate_finish`` reaches ``close_reply_gate`` - and it is held for
exactly one probe's consumption, which is the same grain the message pump gave
this code when each probe was a message of its own: a retarget landing mid-tick
could always drop the REST of that tick, and still can. What it may not do, and
what the lock forbids, is land in the MIDDLE of one probe's bookkeeping, between
the ghost check and the verdict it guards.

It is also the one reason ``threading`` is still imported here, and it goes in
phase 2: once the loop pulls (``tick = await ui.observe()``) instead of being
pushed into, every writer below is on the event-loop thread and there is nothing
left to serialize (ui-monitor.md §6.2, "no ``threading`` import outside
``driver/monitor``").

Held across bookkeeping, not across work. Every paint leaves through the view
port, which is non-blocking by contract, and the expensive halves of a tick -
the capture and the template search - happen inside the monitor, nowhere near
it, as does every search the sequences make. The one call under it that can touch a disk is ``has_appearance`` on a cold
profile cache, which is the shell's own read of its own file and is why the
port's contract asks for it to be cheap.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, Protocol

from agentclip.config import (
    DELIVERY_STREAM,
    SCROLL_END,
    SCROLL_PAGE_DOWN,
    ServicePreset,
)
from agentclip.driver.automation.alerts import AttentionAlarm
from agentclip.driver.automation.delivery import (
    AUTO_SEND_FLASH_TEXT,
    ENTER_FLASH_TEXT,
    PASTE_FLASH_TEXT,
    stream_flash_text,
)
from agentclip.driver.automation.finish import (
    SEND_ARM_MIN_DIFF,
    SEND_ARM_TICKS,
    SEND_GATE_SEEN_TIMEOUT_TICKS,
    SEND_GATE_TIMEOUT_TICKS,
    SEND_READY_ARMED,
    SEND_READY_HOLDING,
    SEND_READY_OVERRIDDEN,
    SEND_READY_RELEASED,
    SEND_READY_RESTING,
    SEND_READY_SEEN,
    SEND_READY_STUCK,
    SEND_READY_TIMEOUT,
    SendGate,
    busy_verdict,
    format_busy_probe,
    format_idle_probe,
    format_stale_probe,
    idle_verdict,
    stale_verdict,
)
from agentclip.driver.automation.flow import (
    COPY_CLICK_OFFSETS,
    COPY_SNAP_ROUNDS,
    COPY_VERIFY_INTERVAL_S,
    COPY_VERIFY_READS,
    ELEMENT_CLICK_SETTLE_S,
    SNAP_SETTLE_S,
    above_chatbox,
    how_close,
)
from agentclip.driver.automation.harness_log import (
    HARNESS_LOG_MAX,
    KIND_COPY,
    KIND_GATE,
    KIND_STATE,
    KIND_TRIGGER,
    HarnessEntry,
    state_text,
)
from agentclip.driver.automation.host import AutomationHost, NullHost
from agentclip.driver.automation.loop_state import ATTENTION_STATES, LoopState
from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.automation.view import AutomationView
from agentclip.driver.clip import chunking
from agentclip.driver.clip.base import ClipboardUnavailable
from agentclip.driver.clip.watcher import SelfWriteSet

# The beats every OS-acting sequence below paces itself by, as a MODULE rather
# than as names: cadence belongs to the machine being driven (ui-monitor.md
# §2.10), and reading ``beats.X`` at the call site is what lets a suite shrink a
# beat by writing to it - a ``from ... import X`` would have bound the number
# here at import time and every test would wait out a real browser's repaint.
from agentclip.driver.monitor import beats
from agentclip.driver.monitor.protocol import Located, MonitorSpec, Tick
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import Sighting
from agentclip.driver.screen.matchers import select_matcher
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.slot import AgentSlot, SlotCalibration, new_slots
from agentclip.driver.screen.stale import StaleProbe, StaleTracker
from agentclip.driver.screen.template import CandidateSource

# The two layouts one service's chat box can be drawn in, as one tuple: a fresh
# chat centres its input box and an ongoing one docks it at the bottom. Named
# here because they are asked about together everywhere - hunted one after the
# other for the delivery's click, and handed to ``locate`` as the appearances a
# copy icon may NOT turn out to be.
CHATBOX_KINDS: tuple[TemplateKind, ...] = (
    TemplateKind.CHATBOX_INITIAL,
    TemplateKind.CHATBOX_ONGOING,
)

# The tick's recognitions, cut down to pictures. Sizing a crop depends on which
# renderer the shell can drive, so the CUT is the shell's - but it happens on the
# monitor's thread, the one that captured the frame, because what crosses to a UI
# is then one small picture per appearance rather than a whole chat window. What
# comes back is opaque (``AutomationView.paint_elements`` takes ``object``), and a
# shell that hands nothing in gets the sightings themselves, uncut.
CropFn = Callable[
    [RegionImage, Mapping[TemplateKind, Sighting | None]], Mapping[TemplateKind, object]
]
# One captured frame and what was recognised in it - the monitor's local-only
# frame hook (ui-monitor.md §2.2: pixels are a calibration surface, so they never
# ride the wire and never ride a ``Tick``). No stamp on it, unlike a tick: it is
# delivered on the monitor's own thread immediately after the tick it belongs to,
# so the run it describes is the one the monitor is in.
FrameHook = Callable[[RegionImage, Mapping[TemplateKind, Sighting | None]], None]


class MonitorLike(Protocol):
    """The :class:`~agentclip.driver.monitor.protocol.UIMonitor` this phase needs,
    which is that contract PLUS the local-only tier (ui-monitor.md §3).

    Declared here rather than in ``driver/monitor/protocol.py`` because it is
    this object's requirement and not the wire's: the four members under "the
    local-only tier" below are pixels and Python objects, they will never cross
    a socket, and a ``RemoteUIMonitor`` will never answer them. Everything above
    that line is the real contract, spelled out again only so mypy checks one
    Protocol at the call site instead of two.

    Phase 2 shrank it: ``ops`` is gone, and with it every way this object could
    reach a frame. The five pixel verdicts below (``find_all``, ``locate``,
    ``click_element``, ``hover_scan``, ``snap_to_bottom``) are what replaced it,
    and they are contract members rather than local-only ones - a
    ``RemoteUIMonitor`` answers every one of them. What is still local-only is
    the frame hook (pixels, so they never ride the wire), the self-write register
    and the trackers the shells mirror in their own chrome.
    """

    # -- the contract (driver/monitor/protocol.py) -------------------------
    @property
    def spec(self) -> MonitorSpec | None: ...
    @property
    def generation(self) -> int: ...
    async def configure(self, spec: MonitorSpec) -> int: ...
    async def suspend(self) -> None: ...
    async def resume(self) -> None: ...
    def subscribe(self, hook: Callable[[Tick], None]) -> Callable[[], None]: ...
    def on_clip(self, hook: Callable[[str], None]) -> Callable[[], None]: ...
    async def focus_window(self, handle: int) -> bool: ...
    async def foreground_window(self) -> int | None: ...
    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool: ...
    async def move_cursor(self, x: int, y: int) -> bool: ...
    async def scroll(self, region: ScreenRegion, detents: int) -> bool: ...
    async def scroll_key(self, key: str, taps: int = 1) -> bool: ...
    async def send_paste(self) -> bool: ...
    async def send_enter(self) -> bool: ...
    async def read_clipboard(self) -> str | None: ...
    async def write_clipboard(self, text: str) -> None: ...
    def watch_clipboard(self, on: bool) -> bool: ...
    @property
    def clipboard_kind(self) -> str | None: ...

    # -- the pixel verdicts (ui-monitor.md §2.3) ---------------------------
    # A kind and nothing else: not a template, not a rectangle, not a tolerance.
    # None of those could cross the wire, and a caller that supplied them would
    # be a caller keeping its own copy of the calibration.
    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]: ...
    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located: ...
    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick: ...
    async def hover_scan(self, kind: TemplateKind) -> ScreenRegion | None: ...
    async def snap_to_bottom(self, action: str) -> None: ...

    # -- the local-only tier -----------------------------------------------
    @property
    def self_writes(self) -> SelfWriteSet: ...
    def on_frame(self, hook: FrameHook) -> Callable[[], None]: ...
    def reset_trackers(self) -> None: ...
    @property
    def busy_tracker(self) -> PresenceTracker | None: ...
    @property
    def idle_tracker(self) -> PresenceTracker | None: ...
    @property
    def stale_tracker(self) -> StaleTracker | None: ...


def _accept_all(_text: str) -> bool:
    """Watcher filter for a controller nobody handed one to."""
    return True


def _drop_capture(_text: str) -> None:
    """Capture sink for a controller nobody handed one to."""


def _uncut(
    _scene: RegionImage, sightings: Mapping[TemplateKind, Sighting | None]
) -> Mapping[TemplateKind, object]:
    """Crop function for a controller nobody handed one to: the sightings
    themselves. ``paint_elements`` routes an opaque mapping, so a view with no
    renderer behind it (a test's, the headless case) still sees which
    appearances the tick recognised - it simply gets no pixels."""
    return dict(sightings)


def _nothing_captured(_kind: TemplateKind) -> bool:
    """Appearance lookup for a controller nobody handed one to: a service with
    no captures at all, which is the honest reading of "nothing was wired in" -
    no send gate, and a finish that lands on MANUAL_COPY."""
    return False


def _no_fire() -> None:
    """Fire callback for a controller nobody handed one to. The decision is
    still reached and still narrated; nothing is launched."""


class AutomationController:
    """The automation's state, driving one :class:`AutomationView`."""

    def __init__(
        self,
        view: AutomationView,
        *,
        monitor: MonitorLike,
        host: AutomationHost | None = None,
        services: Mapping[str, str] | None = None,
        accepts: Callable[[str], bool] | None = None,
        on_clipboard_captured: Callable[[str], None] | None = None,
        crop_elements: CropFn | None = None,
        has_appearance: Callable[[TemplateKind], bool] | None = None,
        on_fire: Callable[[], None] | None = None,
        alarm: AttentionAlarm | None = None,
    ) -> None:
        self._view = view
        # The other half of the seam: what the OS-acting sequences still have to
        # ASK the shell (agentclip.driver.automation.host), and the machine they act on
        # (agentclip.driver.monitor). Neither is on the paint port, deliberately - the
        # second one is a whole process boundary in waiting (ui-monitor.md §3), and in
        # local mode it is the object that owns the poll thread, the trackers, the
        # mouse, the keyboard and the clipboard.
        self._host: AutomationHost = host if host is not None else NullHost()
        self._monitor = monitor
        # True is every version of this app before the switch existed, and it
        # stays the default: the tool is useful precisely because it acts.
        self._os_armed = True
        # Every user-drawn calibration, one set per agent slot - which since the
        # appearance model is exactly one thing: the chat window. That single
        # box is where every appearance is searched for, the click target of
        # last resort, and the whole calibration of the staleness detector. It
        # describes where a service's window IS, not what one conversation said,
        # so it survives /new; only the pointers below reset.
        self._slots = new_slots()
        self._calibrating: AgentSlot = AgentSlot.MASTER
        self._live: AgentSlot = AgentSlot.MASTER
        # The service each browser WINDOW is pointed at, keyed by whatever the
        # shell calls its windows - opaque strings here, deliberately: window
        # ids are the shell's tab vocabulary and this object never interprets
        # them. Two windows, two services: a big-context chat for the
        # conversation the user steers, something cheap and fast for delegated
        # sub-tasks.
        self._services: dict[str, str] = dict(services or {})
        # -- the clipboard, at arm's length ------------------------------------
        # The backend, the poll interval, the watcher thread and the self-write
        # register are all the monitor's (ui-monitor.md §2.11). What is left here
        # is whether one is wanted, and the two callbacks either side of it.
        #
        # The protocol pre-filter, passed in for the same reason ``watch`` takes
        # it: ``agentclip.protocol`` is above this layer (tests/test_layering.py),
        # and a hook that accepted everything would drive a turn off any copy.
        # The monitor applies its own copy of it to what it captures; this one is
        # what keeps the ingest honest whoever fed the hook.
        self._accepts: Callable[[str], bool] = accepts if accepts is not None else _accept_all
        self._on_capture: Callable[[str], None] = (
            on_clipboard_captured if on_clipboard_captured is not None else _drop_capture
        )
        # Whether a watcher is polling right now, as the monitor last answered.
        # Moved only through ``_watch``, and only on the UI thread.
        self._watching = False
        # What the watcher was doing when a disarm took it away, so re-arming
        # restores THAT rather than a guess: a user who paused it themselves,
        # disarmed and re-armed does not get handed back a watcher they switched
        # off. Written on transitions only - see ``set_os_armed``.
        self._watch_before_disarm = False
        # -- the tick consumer --------------------------------------------------
        # The one thing a frame still asks the shell for on its way to the
        # consumer: cut this tick's matches down to panel-sized pictures. Handed
        # in at construction like the clipboard sink above and for the same
        # reason - it is the shell's, and it does not change for this lifetime.
        self._crop_elements: CropFn = crop_elements if crop_elements is not None else _uncut
        # What keeps the monitor's thread's consumption from interleaving with
        # the UI thread's writes to the very same bookkeeping. See the module
        # docstring for the grain: one probe, never a whole tick - and for why
        # this one ``threading`` import outlives the rest of them until phase 2
        # turns the push into a pull.
        self._tick_lock = threading.RLock()
        # -- the probe consumer ------------------------------------------------
        # What the shell still answers for the decisions below. Two callbacks and
        # nothing else, because those are the only two questions the fold cannot
        # answer for itself: what the LIVE window's service has a capture of
        # (which lives in the shell's profile cache, keyed off its Config) and
        # what to actually launch when the verdict says "finished". Since 5b
        # either can be called on the POLLER thread, under ``_tick_lock``
        # (``has_appearance`` from the UI thread too, when a paste opens the send
        # gate), so both carry the port's contract: cheap, thread-safe, no
        # widgets - and ``on_fire`` schedules rather than does.
        self._has_appearance: Callable[[TemplateKind], bool] = (
            has_appearance if has_appearance is not None else _nothing_captured
        )
        self._on_fire: Callable[[], None] = on_fire if on_fire is not None else _no_fire
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
        # ...and, next to the two de-bounced icon verdicts, the raw per-frame
        # fact each of their last probes carried (``BusyProbe.generating_now``):
        # did THAT frame's own template search say the model is generating? A
        # ``False`` verdict does not, on its own - a freshly reset tracker
        # reports "generating" for its whole grace period on no evidence at all,
        # and the reset happens at the paste. This is what ``icon_evidence``
        # reads; the verdicts above stay the finish decision's business.
        self._busy_generating_now = False
        self._idle_generating_now = False
        # The latest stale probe's frame-to-frame differing fraction. A READOUT
        # and nothing else since phase 2 - the sidebar's number - because the run
        # it used to feed (``send_arm_min_diff`` consecutive big deltas) is the
        # monitor's arithmetic now and rides in on the tick.
        self._stale_diff: float | None = None
        # The tick the bookkeeping below is currently about, kept because the two
        # STREAKS are its fields rather than this object's (ui-monitor.md §2.2):
        # a count of consecutive ticks is a statement about a screen, and a brain
        # that kept its own running totals would lose them on every reconnect and
        # would have to be told which ticks it had already counted (§2.9). Written
        # by ``_on_tick`` before it unpacks anything, so every ``consume_*`` under
        # it - and ``evaluate_finish`` at the end - reads the counts that belong
        # to the very probe being folded. ``None`` until the first tick lands, and
        # for a shell that calls a ``consume_*`` method with a probe of its own:
        # both mean "no streak has been counted", which is what zero says.
        self._last_tick: Tick | None = None
        # The three finish trackers of the detector the shell built for this
        # run, kept under their own names because the paste, the send and the
        # auto-copy flow all reset the DEBOUNCE without touching what the
        # detector has SEEN, and that is a per-tracker act.
        # ``_active_detectors`` is which of them the current run reports, in the
        # fixed busy -> idle -> stale order - the seam that says which probe
        # closes a tick (``finish_tick_closed_by``). The trackers themselves are
        # the monitor's now; the properties below are a window onto them, for the
        # shells that still install one and the resets that still swap them.
        self._active_detectors: tuple[str, ...] = ()
        # True from the moment ``evaluate_finish`` fires the auto-copy flow until
        # the flow's finally: evaluation is suspended meanwhile, because the
        # flow's own scrolling/hover-scanning reads as "generating" to the
        # stale detector and would re-arm and re-fire the trigger forever.
        self._flow_running = False
        # The one-shot window in which a harvest may be ingested as PROSE. Armed
        # immediately before the flow's ``verified_copy_click`` and disarmed the
        # moment ``ingest_harvest`` returns (and defensively in ``end_flow``), so
        # the loosening of protocol.md 1.4 tolerance #11 is scoped to exactly one
        # click: the one whose clipboard change we just watched happen. Plain
        # attribute writes on the event-loop thread - never read from the poller
        # thread, so it is deliberately outside ``_tick_lock``.
        self._prose_window = False
        # Has the trigger seen the model generating since the last harvest? The
        # brain's half of the fire rule (§2.3) - what a run of agreeing ticks is
        # WORTH is policy, and the run itself is the monitor's count. Trigger
        # state, not calibration, so it is reset whenever the live slot moves.
        self._copy_armed = False
        # The SESSION gate under all of that: True only between an outbound
        # copy (the payload is in the chat and a reply is what we are waiting
        # for) and the reply being harvested. Nothing may arm or fire while it
        # is shut, so a calibrated but idle tab cannot drive the mouse.
        self._awaiting_pasted_reply = False
        # The READY-TO-SEND gate that rides on top of it, and its tick budget.
        # None means "not gating" - the live service captured no SEND_READY
        # appearance, or the gate has already let go - and that is exactly the
        # behaviour that shipped before the gate existed. HOLD/SEEN veto every
        # arm and fire in ``evaluate_finish``: the payload is in the chat box
        # but the user has not pressed Enter yet, so nothing on that screen is
        # a reply. Opened with the reply gate and dies with it.
        self._send_gate: SendGate | None = None
        self._send_gate_ticks = 0
        # Where the browser-automation loop is, as one value
        # (automation.loop_state). The booleans above are the evidence; this is
        # the story the shell's STATE rail draws, and ``set_loop_state`` is its
        # only writer.
        self._loop_state = LoopState.IDLE
        # ...and WHY it went there, kept for the user to read back (`/log`).
        # Bounded, never written to disk, and deliberately NOT cleared by /new:
        # a wedged user resets the session first and goes looking for the
        # evidence second (automation.harness_log).
        self._harness_log: deque[HarnessEntry] = deque(maxlen=HARNESS_LOG_MAX)
        # -- the OS-acting sequences ------------------------------------------
        # The shell's OWN window, recorded by whoever can see that the user is
        # provably interacting with the tool (a launch, a composer send, a
        # sidebar press). OS state, so it lives here rather than in one shell:
        # the auto-copy flow and the new-chat click both hand focus back to it,
        # and with two shells a handle held upstairs is a handle the other one
        # cannot snap back to. None means nothing has been recorded yet, which
        # is a reason not to snap rather than an error.
        self._own_window: int | None = None
        # Said once, never per copy: a focus click the OS refuses is a standing
        # fact about the machine (it is Windows-only), and a toast per outbound
        # would be noise about something the user already knows.
        self._region_click_warned = False
        # The last payload a delivery was attempted with, kept so the sidebar's
        # "Retry insert" button (and `c`'s second tap) can re-run the
        # click-and-paste against exactly that text (``retry_insert``). The
        # clipboard is where the payload already is, but it is the USER's
        # clipboard: between a failed insert and the press that retries it they
        # may well have copied something else, and a retry that pasted whatever
        # happens to be on the clipboard now would drop a stray copy into the
        # chat. Session-scoped - ``forget_pending_insert`` is /new's teardown,
        # which is where "the last outbound" stops meaning anything. Written and
        # read only from the event loop (every delivery is a coroutine a shell
        # scheduled), so unlike the tick bookkeeping it needs no lock.
        self._pending_insert: str | None = None
        # The audible half of "your move" (``ServicePreset.alert_sound``). Owned
        # here because ``set_loop_state`` is the one door every attention state
        # comes through, and passable in so a suite can hear the alarm without
        # the machine making a sound.
        self._alarm = alarm if alarm is not None else AttentionAlarm()
        # -- and the three hooks that make this object the monitor's consumer ---
        # Registered last, so nothing can arrive before the state above exists.
        # One subscription for the whole tick (``_on_tick`` unpacks it into the
        # five ``consume_*`` calls in the fixed order), one for the frame behind
        # it (pixels, local-only tier), one for the clipboard. Never unhooked:
        # this object and its monitor share a lifetime, and a controller that
        # stopped listening would be a screen nobody reads.
        monitor.subscribe(self._on_tick)
        monitor.on_frame(self._on_frame)
        monitor.on_clip(self._on_clip)

    # == what the monitor was configured with =================================
    # The send gate's four budgets. They used to be constructor arguments,
    # because the Pilot suites patch the shell module's copies of them (a test
    # that had to sit out a two-minute gate budget would not be a test). They are
    # ``MonitorSpec`` fields now (ui-monitor.md §2.10): the budgets are counted in
    # TICKS, the monitor is what a tick is, so the monitor is what carries them.
    # Until a spec is set there is nothing to read, and the module's own defaults
    # are the honest answer - a controller nobody configured is a controller with
    # no window to watch, and the gate it never opens is measured in the same
    # numbers it always was.

    @property
    def _send_arm_ticks(self) -> int:
        spec = self._monitor.spec
        return SEND_ARM_TICKS if spec is None else spec.send_arm_ticks

    @property
    def _send_arm_min_diff(self) -> float:
        spec = self._monitor.spec
        return SEND_ARM_MIN_DIFF if spec is None else spec.send_arm_min_diff

    @property
    def _send_gate_timeout_ticks(self) -> int:
        spec = self._monitor.spec
        return SEND_GATE_TIMEOUT_TICKS if spec is None else spec.send_gate_timeout_ticks

    @property
    def _send_gate_seen_timeout_ticks(self) -> int:
        spec = self._monitor.spec
        return SEND_GATE_SEEN_TIMEOUT_TICKS if spec is None else spec.send_gate_seen_timeout_ticks

    def _schedule(self, work: Coroutine[Any, Any, Any]) -> None:
        """Put one monitor coroutine on the running loop and do not wait for it.

        The bridge between a shell's synchronous chrome (a modal opening, a
        window closing) and a contract that is coroutines all the way down. With
        no loop running there is nothing to put it on and nothing to await it
        either, so the coroutine is closed rather than left to warn about never
        having been awaited - which is exactly the state a synchronous test is
        in, and it is asking for a suspend that has nothing to suspend.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            work.close()
            return
        loop.create_task(work)

    # == the ARMED switch =====================================================

    @property
    def os_armed(self) -> bool:
        """May the tool touch the machine right now?

        The awkward name is deliberate: "armed" already means three unrelated
        things in the TUI (``_copy_armed``, the ``st-armed`` status style,
        ``SEND_READY_ARMED``), and this is the only one about the OS.
        """
        return self._os_armed

    def set_os_armed(self, target: bool | None) -> bool:
        """Arm or disarm, move the watcher, and repaint. ``None`` toggles.

        Returns the state that is now in force, so a caller can drive the
        consequences it still owns off one call rather than re-reading and
        hoping - the status segment, the footer's bindings and the toast are all
        the shell's, and all of them run *after* this returns.

        Order inside: the flag, then the machine, then the paint. The watcher is
        settled before ``paint_armed`` so that anything a shell draws from this
        object is drawn from a finished transition; the shell's own status bar
        (which reports the watcher too) repaints after the call for the same
        reason.

        Painting is **unconditional**, matching the behaviour the switch has
        always had: an explicit `/armed off` typed twice repaints rather than
        looking ignored, and the shell re-toasts on top of that for the same
        reason. The watcher half, in contrast, moves only on a real TRANSITION:
        a second `/armed off` that re-read the (already stopped) watcher would
        remember "it was off" and quietly lose the watcher the first one took
        away.

        The rule the transition implements, of the two on offer: disarming
        forces the watcher off and remembers what it was; re-arming puts *that*
        back. Re-arming undoes the disarm, nothing more.

        Under ``_tick_lock``, because ``evaluate_finish`` reads the flag on the
        poller thread to decide whether a finish may launch anything: a
        transition landing in the middle of a fold would let the same tick be
        judged armed and then act disarmed.
        """
        with self._tick_lock:
            was_armed = self._os_armed
            self._os_armed = (not was_armed) if target is None else target
            armed = self._os_armed
            if armed and not was_armed:
                if self._watch_before_disarm:
                    self._watch(True)  # no-ops in manual mode, and if already up
                self._watch_before_disarm = False
            elif was_armed and not armed:
                self._watch_before_disarm = self.watching
                self._watch(False)
            self._view.paint_armed(armed)
            return armed

    # == the clipboard watcher =================================================
    # The thread is the monitor's (ui-monitor.md §2.11). What is left here is the
    # three questions only this object can answer: does a session want one, may
    # it have one, and what did it catch.

    @property
    def watching(self) -> bool:
        """Is the monitor polling the clipboard right now?"""
        return self._watching

    def _watch(self, on: bool) -> None:
        """Ask for a watcher, or ask for it to stop, and record what came back.

        The single door, because "asked for" and "running" are not the same
        thing: a machine with no clipboard backend, or with the write-only
        manual one, honours neither request and says so - and everything that
        reads ``watching`` (the status bar, the re-arm's memory) has to see that
        rather than our intention.
        """
        self._watching = self._monitor.watch_clipboard(on)

    def start_input(self) -> None:
        """A session wants the clipboard watched (``ChatView.start_input``).

        Three answers, and only one of them starts anything. Disarmed: the
        session still WANTS a watcher, so the request is *remembered* and the
        next re-arm honours it - this is the one place the remembered state is
        set from an intention rather than from an observation, and without it a
        user who started a session while disarmed would have to press F5 and
        then `w` to get back to a normal app. No real backend: say so, once,
        because from here on the user is copying and pasting by hand.
        """
        if not self._os_armed:
            self._watch_before_disarm = True
            return
        if self._monitor.clipboard_kind == "manual":
            self._view.notify(
                "manual clipboard mode: press i and paste the model's reply into the box; "
                "outbound payloads go out via the terminal's OSC-52 copy",
                severity="warning",
                timeout=10,
            )
            return
        self._watch(True)

    def stop_input(self) -> None:
        """Stop watching (``ChatView.stop_input``), without waiting for it.

        Deliberately no join anywhere under this: the caller is the UI thread and
        the loop only notices between ticks, so joining would freeze the
        interface for up to a poll interval. "Stopped" is true immediately for
        everything that asks, and a capture that lands in the window the last
        tick leaves open is a real capture the user really made.
        """
        self._watch(False)

    def _on_clip(self, text: str) -> None:
        """One clipboard change the watcher accepted, on its way to the shell.

        Called from the monitor's own thread, so everything it touches carries
        the ``AutomationView`` port's contract: non-blocking and thread-safe. The
        filter is applied again here for the reason it is passed in at all - the
        protocol lives above this layer, and this object is where "is that a
        reply?" is answered on behalf of both shells.
        """
        if self._accepts(text):
            self._on_capture(text)

    # == the monitor's run =====================================================
    # What used to be the poller section. The loop, the thread, the stop flag and
    # the generation counter all moved down (ui-monitor.md §6.1); the three calls
    # left here are the ones the shells still make, and the shell-rewiring phase
    # replaces each with the monitor call beside it.

    @property
    def detector_generation(self) -> int:
        """Which run of the monitor the live window is being watched in.

        Stamped into every tick and compared by everything that consumes one.
        The counter is the monitor's - only ``configure`` moves it - and this is
        the read the bookkeeping below phrases itself in.
        """
        return self._monitor.generation

    def retarget_detectors(self) -> int:
        """The automation moved; hand back the run it moved into.

        A shim, and deliberately a thin one. Retargeting IS ``await
        monitor.configure(spec)`` now: it is the call that rebuilds the trackers
        fresh, bumps the generation and makes every tick in flight a ghost. What
        is left here is the answer the shells read back, so the phase that
        rewires them is a one-line change at each call site rather than a
        rewrite of both.
        """
        with self._tick_lock:
            return self._monitor.generation

    def stop_detectors(self) -> None:
        """Suspend the monitor's polling, without waiting for it.

        The SUSPEND a shell reaches for when a modal takes the screen (the
        service editor's capture overlay is a sustained large delta over the very
        window the detectors watch, which is what arms the auto-copy on staleness
        alone). Deliberately no generation bump: nothing has moved, and the
        rebuild that resumes it opens the new run.
        """
        self._schedule(self._monitor.suspend())

    # == the loop's narration ==================================================

    @property
    def loop_state(self) -> LoopState:
        """Where the browser-automation loop is right now."""
        return self._loop_state

    @property
    def harness_log(self) -> deque[HarnessEntry]:
        """The decision log itself, oldest first.

        The live deque, not a copy: the shell's pane is a VIEW of it - handed
        the container once and mirrored one entry at a time afterwards
        (``paint_harness_entry``), so a pane opened mid-session shows everything
        and an open one shows a decision as it is taken.
        """
        return self._harness_log

    def set_loop_state(self, state: LoopState, reason: str) -> None:
        """Move the browser-automation loop to ``state`` and repaint the rail.

        Called from the loop's own events as they land - the paste attempt, the
        send gate, the finish detectors, the auto-copy flow, the clipboard
        capture - which makes the rail the LIVE window's loop, the same scope
        as the DETECTION block. Display only: nothing reads ``loop_state``
        back to make a decision, so a state the evidence skips over (a manual
        paste-and-send the gate never saw) is simply never shown, not an error.

        ``reason`` is why, in the user's language, and it is REQUIRED because
        this is the one door: the rail draws one box for four different roads
        into ``MANUAL_COPY``, and a caller that could move the loop without
        saying which road it took is a caller whose decision is unreadable
        afterwards. It is logged (``automation.harness_log``) only when the state
        actually changes, so the same evidence arriving twice does not fill the
        log with a repeated non-event.

        Under ``_tick_lock`` because both threads move the loop - a tick's
        verdict and the user's paste alike - and the transition, its log entry
        and its repaint are one act: two states landing at once must not
        interleave into a rail that says one thing and a log that says the other.
        """
        with self._tick_lock:
            if state is self._loop_state:
                return
            before = self._loop_state
            self._loop_state = state
            self.log_harness(KIND_STATE, state_text(before.name, state.name, reason))
            self._view.paint_loop_state(state)
        # Outside the lock on purpose. The alarm reads the live preset (the
        # host's, which is a shell's) and starts a thread, and neither belongs
        # inside the lock a detector tick is waiting on - the transition itself
        # is already committed and the sound is a consequence of it, not part of
        # it.
        self._sound_attention(state)

    def _sound_attention(self, state: LoopState) -> None:
        """Start or stop the "your move" alarm for the state just entered.

        The single hook, and deliberately at the ONE door every state change
        comes through: there are nine sites that hand the loop back to the user
        and a beep copy-pasted into each of them is nine places to forget to
        stop it. Arm on the states that need a human (``ATTENTION_STATES``),
        disarm on everything else - including an attention state on a service
        whose alert is off, which is what makes turning the setting off mid-nag
        stop the noise at the next transition rather than never.
        """
        preset = self._live_preset()
        if state in ATTENTION_STATES and preset.alert_sound:
            self._alarm.arm(repeat_seconds=preset.alert_repeat_seconds)
        else:
            self._alarm.disarm()

    def sound_attention_once(self) -> None:
        """One uh-oh for a re-sync the LOOP never hears about.

        A protocol error is the case: the reply arrived, the loop moved on, and
        yet the user has to go back to the browser and re-copy. There is no
        attention state to arm against and nothing to disarm it afterwards, so
        it is a single chime - still gated on the live service's ``alert_sound``,
        because the setting means "tell me out loud when I am needed" and this
        is one of those moments.
        """
        if self._live_preset().alert_sound:
            self._alarm.chime()

    def stop_alert(self) -> None:
        """Silence the alarm for good (shutdown). Safe when nothing is sounding."""
        self._alarm.disarm()

    def log_harness(self, kind: str, text: str) -> None:
        """Append one decision to the harness log (`/log`).

        The single append site, so the bound and the timestamp are decided once.
        The deque is the log; the shell's pane is a view of it, mirrored one
        entry at a time so an open pane shows a decision as it is taken (§3.3b).

        Under ``_tick_lock`` so the append and the mirror stay in step: the deque
        is the log and the pane is fed one entry at a time, so two threads
        appending unsynchronised could hand the pane a different order than the
        deque holds - and the pane refills itself FROM the deque when it is next
        revealed, which would make the same log read two ways.
        """
        with self._tick_lock:
            entry = HarnessEntry(kind, text)
            self._harness_log.append(entry)
            self._view.paint_harness_entry(entry)

    # == the probe consumer ====================================================
    # What a tick MEANS. The five ``consume_*`` calls below are the other end of
    # the five sinks the poll loop pushes through, and between them they hold
    # every rule the automation has about when the model stopped talking.

    @property
    def active_detectors(self) -> tuple[str, ...]:
        """Which FINISH detectors the current run reports, in the fixed
        busy -> idle -> stale build order. The tick-closing rule reads its last
        entry; the ghost filter reads its membership."""
        return self._active_detectors

    @active_detectors.setter
    def active_detectors(self, names: tuple[str, ...]) -> None:
        self._active_detectors = tuple(names)

    @property
    def busy_tracker(self) -> PresenceTracker | None:
        """The current run's busy-appearance tracker, or None.

        Read-only, and the monitor's object: ``configure`` is what builds a set
        of them, so there is nothing up here to install one with any more. The
        window survives for the shells that MIRROR a tracker in their own chrome,
        and it closes with them.
        """
        return self._monitor.busy_tracker

    @property
    def idle_tracker(self) -> PresenceTracker | None:
        """The current run's idle-appearance tracker, or None."""
        return self._monitor.idle_tracker

    @property
    def stale_tracker(self) -> StaleTracker | None:
        """The current run's staleness tracker, or None."""
        return self._monitor.stale_tracker

    def reset_trackers(self) -> None:
        """Make every live tracker forget the frames it has seen.

        The debounce only, never the verdicts: what the trackers hold is a
        streak and a previous frame, and every caller here (the paste, the send,
        the auto-copy flow) has just PRODUCED the frames behind that streak
        itself.

        **Swap, not clear**, and that is the monitor's job (ui-monitor.md §4.3):
        the tracker being cleared is being polled on the monitor's own thread,
        which reads the streak, spends a template search or a frame diff, and
        writes the streak back - so an in-place ``reset()`` landing inside that
        search is undone by the write that follows it, and the frames the paste
        or the flow produced stay in history. What this object contributes is
        WHEN, which is the only half of it that was ever a decision.

        The two STREAKS go with the debounce, on the far side of the seam and up
        here both. The monitor zeroes its own counts (ui-monitor.md 4.3, and by
        the same argument: they are counts of what those trackers said about
        frames this caller has just produced ITSELF); ``_last_tick`` is dropped
        so nothing reports the pre-reset run in the window before the next tick
        lands. Both readings then say zero, which is what has just become true.
        """
        self._monitor.reset_trackers()
        self._last_tick = None

    def forget_verdicts(self) -> None:
        """Drop everything a rebuilt detector set makes obsolete.

        Every tracker is rebuilt around the window's calibration as it stands
        NOW, so the verdicts they produced belong to detectors that no longer
        exist. The trigger's ARM survives deliberately: it records that the
        model was generating, which recapturing a button does not un-observe -
        which is why this is not ``reset_finish_trigger``.
        """
        with self._tick_lock:
            self._busy_seen = self._idle_seen = self._stale_seen = False
            self._busy_finished = self._idle_finished = self._stale_finished = None
            self._stale_diff = None
            self._active_detectors = ()
            # The trackers those verdicts came out of, and the detector holding
            # them, are not dropped here any more: they are the monitor's, and
            # the ``configure`` that follows this builds a fresh set and swaps
            # them in (§4.3). What is left here is the verdicts themselves,
            # which is all this call was ever about.

    def reset_finish_trigger(self) -> None:
        """Forget every detector verdict and the auto-copy arm.

        Called whenever the live slot moves (a delegation starting or ending)
        and on session teardown: verdicts describe a window, so carrying them
        across a retarget could fire the auto-copy against the wrong chat.
        ``flow_running`` is deliberately NOT cleared here - only the flow's
        own finally lifts the suspension, so a slot move while the flow still
        runs cannot let the trigger fire against its in-flight mouse work."""
        with self._tick_lock:
            self._busy_seen = False
            self._idle_seen = False
            self._stale_seen = False
            self._busy_finished = None
            self._idle_finished = None
            self._stale_finished = None
            self._busy_generating_now = False
            self._idle_generating_now = False
            self._stale_diff = None
            self._copy_armed = False

    # -- the trigger's own state, for shells and tests that mirror it ----------

    @property
    def copy_armed(self) -> bool:
        """Has the trigger seen the model generating since the last harvest?"""
        return self._copy_armed

    @copy_armed.setter
    def copy_armed(self, value: bool) -> None:
        self._copy_armed = value

    @property
    def copy_changed_streak(self) -> int:
        """How many consecutive ticks every live detector has said "finished".

        Read off the latest tick, not counted here: it is the monitor that
        counts consecutive ticks now (ui-monitor.md §2.2), and this object only
        decides what a run of two is worth. Zero before the first tick lands.
        """
        tick = self._last_tick
        return 0 if tick is None else tick.changed_streak

    @property
    def stale_arm_streak(self) -> int:
        """The run of consecutive large-delta stale probes (``send_arm_ticks``
        of them is what arms the trigger on staleness alone).

        The monitor's count as well, and off the same tick.
        """
        tick = self._last_tick
        return 0 if tick is None else tick.stale_arm_streak

    @property
    def stale_diff(self) -> float | None:
        """The last stale probe's frame-to-frame differing fraction."""
        return self._stale_diff

    @property
    def awaiting_pasted_reply(self) -> bool:
        """Is an outbound sitting in the chat with its reply still to come?"""
        return self._awaiting_pasted_reply

    @property
    def prose_window(self) -> bool:
        """May the harvest about to arrive be ingested even with no CLIP blocks?

        True for exactly one act: from just before the flow's verified copy
        click until its ``ingest_harvest`` returns. A shell's harvest reads it
        to know that THIS clipboard text is the model's reply - the flow watched
        the copy button write it - rather than some copy the user made.
        """
        return self._prose_window

    @property
    def flow_running(self) -> bool:
        """Is the auto-copy flow driving the mouse right now?"""
        return self._flow_running

    @flow_running.setter
    def flow_running(self, value: bool) -> None:
        self._flow_running = value

    @property
    def send_gate(self) -> SendGate | None:
        """Where the ready-to-send gate is, or None for "not gating"."""
        return self._send_gate

    @property
    def send_gate_ticks(self) -> int:
        """How many ticks the current gate phase has waited."""
        return self._send_gate_ticks

    # The per-detector readings themselves. Writable because a shell (and a
    # test) has to be able to say "this detector has reported nothing" without
    # going through a probe - which is what a rebuild, a slot move and a
    # forgotten appearance all amount to.
    @property
    def busy_seen(self) -> bool:
        """Has the busy detector reported at all since the last reset?"""
        return self._busy_seen

    @busy_seen.setter
    def busy_seen(self, value: bool) -> None:
        self._busy_seen = value

    @property
    def idle_seen(self) -> bool:
        """Has the idle detector reported at all since the last reset?"""
        return self._idle_seen

    @idle_seen.setter
    def idle_seen(self, value: bool) -> None:
        self._idle_seen = value

    @property
    def stale_seen(self) -> bool:
        """Has the stale detector reported at all since the last reset?"""
        return self._stale_seen

    @stale_seen.setter
    def stale_seen(self, value: bool) -> None:
        self._stale_seen = value

    @property
    def busy_finished(self) -> bool | None:
        """The busy detector's latest verdict (None = no verdict)."""
        return self._busy_finished

    @busy_finished.setter
    def busy_finished(self, value: bool | None) -> None:
        self._busy_finished = value

    @property
    def idle_finished(self) -> bool | None:
        """The idle detector's latest verdict (None = no verdict)."""
        return self._idle_finished

    @idle_finished.setter
    def idle_finished(self, value: bool | None) -> None:
        self._idle_finished = value

    @property
    def stale_finished(self) -> bool | None:
        """The stale detector's latest verdict (None = no verdict)."""
        return self._stale_finished

    @stale_finished.setter
    def stale_finished(self, value: bool | None) -> None:
        self._stale_finished = value

    def end_flow(self) -> None:
        """The auto-copy flow finished (or failed, or was cancelled): lift the
        suspension and throw away the frames the flow itself produced.

        The flow clicks, scrolls and hover-scans the very window all three
        detectors watch, so every streak it leaves behind describes the flow's
        own mouse work rather than the model's - including the send-arming run,
        whose large deltas were the flow's own scrolling. Both streaks go with
        the debounce because both are the monitor's now: ``reset_trackers``
        zeroes them on the far side of the seam (ui-monitor.md §4.3), which is
        the same act and one fewer thing up here to forget.

        The prose window is closed here too, defensively: the flow's own path
        closes it the moment the harvest returns, but a click that never
        verifies, a MANUAL_COPY exit or an exception must not leave it open for
        whatever the user copies next.
        """
        self._prose_window = False
        with self._tick_lock:
            self._flow_running = False
            self.reset_trackers()
            self._stale_diff = None

    # -- which probes count ----------------------------------------------------

    def finish_tick_closed_by(self, detector: str) -> bool:
        """Is ``detector``'s probe the tick's LAST, given what is running?

        The poller pushes busy -> idle -> stale each tick, skipping whichever
        detector it was not built with, and the combined verdict must fold
        exactly once per tick - on the closing probe - or a half-reported
        tick could arm or fire on one detector's word while another's is still
        in flight. ``active_detectors`` is that build order, so the closer is
        simply its last entry.
        """
        active = self._active_detectors
        return bool(active) and detector == active[-1]

    def is_ghost(self, detector: str, generation: int) -> bool:
        """Is this verdict left over from a poller run that is no longer live?

        Cancelling a poll loop only raises a flag: the loop it interrupts
        still finishes the tick it was in and pushes its verdicts, which land
        AFTER the shell rebuilt everything. Two ways that hurts, and the run's
        ``generation`` stamp is what catches both.

        The stamp is the load-bearing half. A probe is a reading of ONE browser
        window, and the poller is restarted precisely when the automation
        changes windows: /abort during a generating sub-run hands the master
        back the live slot, and the cancelled loop's in-flight "still
        generating" then arrives about the SUB-agent's window. Filtering by
        detector name alone let it through - both windows run a stale detector -
        so it armed the trigger and two quiet ticks later fired the copy flow at
        the master's chat. Same story for two runs of the same-named detector
        across a service switch.

        The name check is the older half: when the new detector set is SMALLER
        (a forgotten busy appearance, an unticked signal) the leftovers are
        verdicts about a detector that no longer exists, and a leaked
        "generating" one re-arms the trigger every time, wedging auto-copy shut.

        Dropping a verdict is always safe: a detector that is still running is
        simply refreshed by the next tick, a poll interval later.
        """
        if generation != self._monitor.generation:
            return True
        return detector not in self._active_detectors

    # -- one tick's readings ---------------------------------------------------
    # Called on the POLLER thread, straight out of the loop above (and, in the
    # suites, straight off ``feed_probe``). Each one takes ``_tick_lock`` for its
    # whole body, so the ghost check and the bookkeeping it guards are one
    # indivisible act against anything the UI thread does to the same state - a
    # paste opening the reply gate, a delegation retargeting the run, a modal
    # forgetting the verdicts. The grain is one probe, deliberately: that is
    # exactly what the message pump gave this code when each probe was a message,
    # and a retarget mid-tick could always drop the rest of that tick.

    def consume_busy_probe(self, probe: BusyProbe, generation: int) -> None:
        """One look for the busy appearance: show it, record it, maybe fold."""
        with self._tick_lock:
            if self.is_ghost("busy", generation):
                return
            self._view.paint_detection(TemplateKind.BUSY, format_busy_probe(probe))
            self._busy_seen = True
            self._busy_finished = busy_verdict(probe)
            self._busy_generating_now = probe.generating_now
            if self.finish_tick_closed_by("busy"):
                self.evaluate_finish()

    def consume_idle_probe(self, probe: BusyProbe, generation: int) -> None:
        """The same for the idle appearance, whose polarity is the inverse."""
        with self._tick_lock:
            if self.is_ghost("idle", generation):
                return
            self._view.paint_detection(TemplateKind.IDLE, format_idle_probe(probe))
            self._idle_seen = True
            self._idle_finished = idle_verdict(probe)
            self._idle_generating_now = probe.generating_now
            if self.finish_tick_closed_by("idle"):
                self.evaluate_finish()

    def consume_stale_probe(self, probe: StaleProbe, generation: int) -> None:
        """The same for the staleness of the drawn region, which needs no
        appearance at all - and whose ``diff`` is what the send-arming run is
        built out of."""
        with self._tick_lock:
            if self.is_ghost("stale", generation):
                return
            self._view.paint_stale(format_stale_probe(probe))
            self._stale_seen = True
            self._stale_finished = stale_verdict(probe)
            self._stale_diff = probe.diff
            if self.finish_tick_closed_by("stale"):
                self.evaluate_finish()

    def consume_elements(self, crops: Mapping[TemplateKind, object], generation: int) -> None:
        """Show one tick's recognised crops.

        Ghost-filtered on the generation stamp alone, like
        ``consume_send_ready`` and for the same reason: it is not in
        ``active_detectors``, so ``is_ghost`` would reject every one on the name
        check. Crops from a cancelled run are pictures from a window that may no
        longer be the live one, and the heading above them has already been
        repointed - showing them would caption the master's send button as the
        sub-agent's.
        """
        with self._tick_lock:
            if generation != self._monitor.generation:
                return
            self._view.paint_elements(crops)

    def consume_send_ready(self, found: bool | None, generation: int) -> None:
        """Fold one look for the ready-to-send button into the send gate.

        Not a finish detector: it closes no tick, folds into no verdict and is
        absent from ``active_detectors`` - so the ghost test here is the
        generation stamp alone (``is_ghost`` would reject every one of these on
        the name check). The stamp still matters for the same reason: a probe
        taken from the window the automation was driving before a delegation
        started must not release the gate the new window just opened.

        Three answers, three jobs. FOUND while holding is the sighting the gate
        is waiting for. NOT FOUND *after* a sighting is the send itself - the
        button only exists while there is something to send, so its
        disappearance is the user's Enter. Anything else - not found before any
        sighting, a failed capture, or a button that goes on being found long
        after the send - only runs the clock down.

        BOTH phases are on a clock, on two very different budgets: the sighting
        the HOLD phase waits for should arrive within a second or two
        (``send_gate_timeout_ticks``), while the SEEN phase is waiting for a
        human and may not expire on one who paused to read
        (``send_gate_seen_timeout_ticks``). The clock restarts at the
        transition, so a slow sighting does not eat the user's reading time.

        No debounce on the disappearance, deliberately: one dropped frame costs
        an early release, which is precisely the behaviour that shipped before
        this gate existed, while one dropped frame in the other direction would
        mean holding a session open on a button that is already gone.
        """
        with self._tick_lock:
            if generation != self._monitor.generation:
                return
            gate = self._send_gate
            if gate is None:
                return  # the gate let go (or was closed) while this probe flew
            if found and gate is SendGate.HOLD:
                # The one productive tick in the whole gate: the phase changes, so
                # the clock restarts rather than counting on into the next budget.
                self._send_gate = SendGate.SEEN
                self._send_gate_ticks = 0
                self.log_harness(
                    KIND_GATE,
                    "the ready-to-send button is on screen: there is unsent text in the "
                    "chat box, so the send has not happened yet",
                )
                # The button only renders over a non-empty composer, so a manual
                # insert is now proven to have landed.
                if self._loop_state is LoopState.MANUAL_INSERT:
                    self.set_loop_state(
                        LoopState.WAIT_SEND,
                        "the ready-to-send button appeared, which proves your paste landed",
                    )
                self.paint_send_gate()
                return
            self._send_gate_ticks += 1
            if found is False and gate is SendGate.SEEN:
                self.release_send_gate()
                return
            # Nothing was learned this tick: the button has not shown up yet, the
            # capture failed, or it is STILL on screen after a sighting. All three
            # count against the phase's budget, or a browser that cannot be captured
            # - or a capture that stopped matching the composer's post-send state -
            # would hold the session open for ever.
            budget = (
                self._send_gate_seen_timeout_ticks
                if gate is SendGate.SEEN
                else self._send_gate_timeout_ticks
            )
            if self._send_gate_ticks >= budget:
                self.time_out_send_gate(seen=gate is SendGate.SEEN)

    # -- one tick, unpacked ----------------------------------------------------

    def _on_tick(self, tick: Tick) -> None:
        """Consume one observation of the chat window (``UIMonitor.subscribe``).

        The whole of this object's share of a tick, in the fixed order the
        tick-closing rule reads: busy -> idle -> stale, then the send button.
        Each probe is skipped when the configuration has no such detector, which
        is what ``None`` means on the tick; the send button is asked about only
        when it was SEARCHED for, because "not on screen" and "never looked" are
        different answers and only the first one may run a gate's clock down.

        Ghosts never get here - the monitor drops a tick captured under an older
        generation before any subscriber sees it (ui-monitor.md §4.2) - but the
        stamp still rides along, because the ``consume_*`` methods below are a
        public seam and their ghost check is what makes them safe to call.

        Called from the monitor's thread, so everything under it obeys the same
        rules the poll loop's own call stack did: one probe at a time, each one
        whole, under ``_tick_lock``.

        The tick itself is recorded FIRST, before a single probe is unpacked,
        because the two streaks ``evaluate_finish`` reads are its fields and the
        fold happens deep inside the last ``consume_*`` below - so the counts
        have to be in place before the first one runs, not after the last.
        """
        with self._tick_lock:
            self._last_tick = tick
        generation = tick.generation
        if tick.busy is not None:
            self.consume_busy_probe(tick.busy, generation)
        if tick.idle is not None:
            self.consume_idle_probe(tick.idle, generation)
        if tick.stale is not None:
            self.consume_stale_probe(tick.stale, generation)
        # The send button, every tick it is captured - it closes no tick and
        # folds into no verdict, and the gate that consumes it ignores it
        # whenever it is not holding. Three-valued: on screen, not on screen, or
        # no answer because the capture failed - and that last one is why the
        # dropped frame is asked about too. A tick that captured nothing searched
        # nothing, so its map is empty and ``searched`` says no; the gate still
        # has to hear about it, or a browser that has stopped being capturable
        # would hold the session open for ever.
        if tick.searched(TemplateKind.SEND_READY) or not tick.captured:
            self.consume_send_ready(tick.present(TemplateKind.SEND_READY), generation)

    def _on_frame(
        self, scene: RegionImage, sightings: Mapping[TemplateKind, Sighting | None]
    ) -> None:
        """One tick's pictures (``UIMonitor.on_frame``, the local-only tier).

        Beside the tick rather than on it, because a crop is pixels and a tick
        carries none (ui-monitor.md §2.2). After it, because the pictures
        illustrate verdicts that have already been folded. A frame that
        recognised nothing says nothing: an empty map would blank rows this tick
        is no evidence about, so a failed capture simply never arrives here.

        The stamp is read rather than carried: the frame arrives on the monitor's
        own thread the instant after the tick it belongs to, so the run it
        describes is the run the monitor is in.
        """
        if not sightings:
            return
        self.consume_elements(
            self._crop_elements(scene, sightings), self._monitor.generation
        )

    # -- the reply gate --------------------------------------------------------

    def open_reply_gate(self) -> None:
        """An outbound just went into the chat: a reply is now due, so the
        detectors may arm and fire until it has been harvested.

        Everything they observed BEFORE this instant is thrown away with the
        gate opening. The focus click, the synthetic Ctrl+V and (with the
        chat-region picker suspension, §3.4e) an overlay closing are all large
        frame deltas about AgentClip's own doing, and a sustained run of them
        left standing would arm the trigger on a chat nobody has answered yet.
        The trackers' own debounce state goes with it for the same reason the
        auto-copy flow resets them: the frames behind those streaks describe a
        screen we produced.
        """
        with self._tick_lock:
            self.reset_finish_trigger()
            self.reset_trackers()
            self._awaiting_pasted_reply = True
            self.open_send_gate()

    def close_reply_gate(self) -> None:
        """No reply is outstanding any more, so nothing may move the mouse.

        Four moments close it, and they are the four ways "the outbound this
        tab is waiting on" stops being true: the auto-copy flow firing (that
        IS the harvest), ``/new`` tearing the session down, and the live slot
        moving in either direction - a delegation's outbound is pasted into the
        window ``start_browser_chat`` just opened, and the master's next one is
        composed after ``end_browser_chat`` hands the automation back, so each
        window's gate is opened by its own outbound copy.

        Deliberately NOT part of ``reset_finish_trigger``: that forgets what
        the detectors *saw*, and suspending them for a modal (the service
        editor) does exactly that without the awaited reply going anywhere. A
        turn interrupted by an F2 visit must still be auto-copied when it
        finishes.
        """
        with self._tick_lock:
            self._awaiting_pasted_reply = False
            # The send gate is a phase OF this gate - "the outbound is out but
            # not sent yet" - so it can outlive neither the reply it is holding
            # for nor the window that reply belongs to. Every closer above
            # therefore drops it too, and that is also the reason it is not part
            # of ``reset_finish_trigger``: an F2 visit forgets what the detectors
            # saw without un-pasting anything, and a payload still sitting unsent
            # in the chat box must still be waited for afterwards.
            self.clear_send_gate()
            self.paint_send_gate()

    # -- the ready-to-send gate ------------------------------------------------

    def open_send_gate(self) -> None:
        """Hold finish detection back until the user is seen to press Enter.

        A capture is a capability, not an instruction: the gate exists for
        exactly those services whose profile has a ``SEND_READY`` appearance,
        and for every other one this leaves ``None`` behind and the whole
        feature is a no-op. There is no checkbox and no ``finish_signals``
        entry, because there is nothing to decide - a service either shows a
        send button while the composer holds text or it does not.

        What it buys: between AgentClip's paste and the user's Enter the chat
        is *still*, and a still screen is what the stale detector calls
        finished. The ``send_arm_*`` rules keep that from firing the auto-copy
        at a reply-less chat by demanding a sustained large delta first; this
        closes the same hole from the other end, with the one piece of evidence
        that is not a heuristic at all - the send button being on screen means
        there is unsent text in the box, and its disappearance means there is
        not.

        Three ways out, and every one of them is bounded, because the gate may
        delay a session and may never deadlock one: the button seen to GO
        (``release_send_gate`` - the user's Enter, and the ordinary case), a
        busy/idle detector reporting that the model is generating
        (``override_send_gate`` - better evidence than the button, and what
        rescues a session whose button never yields a clean not-found frame),
        or the clock (``time_out_send_gate``). The clock starts here
        (``send_gate_ticks``) and restarts at the sighting, because the two
        phases wait for very different things on very different budgets:
        ``send_gate_timeout_ticks`` for a button to appear at all,
        ``send_gate_seen_timeout_ticks`` for a human to read and press Enter.
        """
        with self._tick_lock:
            self._send_gate = (
                SendGate.HOLD if self._has_appearance(TemplateKind.SEND_READY) else None
            )
            self._send_gate_ticks = 0
            if self._send_gate is SendGate.HOLD:
                self.log_harness(
                    KIND_GATE,
                    "holding finish detection until the send is seen - between the paste "
                    "and your Enter the chat is still, and a still chat reads as finished",
                )
            self.paint_send_gate()

    def clear_send_gate(self) -> None:
        """Stop gating, without saying anything about why."""
        self._send_gate = None
        self._send_gate_ticks = 0

    def release_send_gate(self) -> None:
        """Seen, then gone: the user pressed Enter, so let the detectors go.

        Everything from here is the behaviour that shipped before the gate
        existed - and it starts from *here* rather than from the paste, which
        is why the trigger and every tracker's debounce are reset on the way
        out: the frames the gate held through show a chat box with an unsent
        message in it, and a streak built out of those describes the user
        typing, not the model answering. The ``>>> PRESS ENTER <<<`` banner
        comes down for the same reason it comes down on an icon arm - the send
        is proven, so the nag is over.
        """
        with self._tick_lock:
            self.clear_send_gate()
            self.reset_finish_trigger()
            self.reset_trackers()
            self.log_harness(
                KIND_GATE,
                "the ready-to-send button was seen and is now gone: finish detection is "
                "released, and it starts from the send rather than from the paste",
            )
            # The disappearance IS the user's Enter: the message is away and the
            # model's answer is what happens next.
            self.set_loop_state(
                LoopState.WAIT_GENERATE,
                "the ready-to-send button went away, which is your Enter",
            )
            self._view.hide_paste_flash()
            self.paint_send_gate(SEND_READY_RELEASED)

    def override_send_gate(self) -> None:
        """The model is generating, so the send already happened: let go.

        The gate's own release needs the button to be seen GOING, which is one
        non-debounced template match away from never happening - and a first
        message in a fresh chat, whose composer is centred and animating rather
        than docked where the capture was taken, is exactly where it does not
        happen. A reasoning icon on screen settles the same question the gate
        was asking, so this is a release on better evidence rather than a
        surrender to a clock.

        Deliberately NOT ``release_send_gate``: that resets the trigger and
        every tracker's debounce, on the grounds that the frames the gate held
        through show an unsent composer. Here the very frame doing the releasing
        is a genuine post-send reading of a generating chat, and throwing it
        away would cost the caller the arm it is about to take from it. So the
        gate simply goes, and ``evaluate_finish`` carries straight on into the
        icon-evidence branch with its verdicts intact.
        """
        self.clear_send_gate()
        self.log_harness(
            KIND_GATE,
            "gate overridden by better evidence: a busy/idle icon is on screen, and "
            "nothing generates a reply to a message that was never sent",
        )
        self.paint_send_gate(SEND_READY_OVERRIDDEN)

    def time_out_send_gate(self, *, seen: bool) -> None:
        """Give up waiting on a button, and say so.

        The gate may delay a session; it may never deadlock one - and BOTH of
        its phases can be waited on for ever, so both are on a clock (see
        ``send_gate_timeout_ticks`` / ``send_gate_seen_timeout_ticks``).

        Before a sighting: a capture that has stopped matching (a theme switch,
        a site redesign), a chat scrolled so the composer is off the drawn
        region, or a browser that cannot be captured at all. After one, the
        mirror image: the button matches happily but never stops matching, so
        the disappearance the release waits for never arrives - a capture taken
        against the docked composer, held up against a fresh chat's centred one,
        does this. Either way it hands finish detection straight back and
        behaves exactly as it did before the gate existed, banner included.

        Loudly, and with the two cases named apart, because the user's fix
        differs: one capture never matches and the other never stops.
        """
        self.clear_send_gate()
        self.paint_send_gate(SEND_READY_STUCK if seen else SEND_READY_TIMEOUT)
        what = (
            "the ready-to-send button never went away after the paste"
            if seen
            else "the ready-to-send button never appeared after the paste"
        )
        # The same sentence the toast makes, so the user who dismissed the toast
        # can still find out what happened.
        self.log_harness(
            KIND_GATE,
            f"gate timed out: {what} - finish detection is running as usual",
        )
        self._view.notify(
            f"{what} - finish detection is running as usual; recapture it in F2 if the "
            "chat has changed",
            severity="warning",
        )

    def send_gate_line(self) -> str:
        """The send line, re-derived from the gate rather than stored."""
        if self._send_gate is SendGate.HOLD:
            return SEND_READY_HOLDING
        if self._send_gate is SendGate.SEEN:
            return SEND_READY_SEEN
        if self._has_appearance(TemplateKind.SEND_READY):
            return SEND_READY_ARMED
        return SEND_READY_RESTING

    def paint_send_gate(self, text: str | None = None) -> None:
        """Repaint the send line - with an outcome, or from the gate's state."""
        self._view.paint_detection(
            TemplateKind.SEND_READY, text if text is not None else self.send_gate_line()
        )

    # -- the combined verdict --------------------------------------------------

    def icon_evidence(self) -> bool:
        """Did an ICON detector's LATEST FRAME see the model generating?

        The strongest evidence the poller produces, and the only kind two rules
        trust on a single frame: the reasoning appearance being on screen (or
        the idle one having been watched to go) is something no still,
        unanswered chat can fake. Staleness deliberately does not count - see
        ``SEND_ARM_MIN_DIFF`` for what a blinking caret does to that verdict.

        Which is why this reads ``generating_now`` and NOT ``_busy_finished is
        False``. The verdicts are de-bounced, and asymmetrically: a tracker
        reports "generating" for the whole settling window after a reset,
        whether or not it has seen anything (screen/presence.py). Every paste
        resets every tracker (``open_reply_gate``), so the old test made tick
        one of every single message claim a reasoning icon - which armed the
        auto-copy and overrode the send gate against a chat nobody had answered
        yet, and two seconds later "finished" it into MANUAL_COPY. The raw
        per-frame fact is what the docstring above was always describing.

        A detector that never reported cannot vote - and needs no ``_seen``
        check to be kept out, since the flags below can only ever be set by a
        probe of its own that survived the ghost filter.
        """
        return self._busy_generating_now or self._idle_generating_now

    def evaluate_finish(self) -> None:
        """Fold every live detector's latest verdict into one "the model
        stopped" decision, once per poll tick.

        Only while a reply is actually outstanding (``awaiting_pasted_reply``,
        opened by the outbound copy). Calibration is not consent: a tab whose
        appearances are captured and whose window is drawn is *configured*, not
        *running*, and the poller watches it either way (its verdicts are the
        sidebar's DETECTION readout). Without this gate, merely finishing the
        calibration of a resting chat armed the trigger on one frame of screen
        noise and fired the auto-copy two quiet ticks later - a click, a scroll
        and possibly a hover scan across a conversation nobody had asked
        anything. Every rule below is about *which* reply-shaped evidence
        counts; this one is about whether there is a reply at all.

        * ANY detector saying "generating" breaks the finished-streak. That
          includes a busy/idle tracker still settling after a reset, whose
          "generating" is a default rather than a reading - biasing AWAY from
          "finished" on no evidence is exactly right, and it is the only thing
          such a tick may do.
        * A busy/idle detector that SAW its icon on the frame just probed
          (``icon_evidence``, which is a strictly stronger test than the
          verdict) also ARMS the auto-copy trigger, immediately, and stops the
          paste nag - a reasoning icon on screen is evidence nothing else
          produces.
        * The STALE detector saying "generating" arms it only as part of a
          sustained large delta: ``send_arm_ticks`` consecutive ticks whose
          diff cleared ``send_arm_min_diff`` - counted by the MONITOR and read
          off the tick (``Tick.stale_arm_streak``, ui-monitor.md §2.2), because
          a count of consecutive ticks is a statement about a screen and not
          about this object. A caret blinking in the composer,
          or a mouse-over highlight, is a CHANGING verdict too - and arming on
          one of those between AgentClip's paste and the user's Enter meant the
          still, reply-less pre-Enter screen then read as a finished response
          and fired the auto-copy at nothing.
        * The trigger fires only when EVERY live detector says "finished" on
          two consecutive ticks (``Tick.changed_streak``, the monitor's other
          count). With one detector that is today's MATCH-then-two-CHANGED
          rule; with both it is the agreement the second detector exists for.
        * A capture error (no verdict) breaks the streak but leaves the arm
          alone: one bad frame must not silently cancel an in-flight finish.

        Firing disarms the TRIGGER (``copy_armed`` - not the app's ARMED
        switch), so the flow cannot repeat until the model generates again. A
        detector that has never reported is ignored entirely - it can neither
        veto nor fake a finish.

        And when the app itself is DISARMED (``set_os_armed``) the decision is
        still reached, still shown, and simply never launches anything: the
        whole of the above keeps running against live probes, and the fire step
        lands on MANUAL_COPY instead. Detection is not what disarming turns off.

        Suspended, too, while the READY-TO-SEND gate holds
        (``send_gate``): the outbound is sitting in the chat box UNSENT, so
        every verdict about that screen is about a message nobody has asked
        anything with. The probes still land and still paint the readout - they
        simply may not arm, fire, or roll the large-delta run forward - and the
        gate releasing resets all of that, so detection starts from the send
        rather than from the paste. See ``open_send_gate``.

        ...unless a busy or idle detector SEES the model generating on the frame
        just probed, which overrides the gate outright
        (``override_send_gate``). The gate is a question - "has the user
        pressed Enter yet?" - and a reasoning icon on screen answers it past any
        argument: nothing generates a reply to a message that was never sent.
        The same-frame requirement is load-bearing here above everywhere else,
        because ``open_reply_gate`` opens this gate and resets the trackers in
        the same breath: a gate that took the settling window's default
        "generating" for an answer released itself before the user could
        possibly have reached the Enter key. It is only staleness the gate exists to
        distrust, and a stale "generating" is deliberately NOT enough here.
        Without this the gate could outlive its own purpose: its release is one
        non-debounced template match going away, and on a fresh chat - centred,
        animating composer, not the docked one the capture was taken against -
        the button can be seen once and never yield a clean not-found frame, so
        the send happened, the model answered, and nothing ever came of it.

        Suspended while the auto-copy flow runs (``flow_running``): the flow
        scrolls and hover-scans the browser, which the stale detector reads as
        the response region changing - a fresh generation - so evaluating
        mid-flow would re-arm the trigger against the flow's own mouse work
        and re-fire it forever. ``end_flow`` lifts the suspension and resets the
        trackers.

        Runs on the POLLER thread, inside the ``_tick_lock`` the closing
        ``consume_*`` took - which is the only way it is ever reached. Everything
        it reads and writes is therefore consistent with the probe that closed
        the tick, and nothing the UI thread does can land halfway through.

        Neither streak is counted here any more (phase 2): both ride in on
        ``_last_tick``, the tick ``_on_tick`` recorded before it unpacked the
        probe that reached this call. A shell that calls a ``consume_*`` method
        with a probe of its own - the public seam those methods are - therefore
        folds against zero, which is the honest answer for a reading that never
        came off a tick.
        """
        if self._flow_running or not self._awaiting_pasted_reply:
            return
        if self._send_gate is not None:
            if not self.icon_evidence():
                return
            self.override_send_gate()
        verdicts: list[bool | None] = []
        if self._busy_seen:
            verdicts.append(self._busy_finished)
        if self._idle_seen:
            verdicts.append(self._idle_finished)
        if self._stale_seen:
            verdicts.append(self._stale_finished)
        if not verdicts:
            return
        tick = self._last_tick
        arm_streak = 0 if tick is None else tick.stale_arm_streak
        changed_streak = 0 if tick is None else tick.changed_streak
        if any(verdict is False for verdict in verdicts):
            if self.icon_evidence() or arm_streak >= self._send_arm_ticks:
                # WHICH evidence armed it is the whole difference between "a
                # reasoning icon is on screen" (proof) and "the region kept
                # changing a lot" (inference), and only the second one can be
                # fooled by a video or an animation the user has open.
                why = (
                    "a busy/idle icon shows the model generating"
                    if self.icon_evidence()
                    else f"{self._send_arm_ticks} sustained large frame deltas in a row "
                    f"(≥ {self._send_arm_min_diff:.2f})"
                )
                if not self._copy_armed:
                    self.log_harness(KIND_TRIGGER, f"auto-copy trigger armed: {why}")
                self._copy_armed = True
                # The send demonstrably happened - the Ctrl+V landed and the
                # user pressed Enter, so stop nagging them to. Same evidence
                # moves the loop: whatever the gate saw or missed, the model
                # is now visibly generating.
                self.set_loop_state(LoopState.WAIT_GENERATE, why)
                self._view.hide_paste_flash()
            return
        if not all(verdict is True for verdict in verdicts) or not self._copy_armed:
            return
        if changed_streak < 2:
            return
        if not self._os_armed:
            # The finish is real and everything above it stays true - which is
            # the point: disarming stops the ACTING, so the rail still tracks
            # the turn and simply lands on the state where the harvest is the
            # user's. Handled exactly like the no-copy-button case below, arm
            # and streaks left as they are, because it is the same situation
            # from the loop's point of view: finished, nothing for us to click.
            if self._loop_state is not LoopState.MANUAL_COPY:
                self._view.notify(
                    "disarmed - the reply looks finished: copy it yourself, then press "
                    "i to ingest it (the watcher is off too)",
                    severity="warning",
                    timeout=8,
                )
            self.set_loop_state(
                LoopState.MANUAL_COPY,
                "auto-copy suppressed: disarmed - the reply looks finished but the "
                "tool may not click, so copy it yourself and press i",
            )
            return
        if not self._has_appearance(TemplateKind.COPY):
            # Finished, but there is no captured copy button to click: the
            # harvest is the user's. Display only - the trigger stays exactly
            # as armed as it always was, so nothing else changes.
            self.set_loop_state(
                LoopState.MANUAL_COPY,
                "no copy button is captured for this service, so there is nothing "
                "to click (capture one in F2)",
            )
            return
        self._copy_armed = False
        # The reply we were waiting for is being harvested right now: nothing
        # is outstanding again until the next outbound goes out.
        self.close_reply_gate()
        # SYNCHRONOUSLY, and before anything below it can hand control back to a
        # caller: this is what makes the fire one-shot. ``on_fire`` schedules
        # work rather than doing it - and since 5b it does not even reach the UI
        # thread until this call stack has unwound, so between this line and the
        # flow's first await there is a whole message hop the NEXT tick can
        # arrive in. A second fire against a chat the first one is already
        # harvesting would double-click a copy button and ingest the reply
        # twice; this flag, set here rather than by the flow, is what forbids it.
        self._flow_running = True
        self.set_loop_state(
            LoopState.AUTO_COPY,
            "every live detector said the model stopped on two ticks running",
        )
        self._on_fire()

    # == the slot pointers =====================================================

    @property
    def live_slot(self) -> AgentSlot:
        """Which slot the automation is driving (paste click, poller, auto-copy)."""
        return self._live

    @property
    def calibrating_slot(self) -> AgentSlot:
        """Which slot the sidebar/settings surface is configuring."""
        return self._calibrating

    @property
    def live(self) -> SlotCalibration:
        """The driven slot's calibration."""
        return self._slots[self._live]

    @property
    def calibrating(self) -> SlotCalibration:
        """The configured slot's calibration."""
        return self._slots[self._calibrating]

    @property
    def slots(self) -> dict[AgentSlot, SlotCalibration]:
        """Every slot's calibration, by slot.

        The live mapping, not a copy: a ``SlotCalibration`` is mutable and
        long-lived by design (``screen/slot.py``), so handing out the dict says
        no more than ``calibration`` already does one slot at a time.
        """
        return self._slots

    def calibration(self, slot: AgentSlot) -> SlotCalibration:
        """One slot's calibration."""
        return self._slots[slot]

    def set_calibration(self, slot: AgentSlot, region: ScreenRegion | None) -> None:
        """Adopt (or forget) the box the user drew around a chat window.

        ``slot`` is a parameter rather than a read of ``calibrating`` because
        the picker blocks for as long as the user takes to drag, and the
        pointers move on their own meanwhile - what was selected when the picker
        opened is what the user was answering.
        """
        self._slots[slot].chat_region = region

    def select_live_slot(self, slot: AgentSlot) -> None:
        """Point the automation at another chat window (a delegation starting or
        ending, and ``/new`` going home to the master)."""
        self._live = slot

    def select_calibrating_slot(self, slot: AgentSlot) -> None:
        """Point the configuration surface at another chat window. Never moves
        ``live``: selecting a tab must not retarget the automation."""
        self._calibrating = slot

    # == a service per window ==================================================

    def service_of(self, window: str) -> str:
        """The service key a window is pointed at, or ``""`` for an unknown one.

        Raw: resolving a stale or blank key against the config is the caller's,
        because the config is the caller's.
        """
        return self._services.get(window, "")

    def set_service(self, window: str, key: str) -> None:
        """Point one window at a service."""
        self._services[window] = key

    def services(self) -> dict[str, str]:
        """Every window's service key - a copy, safe to iterate while writing."""
        return dict(self._services)

    # == the OS-acting sequences ===============================================
    # Everything below RUNS ON THE EVENT LOOP: they are coroutines a shell
    # schedules (the Textual side puts the harvest on a ``run_worker``), and
    # every blocking primitive inside them goes out through ``asyncio.to_thread``
    # exactly as it did when this code was the screen's.
    #
    # **The tick lock is never held across an await.** Not one line here takes
    # ``_tick_lock`` itself; what they do is call the mutators above - a loop
    # state, a log line, a gate, ``end_flow`` - each of which brackets its own
    # SYNCHRONOUS body with it and returns. That is the rule, and it is
    # load-bearing: holding the lock over one of these awaits would stall the
    # poller thread for the whole of a scroll, a settle or a capture, which is
    # the exact stall the thread split exists to remove.

    # -- our own window --------------------------------------------------------

    @property
    def own_window(self) -> int | None:
        """The shell's own window handle, or None if none was ever recorded."""
        return self._own_window

    def set_own_window(self, handle: int | None) -> None:
        """Record where "back to the tool" is.

        Called by a shell at the moments the user is provably interacting with
        it - launch, a composer send, a sidebar press - because that is when the
        foreground window is trustworthy. A ``None`` reading (mid focus switch,
        non-Windows) keeps the last good handle rather than forgetting it: the
        answer to "where do I hand focus back to" does not get better for being
        cleared.
        """
        if handle is not None:
            self._own_window = handle

    async def snap_focus_back(self) -> bool:
        """Bring the shell's own window back to the foreground after a click in
        the browser. False = it never got there (nothing recorded, or Windows
        kept focus in the browser); the caller carries on either way.

        Verified and retried (``screen.focus.focus_window_verified``) rather
        than asked once: the click that preceded this is *also* an activation
        request, and the browser's own is still in flight when we make ours - a
        single ``SetForegroundWindow`` wins the race often enough to look like it
        works and loses it often enough to be the bug. Off the event loop,
        because the verification sleeps between tries.
        """
        handle = self._own_window
        if handle is None:
            return False
        return await self._monitor.focus_window(handle)

    async def snap_back_after_click(self) -> bool:
        """Let a click in the browser register, then take the foreground back.

        The two-line shape every "we clicked over there and the user's next move
        is over HERE" site had copied out: a beat
        (``delivery.SNAP_BACK_SETTLE_S``) so the browser has the click before
        focus moves off it, then the verified snap. False (and no beat at all)
        when no handle was ever recorded - there is nowhere to snap to, and
        sleeping for it would only delay the caller.

        It is deliberately NOT called after every browser click. A click whose
        outcome leaves work for the user IN the browser - a paste the banner is
        asking them to send, a new-chat button that never landed - keeps the
        browser focused, because pulling the foreground back would make them
        click into it again to do what we just told them to do.
        """
        if self._own_window is None:
            return False
        await asyncio.sleep(beats.SNAP_BACK_SETTLE_S)
        return await self.snap_focus_back()

    async def _await_browser_activation(self) -> bool:
        """Wait until our own window is no longer the foreground one - i.e. the
        focus click's activation has actually been granted to the browser.

        The verified half of the paste settle (``delivery.ACTIVATION_ATTEMPTS``
        / ``ACTIVATION_POLL_S``). True = the foreground moved off us inside the
        budget; False = it did not, or there is nothing to compare against.

        Never a failure. A budget that runs out means we stop waiting and paste
        anyway, exactly as the blind sleep always did: the alternative is
        refusing to deliver a payload that would probably have landed, and the
        banner plus the retry button already cover a paste that goes nowhere.
        The same is true when no window handle was ever recorded (a shell that
        never called ``set_own_window``, or a platform where the read returns
        None) - with no "us" to compare the foreground to, there is no question
        to answer, so the poll is skipped rather than spent.
        """
        handle = self._own_window
        if handle is None:
            return False
        for _ in range(beats.ACTIVATION_ATTEMPTS):
            current = await self._monitor.foreground_window()
            if current is not None and current != handle:
                return True
            await asyncio.sleep(beats.ACTIVATION_POLL_S)
        return False

    # -- the two clicks every sequence is built out of -------------------------

    async def focus_click(self, target: ScreenRegion) -> bool:
        """Click ``target``'s centre to put the chat's window in front, warning
        once - never per copy - if the OS refuses. True only when the click
        landed, which is what tells a caller it is safe to type into whatever
        that click just focused.

        WHERE to click is the caller's decision: the paste path wants the chat
        box itself (a caret in the input field is the point), while a keyboard
        scroll wants anywhere but (see ``flow.above_chatbox``).
        """
        clicked = await self._monitor.click(target)
        if not clicked and not self._region_click_warned:
            self._region_click_warned = True  # once, not on every copy
            self._view.notify(
                "the focus click did not land (it is Windows-only) - alt-tab to the chat instead",
                severity="warning",
            )
        return clicked

    async def find_all(
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

        All of them rather than the first, with near-duplicate hits on one
        physical element folded away first, so a list longer than one really does
        mean two elements.

        Since phase 2 this is one line onto ``UIMonitor.find_all`` and the whole
        search - the capture, the fold, the tolerance and the matcher - is the
        monitor's (ui-monitor.md 2.3). Both extra parameters are therefore
        VESTIGIAL, kept only so the two shells' host methods, frozen while this
        phase runs, still type-check against the call they make:

        * ``slot`` cannot be honoured, because the monitor watches ONE window -
          the one it was last configured with, which is the live one. A caller
          that means another slot retargets first, which is what
          ``rebuild_detectors`` is for.
        * ``scene`` cannot be honoured either: a frame is pixels, and pixels stop
          at the seam. Reusing one across several searches was an optimisation of
          the old in-process arrangement, and ``locate`` - one capture, answering
          about the kind asked for AND the kinds it may not be - is what replaced
          it.

        Passing both is still refused rather than silently ignored: it was never
        answerable, and a caller that asks for it has misunderstood the search.

        Empty - never raised - for every way this can come up empty: no chat
        region drawn, no such appearance captured, the capture failed, or it
        simply is not on screen. The monitor makes them one answer, because a
        caller that may not click has one next move for all of them.

        This is the implementation BOTH shells' ``AutomationHost.find_all``
        delegates to (``verified_copy_click``'s arrangement exactly): each of
        them had spelled the same search out, because a shell may not import
        another shell. What stays a host method is the SEAM - it is what the
        Textual suites substitute to put appearances on an imaginary screen.
        """
        if scene is not None and slot is not None:
            raise ValueError("find_all takes a slot or a captured scene, never both")
        return list(await self._monitor.find_all(kind))

    async def chatbox_region(self) -> ScreenRegion | None:
        """Which chat input box to poke right now, or None if none is known.

        The RECTANGLE, which is what a caller that has to reason about the box
        - ``flow.above_chatbox`` measures the padding over its top edge - needs.
        Where inside it a click goes is :meth:`chatbox_target`'s question."""
        found = await self._chatbox_match()
        return None if found is None else found[0]

    async def chatbox_target(self) -> tuple[ScreenRegion, ScreenRegion] | None:
        """The chat box's rectangle AND the one pixel in it to click, or None.

        Both, because the caller needs both: the point is where the caret goes,
        and the rectangle is what a keyboard scroll measures its "just above the
        box" from (``flow.above_chatbox``).

        The point is the service's own (``ServiceProfile.click_point``) only
        when a capture actually matched. The whole-drawn-window fallback below
        keeps its centre: a per-image click point describes where inside THAT
        PICTURE to click, and a window the user drew around their whole chat is
        not that picture - aiming 10% into it would land in the transcript.

        This is the HARVEST's question - the focus click the auto-copy flow
        makes before it scrolls, which only has to put the page in front and
        aims deliberately AWAY from the input field. The delivery asks
        :meth:`verified_chatbox_target` instead, and the difference between the
        two is the whole point: a click that is about to be followed by a paste
        may not land on a guess.
        """
        found = await self._chatbox_match()
        if found is None:
            return None
        box, kind = found
        if kind is None:
            return box, box
        return box, click_point_region(box, *self._live_profile().click_point(kind))

    async def verified_chatbox_target(self) -> ScreenRegion | None:
        """The one pixel to click when the chat box is really ON SCREEN - or
        None, which means "do not click at all".

        The DELIVERY's question, and the one rule the paste path has: a payload
        goes into a box we can see, or it goes nowhere. There used to be no
        such rule - the delivery took :meth:`chatbox_target`'s whole-drawn-window
        fallback and clicked the middle of the user's own rectangle - and the
        middle of a chat window is the TRANSCRIPT: the click selects a word of
        an old response, or lands on a link, and the synthetic Ctrl+V that
        follows goes wherever that left the caret. A paste into the void is
        recoverable; a paste into the wrong place is what the user has to
        notice and undo. So the fallback is refused here, on all three of its
        roads at once:

        * neither appearance matched - the page is mid-transition, a dialog is
          over it, or the capture has drifted;
        * two of one layout matched, which is two conversations under one drawn
          region and a coin toss between them;
        * the service has no chat box captured at all, the pre-calibration
          degraded mode.

        All three land on the same banner (``LoopState.MANUAL_INSERT``): the
        payload is already parked on the clipboard, so the user clicks their own
        chat box and presses Ctrl+V - which is the manual path that has always
        been there, reached without a second implementation of it.
        """
        found = await self._chatbox_match()
        if found is None:
            return None
        box, kind = found
        if kind is None:
            return None
        return click_point_region(box, *self._live_profile().click_point(kind))

    async def _chatbox_match(self) -> tuple[ScreenRegion, TemplateKind | None] | None:
        """The chat box and WHICH appearance found it - None kind for the
        fallback, and None outright when nothing at all is drawn.

        A fresh chat centres its input box and an ongoing one docks it at the
        bottom, so both layouts are asked about, ongoing first: mid-session it is
        the common case, and the hunt stops at the first hit. One
        :meth:`UIMonitor.locate` per layout rather than one capture searched
        twice - the frame is the monitor's now, and a caller that held one could
        not hand it back across the seam (ui-monitor.md 2.2).

        No ``exclude_kinds``, deliberately, and it is the one place that wants
        saying: the two layouts are the same CONTROL drawn two ways, so a service
        whose captures of them overlap would have each veto the other and the
        delivery would refuse a chat box that is plainly on screen. Excluding is
        for a kind that must not be MISTAKEN for another - the copy icon and the
        box it may not be found inside; these two are alternatives, and taking
        the first that answers is what "whichever layout is up" means.

        When neither is found (the page is mid-transition, a dialog covers it,
        or the service has no chat box captured at all) the whole chat window is
        the answer, with no kind beside it. An AMBIGUOUS answer takes that same
        road: an appearance belongs to the SERVICE, so a second window of it under
        one drawn region resolves the same box twice and picking one is a coin
        toss between two conversations.

        That un-kinded answer is a WEAK one, and every caller reads it as such.
        The harvest's focus click takes it - it only has to put the page in
        front, and the region is the user's own answer to "where is this chat".
        The delivery refuses it outright (:meth:`verified_chatbox_target`),
        because the click it makes is followed by a paste.

        Always the LIVE slot: the monitor watches the window it was configured
        with, and mid-delegation that is the sub-agent's.
        """
        region = self.live.chat_region
        if region is None:
            return None
        for kind in (TemplateKind.CHATBOX_ONGOING, TemplateKind.CHATBOX_INITIAL):
            found = await self._monitor.locate(kind)
            if found.ambiguous:
                self._view.notify(
                    f"several things look like the {kind.label} in the chat window - "
                    "AgentClip will not paste into a maybe-wrong one; redraw the "
                    "window so it contains only this chat",
                    severity="warning",
                )
                return region, None
            if found.region is not None:
                return found.region, kind
        return region, None

    async def click_profile_element(
        self, slot: AgentSlot, kind: TemplateKind, *, settle_s: float = ELEMENT_CLICK_SETTLE_S
    ) -> ElementClick:
        """Find ``kind`` inside ``slot``'s chat region right now, and click it.

        The primitive every programmatic click on a service appearance goes
        through. It replaces "click where those pixels used to be" with "click
        where they *are*", which is both safer and the reason the browser may
        move: a page that re-laid itself out, scrolled, or opened a dialog
        simply reads as not-on-screen and gets no click at all. Refusing is
        always the safe answer - the user can click it themselves.

        Finding it TWICE is refused just as firmly, and that refusal is the
        monitor's (AMBIGUOUS). An appearance belongs to the service, so a second
        window of the same service sitting inside the drawn region carries an
        identical button; picking one of them is a coin toss between two
        conversations, and the loser is a chat that gets clicked - reset, even -
        on behalf of the other.

        **Two of the six verdicts are decided HERE and four over there**
        (ui-monitor.md 2.3). DISARMED and NOT_CALIBRATED are refusals a brain
        makes before it asks anything: the armed switch is policy, and "there is
        nothing captured to look for" is answered against calibration this object
        is holding anyway. MISMATCH, AMBIGUOUS, CLICKED and NOT_CLICKED are what
        looking at the screen and pressing a pixel came to, and they are the only
        things :meth:`UIMonitor.click_element` ever answers - including WHERE
        inside the matched rectangle the press lands, which is the service's own
        click point and travels with the profile that owns it.

        DISARMED is answered FIRST, above even the calibration check: this is
        one of the four chokepoints the armed switch is enforced at
        (``set_os_armed``), it is the only programmatic click on a service
        appearance in the app, and a refusal that has already captured the
        screen and searched it would be answering a question nobody may act on.

        ``slot`` still names which calibration the refusal is judged against -
        the sub-agent's new-chat button is not the master's - but it cannot aim
        the SEARCH, which happens in the window the monitor was last configured
        with. Every caller that means another window retargets first
        (``rebuild_detectors``); the parameter stays because both shells pass it
        and they are frozen while this phase runs.
        """
        if not self._os_armed:
            return ElementClick.DISARMED
        cal = self.calibration(slot)
        if cal.chat_region is None or not self._host.profile_for(slot).has(kind):
            return ElementClick.NOT_CALIBRATED
        return await self._monitor.click_element(kind, settle_s=settle_s)

    # -- what the live window is, for the sequences that read it ---------------

    def _live_preset(self) -> ServicePreset:
        """The preset of the window the automation is driving: how it scrolls,
        whether it wants a hover scan, how its appearances are hunted for."""
        return self._host.live_preset()

    def _live_profile(self) -> ServiceProfile:
        """What the window the automation is driving looks like."""
        return self._host.profile_for(self._live)

    def live_search(self) -> tuple[int, CandidateSource]:
        """How the live window's service wants its appearances hunted for.

        Every search that happens OUTSIDE the poller - the auto-copy harvest,
        the chat-box click, a shell's own manual hunt - has to use the same two
        settings the poller was built with, or the ELEMENTS column and the thing
        about to click would be answering with different rulers. The detector
        gets them via ``build_detector``; this is the same pair for everybody
        else, read from the LIVE window's preset because that is the window all
        of those touch.
        """
        preset = self._live_preset()
        return preset.tolerance, select_matcher(preset.matcher).origins

    # -- the harvest's own readout ---------------------------------------------

    def copy_status(self, text: str) -> None:
        """Repaint the copy button's status line, keeping its captured size in
        front of whatever the flow has to report."""
        templates = self._live_profile().variants(TemplateKind.COPY)
        size = ""
        if templates:
            # The first image's size, plus how many more are being ORed with it -
            # a line that named one size while three pictures were being searched
            # for would misreport the calibration.
            extra = f" +{len(templates) - 1}" if len(templates) > 1 else ""
            size = f"{templates[0].width}×{templates[0].height}{extra} · "
        self._view.paint_detection(TemplateKind.COPY, f"{size}{text}")

    # -- the pieces of one harvest ---------------------------------------------
    # What is left of them after phase 2: the hover scan, the snap and the
    # copy-icon hunt are ``UIMonitor`` verbs now (ui-monitor.md 2.3), and the
    # crop of the frame a click was aimed at went with them - a crop is pixels,
    # and the ELEMENTS column is fed from the monitor's own frame hook instead
    # (``_on_frame``), which is the picture of the same instant.

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        """Click where the copy button was found, retrying at slightly offset
        points (still inside the icon) until the clipboard actually changes.

        ``target`` is already the ONE pixel the caller aimed at (the middle of
        the matched rectangle unless the service moved its click point), so the
        offsets below walk around the point the user chose.

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
            before = await self._monitor.read_clipboard()
        except ClipboardUnavailable:
            await self._monitor.click(target, settle_s=0.05)
            return True

        for dx, dy in COPY_CLICK_OFFSETS:
            shifted = ScreenRegion(target.left + dx, target.top + dy, target.width, target.height)
            await self._monitor.click(shifted, settle_s=0.05)
            for _ in range(COPY_VERIFY_READS):
                await asyncio.sleep(COPY_VERIFY_INTERVAL_S)
                try:
                    after: str | None = await self._monitor.read_clipboard()
                except ClipboardUnavailable:
                    after = None
                if after != before:
                    return True
        return False

    # == the delivery ==========================================================
    # The outbound half of the loop, and the mirror of the harvest below: a
    # payload is ready, so put it on the clipboard, click the chat's input box,
    # let the focus settle, paste it (in one burst or a stream of them), tap
    # Enter for a service that asked us to, and say on the banner whose move it
    # is now. A shell's part is the SCHEDULING and nothing else - these are
    # coroutines it puts on its own loop, the same arrangement slice 6 gave the
    # harvest.

    @property
    def self_writes(self) -> SelfWriteSet:
        """Hashes of every clipboard write the monitor made on our behalf.

        The watcher's filter and the delivery's register are one object, and it
        lives with the clipboard it is about (ui-monitor.md §2.11): the write and
        the tagging that stops the watcher ingesting it back have to happen on
        the same side of the seam or there is a window between them.
        """
        return self._monitor.self_writes

    @property
    def pending_insert(self) -> str | None:
        """What a retry would re-deliver, or None when nothing has been copied
        in this session yet."""
        return self._pending_insert

    def forget_pending_insert(self) -> None:
        """/new: the last outbound belonged to the session being torn down, so
        there is nothing left for the retry button to re-deliver."""
        self._pending_insert = None

    async def park_on_clipboard(self, text: str) -> bool:
        """Put the whole outbound on the clipboard, as a self-write. False = no
        real clipboard backend, so the shell was handed the payload to park
        however it can (``AutomationHost.park_off_clipboard`` - the TUI's OSC-52
        escape, which is write-only) and a synthetic Ctrl+V has nothing here to
        paste.

        Every delivery starts here, whichever way it is about to be delivered: a
        stream leaves its last chunk on the clipboard, and this is the write
        every manual recovery (the user's own Ctrl+V, /copy, the retry button) is
        aimed at.
        """
        try:
            await self._monitor.write_clipboard(text)
        except ClipboardUnavailable:
            self._host.park_off_clipboard(text)
            self._view.notify(
                "no clipboard backend - sent via the terminal's OSC-52 escape; if pasting "
                "fails, copy from .agentclip/sessions/<id>/outbound/",
                severity="warning",
            )
            return False
        return True

    async def copy_outbound(self, text: str) -> None:
        """Deliver one outbound payload: park it, then insert it.

        The whole of ``ChatView.copy_outbound`` (docs/design/tui.md §3.4b) -
        which is a DELIVERY and not a clipboard concern, and is why the shell
        method of that name is one line onto this.
        """
        # The loop leaves IDLE (or INTERPRETING - the turn's next payload) here:
        # there is an outbound, and the first move is to insert it ourselves.
        self.set_loop_state(
            LoopState.AUTO_INSERT, "an outbound payload is ready to go into the chat box"
        )
        # The WHOLE payload goes on the clipboard first, whichever way it is
        # about to be delivered: a stream leaves its last chunk there, and this
        # is the write every manual recovery (the user's own Ctrl+V, /copy) is
        # aimed at.
        clipboard_ok = await self.park_on_clipboard(text)
        # What a retry would re-deliver, recorded before the first attempt so it
        # is right whichever way that attempt ends (see ``retry_insert``).
        self._pending_insert = text
        await self.deliver(text, clipboard_ok=clipboard_ok)

    async def park_outbound(self, text: str) -> None:
        """Put the last outbound back on the clipboard and stop there.

        Stage one of the `c` re-copy (tui.md 3.4a). It is exactly the clipboard
        half of ``copy_outbound`` and none of the rest - no focus click, no
        synthetic Ctrl+V, and no ``set_loop_state``, because nothing about the
        browser round trip has moved: the payload is simply back where the user
        can paste it. It goes through ``park_on_clipboard`` like every other
        write, so this copy is registered as a self-write and the watcher cannot
        ingest our own outbound back as if it were a reply.

        ``_pending_insert`` is deliberately left alone: it is what the sidebar's
        retry button would re-deliver, and re-copying the payload that is
        already the pending one changes nothing about that.
        """
        await self.park_on_clipboard(text)

    def may_redeliver(self) -> bool:
        """May the `c` double tap escalate to a real delivery right now?

        The two refusals a re-delivery can hit, said in the words that name the
        way out of each. They are the retry button's - the same act, so the same
        reasons it may not happen (the third, "nothing has been copied yet", is
        ``SessionController._last_outbound`` and never reaches this layer).

        The decision and its wording live here; the SCHEDULING does not. A shell
        answering True puts ``copy_outbound`` on its own loop, in the same
        exclusive group the retry button uses, because the controller is on the
        event loop and must not park for the seconds a streamed delivery takes -
        and two inserts racing into one chat box is what that group prevents.
        """
        if not self._os_armed:
            self._view.notify(
                "disarmed - AgentClip may not click or type: press F5 to arm, or paste "
                "the payload into the chat yourself",
                severity="warning",
            )
            return False
        if self._flow_running:
            self._view.notify("the auto-copy flow is driving the mouse - let it finish first")
            return False
        return True

    async def retry_insert(self) -> None:
        """Do the insert again: park the last payload back on the clipboard,
        click the chat box, settle, paste, and auto-submit if the service does.

        The recovery for the failure the settle exists to make rarer - the click
        landed but the paste went nowhere, and the reply is sitting on the
        clipboard with the sidebar asking the user to Ctrl+V it into the browser
        by hand. One press does that for them, through the SAME ``deliver`` the
        auto flow uses, so the retry cannot deliver a different thing (or skip
        the auto-submit) than the attempt it replaces.

        Three refusals, none of them harmful to press into:

        * nothing has been copied yet (a press before the first outbound);
        * the app is DISARMED, where clicking and typing are exactly what is
          promised not to happen - the toast names the switch;
        * the auto-copy flow is mid-sequence, whose clicks and hover scans this
          would shove the mouse through the middle of.
        """
        text = self._pending_insert
        if text is None:
            self._view.notify(
                "nothing to re-insert yet - no outbound payload has been copied"
            )
            return
        if not self._os_armed:
            self._view.notify(
                "disarmed - AgentClip may not click or type: press F5 to arm, or paste "
                "into the chat yourself",
                severity="warning",
            )
            return
        if self._flow_running:
            self._view.notify("the auto-copy flow is driving the mouse - let it finish first")
            return
        # Back to AUTO_INSERT even though the user asked for this by hand: the
        # rail says what the automation is DOING, and what it is about to do is
        # the auto insert over again.
        self.set_loop_state(
            LoopState.AUTO_INSERT, "the insert is being retried from the sidebar"
        )
        clipboard_ok = await self.park_on_clipboard(text)
        await self.deliver(text, clipboard_ok=clipboard_ok)

    async def deliver(self, text: str, *, clipboard_ok: bool) -> bool:
        """Click the chat's input box, let the focus settle, paste, and - for a
        service that opted in - tap Enter. True only when the payload really
        landed in the box.

        ...and *only* when the input box is actually on screen. There is no
        blind click here and no aiming at the drawn window: with no captured
        chat box verifying inside it (``verified_chatbox_target``) this refuses
        the whole OS half and puts the "your move" banner up instead. The single
        door every outbound goes through - the bootstrap, a turn's results, the
        `c` re-delivery, the retry button and a sub-agent's first paste are all
        ``copy_outbound`` or ``retry_insert`` onto this - so that refusal is one
        rule rather than five.

        The payload is already on the clipboard when this runs (``copy_outbound``
        and ``retry_insert`` both park it there first): this half is the OS work
        on top of that write, and it is a method of its own precisely so the
        retry button re-runs *this* sequence rather than a second, drifting copy
        of it. Everything the auto flow does after the click - the settle, the
        stream-or-burst choice, the auto-submit tap, the loop state, the reply
        gate and the sidebar's nag - is therefore one thing, done once.

        ``clipboard_ok`` is how the payload got parked, and the only thing it
        decides is whether the STREAM path is available: a stream writes each
        chunk through the clipboard, so a service that asked for one still falls
        back to the single burst when there is no backend behind it. It is a
        parameter rather than something this method works out for itself because
        a shell may have parked the payload somewhere this layer cannot see -
        the TUI's OSC-52 escape (docs/design/gui.md §0).
        """
        # DISARMED stops here, one line below the clipboard write and above
        # every OS call - which is the whole shape of the feature: the payload
        # is where the user can paste it, and the click and the synthetic Ctrl+V
        # simply do not happen. Everything after this is the existing "the click
        # never landed" path (MANUAL_INSERT, the Ctrl+V nag), which is exactly
        # the disarmed UX and needs no second implementation.
        #
        # The chat box is resolved HERE rather than inside the click, because
        # "there is nowhere to aim" and "the aim was refused" are two different
        # things to say on the banner - and the first of them is the one this
        # whole branch exists for. ``verified_chatbox_target`` answers None
        # unless a captured appearance actually matched inside the drawn region:
        # no guess, no whole-window fallback, and therefore no click and no
        # paste. Clicking the middle of the user's rectangle would be clicking
        # the TRANSCRIPT, and a synthetic Ctrl+V after that lands wherever the
        # page left the caret.
        target: ScreenRegion | None = None
        if self._os_armed:
            target = await self.verified_chatbox_target()
            clicked = target is not None and await self._click_after_response(target)
        else:
            clicked = False
            self._view.notify(
                "disarmed - the payload is on your clipboard: click the chat box and "
                "press Ctrl+V yourself (F5 arms)",
                severity="warning",
            )
        # Only paste when the click actually landed - focus could be on any
        # window otherwise, and pasting into an unknown app is the one
        # unforgivable failure mode here.
        pasted = False
        if clicked:
            # THE seam between the click and the paste, and the one place it
            # exists: the click above only tells us the OS accepted the input,
            # never that the chat box has finished taking focus. Two halves,
            # because the wait has two halves. First WAIT FOR THE ACTIVATION -
            # poll the foreground until it is somebody else's window, which is
            # the OS telling us the browser has the click's activation (see
            # ``_await_browser_activation``); on a machine that hands it over
            # immediately this costs one read, and on a loaded one it waits
            # exactly as long as it has to. Then the flat beat
            # (``delivery.PASTE_SETTLE_DELAY``) for the half no handle reports
            # on: the page still has to route the click into the chat box and
            # put a caret there. Non-blocking throughout, so the shell keeps
            # painting (the STATE rail and the flash are what the user has to
            # look at while this happens) - and it covers the streamed delivery
            # below too, since that is the same first Ctrl+V into the same fresh
            # focus.
            await self._await_browser_activation()
            await asyncio.sleep(beats.PASTE_SETTLE_DELAY)
            # Streaming needs a clipboard to write each chunk through, so a
            # service that asks for it still falls back to the single burst when
            # there is no backend - whatever the shell parked is all there is.
            if self._live_preset().delivery == DELIVERY_STREAM and clipboard_ok:
                pasted = await self._stream_outbound(text)
            else:
                pasted = await self._monitor.send_paste()
        # The auto-insert resolved: the payload is in the box awaiting the
        # user's Enter, or it never landed and the Ctrl+V is theirs to do. Four
        # reasons, not one, because "the Ctrl+V is yours" has four very different
        # causes and only two of them are failures - the switch the user threw
        # themselves reads as a fault otherwise, and a chat box that is simply
        # not on screen is a different thing to fix than a click the OS refused.
        auto_sent = False
        if pasted:
            self.set_loop_state(
                LoopState.WAIT_SEND, "the payload was pasted into the chat box"
            )
            if self._live_preset().auto_submit:
                # Opt-in per service: tap Enter ourselves instead of waiting
                # for the user's. Still WAIT_SEND, and deliberately so - the
                # tap is an attempt, not a fact, and the send gate's evidence
                # (the ready button vanishing, the busy icon appearing) stays
                # the only thing that moves the loop to WAIT_GENERATE, exactly
                # as for a human Enter. If the tap did not take, the gate times
                # out as ever, and the flash below says whose Enter it is now.
                await asyncio.sleep(beats.SUBMIT_SETTLE_S)
                auto_sent = await self._monitor.send_enter()
                self.log_harness(
                    KIND_GATE,
                    "auto-submit tapped Enter after the paste"
                    if auto_sent
                    else "auto-submit could not type Enter - the send is yours",
                )
        elif not self._os_armed:
            self.set_loop_state(
                LoopState.MANUAL_INSERT,
                "auto-insert suppressed: disarmed - the payload is on your clipboard "
                "to paste yourself",
            )
        elif target is None:
            # The user's complaint, answered: nothing that looks like the chat
            # box verified inside the drawn region, so nothing was clicked and
            # nothing was pasted. The toast says what to do about it and the
            # banner keeps saying it until they have - and the payload is on the
            # clipboard already, so "click it and paste" is the whole recovery.
            #
            # Two sentences rather than one, because the two roads here want
            # different things done about them: an undrawn window is a setup
            # step the user has not taken yet, while a box that did not verify
            # inside a window they DID draw is a page to look at (or a capture
            # to re-take with F2).
            if self.live.chat_region is None:
                self._view.notify(
                    "no chat window is drawn, so there was nowhere to paste - the payload "
                    "is on your clipboard: draw the window (SET REGION), or paste it "
                    "yourself",
                    severity="warning",
                )
                self.set_loop_state(
                    LoopState.MANUAL_INSERT,
                    "no chat window is drawn, so nothing was clicked - paste it yourself",
                )
            else:
                self._view.notify(
                    "the chat box was not found on screen - nothing was clicked: the "
                    "payload is on your clipboard, click the chat box and press Ctrl+V "
                    "yourself",
                    severity="warning",
                )
                self.set_loop_state(
                    LoopState.MANUAL_INSERT,
                    "the chat box was not found on screen - click it and paste yourself "
                    "(press c to re-copy)",
                )
        elif not clicked:
            self.set_loop_state(
                LoopState.MANUAL_INSERT,
                "the chat box was found but the focus click on it was refused, "
                "so nothing was pasted",
            )
        else:
            self.set_loop_state(
                LoopState.MANUAL_INSERT,
                "the chat box was focused but the synthetic Ctrl+V did not go through",
            )
        # This is the moment a reply becomes something to wait for, so it is
        # the moment the finish detectors are allowed to act - see
        # ``open_reply_gate``. Unconditional: whether the Ctrl+V landed or the
        # user still has to send it themselves, the payload is out and the next
        # thing to happen in that chat is the answer to it.
        self.open_reply_gate()
        # The payload now waits on the user's Enter (pasted), Ctrl+V+Enter (not
        # pasted), or on the send gate confirming the Enter auto-submit already
        # tapped - nag until the busy region reports the model chewing (or a
        # new capture/reset happens).
        self._view.show_paste_flash(
            AUTO_SEND_FLASH_TEXT if auto_sent else ENTER_FLASH_TEXT if pasted else PASTE_FLASH_TEXT,
            # ...and offer the one-press re-run beside that nag, exactly
            # when the nag is the "you paste it yourself" one. An insert
            # that landed has nothing to retry, and a button offering to
            # click into the chat and paste a second payload on top of the
            # first is worse than no button at all.
            retry=not pasted,
        )
        # ...and, on the ONE outcome that leaves the user nothing to do in the
        # browser, bring them back here. auto_sent means the payload is in the
        # box and the Enter has been tapped for them: the next thing worth
        # watching is this window's rail and transcript, and a user who was
        # reading the chat when the turn came round would otherwise have to
        # alt-tab back by hand. The other two outcomes deliberately keep the
        # browser focused - ">>> PRESS ENTER <<<" and ">>> PRESS CTRL+V <<<" are
        # both instructions to act over THERE, and stealing the foreground while
        # asking for a keystroke in another window is how the banner ends up
        # lying about what a press will do. Covers the streamed delivery too:
        # its auto-submit is this same tap, on this same flag.
        #
        # ...unless the service says not to (``ServicePreset.snap_back``). That
        # switch is a debugging aid and reads as one: with the foreground left
        # in the browser the user can see for themselves where the click landed
        # and whether the chat box was ever selected, which is a question this
        # window cannot answer once it has taken the focus back.
        if auto_sent and self._live_preset().snap_back:
            await self.snap_back_after_click()
        return pasted

    async def _click_after_response(self, target: ScreenRegion) -> bool:
        """The payload is on the clipboard - poke the chat box at ``target`` so
        the browser has focus and the paste lands without alt-tab. Returns True
        only when the click landed, which is the signal ``deliver`` uses to
        decide whether it is safe to send Ctrl+V.

        WHERE is the caller's, and deliberately so: this method may only ever be
        handed a point that a captured chat box actually verified at
        (``verified_chatbox_target``), so there is no route through here that
        clicks a guess.

        TWO clicks, ``delivery.FOCUS_CLICK_GAP_S`` apart. The first one is
        spent waking the browser: a window that is not in the foreground takes
        the click as its activation and the page never sees it routed to the
        input field, which leaves the window focused, the caret nowhere, and
        the Ctrl+V below going into nothing the user can see. The second lands
        on a window that is already awake, and the gap is wide enough (half a
        second) that a busy page has finished reflowing its composer AND that
        the OS reads the pair as two single clicks rather than a double one.
        It lives in this method rather than in ``focus_click`` anyway, because
        this is the only caller aiming at an EMPTY box: the other ones aim at a
        transcript full of text, where a pair of clicks close enough together
        would select a word of somebody's reply.

        The verdict is the FIRST click's: it is the one that proves the OS
        accepts input for that target at all, and a second click refused after
        the first landed would only mean the burst was throttled, not that the
        box is unfocused. So the reinforcement is best effort.
        """
        clicked = await self.focus_click(target)
        if not clicked:
            return False
        await asyncio.sleep(beats.FOCUS_CLICK_GAP_S)
        await self.focus_click(target)
        return True

    async def _stream_outbound(self, text: str) -> bool:
        """Walk ``text`` into the focused chat box a chunk at a time (opt-in per
        service, ``ServicePreset.delivery``). True only if every chunk landed.

        Chunked CLIPBOARD PASTES rather than synthetic typing: a typed newline
        is Enter in most chat boxes, which would submit half a payload - the
        exact accident this whole flow exists to avoid. So each chunk is a
        clipboard write plus the same single Ctrl+V burst the paste mode sends,
        with a beat between them for the page to keep up. Every chunk goes
        through ``write_via``, so each is registered as a self-write and the
        watcher can never ingest our own outbound back as a reply.

        The stream stays inside ``LoopState.AUTO_INSERT`` - it is one insert
        that takes a while, not a state of its own - and reports itself on the
        sidebar's banner, which is the only thing on screen while the user is
        looking at the browser.

        A chunk that fails to paste ends the stream: the box then holds a
        partial payload the user has to clear, so the FULL text goes back on the
        clipboard and the caller's existing MANUAL_INSERT path takes over, with
        a toast that says both halves of that.
        """
        # The size is read off the MODULE rather than defaulted so the whole
        # cadence - chunk size and inter-chunk beat - is one pair a shell's
        # suites can shrink without pasting a real payload's worth of bursts.
        chunks = chunking.split_for_stream(text, chunking.STREAM_CHUNK_CHARS)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            self._view.show_paste_flash(stream_flash_text(index, total))
            try:
                await self._monitor.write_clipboard(chunk)
            except ClipboardUnavailable:
                landed = False
            else:
                landed = await self._monitor.send_paste()
            if not landed:
                await self._restore_after_partial_stream(text, index, total)
                return False
            if index < total:
                await asyncio.sleep(beats.STREAM_CHUNK_SETTLE_S)
        return True

    async def _restore_after_partial_stream(self, text: str, index: int, total: int) -> None:
        """Undo what a half-delivered stream left behind, as far as it can be
        undone from here: the clipboard gets the whole payload back (the last
        thing on it is a chunk, and the manual Ctrl+V the caller is about to ask
        for must paste the message, not a fragment of it), and the toast says
        the chat box is the part only the user can fix."""
        try:
            await self._monitor.write_clipboard(text)
        except ClipboardUnavailable:
            self._host.park_off_clipboard(text)
        self._view.notify(
            f"streaming stopped at chunk {index}/{total} - the chat box holds a partial "
            "message: clear it, then press Ctrl+V for the whole payload",
            severity="warning",
        )

    # -- the harvest -----------------------------------------------------------

    async def run_auto_copy_flow(
        self, flow: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """``auto_copy_flow`` inside the flow-suspension bracket.

        A wrapper rather than a try/finally inside the flow itself so the
        guard's mechanics hold even when a shell's suites stub the flow out -
        which is what ``flow`` is for: the Textual side hands in its own seam,
        one delegation above the real body. Whatever that body does (return,
        raise, or get cancelled), the suspension lifts and every tracker forgets
        the frames the flow's own scrolling and hover-scanning produced
        (``end_flow``) - polling resumes from a clean post-flow baseline instead
        of reading the flow's mouse work as a new generation.

        SCHEDULING is deliberately not here: a shell puts this on its own loop
        (Textual: ``run_worker(..., group="copyflow", exclusive=True)``), because
        which primitive runs a coroutine is the one thing a UI framework really
        does own.
        """
        try:
            await (self.auto_copy_flow() if flow is None else flow())
        finally:
            self.end_flow()

    async def auto_copy_flow(self) -> None:
        """Fired once by ``evaluate_finish`` when the detectors agree reasoning
        finished: focus the browser, snap the transcript to the bottom, then look
        for the newest (lowest) copy-button icon anywhere in the chat region and
        click it - the clipboard watcher ingests the resulting copy on its own.

        The search is the whole chat region, not a same-width band beneath a
        remembered icon: the icon appears once per response down the transcript,
        and *lowest inside the window the user drew* is the same answer without
        anyone having to remember a column.

        It is its OWN search, deliberately, even though the poller has been
        looking for the same icon twice a second all along. Three reasons, and
        the first is enough: this flow has just clicked, scrolled and waited for
        the page to render, so the newest response's icon is somewhere the last
        poll frame does not show it - **a location up to half a second old is not
        a click target**. The poller also answers a different question (is one on
        screen, anywhere) from the one a harvest asks (which is the LOWEST, i.e.
        newest), and it stops at the first hit to stay cheap. What the poller's
        record IS good for is explaining a miss, which is where it is read below
        (``AutomationHost.copy_seen_note``).

        **The snap gets ``COPY_SNAP_ROUNDS`` goes, not one.** A miss on the
        static frame used to end the hunt, and the commonest cause of one is not
        a drifted capture at all - it is a page that had not finished arriving.
        A streamed reply keeps growing after the detectors call it finished, a
        virtualized transcript renders the rows it just scrolled to a beat later,
        and either way the bottom moves out from under a single snap. So a miss
        re-scrolls (the same action, the same settle) and re-searches, up to
        three rounds in all, each one logged as *round n/3* so a failure reads as
        "we tried and the page never showed one" rather than as one unlucky
        frame. The focus click and the pointer park are **not** repeated: nothing
        between rounds touches the mouse or the focus, so both are still exactly
        where round 1 put them, and re-clicking a transcript risks selecting text
        or following a link.

        The hover scan (opt-in, per service) runs after the LAST static miss, not
        after the first: it drives the user's real cursor across the screen, and
        doing that three times over would be three times the intrusion for the
        same answer. The failure report keeps the **best** ``best_miss`` of all
        the rounds - the closest the capture ever came is the number that
        separates "drifted, recapture it" from "no candidate at all", and taking
        the last round's would throw away the one informative frame whenever the
        final scroll landed somewhere blank.
        """
        region = self.live.chat_region
        templates = self._live_profile().variants(TemplateKind.COPY)
        if region is None or not templates:
            missing_part = (
                "no chat window is drawn" if region is None else "no copy button is captured"
            )
            self.log_harness(KIND_COPY, f"auto-copy flow could not start: {missing_part}")
            self.set_loop_state(
                LoopState.MANUAL_COPY, "there is nothing for the auto-copy flow to search"
            )
            return

        # Focus the chat window and park the pointer on the transcript. Both are
        # done ONCE, in front of the first snap: the rounds that follow scroll
        # the way this service says it scrolls (``ServicePreset.scroll_action``,
        # ``UIMonitor.snap_to_bottom``) and touch neither the focus nor the
        # mouse, so a retry inherits this choreography rather than repeating it.
        #
        # The keyboard forms ride this focus click - keys go to whatever has
        # focus - so the click may not land in the CHAT BOX the way the paste
        # path's deliberately does: a caret in the input field swallows End
        # outright (it means "end of line" there) and the transcript never
        # moves. The padding just above the box focuses the same window with
        # nothing typable under the pointer. The wheel is aimed by coordinates
        # and does not care either way, so it keeps the plain box click.
        #
        # Whatever the click focused, the pointer is then parked on the
        # transcript's center before anything scrolls, because some chat pages
        # only scroll the pane the pointer is over: with the cursor left sitting
        # in the input box the wheel turns against a one-line field and the keys
        # go nowhere the reader can see. It has to be ``move_cursor`` - a real
        # synthetic MOVE - and not the ``SetCursorPos`` teleport inside
        # ``scroll_region``, since a teleported pointer does not reliably make a
        # browser fire the hover chain those pages track. Best-effort: a move
        # that does not happen (unsupported platform, disarmed) leaves the snap
        # exactly as it was before this step existed, which is worth trying
        # anyway.
        scroll_action = self._live_preset().scroll_action
        chatbox = await self.chatbox_target()  # the live chat box, else the chat region
        if chatbox is not None:
            box, target = chatbox
            if scroll_action in (SCROLL_PAGE_DOWN, SCROLL_END):
                # Measured off the RECTANGLE, not off the click point: the
                # padding this aims at is over the box's top edge, wherever
                # inside the box a caret-seeking click would have gone.
                target = above_chatbox(box, region) or target
            await self.focus_click(target)
        await asyncio.sleep(0.15)
        await self._monitor.move_cursor(*region.center)
        await asyncio.sleep(0.1)  # let the page's hover tracking register it

        found: ScreenRegion | None = None
        best_miss: float | None = None
        for attempt in range(1, COPY_SNAP_ROUNDS + 1):
            await self._monitor.snap_to_bottom(scroll_action)
            await asyncio.sleep(SNAP_SETTLE_S)  # let the page render what it scrolled to

            # One monitor verb per round, and the whole hunt is inside it: a
            # capture of the configured region, the copy stack searched across
            # it, the bottom-most hit and - on a miss - how close the closest
            # rejected candidate came. A capture that failed reads as a miss
            # here, deliberately: it says nothing about where the icon is, which
            # is the same fact as "it is not on screen" to a caller that may not
            # click either way (ui-monitor.md 2.3).
            #
            # ``exclude_kinds`` is the chat box, both layouts. The copy icon is
            # hunted across the WHOLE drawn window, the composer included, and a
            # service whose copy capture also matches a corner of its input box
            # would have the harvest click into the chat box and copy nothing -
            # a hit that is really the other control is a click on the wrong
            # thing, which is exactly what the exclusion is for.
            located = await self._monitor.locate(
                TemplateKind.COPY, exclude_kinds=CHATBOX_KINDS
            )
            found = located.region
            # The closest ANY round came, not the last one's: see the docstring.
            if located.best_miss is not None and (
                best_miss is None or located.best_miss < best_miss
            ):
                best_miss = located.best_miss
            # ``ambiguous`` is deliberately NOT consulted. Every response in a
            # transcript stamps its own copy icon, so several of them on screen
            # is the ordinary case rather than the two-windows trouble it means
            # for a button there is only ever one of - and ``locate`` already
            # answers with the LOWEST, which is the newest reply's.
            if found is not None:
                break
            if attempt < COPY_SNAP_ROUNDS:
                # Deliberately NOT the word "not found": that line is the
                # flow's verdict, and a hunt that is still scrolling has not
                # reached one. Saying it here would report a failure the very
                # next round can overturn.
                self.copy_status(f"re-snapping ({attempt + 1}/{COPY_SNAP_ROUNDS})")
                self.log_harness(
                    KIND_COPY,
                    f"copy button not found on round {attempt}/{COPY_SNAP_ROUNDS} "
                    f"({how_close(best_miss)}) - snapping to the bottom again",
                )
        if found is None and self._live_preset().hover_scan:
            # Nothing in the static frame: this service is one of the chats that
            # only paint the icon under the pointer, so try again while hovering
            # up the region. Opt-in per service (``hover_scan``) because the scan
            # drives the user's real mouse across the screen - worth it where it
            # is the only way to find the icon, gratuitous everywhere else, where
            # a static miss simply means the icon is not there.
            self.copy_status("hover-scanning")
            found = await self._monitor.hover_scan(TemplateKind.COPY)
        if found is None:
            self._view.notify("copy button not found on screen", severity="warning")
            self.copy_status("not found")
            # The number goes on the ``copy`` entry, the consequence on the
            # ``state`` one: the two print on adjacent lines, and repeating the
            # parenthetical on both made the log read as a stutter.
            self.log_harness(
                KIND_COPY,
                f"copy button not found after {COPY_SNAP_ROUNDS} snaps "
                f"({how_close(best_miss)}{self._host.copy_seen_note()})",
            )
            self.set_loop_state(
                LoopState.MANUAL_COPY, "the copy button was not found on screen"
            )
            return

        # The rectangle the search came back with, reduced to the one pixel this
        # service aims its copy click at (the centre unless the user moved it).
        # Reduced HERE rather than inside the click, so the small retry offsets
        # ``verified_copy_click`` walks through stay around the point the user
        # chose rather than around a centre they rejected - which is also why
        # this click does not go through ``UIMonitor.click_element``: the copy
        # click is CLIPBOARD-verified, and that verification is policy.
        target = click_point_region(
            found, *self._live_profile().click_point(TemplateKind.COPY)
        )
        # Arm the prose window for THIS click and nothing else: whatever the
        # clipboard holds when the click verifies is the model's reply, so the
        # harvest may show it even with no CLIP blocks in it. Disarmed the
        # moment the harvest returns below - and by ``end_flow`` on every other
        # way out of here.
        self._prose_window = True
        clicked = await self._host.verified_copy_click(target)
        if clicked:
            self._view.notify("copy button clicked")
            self.copy_status("clicked")
            self.log_harness(
                KIND_COPY,
                "copy button found and clicked; the clipboard changed, so the reply "
                "is on its way in",
            )
            # The response is on its way to the clipboard - hand focus back to
            # AgentClip so the user watches the ingest here, not the browser.
            # The beat before it is ``snap_back_after_click``'s.
            #
            # ...unless the service says not to (``ServicePreset.snap_back``),
            # the same switch the auto-send snap reads. It is a debugging aid,
            # and an aid that covered only the delivery would be no aid at all:
            # the harvest fires seconds later on the same turn and would take
            # the browser away again just as the user was watching where the
            # clicks landed. Default True, so nothing moves for anyone who has
            # not deliberately turned it off.
            if self._live_preset().snap_back:
                await self.snap_back_after_click()
            try:
                await self._host.ingest_harvest()
            finally:
                # Back to strict checking the instant the harvest is in.
                self._prose_window = False
            return

        # Every attempt clicked but the clipboard never changed - leave the
        # browser focused so the user can click the copy button themselves.
        self._view.notify(
            "copy click did not take - click the response's copy button yourself",
            severity="warning",
        )
        self.copy_status("click did not take")
        self.log_harness(
            KIND_COPY,
            "the copy button was found and clicked, but the clipboard never changed",
        )
        self.set_loop_state(
            LoopState.MANUAL_COPY,
            "the copy click did not take - click the response's copy button yourself",
        )

    # -- moving the automation between browser windows -------------------------

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
        outcome = await self.click_profile_element(slot, TemplateKind.NEW_CHAT)
        if outcome is ElementClick.DISARMED:
            # Same all-or-nothing contract as every other refusal here: the
            # delegation is abandoned before a single character is pasted, which
            # is the only safe answer when the sub-agent's chat was never opened.
            self._view.notify(
                f"disarmed - the {slot.label} chat was not opened, so nothing was "
                "delegated; press F5 to arm",
                severity="error",
            )
            return False
        if outcome is ElementClick.NOT_CALIBRATED:
            self._view.notify(
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
            self._view.notify(
                f"the {slot.label} chat's new-chat button {reason} - nothing was "
                "clicked and nothing was pasted",
                severity="error",
            )
            return False
        self.select_live_slot(slot)
        self.reset_finish_trigger()
        # The master's outstanding reply is not this window's business: the
        # sub-agent's own bootstrap copy re-opens the gate a moment from now.
        self.close_reply_gate()
        self._view.hide_paste_flash()
        self._host.rebuild_detectors()  # baseline + regions from the new live slot
        # Let the fresh chat render its (centred) input box.
        await asyncio.sleep(beats.NEW_CHAT_SETTLE_S)
        return True

    def end_browser_chat(self) -> None:
        """Hand the automation back to the master chat when a delegation ends.

        Unconditional and never fails: the master window is where the session
        lives, so returning to it must work even after the sub-run blew up.
        """
        self.select_live_slot(AgentSlot.MASTER)
        self.reset_finish_trigger()
        # Symmetrically: the sub-run's last reply is done with, and the
        # master's turn resumes by composing and copying its next outbound.
        self.close_reply_gate()
        self._view.hide_paste_flash()
        self._host.rebuild_detectors()
