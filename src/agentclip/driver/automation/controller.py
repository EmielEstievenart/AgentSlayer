"""AutomationController: the UI-agnostic screen-automation core.

Sibling of :class:`~agentclip.shell.app.controller.SessionController` and the same
kind of object: the state and the decisions behind what AgentClip does *to* the
browser chat window, lifted out of the Textual ``MainScreen`` so a second shell
can drive the identical loop. It talks to the UI only through the
:class:`~agentclip.driver.automation.view.AutomationView` port and therefore imports no
Textual (docs/design/gui.md §1).

It is being filled one slice at a time; today it holds the state that everything
else in the loop is read against, plus both of the polling threads:

**The armed flag.** ``/armed`` and F5. DISARMED means the tool stops ACTING on
the machine - no clicks, no synthetic paste, no cursor moves, no focus stealing,
no clipboard watching - while every read-only half (capture, the finish
detectors, the whole sidebar readout) stays live. It lives *below* both shells
rather than inside one, because with two of them a view-owned flag is a flag
that drifts. The consequences it owns *itself* are the ones made of state down
here: the clipboard watcher a disarm stops and the memory of what that watcher
was doing. The rest - the three remaining chokepoints, the toasts, the status
bar - is still the shell's, which is why ``set_os_armed`` returns the state now
in force.

**The clipboard watcher.** One plain ``threading.Thread`` running
:func:`agentclip.driver.clip.watcher.watch`, which was always thread-agnostic - a
blocking poll loop taking ``should_stop``/``on_capture`` callbacks - so only its
OWNER moved down here. Captures leave the thread through the
``on_clipboard_captured`` callback the shell hands in at construction (the
Textual shell posts a message from it; the GUI will enqueue onto its bridge),
which is the same non-blocking, thread-safe contract every ``AutomationView``
method has.

**The detector poller.** The second thread: the loop that captures the live
chat window once per tick, hands that one frame to one
:class:`~agentclip.driver.screen.detector.ScreenDetector`, and - since slice 5b - feeds
the answers straight into its own consumer, in the same call stack.

**The probe consumer**, which is what those answers MEAN. The per-detector
bookkeeping, the readout, the ready-to-send gate, the loop's narration and the
combined finish verdict are the five ``consume_*`` methods and everything under
them, and the poll loop calls them itself: probe, bookkeeping, gates,
evaluation, fire - one call stack per tick, in the busy -> idle -> stale ->
send-ready -> elements order the message pump used to serialize. The tick-closing
rule is trivially true now, because there is no queue between the producer and
the consumer to reorder anything (docs/design/gui.md §1).

Two things the fold cannot answer for itself stay the shell's, and cross as
callbacks handed in at construction: ``has_appearance`` (what the LIVE window's
service has a capture of - a question about a profile cache keyed off the
shell's Config) and ``on_fire`` (what to launch when the verdict says
"finished"). Everything else it needs it owns, which is why the trigger, both
gates, ``LoopState`` and the harness log all live here now.

Which is why the run's ``generation`` stamp is here. Stopping a poller is a
flag, not a join: the loop it interrupts still finishes the tick it was in and
consumes those probes, so they are read after the automation may already have
been retargeted at another browser window. The stamp is what makes that
decidable - ``retarget_detectors`` opens a new run, every probe carries the run
it was taken in, and ``is_ghost`` compares.

The composition is deliberately split in three calls, because the shell has
paints to interleave: ``retarget_detectors`` (stop, and bump the counter - even
when nothing new starts), ``detector_loop`` (compose this run's loop around a
detector the shell built) and ``start_detectors`` (run it on a thread).

**The slot pointers.** Which chat window is being configured and which one is
being driven, plus the drawn box and the service key behind each. Two pointers,
independent on purpose: *calibrating* is the slot behind the selected window tab
- what the sidebar's region picker writes into - and *live* is the slot the
automation (paste click, detector poller, auto-copy) is driving right now. The
user must be able to draw the sub-agent's window while the master chat is
mid-turn, and a delegation must be able to retarget the automation without
dragging the user's view along with it.

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
as :class:`~agentclip.driver.automation.host.AutomationHost`. The machine itself is
reached through :class:`~agentclip.driver.automation.ops.ScreenOps`, which is
``agentclip.driver.screen`` behind one substitutable object - deliberately NOT on the
paint port (docs/design/gui.md §1).

**Threading, as of this slice.** Two threads now write the state below: the UI
thread (a paste, a slot move, a modal, ``/new``) and the poller thread (a tick).
``_tick_lock`` is what keeps them from interleaving. It is a ``RLock`` because
consumption is re-entrant - ``consume_stale_probe`` reaches ``evaluate_finish``
reaches ``close_reply_gate`` - and it is held for exactly one probe's
consumption, which is the same grain the message pump gave this code when each
probe was a message of its own: a retarget landing mid-tick could always drop the
REST of that tick, and still can. What it may not do, and what the lock forbids,
is land in the MIDDLE of one probe's bookkeeping, between the ghost check and the
verdict it guards.

Held across bookkeeping, not across work. Every paint leaves through the view
port, which is non-blocking by contract, and the two expensive halves of a tick
- ``capture`` and ``detector.observe`` - happen OUTSIDE it: a UI thread waiting
on a template search would be the stall this whole split exists to remove. The
one call under it that can touch a disk is ``has_appearance`` on a cold profile
cache, which is the shell's own read of its own file and is why the port's
contract asks for it to be cheap.

The other piece of state each thread shares with the loop it started is a
``threading.Event``, which is what makes "stop" a flag rather than a lock.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import cast

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
    PAGE_DOWN_TAPS,
    SNAP_SETTLE_S,
    SNAP_WHEEL_DETENTS,
    above_chatbox,
    element_rects,
    how_close,
    lowest_match,
    lowest_match_scored,
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
from agentclip.driver.automation.ops import ElementClick, ScreenOps
from agentclip.driver.automation.view import AutomationView
from agentclip.driver.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.driver.clip.chunking import split_for_stream
from agentclip.driver.clip.watcher import SelfWriteSet, watch, write_via
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.detector import ScreenDetector, Sighting
from agentclip.driver.screen.hover import hover_scan_points
from agentclip.driver.screen.matchers import select_matcher
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.slot import AgentSlot, SlotCalibration, new_slots
from agentclip.driver.screen.stale import StaleProbe, StaleTracker
from agentclip.driver.screen.template import CandidateSource, RegionMatch, Template, match_rect

# The two things one poll tick needs handed in. Both are called FROM the poller
# thread, so an implementation must be non-blocking and thread-safe - the same
# contract the ``AutomationView`` port carries.
CaptureFn = Callable[[ScreenRegion], RegionImage]
# The tick's recognitions, cut down to pictures. Sizing a crop depends on which
# renderer the shell can drive, so the CUT is the shell's - but it happens here,
# on the thread that captured the frame, because what crosses to a UI is then one
# small picture per appearance rather than a whole chat window. What comes back
# is opaque (``AutomationView.paint_elements`` takes ``object``), and a shell
# that hands nothing in gets the sightings themselves, uncut.
CropFn = Callable[
    [RegionImage, Mapping[TemplateKind, Sighting | None]], Mapping[TemplateKind, object]
]
# What one probe can be, for the ``feed_probe`` test seam's single door.
Probe = BusyProbe | StaleProbe | bool | Mapping[TemplateKind, object] | None


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


class DetectorPoller:
    """One running poll loop: the thread, and the flag that ends it.

    Handed back by ``start_detectors`` so a shell can mirror the run in its own
    chrome and a test can join it. ``cancel``/``is_cancelled`` keep the
    vocabulary the Textual worker this replaced had, because "cancelled" is what
    the loop's own tick check reads and what a caller asks about after a
    retarget - the thread outlives the cancel by one tick, deliberately.
    """

    def __init__(self, thread: threading.Thread, stop: threading.Event) -> None:
        self.thread = thread
        self._stop = stop

    @property
    def is_cancelled(self) -> bool:
        """Has this run been told to end? True the instant ``cancel`` is called,
        which is up to one tick before the thread actually finishes."""
        return self._stop.is_set()

    def cancel(self) -> None:
        """End the run. Idempotent, and never a join: the caller is the UI
        thread and the loop only notices between ticks."""
        self._stop.set()


class AutomationController:
    """The automation's state, driving one :class:`AutomationView`."""

    def __init__(
        self,
        view: AutomationView,
        *,
        host: AutomationHost | None = None,
        ops: ScreenOps | None = None,
        services: Mapping[str, str] | None = None,
        clipboard: ClipboardProvider | None = None,
        self_writes: SelfWriteSet | None = None,
        poll_interval_ms: int = 300,
        accepts: Callable[[str], bool] | None = None,
        on_clipboard_captured: Callable[[str], None] | None = None,
        crop_elements: CropFn | None = None,
        has_appearance: Callable[[TemplateKind], bool] | None = None,
        on_fire: Callable[[], None] | None = None,
        send_arm_ticks: int = SEND_ARM_TICKS,
        send_arm_min_diff: float = SEND_ARM_MIN_DIFF,
        send_gate_timeout_ticks: int = SEND_GATE_TIMEOUT_TICKS,
        send_gate_seen_timeout_ticks: int = SEND_GATE_SEEN_TIMEOUT_TICKS,
        alarm: AttentionAlarm | None = None,
    ) -> None:
        self._view = view
        # The other half of the seam: what the OS-acting sequences still have to
        # ASK the shell (agentclip.driver.automation.host), and the hand this object
        # puts on the machine (agentclip.driver.automation.ops). Neither is on the paint
        # port, deliberately - the second one IS ``agentclip.driver.screen``, and it is
        # an object only so a shell can hand in shims its own suites can stub.
        self._host: AutomationHost = host if host is not None else NullHost()
        self._ops = ops if ops is not None else ScreenOps()
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
        # -- the clipboard watcher ---------------------------------------------
        # The backend is CONSTRUCTED by the shell (cli.py picks it at startup and
        # hands it down) and only driven here: which clipboard exists is a
        # startup question about the machine, not an automation decision. None
        # means a controller nobody wired one into - the headless tests - and
        # behaves exactly like the manual provider: nothing to poll.
        self._clipboard = clipboard
        # Hashes of what WE put on the clipboard, so the watcher cannot ingest
        # our own outbound back as a reply. Owned here since the delivery path
        # came down (slice 7): the same object is the watcher's filter and the
        # writer's register, and both ends are now this controller's. It can
        # still be handed IN, for the one case that has to see both ends at once
        # - a test that writes a payload the way the delivery would and then
        # asserts the watcher ignored it.
        self._self_writes = self_writes if self_writes is not None else SelfWriteSet()
        self._poll_interval_ms = poll_interval_ms
        # The protocol pre-filter, passed in for the same reason ``watch`` takes
        # it: ``agentclip.protocol`` is above this layer (tests/test_layering.py),
        # and a watcher that accepted everything would drive a turn off any copy.
        self._accepts: Callable[[str], bool] = accepts if accepts is not None else _accept_all
        self._on_capture: Callable[[str], None] = (
            on_clipboard_captured if on_clipboard_captured is not None else _drop_capture
        )
        # The running watcher and its stop flag, or None/None when nothing is
        # polling. They move together and only on the UI thread.
        self._watcher: threading.Thread | None = None
        self._watcher_stop: threading.Event | None = None
        # What the watcher was doing when a disarm took it away, so re-arming
        # restores THAT rather than a guess: a user who paused it themselves,
        # disarmed and re-armed does not get handed back a watcher they switched
        # off. Written on transitions only - see ``set_os_armed``.
        self._watch_before_disarm = False
        # -- the detector poller ------------------------------------------------
        # The one thing a tick still asks the shell for on its way to the
        # consumer: cut this tick's matches down to panel-sized pictures. Handed
        # in at construction like the clipboard sink above and for the same
        # reason - it is the shell's, and it does not change for this lifetime.
        self._crop_elements: CropFn = crop_elements if crop_elements is not None else _uncut
        # What keeps the poller thread's consumption from interleaving with the
        # UI thread's writes to the very same bookkeeping. See the module
        # docstring for the grain: one probe, never a whole tick.
        self._tick_lock = threading.RLock()
        # Which poller RUN a probe belongs to: bumped by every
        # ``retarget_detectors``, stamped into everything the loop pushes, and
        # compared by whoever consumes it. The module docstring says why the
        # counter is here and the comparison is not.
        self._detector_generation = 0
        # The current run's stop flag - replaced per run rather than cleared, so
        # a cancelled loop's flag stays set forever and that loop can only end.
        self._detector_stop = threading.Event()
        self._detector_poller: DetectorPoller | None = None
        # The detector the current run polls through, remembered by
        # ``detector_loop``. It is the object that holds the live trackers, so a
        # reset that swaps a tracker has to reach it or the poller keeps folding
        # into the one that was replaced (``reset_trackers``).
        self._detector: ScreenDetector | None = None
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
        # The tunables, injected rather than read off the module: the Pilot
        # suites patch the shell module's copies of these names (a test that had
        # to sit out a two-minute gate budget would not be a test), so whoever
        # constructs this decides what its clocks are.
        self._send_arm_ticks = send_arm_ticks
        self._send_arm_min_diff = send_arm_min_diff
        self._send_gate_timeout_ticks = send_gate_timeout_ticks
        self._send_gate_seen_timeout_ticks = send_gate_seen_timeout_ticks
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
        # The latest stale probe's frame-to-frame differing fraction, and the run
        # of consecutive stale probes whose diff cleared ``send_arm_min_diff``.
        # Only such a run may arm the auto-copy trigger on staleness alone - see
        # ``evaluate_finish``.
        self._stale_diff: float | None = None
        self._stale_arm_streak = 0
        # The three finish trackers of the detector the shell built for this
        # run, kept under their own names because the paste, the send and the
        # auto-copy flow all reset the DEBOUNCE without touching what the
        # detector has SEEN, and that is a per-tracker act.
        # ``_active_detectors`` is which of them the current run reports, in the
        # fixed busy -> idle -> stale order - the seam that says which probe
        # closes a tick (``finish_tick_closed_by``).
        self._busy_tracker: PresenceTracker | None = None
        self._idle_tracker: PresenceTracker | None = None
        self._stale_tracker: StaleTracker | None = None
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
        # ``_copy_armed``/``_copy_changed_streak`` track the probe sequence that
        # fires the auto-copy flow - see ``evaluate_finish``. Trigger state, not
        # calibration, so it is reset whenever the live slot moves.
        self._copy_armed = False
        self._copy_changed_streak = 0
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
                    self.start_watching()  # no-ops in manual mode, and if already up
                self._watch_before_disarm = False
            elif was_armed and not armed:
                self._watch_before_disarm = self.watching
                self.stop_input()
            self._view.paint_armed(armed)
            return armed

    # == the clipboard watcher =================================================

    @property
    def watching(self) -> bool:
        """Is a watcher thread polling the clipboard right now?"""
        return self._watcher is not None

    @property
    def watcher_thread(self) -> threading.Thread | None:
        """The watcher thread itself, or None. For shells that mirror its
        existence into their own chrome, and for tests that join it."""
        return self._watcher

    def start_input(self) -> None:
        """A session wants the clipboard watched (``ChatView.start_input``).

        Three answers, and only one of them starts a thread. Disarmed: the
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
        if self._clipboard is not None and self._clipboard.name == "manual":
            self._view.notify(
                "manual clipboard mode: press i and paste the model's reply into the box; "
                "outbound payloads go out via the terminal's OSC-52 copy",
                severity="warning",
                timeout=10,
            )
            return
        self.start_watching()

    def start_watching(self) -> None:
        """Start the poll loop, unless there is nothing to poll or it is already
        running. The raw start behind ``start_input``, the re-arm and the shell's
        pause/resume key alike - no arming check of its own, because each of
        those callers has already made that decision."""
        if self._watcher is not None or self._clipboard is None:
            return
        if self._clipboard.name == "manual":
            return
        stop = threading.Event()
        provider = self._clipboard
        interval = self._poll_interval_ms
        accepts = self._accepts
        on_capture = self._on_capture
        self_writes = self._self_writes

        def loop() -> None:
            watch(
                provider,
                interval,
                should_stop=stop.is_set,
                accepts=accepts,
                on_capture=on_capture,
                self_writes=self_writes,
            )

        thread = threading.Thread(target=loop, name="agentclip-clipwatch", daemon=True)
        self._watcher = thread
        self._watcher_stop = stop
        thread.start()

    def stop_input(self) -> None:
        """Stop watching (``ChatView.stop_input``), without waiting for it.

        Deliberately no join: the caller is the UI thread and the loop only
        notices between ticks, so joining would freeze the interface for up to a
        poll interval. Dropping the handles makes "stopped" true immediately for
        everything that asks, and the thread it leaves finishing its last tick
        holds nothing anyone else waits on - a capture that lands in that window
        is a real capture the user really made, exactly as it was when a Textual
        worker's ``cancel()`` owned this.
        """
        if self._watcher_stop is not None:
            self._watcher_stop.set()
        self._watcher = None
        self._watcher_stop = None

    # == the detector poller ===================================================

    @property
    def detector_generation(self) -> int:
        """Which poller run is the live one. Stamped into every probe."""
        return self._detector_generation

    @property
    def detectors_running(self) -> bool:
        """Is a poll loop watching the live window right now?"""
        return self._detector_poller is not None

    @property
    def detector_poller(self) -> DetectorPoller | None:
        """The running poller, or None. For shells that mirror it into their own
        chrome, and for tests that join its thread."""
        return self._detector_poller

    def retarget_detectors(self) -> int:
        """Close the current poller run and open a new one; returns its stamp.

        The first of the three calls a rebuild is made of, and the only one that
        always happens: a rebuild that finds nothing to watch (no drawn window,
        nothing calibrated) still ENDS the run that was watching the old one,
        and still has to invalidate the probes that run has in flight. Bumping
        the counter here rather than at the start of a loop is what makes that
        true - "the automation moved" and "a new loop exists" are different
        events, and only the first one is the question a probe is asking.

        The stop is the same flag-not-join as ``stop_input``'s, for the same
        reason: the caller is the UI thread. So the loop this ends is still free
        to finish the tick it was in, and the probes it consumes carry the OLD
        stamp - which is the whole point.

        Under ``_tick_lock``, because the counter this bumps is the one the
        poller thread is reading in ``is_ghost``: without it a retarget could
        land between a probe's ghost check and the bookkeeping that check
        guards, and the tick would half-belong to each run.
        """
        with self._tick_lock:
            self.stop_detectors()
            self._detector_generation += 1
            self._detector_stop = threading.Event()
            return self._detector_generation

    def detector_loop(
        self,
        detector: ScreenDetector,
        region: ScreenRegion,
        *,
        capture: CaptureFn,
        poll_seconds: float,
    ) -> Callable[[], None]:
        """Compose the current run's poll loop, ready to be handed to a thread.

        ONE capture per tick, handed to ONE detector. That is not only cheaper:
        every verdict then describes the same instant of a moving screen rather
        than four moments of it, and a failed capture reaches all of them as the
        same ERROR instead of some seeing a frame and others not.

        What the detector searches for was decided when the SHELL built it (from
        one window's calibration - ``screen.detector.build_detector``), and the
        loop hands the answers straight to this object's own consumer, in the
        fixed busy -> idle -> stale order the tick-closing rule reads, then the
        send button and the pictures. One call stack per tick: the probe, its
        bookkeeping, the gates, the fold and - if it comes to that - the fire all
        happen before the next capture, which is what makes "the LAST detector
        closes the tick" a fact about the code rather than about a queue.

        Everything is read once, here: the region, the detector, the cadence and
        the stamp all describe the window this run was started for, and a run
        that re-read them mid-flight would drift onto another one. ``capture``
        is passed in rather than imported for the same reason the clipboard
        provider is - and because the shell's test suites stub the capture at
        their own call site.
        """
        stop = self._detector_stop
        generation = self._detector_generation
        # Remembered, not only closed over: ``reset_trackers`` swaps a tracker
        # rather than clearing it in place, and the object that has to end up
        # holding the replacement is this one - the poller reads its trackers
        # through ``detector.busy``/``.idle``/``.stale`` every tick.
        self._detector = detector

        def loop() -> None:
            while not stop.is_set():
                try:
                    scene: RegionImage | None = capture(region)
                except CaptureError:
                    scene = None  # every detector hears about it the same way
                tick = detector.observe(scene)
                if tick.busy is not None:
                    self.consume_busy_probe(tick.busy, generation)
                if tick.idle is not None:
                    self.consume_idle_probe(tick.idle, generation)
                if tick.stale is not None:
                    self.consume_stale_probe(tick.stale, generation)
                # The send button, every tick it is captured - it closes no tick
                # and folds into no verdict, and the gate that consumes it
                # ignores it whenever it is not holding. Three-valued: on
                # screen, not on screen, or no answer because the capture failed.
                if detector.searches(TemplateKind.SEND_READY):
                    self.consume_send_ready(tick.present(TemplateKind.SEND_READY), generation)
                # One pass for the whole tick's pictures, after the verdicts they
                # illustrate, out of the very frame the matches were verified
                # against - and cut HERE, on the thread that captured it. A failed
                # capture recognised nothing and says nothing: an empty map would
                # blank rows a dropped frame is no evidence about, so the tick
                # simply says nothing.
                if scene is not None and tick.sightings:
                    self.consume_elements(self._crop_elements(scene, tick.sightings), generation)
                # Sleep in short increments so cancellation lands promptly.
                remaining = poll_seconds
                while remaining > 0 and not stop.is_set():
                    step = min(0.05, remaining)
                    time.sleep(step)
                    remaining -= step

        return loop

    def start_detectors(self, loop: Callable[[], None]) -> DetectorPoller:
        """Run a composed loop on a fresh ``agentclip-detector`` thread.

        The last of the three calls, and the only one that touches a thread - so
        a shell (or a test) that wants the composition without the polling stops
        here and never calls it. Daemon, like the watcher: an exit must never
        wait on a poll interval.

        It runs the loop of the run ``retarget_detectors`` opened, and shares
        that run's stop flag with it: starting a loop composed for a run that
        has since been stopped hands back a poller that ends on its first check,
        which is what a caller skipping the retarget is asking for.
        """
        thread = threading.Thread(target=loop, name="agentclip-detector", daemon=True)
        poller = DetectorPoller(thread, self._detector_stop)
        self._detector_poller = poller
        thread.start()
        return poller

    def stop_detectors(self) -> None:
        """End the running poller, without waiting for it.

        Deliberately no join, exactly like ``stop_input``: the caller is the UI
        thread and the loop only notices between ticks. Dropping the handle
        makes "stopped" true immediately for everything that asks, and the tick
        the thread it leaves is still finishing pushes probes stamped with a run
        that is no longer the live one.

        This is also the SUSPEND a shell reaches for when a modal takes the
        screen (the service editor's capture overlay is a sustained large delta
        over the very window the detectors watch, which is what arms the
        auto-copy on staleness alone). Deliberately the same call and
        deliberately no generation bump: nothing has moved, and the rebuild that
        resumes it opens the new run. The resume itself cannot live here -
        putting the poller back means rebuilding the detector around whatever
        the calibration says NOW, and composing that is the shell's.
        """
        if self._detector_poller is not None:
            self._detector_poller.cancel()
        self._detector_poller = None

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
        """The current run's busy-appearance tracker, or None."""
        return self._busy_tracker

    @busy_tracker.setter
    def busy_tracker(self, tracker: PresenceTracker | None) -> None:
        self._busy_tracker = tracker

    @property
    def idle_tracker(self) -> PresenceTracker | None:
        """The current run's idle-appearance tracker, or None."""
        return self._idle_tracker

    @idle_tracker.setter
    def idle_tracker(self, tracker: PresenceTracker | None) -> None:
        self._idle_tracker = tracker

    @property
    def stale_tracker(self) -> StaleTracker | None:
        """The current run's staleness tracker, or None."""
        return self._stale_tracker

    @stale_tracker.setter
    def stale_tracker(self, tracker: StaleTracker | None) -> None:
        self._stale_tracker = tracker

    def reset_trackers(self) -> None:
        """Make every live tracker forget the frames it has seen.

        The debounce only, never the verdicts: what the trackers hold is a
        streak and a previous frame, and every caller here (the paste, the send,
        the auto-copy flow) has just PRODUCED the frames behind that streak
        itself.

        **Swap, not clear.** Every caller is on the UI thread and the tracker
        being cleared is being polled on the detector thread, which reads the
        streak, spends a template search or a frame diff, and writes the streak
        back - so an in-place ``reset()`` landing inside that search is undone
        by the write that follows it, and the frames the paste or the flow
        produced stay in history. That is the one thing ``_tick_lock`` cannot
        fix at its own grain: the expensive halves of a tick are deliberately
        OUTSIDE it (module docstring), and a UI thread waiting on a template
        search is precisely the stall this split exists to remove. So each
        tracker is replaced by a ``fresh()`` one of the same calibration, and
        the poll still in flight folds its frame into an object nobody will read
        again.

        The replacement has to reach the DETECTOR, because that is what the
        poller reads its trackers through; this object's three slots are the
        same instances under their own names. The identity guard is what keeps a
        test that installed a tracker of its own from having the detector's
        overwritten with it.
        """
        with self._tick_lock:
            detector = self._detector
            if self._busy_tracker is not None:
                spare = self._busy_tracker.fresh()
                if detector is not None and detector.busy is self._busy_tracker:
                    detector.busy = spare
                self._busy_tracker = spare
            if self._idle_tracker is not None:
                spare = self._idle_tracker.fresh()
                if detector is not None and detector.idle is self._idle_tracker:
                    detector.idle = spare
                self._idle_tracker = spare
            if self._stale_tracker is not None:
                stale_spare = self._stale_tracker.fresh()
                if detector is not None and detector.stale is self._stale_tracker:
                    detector.stale = stale_spare
                self._stale_tracker = stale_spare

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
            # A half-built large-delta run belongs to the tracker that produced it.
            self._stale_diff = None
            self._stale_arm_streak = 0
            self._busy_tracker = None
            self._idle_tracker = None
            self._stale_tracker = None
            self._active_detectors = ()
            # The detector those trackers belonged to goes with them: it
            # describes a composition that no longer exists, and the shell calls
            # this immediately before building the one that replaces it.
            self._detector = None

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
            self._stale_arm_streak = 0
            self._copy_armed = False
            self._copy_changed_streak = 0

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
        """How many consecutive ticks every live detector has said "finished"."""
        return self._copy_changed_streak

    @copy_changed_streak.setter
    def copy_changed_streak(self, value: int) -> None:
        self._copy_changed_streak = value

    @property
    def stale_arm_streak(self) -> int:
        """The run of consecutive large-delta stale probes (``send_arm_ticks``
        of them is what arms the trigger on staleness alone)."""
        return self._stale_arm_streak

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
        whose large deltas were the flow's own scrolling.

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
            self._stale_arm_streak = 0

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
        if generation != self._detector_generation:
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
            if generation != self._detector_generation:
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
            if generation != self._detector_generation:
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

    # -- the test seam ---------------------------------------------------------

    def feed_probe(self, detector: str, probe: Probe = None, generation: int | None = None) -> None:
        """Consume one probe as the poll loop would have. The suites' one door.

        There is no message to inject any more - the loop calls the consumer in
        its own call stack - so this is what a test posted ``BusyProbed(...)``
        for: one named reading, stamped with the run it belongs to. The stamp
        defaults to the LIVE one, because "speak as the current poller" is what
        nearly every caller wants; pass it explicitly to speak as a run that has
        been retargeted away (a ghost) or as one that does not exist yet.

        ``detector`` is the same vocabulary ``active_detectors`` and ``is_ghost``
        use, plus the two readings that are not finish detectors at all.
        """
        stamp = self._detector_generation if generation is None else generation
        if detector == "busy":
            self.consume_busy_probe(cast("BusyProbe", probe), stamp)
        elif detector == "idle":
            self.consume_idle_probe(cast("BusyProbe", probe), stamp)
        elif detector == "stale":
            self.consume_stale_probe(cast("StaleProbe", probe), stamp)
        elif detector == "send_ready":
            self.consume_send_ready(cast("bool | None", probe), stamp)
        elif detector == "elements":
            self.consume_elements(cast("Mapping[TemplateKind, object]", probe), stamp)
        else:
            raise ValueError(f"no such detector: {detector!r}")

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
          sustained large delta: ``send_arm_ticks`` consecutive probes whose
          diff clears ``send_arm_min_diff``. A caret blinking in the composer,
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
        # Roll the large-delta run forward on every tick the stale detector
        # reported, so "consecutive" really means consecutive: a small-diff
        # CHANGING (and a STALE or an ERROR) breaks it.
        if self._stale_seen:
            big_delta = (
                self._stale_finished is False
                and self._stale_diff is not None
                and self._stale_diff >= self._send_arm_min_diff
            )
            self._stale_arm_streak = self._stale_arm_streak + 1 if big_delta else 0
        if any(verdict is False for verdict in verdicts):
            self._copy_changed_streak = 0
            if self.icon_evidence() or self._stale_arm_streak >= self._send_arm_ticks:
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
            self._copy_changed_streak = 0
            return
        self._copy_changed_streak += 1
        if self._copy_changed_streak < 2:
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
        self._copy_changed_streak = 0
        self._stale_arm_streak = 0
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
        return await asyncio.to_thread(self._ops.focus_window, handle)

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
        await asyncio.sleep(self._ops.snap_back_settle())
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
        for _ in range(self._ops.activation_attempts()):
            current = await asyncio.to_thread(self._ops.foreground_window)
            if current is not None and current != handle:
                return True
            await asyncio.sleep(self._ops.activation_poll())
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
        clicked = await asyncio.to_thread(self._ops.click, target)
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
        physical element folded away first (``flow.element_rects``), so a list
        longer than one really does mean two elements.

        ``scene`` lets a caller that already captured the chat region reuse the
        frame, so several appearances can be hunted in one picture of one
        instant - which is why it may not be combined with ``slot``: the frame
        was taken from one window, and translating its matches back through
        another slot's rectangle would put them anywhere at all.

        Empty - never raised - for every way this can come up empty: no chat
        region drawn, no such appearance captured, the capture failed, or it
        simply is not on screen.

        This is the implementation BOTH shells' ``AutomationHost.find_all``
        delegates to (``verified_copy_click``'s arrangement exactly): each of
        them had spelled the same search out, because a shell may not import
        another shell. What stays a host method is the SEAM - the sequences
        above still ask through ``self._host``, because that indirection is what
        the Textual suites substitute to put appearances on an imaginary screen.
        """
        if scene is not None and slot is not None:
            raise ValueError("find_all takes a slot or a captured scene, never both")
        target = slot if slot is not None else self._live
        region = self.calibration(target).chat_region
        if region is None:
            return []
        templates = self._host.profile_for(target).variants(kind)
        if not templates:
            return []
        if scene is None:
            try:
                scene = await asyncio.to_thread(self._ops.capture, region)
            except CaptureError:
                return []
        tolerance, matcher = self.live_search()
        return await asyncio.to_thread(
            element_rects,
            self._ops.all_matches,
            templates,
            scene,
            region,
            max_diff=kind.max_diff,
            tolerance=tolerance,
            matcher=matcher,
        )

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
        bottom, so both appearances are hunted in ONE capture of the live chat
        region - the two layouts are mutually exclusive, so whichever is found
        is the one on screen. Ongoing goes first: mid-session it is the common
        case, and the search stops at the first hit.

        When neither is found (the page is mid-transition, a dialog covers it,
        or the service has no chat box captured at all) the whole chat window is
        the answer, with no kind beside it. Two input boxes of the same layout
        inside the region take that same answer: an appearance belongs to the
        SERVICE, so a second window of it under one drawn region resolves the
        same box twice and picking one is a coin toss between two conversations.

        That un-kinded answer is a WEAK one, and every caller reads it as such.
        The harvest's focus click takes it - it only has to put the page in
        front, and the region is the user's own answer to "where is this chat".
        The delivery refuses it outright (:meth:`verified_chatbox_target`),
        because the click it makes is followed by a paste.

        Always the LIVE slot: mid-delegation this is the sub-agent's window.
        """
        region = self.live.chat_region
        if region is None:
            return None
        try:
            scene: RegionImage | None = await asyncio.to_thread(self._ops.capture, region)
        except CaptureError:
            return region, None
        for kind in (TemplateKind.CHATBOX_ONGOING, TemplateKind.CHATBOX_INITIAL):
            found = await self._host.find_all(kind, scene=scene)
            if len(found) == 1:
                return found[0], kind
            if len(found) > 1:
                self._view.notify(
                    f"found {len(found)} things that look like the {kind.label} in the chat "
                    "window - AgentClip will not paste into a maybe-wrong one; redraw the "
                    "window so it contains only this chat",
                    severity="warning",
                )
                return region, None
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

        Finding it TWICE is refused just as firmly. An appearance belongs to
        the service, so a second window of the same service sitting inside the
        drawn region carries an identical button; picking one of them is a coin
        toss between two conversations, and the loser is a chat that gets
        clicked - reset, even - on behalf of the other.

        DISARMED is answered FIRST, above even the calibration check: this is
        one of the four chokepoints the armed switch is enforced at
        (``set_os_armed``), it is the only programmatic click on a service
        appearance in the app, and a refusal that has already captured the
        screen and searched it would be answering a question nobody may act on.
        """
        if not self._os_armed:
            return ElementClick.DISARMED
        cal = self.calibration(slot)
        profile = self._host.profile_for(slot)
        if cal.chat_region is None or not profile.has(kind):
            return ElementClick.NOT_CALIBRATED
        found = await self._host.find_all(kind, slot)
        if not found:
            return ElementClick.MISMATCH
        if len(found) > 1:
            return ElementClick.AMBIGUOUS
        # Where inside the matched rectangle, per the service's own click point:
        # the middle of a control is only the right pixel until a service draws
        # one whose middle is a label and whose left third is the button.
        target = click_point_region(found[0], *profile.click_point(kind))
        clicked = await asyncio.to_thread(self._ops.click, target, settle_s=settle_s)
        return ElementClick.CLICKED if clicked else ElementClick.NOT_CLICKED

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

    def show_copy_crop(
        self, scene: RegionImage, found: tuple[Template, RegionMatch] | None
    ) -> None:
        """Put the flow's OWN copy-button search result in the ELEMENTS column.

        The poller draws that row every tick from its own presence search, so
        this is not what keeps the row alive - it is the picture of the frame
        the click was actually aimed at, cut at the instant it was aimed, which
        is the one the user wants to see when a click misses. The next poll tick
        will replace it with a picture of *now*, as it should.

        Through the same ``paint_elements`` door the poller uses, so it carries
        the shell's paint epoch like every other run-scoped paint: a crop asked
        for just before a rebuild is a picture of a window the column no longer
        describes.
        """
        sighting = (
            None
            if found is None
            else Sighting(TemplateKind.COPY, found[0], found[1], time.monotonic())
        )
        self._view.paint_elements(self._crop_elements(scene, {TemplateKind.COPY: sighting}))

    # -- the pieces of one harvest ---------------------------------------------

    def hover_scan_for_copy(
        self,
        region: ScreenRegion,
        templates: Sequence[Template],
        *,
        tolerance: int,
        matcher: CandidateSource | None = None,
    ) -> tuple[Template, RegionMatch] | None:
        """Walk the real cursor up ``region`` and stop at the FIRST frame the
        copy icon appears in, or None if it never does.

        Claude's chat only renders a response's copy button while the pointer is
        over that response, so the cheap static capture finds nothing there no
        matter how good the template is. Bottom-up (``screen.hover`` picks the
        stops) because the newest response - the one we want - is at the bottom,
        so the usual answer is one or two stops in.

        Blocking by design: a cursor move, a settle pause and a capture + region
        scan per stop. Runs in a worker thread, never on the event loop. Any
        failure (unsupported platform, a capture that fails) ends the scan,
        which the caller reports the same way as "not found" - a scan that
        cannot see is not a scan that found nothing.
        """
        for x, y in hover_scan_points(region):
            if not self._ops.move_cursor(x, y):
                return None
            time.sleep(self._ops.hover_step_delay())
            try:
                scene = self._ops.capture(region)
            except CaptureError:
                return None
            found = lowest_match(
                self._ops.lowest_match,
                templates,
                scene,
                max_diff=TemplateKind.COPY.max_diff,
                tolerance=tolerance,
                matcher=matcher,
            )
            if found is not None:
                return found
        return None

    async def snap_to_bottom(self, region: ScreenRegion, scroll_action: str) -> None:
        """One snap of the transcript to its bottom, the way this service scrolls.

        Its own method because the flow does it up to ``COPY_SNAP_ROUNDS`` times
        and the three branches must not drift apart between rounds - a retry
        that quietly used the wheel on a page whose preset says End would be a
        retry of something else.

        Deliberately *only* the scroll: the focus click and the pointer park in
        front of round 1 are one-time choreography (nothing between rounds moves
        either), and the settle belongs to the caller, which pays it per round.
        """
        if scroll_action == SCROLL_PAGE_DOWN:
            await asyncio.to_thread(self._ops.scroll_key, "page_down", PAGE_DOWN_TAPS)
        elif scroll_action == SCROLL_END:
            await asyncio.to_thread(self._ops.scroll_key, "end")
        else:
            await asyncio.to_thread(self._ops.scroll, region, SNAP_WHEEL_DETENTS)

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
            before = await asyncio.to_thread(self._read_clipboard)
        except ClipboardUnavailable:
            await asyncio.to_thread(self._ops.click, target, settle_s=0.05)
            return True

        for dx, dy in COPY_CLICK_OFFSETS:
            shifted = ScreenRegion(target.left + dx, target.top + dy, target.width, target.height)
            await asyncio.to_thread(self._ops.click, shifted, settle_s=0.05)
            for _ in range(COPY_VERIFY_READS):
                await asyncio.sleep(COPY_VERIFY_INTERVAL_S)
                try:
                    after: str | None = await asyncio.to_thread(self._read_clipboard)
                except ClipboardUnavailable:
                    after = None
                if after != before:
                    return True
        return False

    def _read_clipboard(self) -> str | None:
        """The provider's text, or ``ClipboardUnavailable`` when a controller was
        wired up without one - which is the same answer a backend that cannot
        read gives, and is handled by the same branch."""
        if self._clipboard is None:
            raise ClipboardUnavailable("no clipboard provider")
        return self._clipboard.read_text()

    def _write_clipboard(self, text: str) -> None:
        """Put ``text`` on the clipboard and register it as OUR write, so the
        watcher polling the very same clipboard cannot ingest our own outbound
        back as if it were a reply (``clip.watcher.write_via``).

        Raises ``ClipboardUnavailable`` on a controller wired up without a
        provider, exactly as ``_read_clipboard`` does and into the same branch.
        """
        if self._clipboard is None:
            raise ClipboardUnavailable("no clipboard provider")
        write_via(self._clipboard, self._self_writes, text)

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
        """Hashes of every clipboard write this controller made. The watcher's
        filter and the delivery's register are one object, and this is it."""
        return self._self_writes

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
            await asyncio.to_thread(self._write_clipboard, text)
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
            await asyncio.sleep(self._ops.paste_settle())
            # Streaming needs a clipboard to write each chunk through, so a
            # service that asks for it still falls back to the single burst when
            # there is no backend - whatever the shell parked is all there is.
            if self._live_preset().delivery == DELIVERY_STREAM and clipboard_ok:
                pasted = await self._stream_outbound(text)
            else:
                pasted = await asyncio.to_thread(self._ops.send_paste)
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
                await asyncio.sleep(self._ops.submit_settle())
                auto_sent = await asyncio.to_thread(self._ops.send_enter)
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
        on a window that is already awake. Safe HERE and nowhere else - the box
        is empty at this point in the sequence, so there is no word for a
        double click to select - which is why it lives in this method rather
        than in ``focus_click``, whose other callers aim at a transcript full
        of text.

        The verdict is the FIRST click's: it is the one that proves the OS
        accepts input for that target at all, and a second click refused after
        the first landed would only mean the burst was throttled, not that the
        box is unfocused. So the reinforcement is best effort.
        """
        clicked = await self.focus_click(target)
        if not clicked:
            return False
        await asyncio.sleep(self._ops.focus_click_gap())
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
        # The size is read off ``ScreenOps`` rather than defaulted so the whole
        # cadence - chunk size and inter-chunk beat - is one pair a shell's
        # suites can shrink without pasting a real payload's worth of bursts.
        chunks = split_for_stream(text, self._ops.stream_chunk_chars())
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            self._view.show_paste_flash(stream_flash_text(index, total))
            try:
                await asyncio.to_thread(self._write_clipboard, chunk)
            except ClipboardUnavailable:
                landed = False
            else:
                landed = await asyncio.to_thread(self._ops.send_paste)
            if not landed:
                await self._restore_after_partial_stream(text, index, total)
                return False
            if index < total:
                await asyncio.sleep(self._ops.stream_chunk_settle())
        return True

    async def _restore_after_partial_stream(self, text: str, index: int, total: int) -> None:
        """Undo what a half-delivered stream left behind, as far as it can be
        undone from here: the clipboard gets the whole payload back (the last
        thing on it is a chunk, and the manual Ctrl+V the caller is about to ask
        for must paste the message, not a fragment of it), and the toast says
        the chat box is the part only the user can fix."""
        try:
            await asyncio.to_thread(self._write_clipboard, text)
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
        # ``snap_to_bottom``) and touch neither the focus nor the mouse, so a
        # retry inherits this choreography rather than repeating it.
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
        await asyncio.to_thread(self._ops.move_cursor, *region.center)
        await asyncio.sleep(0.1)  # let the page's hover tracking register it

        tolerance, matcher = self.live_search()
        found: tuple[Template, RegionMatch] | None = None
        best_miss: float | None = None
        for attempt in range(1, COPY_SNAP_ROUNDS + 1):
            await self.snap_to_bottom(region, scroll_action)
            await asyncio.sleep(SNAP_SETTLE_S)  # let the page render what it scrolled to

            try:
                scene = await asyncio.to_thread(self._ops.capture, region)
            except CaptureError as exc:
                self._view.notify(
                    f"could not capture the chat region: {exc}", severity="error"
                )
                self.copy_status("capture failed")
                self.log_harness(KIND_COPY, f"could not capture the chat region: {exc}")
                self.set_loop_state(
                    LoopState.MANUAL_COPY, "the chat region could not be captured to search in"
                )
                return
            found, miss = await asyncio.to_thread(
                lowest_match_scored,
                self._ops.lowest_match,
                templates,
                scene,
                max_diff=TemplateKind.COPY.max_diff,
                tolerance=tolerance,
                matcher=matcher,
            )
            # The closest ANY round came, not the last one's: see the docstring.
            if miss is not None and (best_miss is None or miss < best_miss):
                best_miss = miss
            # The ELEMENTS column's picture of the frame the click is being
            # AIMED at, which the poller's own copy row cannot be: this frame is
            # the one after the scroll and the settle. Cut from THIS frame, which
            # is why it happens before the hover scan - a hover-scan hit was
            # verified against a frame taken with the pointer somewhere else, and
            # cutting it out of the static one would draw whatever the icon was
            # hiding. Repainted every round, so the column shows the frame the
            # flow is looking at now rather than the one it started with.
            self.show_copy_crop(scene, found)
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
            found = await asyncio.to_thread(
                self.hover_scan_for_copy,
                region,
                templates,
                tolerance=tolerance,
                matcher=matcher,
            )
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

        template, match = found
        # The rectangle the match translates back to, reduced to the one pixel
        # this service aims its copy click at (the centre unless the user moved
        # it). Reduced HERE rather than inside the click, so the small retry
        # offsets walk around the point the user chose rather than around a
        # centre they rejected.
        target = click_point_region(
            match_rect(region, template, match),
            *self._live_profile().click_point(TemplateKind.COPY),
        )
        # Arm the prose window for THIS click and nothing else: whatever the
        # clipboard holds when the click verifies is the model's reply, so the
        # harvest may show it even with no CLIP blocks in it. Disarmed the
        # moment the harvest returns below - and by ``end_flow`` on every other
        # way out of here.
        self._prose_window = True
        clicked = await self._host.verified_copy_click(target)
        if clicked:
            self._view.notify(f"copy button clicked (diff {match.diff:.2f})")
            self.copy_status(f"clicked (diff {match.diff:.2f})")
            self.log_harness(
                KIND_COPY,
                f"copy button found and clicked (diff {match.diff:.2f}); the clipboard "
                "changed, so the reply is on its way in",
            )
            # The response is on its way to the clipboard - hand focus back to
            # AgentClip so the user watches the ingest here, not the browser.
            # The beat before it is ``snap_back_after_click``'s.
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
            f"the copy button was found (diff {match.diff:.2f}) and clicked, but the "
            "clipboard never changed",
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
        await asyncio.sleep(self._ops.new_chat_settle())
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
