"""AutomationController: the UI-agnostic screen-automation core.

Sibling of :class:`~agentclip.app.controller.SessionController` and the same
kind of object: the state and the decisions behind what AgentClip does *to* the
browser chat window, lifted out of the Textual ``MainScreen`` so a second shell
can drive the identical loop. It talks to the UI only through the
:class:`~agentclip.automation.view.AutomationView` port and therefore imports no
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
:func:`agentclip.clip.watcher.watch`, which was always thread-agnostic - a
blocking poll loop taking ``should_stop``/``on_capture`` callbacks - so only its
OWNER moved down here. Captures leave the thread through the
``on_clipboard_captured`` callback the shell hands in at construction (the
Textual shell posts a message from it; the GUI will enqueue onto its bridge),
which is the same non-blocking, thread-safe contract every ``AutomationView``
method has.

**The detector poller.** The second thread, and so far only its PRODUCER half:
the loop that captures the live chat window once per tick, hands that one frame
to one :class:`~agentclip.screen.detector.ScreenDetector`, and pushes the
answers out through the five probe callbacks handed in at construction. What
those answers MEAN - the per-detector bookkeeping, the sidebar readout, the
combined finish verdict - is still the shell's, and moves down here as one unit
in the next slice, because a decision split across two threads is a race the
single-threaded handlers do not have today (docs/design/gui.md §1).

Which is why the run's ``generation`` stamp is here rather than there. Stopping
a poller is a flag, not a join: the loop it interrupts still finishes the tick
it was in and pushes those probes, so they arrive after the automation may
already have been retargeted at another browser window. The stamp is what makes
that decidable - ``retarget_detectors`` opens a new run, every probe carries the
run it was taken in, and a consumer compares. Nothing here filters: the counter
belongs to whoever starts the threads, the judgement to whoever reads the
probes.

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
:class:`~agentclip.screen.profile.ServiceProfile` - what a service LOOKS like -
is loaded from disk and cached by the shell, which hands resolved values in;
this object holds the service KEY per window and nothing about what that key
resolves to. Same rule for the calibration: the controller owns which slot is
which, the shell owns what it does with the rectangle.

Threading: every method here is called from the UI thread, and the watcher and
poller threads only ever call *out* (through the callbacks). The one piece of
state each shares with the loop it started is a ``threading.Event``, which is
what makes "stop" a flag rather than a lock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from agentclip.automation.view import AutomationView
from agentclip.clip.base import ClipboardProvider
from agentclip.clip.watcher import SelfWriteSet, watch
from agentclip.screen.busy import BusyProbe
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.detector import ScreenDetector, Sighting
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, SlotCalibration, new_slots
from agentclip.screen.stale import StaleProbe

# What a poll tick pushes out, and the one thing it needs pushed in. Every sink
# is called FROM the poller thread with the generation of the run that produced
# the reading, so an implementation must be non-blocking and thread-safe - the
# same contract the ``AutomationView`` port carries.
CaptureFn = Callable[[ScreenRegion], RegionImage]
BusySink = Callable[[BusyProbe, int], None]
StaleSink = Callable[[StaleProbe, int], None]
SendReadySink = Callable[[bool | None, int], None]
# The tick's recognitions, raw: the frame they were verified against and the
# sightings inside it. Cutting the crops out is the shell's (a crop is sized for
# whatever renderer will draw it), and it happens on this thread - the UI thread
# gets one small picture per appearance, never the frame.
ElementsSink = Callable[[RegionImage, Mapping[TemplateKind, Sighting | None], int], None]


def _accept_all(_text: str) -> bool:
    """Watcher filter for a controller nobody handed one to."""
    return True


def _drop_capture(_text: str) -> None:
    """Capture sink for a controller nobody handed one to."""


def _drop_busy(_probe: BusyProbe, _generation: int) -> None:
    """Busy/idle probe sink for a controller nobody handed one to."""


def _drop_stale(_probe: StaleProbe, _generation: int) -> None:
    """Stale probe sink for a controller nobody handed one to."""


def _drop_send_ready(_found: bool | None, _generation: int) -> None:
    """Send-ready probe sink for a controller nobody handed one to."""


def _drop_elements(
    _scene: RegionImage,
    _sightings: Mapping[TemplateKind, Sighting | None],
    _generation: int,
) -> None:
    """Recognition sink for a controller nobody handed one to."""


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
        services: Mapping[str, str] | None = None,
        clipboard: ClipboardProvider | None = None,
        self_writes: SelfWriteSet | None = None,
        poll_interval_ms: int = 300,
        accepts: Callable[[str], bool] | None = None,
        on_clipboard_captured: Callable[[str], None] | None = None,
        on_busy_probe: BusySink | None = None,
        on_idle_probe: BusySink | None = None,
        on_stale_probe: StaleSink | None = None,
        on_send_ready_probe: SendReadySink | None = None,
        on_elements: ElementsSink | None = None,
    ) -> None:
        self._view = view
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
        # our own outbound back as a reply. Shared with the shell rather than
        # owned here, because the writes are still the shell's (``write_via``)
        # until the delivery path comes down in a later slice.
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
        # Where every tick's readings go, one sink per thing a tick can say, in
        # the order the loop pushes them. Handed in at construction like the
        # capture sink above and for the same reason: they are the shell's way
        # across the thread boundary, and they do not change for its lifetime.
        self._on_busy_probe: BusySink = on_busy_probe if on_busy_probe is not None else _drop_busy
        self._on_idle_probe: BusySink = on_idle_probe if on_idle_probe is not None else _drop_busy
        self._on_stale_probe: StaleSink = (
            on_stale_probe if on_stale_probe is not None else _drop_stale
        )
        self._on_send_ready_probe: SendReadySink = (
            on_send_ready_probe if on_send_ready_probe is not None else _drop_send_ready
        )
        self._on_elements: ElementsSink = on_elements if on_elements is not None else _drop_elements
        # Which poller RUN a probe belongs to: bumped by every
        # ``retarget_detectors``, stamped into everything the loop pushes, and
        # compared by whoever consumes it. The module docstring says why the
        # counter is here and the comparison is not.
        self._detector_generation = 0
        # The current run's stop flag - replaced per run rather than cleared, so
        # a cancelled loop's flag stays set forever and that loop can only end.
        self._detector_stop = threading.Event()
        self._detector_poller: DetectorPoller | None = None

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
        """
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
        to finish the tick it was in, and the probes it pushes carry the OLD
        stamp - which is the whole point.
        """
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
        loop only carries the answers out, in the fixed busy -> idle -> stale
        order the tick-closing rule downstream reads. It is a bridge, not a
        policy: nothing here knows which appearances exist or what a verdict
        means.

        Everything is read once, here: the region, the detector, the cadence and
        the stamp all describe the window this run was started for, and a run
        that re-read them mid-flight would drift onto another one. ``capture``
        is passed in rather than imported for the same reason the clipboard
        provider is - and because the shell's test suites stub the capture at
        their own call site.
        """
        stop = self._detector_stop
        generation = self._detector_generation
        on_busy = self._on_busy_probe
        on_idle = self._on_idle_probe
        on_stale = self._on_stale_probe
        on_send_ready = self._on_send_ready_probe
        on_elements = self._on_elements

        def loop() -> None:
            while not stop.is_set():
                try:
                    scene: RegionImage | None = capture(region)
                except CaptureError:
                    scene = None  # every detector hears about it the same way
                tick = detector.observe(scene)
                if tick.busy is not None:
                    on_busy(tick.busy, generation)
                if tick.idle is not None:
                    on_idle(tick.idle, generation)
                if tick.stale is not None:
                    on_stale(tick.stale, generation)
                # The send button, every tick it is captured - it closes no tick
                # and folds into no verdict, and the gate that consumes it
                # ignores it whenever it is not holding. Three-valued: on
                # screen, not on screen, or no answer because the capture failed.
                if detector.searches(TemplateKind.SEND_READY):
                    on_send_ready(tick.present(TemplateKind.SEND_READY), generation)
                # One push for the whole tick's pictures, after the verdicts they
                # illustrate, out of the very frame the matches were verified
                # against. A failed capture recognised nothing and says nothing:
                # an empty map would blank rows a dropped frame is no evidence
                # about, so the tick simply pushes nothing.
                if scene is not None and tick.sightings:
                    on_elements(scene, tick.sightings, generation)
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
