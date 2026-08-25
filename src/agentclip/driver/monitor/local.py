"""The in-process :class:`~agentclip.driver.monitor.protocol.UIMonitor`.

One object that owns everything about *watching a chat window on this machine*:
the poll thread and its stop flag, the detector the thread feeds, the trackers
behind that detector and the swap discipline that resets them, the generation
stamp that makes a leftover tick a ghost, the clipboard watcher thread, and the
hand that clicks/scrolls/types (:class:`~agentclip.driver.monitor.ops.ScreenOps`).

docs/design/ui-monitor.md §6.1 is what this file is: a *plumbing* extraction. The
loop, the stamp, the ghost filter and the watcher are lifted from
``driver/automation/controller.py`` with their reasoning intact - what changed is
where they end and what comes out. The old loop called the controller's five
``consume_*`` methods in its own call stack; this one builds one
:class:`~agentclip.driver.monitor.protocol.Tick` per capture and hands it to
whoever subscribed. Nothing here knows what a ``LoopState`` is (§2.3), and
nothing here imports ``driver/automation``.

**Two threads and one lock.** The poller thread produces ticks; the event-loop
thread configures, suspends, observes and acts. ``_tick_lock`` is what keeps
them from interleaving, and it covers *bookkeeping only*: the two expensive
halves of a tick - ``capture`` and ``detector.observe`` - happen outside it, and
so does every subscriber hook, because a hook that blocks under the lock would
stall the very configure() trying to retarget away from it (§4.4). It is an
``RLock`` because the stamping helpers below take it re-entrantly.

**Three tiers of surface.** The Protocol (what a brain may ask, local or
remote); the local-only tier the wire never carries (``capture``, ``ops``,
``detector``, ``reset_trackers``, ``on_frame`` - the ELEMENTS panel's crops and,
from §6.4, the calibration window); and ``feed``/``stamp``, the test seam that
replaces the controller's ``feed_probe``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from agentclip.config import SCROLL_END, SCROLL_PAGE_DOWN
from agentclip.driver.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.driver.clip.watcher import SelfWriteSet, watch, write_via
from agentclip.driver.monitor.beats import (
    ELEMENT_CLICK_SETTLE_S,
    PAGE_DOWN_TAPS,
    SNAP_WHEEL_DETENTS,
)
from agentclip.driver.monitor.ops import ScreenOps
from agentclip.driver.monitor.protocol import (
    ClipHook,
    ElementClick,
    Located,
    MonitorSpec,
    Tick,
    TickHook,
    UIMonitor,
)
from agentclip.driver.monitor.regions import load_region, save_region
from agentclip.driver.monitor.search import element_rects, lowest_match, lowest_match_scored
from agentclip.driver.monitor.verdicts import roll_arm_streak, roll_changed_streak
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.detector import (
    DetectionSnapshot,
    ScreenDetector,
    Sighting,
    build_detector,
)
from agentclip.driver.screen.hover import hover_scan_points
from agentclip.driver.screen.matchers import select_matcher
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.stale import StaleProbe, StaleTracker
from agentclip.driver.screen.template import CandidateSource, Template, match_rect, same_element

_log = logging.getLogger(__name__)

# The monitor's own cadence, and now the only copy of it: both shells used to
# hold a ``_BUSY_POLL_S`` of their own and convert ``stable_seconds`` against it
# (tui main.py:3120, gui view.py:3367). Cadence is a property of the machine
# whose screen is being watched, not of the policy watching it
# (docs/design/ui-monitor.md §2.10), so the brain ships raw seconds and the
# conversion happens here.
POLL_SECONDS = 0.5

CaptureFn = Callable[[ScreenRegion], RegionImage]
FrameHook = Callable[[RegionImage, Mapping[TemplateKind, Sighting | None]], None]
ProfileLookup = Callable[[str], ServiceProfile | None]


def required_ticks(stable_seconds: float, poll_seconds: float = POLL_SECONDS) -> int:
    """How many consecutive quiet polls a ``stable_seconds`` setting is worth.

    The conversion both shells did for themselves, in one place. ``max(1, ...)``
    because a debounce of zero ticks is not a debounce: a service configured
    faster than one poll still has to see the screen hold still ONCE.
    """
    return max(1, round(stable_seconds / poll_seconds))


class DetectorPoller:
    """One running poll loop: the thread, and the flag that ends it.

    Handed back by the monitor's own start so a test can join it.
    ``cancel``/``is_cancelled`` keep the vocabulary the Textual worker this
    replaced had, because "cancelled" is what the loop's own tick check reads
    and what a caller asks about after a retarget - the thread outlives the
    cancel by one tick, deliberately.
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
        """End the run. Idempotent, and never a join: the caller is the event
        loop thread and the loop only notices between ticks."""
        self._stop.set()


class _Waiter:
    """One parked :meth:`LocalUIMonitor.observe`, and the seq it is waiting past.

    The future belongs to the event loop that created it and the tick that
    resolves it arrives on the poller thread, so the hand-over is a
    ``call_soon_threadsafe`` - the one thread-safe door asyncio gives a future.
    """

    __slots__ = ("armed", "future", "loop")

    def __init__(self, armed: int, loop: asyncio.AbstractEventLoop, future: asyncio.Future[Tick]):
        self.armed = armed
        self.loop = loop
        self.future = future

    def resolve(self, tick: Tick) -> None:
        def deliver() -> None:
            if not self.future.done():
                self.future.set_result(tick)

        try:
            self.loop.call_soon_threadsafe(deliver)
        except RuntimeError:  # the loop went away under a parked observe
            _log.debug("observe() waiter dropped: its event loop is closed")


def _accept_all(_text: str) -> bool:
    """Clipboard filter for a monitor nobody handed one to: everything counts.

    The protocol pre-filter lives above this layer (tests/test_layering.py), so
    it arrives as a predicate exactly the way ``clip.watcher.watch`` takes it.
    """
    return True


class LocalUIMonitor:
    """A ``UIMonitor`` over this machine's own screen, mouse and clipboard."""

    def __init__(
        self,
        *,
        profile_for: ProfileLookup,
        ops: ScreenOps | None = None,
        clipboard: ClipboardProvider | None = None,
        self_writes: SelfWriteSet | None = None,
        clip_poll_interval_ms: int = 300,
        clip_accepts: Callable[[str], bool] | None = None,
        capture: CaptureFn | None = None,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        regions_dir: Path | None = None,
    ) -> None:
        # What a service KEY looks like on this machine. Profiles are template
        # PNGs and never cross the wire (§2.10): the spec names a service, and
        # resolving it is the monitor's own business - which is why this is a
        # callback rather than a store, so the shell's profile cache stays the
        # shell's.
        self._profile_for = profile_for
        # Where chat regions drawn on THIS machine are remembered (§8, closed by
        # regions.py). ``None`` is the old behaviour exactly - no store, no
        # fallback, no save - which is what every suite that does not care about
        # persistence gets, and what an embedded monitor inside the app binary
        # gets until somebody hands it a directory.
        self._regions_dir = regions_dir
        self._ops = ops if ops is not None else ScreenOps()
        # Injected rather than reached for, because the suites stub the capture
        # at their own call site and because the calibration window will want to
        # capture through the very same seam the poller does.
        self._capture: CaptureFn = capture if capture is not None else self._ops.capture
        self._poll_seconds = poll_seconds
        self._clock = clock
        # -- the tick's bookkeeping --------------------------------------------
        # Covers the generation, the latest tick, the waiter list and the hook
        # lists. Never a capture, never a search, never a hook call (§4.4).
        # Re-entrant because ``stamp`` takes it inside ``feed``'s caller.
        self._tick_lock = threading.RLock()
        # Which poller RUN a tick belongs to: bumped by every ``configure``,
        # stamped into every tick, and compared in ``_deliver``. "The monitor
        # moved" and "a new loop exists" are different events, and only the
        # first one is the question a ghost check is asking - which is why the
        # bump happens in configure even when nothing new starts.
        self._generation = 0
        # Counts every tick this monitor ever produced and never repeats; what
        # ``observe`` arms against.
        self._seq = 0
        self._latest: Tick | None = None
        self._waiters: list[_Waiter] = []
        self._hooks: list[TickHook] = []
        self._frame_hooks: list[FrameHook] = []
        self._clip_hooks: list[ClipHook] = []
        # -- the poller ---------------------------------------------------------
        self._spec: MonitorSpec | None = None
        # The current run's stop flag - replaced per run rather than cleared, so
        # a cancelled loop's flag stays set forever and that loop can only end.
        self._stop = threading.Event()
        self._poller: DetectorPoller | None = None
        # The detector the current run polls through. It is the object that
        # holds the live trackers, so a reset that swaps one has to reach it or
        # the poller keeps folding into the tracker that was replaced.
        self._detector: ScreenDetector | None = None
        self._busy_tracker: PresenceTracker | None = None
        self._idle_tracker: PresenceTracker | None = None
        self._stale_tracker: StaleTracker | None = None
        # -- the two streaks -----------------------------------------------------
        # Consecutive-tick counts, rolled forward as each tick is stamped and
        # carried on it. They live beside the trackers rather than on the
        # detector because they are counts of what the DETECTOR SAID, not of
        # what it saw - and they are reset by exactly what resets a tracker's
        # debounce: a retarget, and the flow's own "forget the frames you have
        # seen" (both of which mean the screen these counts describe is gone).
        self._stale_arm_streak = 0
        self._changed_streak = 0
        # -- the clipboard ------------------------------------------------------
        # The backend is constructed by the shell and only driven here: which
        # clipboard exists is a startup question about the machine. None means a
        # monitor nobody wired one into, and behaves exactly like the manual
        # provider: nothing to poll.
        self._clipboard = clipboard
        # Hashes of what WE put on the clipboard, so the watcher cannot ingest
        # our own outbound back as if it were a reply. The watcher's filter and
        # the writer's register are one object, and this is it - it can be
        # handed in for the one test that has to see both ends at once.
        self._self_writes = self_writes if self_writes is not None else SelfWriteSet()
        self._clip_poll_interval_ms = clip_poll_interval_ms
        self._clip_accepts: Callable[[str], bool] = (
            clip_accepts if clip_accepts is not None else _accept_all
        )
        self._watcher: threading.Thread | None = None
        self._watcher_stop: threading.Event | None = None

    # == lifecycle / configuration =============================================

    async def configure(self, spec: MonitorSpec) -> int:
        """Retarget onto ``spec``; returns the new generation.

        Three acts, and the split matters. Under the lock: end the run that was
        watching the old window, bump the counter, and hand the next run a stop
        flag of its own. That much always happens - a spec with no region, or a
        service this machine has no profile for, still ENDS the previous run and
        still has to invalidate the ticks that run has in flight.

        Outside the lock: resolve the profile (a disk read on a cold cache) and
        compose a detector around it. Neither belongs under a lock the poller
        thread takes once a tick, and neither has to be: the generation is
        already bumped, so anything the old loop pushes from here on is a ghost.

        The loop the old run is still finishing is free to finish it, exactly as
        it was when this was ``retarget_detectors``: its ticks carry the OLD
        stamp, which is the whole point.

        Before any of that, and outside the lock because it is a disk read: the
        region store gets its say (§8). A spec that CARRIES a region is
        authoritative and is what gets remembered; a spec with none falls back to
        whatever was drawn on this machine last time. That is why the store is
        consulted here rather than by the brain - the rectangle is a fact about
        this desktop, and the brain may be on another OS entirely.
        """
        spec = self._remember_region(spec)
        with self._tick_lock:
            self._stop_poller()
            self._generation += 1
            self._stop = threading.Event()
            generation = self._generation
            stop = self._stop
            self._spec = spec
            self._detector = None
            self._busy_tracker = None
            self._idle_tracker = None
            self._stale_tracker = None
            # A streak is a claim about one screen holding still, or one screen
            # churning. Retargeting says the screen is a different screen, so
            # both claims expire with the generation that made them.
            self._stale_arm_streak = 0
            self._changed_streak = 0
        region = spec.region
        if region is None:
            return generation
        profile = self._profile_for(spec.service)
        if profile is None:
            return generation
        detector = build_detector(
            region,
            profile,
            signals=spec.finish_signals,
            required_ticks=required_ticks(spec.stable_seconds, self._poll_seconds),
            tolerance=spec.tolerance,
            matcher=spec.matcher,
            clock=self._clock,
        )
        # Nothing to feed and nothing to look for: a poll loop would be pure
        # cost, so the monitor stays configured and idle (``latest`` never
        # advances) rather than burning a capture every half second.
        if not detector.watching:
            return generation
        self._start(self._compose(detector, region, generation, stop), stop)
        return generation

    @property
    def regions_dir(self) -> Path | None:
        """Where remembered regions are stored, or None for a monitor that
        remembers nothing."""
        return self._regions_dir

    def saved_region(self, service: str) -> ScreenRegion | None:
        """What this machine remembers about where ``service``'s chat is.

        The local-only tier (§3): it never crosses the wire, because the answer
        is either already ON the spec the brain gets back or is a rectangle in
        this desktop's coordinates that the brain has no use for. It exists for
        the calibration window and the Serve panel, which want to say "there is
        a box saved for this service" without configuring anything.
        """
        if self._regions_dir is None:
            return None
        return load_region(self._regions_dir, service)

    def _remember_region(self, spec: MonitorSpec) -> MonitorSpec:
        """Fill a region-less spec from the store, or save the one it carries.

        A read and possibly a write, both on the caller's thread and both
        OUTSIDE ``configure``'s lock - the poller takes that lock once a tick and
        a disk write has no business in it.

        The save is conditional on the value CHANGING, which is not an
        optimisation: ``configure`` is called on every reconnect and on every
        service switch, and a store rewritten each time would be a file whose
        mtime says nothing about when the operator last drew a box.
        """
        if self._regions_dir is None:
            return spec
        if spec.region is None:
            saved = load_region(self._regions_dir, spec.service)
            if saved is None:
                return spec
            return dataclasses.replace(spec, region=saved)
        if load_region(self._regions_dir, spec.service) != spec.region:
            try:
                save_region(self._regions_dir, spec.service, spec.region)
            except OSError:
                # A read-only or full disk costs the operator a redraw after the
                # next reboot. It must not cost them the retarget they asked for.
                _log.warning("could not remember the chat region for %s", spec.service)
        return spec

    async def suspend(self) -> None:
        """Stop polling without bumping the generation.

        The capture overlay is about to own the screen (a sustained large delta
        over the very window the detectors watch, which is what arms the
        auto-copy on staleness alone) and nothing has MOVED - so the ticks the
        interrupted loop is still finishing are honest readings of the same
        window, and dropping them as ghosts would be a lie. Deliberately the
        same call ``stop_detectors`` was, and deliberately no bump.
        """
        with self._tick_lock:
            self._stop_poller()

    async def resume(self) -> None:
        """Poll again under the same configuration; a no-op while polling.

        The other half of :meth:`suspend`, and the half the controller could
        never have: putting the poller back used to mean rebuilding the detector
        around whatever the calibration said now, which was the shell's job.
        Here the detector never went anywhere - only its thread did - so the
        same object, the same generation and a fresh stop flag are the whole of
        a resume.
        """
        with self._tick_lock:
            if self._poller is not None and not self._poller.is_cancelled:
                return
            detector = self._detector
            spec = self._spec
            if detector is None or spec is None or spec.region is None:
                return
            self._stop = threading.Event()
            stop = self._stop
            loop = self._compose(detector, spec.region, self._generation, stop)
        self._start(loop, stop)

    async def close(self) -> None:
        """Stop every thread for good. Idempotent."""
        with self._tick_lock:
            self._stop_poller()
        self._stop_clip_watch()

    # == observation ===========================================================

    @property
    def generation(self) -> int:
        """The stamp the live run's ticks carry."""
        with self._tick_lock:
            return self._generation

    @property
    def latest(self) -> Tick | None:
        """The newest non-ghost tick, or None before the first one. A local read
        by contract (§2.1): free, and never a round trip even when remote."""
        with self._tick_lock:
            return self._latest

    @property
    def spec(self) -> MonitorSpec | None:
        """What this monitor was last configured with - the send-gate budgets a
        consumer measures its own gates in, and the service behind the window."""
        with self._tick_lock:
            return self._spec

    async def observe(self) -> Tick:
        """The next tick captured AFTER this call - never the cached one.

        The bug this exists to stop is reading a tick from before the scroll,
        the click or the paste that was just performed. So the wait is armed
        against the sequence counter as it stands right now, and only a tick
        that came LATER can resolve it; a ghost resolves nothing at all, because
        ``_deliver`` drops it before it ever reaches a waiter (§4.2). A parked
        observe therefore survives a ``configure`` and is answered by the first
        tick of the new run - which is what a recipe re-running after a retarget
        wants.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Tick] = loop.create_future()
        with self._tick_lock:
            waiter = _Waiter(self._seq, loop, future)
            self._waiters.append(waiter)
        return await future

    def subscribe(self, hook: TickHook) -> Callable[[], None]:
        """Every non-ghost tick, as it lands; returns the unsubscribe.

        Called on the POLLER thread and must not block - it is the seam the
        GUI's live detection panel and the automation's own bookkeeping hang
        off, and both of those are supposed to enqueue rather than work.
        """
        with self._tick_lock:
            self._hooks.append(hook)

        def unsubscribe() -> None:
            with self._tick_lock:
                if hook in self._hooks:
                    self._hooks.remove(hook)

        return unsubscribe

    def on_clip(self, hook: ClipHook) -> Callable[[], None]:
        """Every clipboard change the watcher accepts (never our own writes);
        returns the unsubscribe. Called on the WATCHER thread."""
        with self._tick_lock:
            self._clip_hooks.append(hook)

        def unsubscribe() -> None:
            with self._tick_lock:
                if hook in self._clip_hooks:
                    self._clip_hooks.remove(hook)

        return unsubscribe

    # == the poll loop =========================================================

    @property
    def poller(self) -> DetectorPoller | None:
        """The running poller, or None. For a shell that mirrors the run in its
        own chrome, and for a test that joins the thread."""
        return self._poller

    def _compose(
        self,
        detector: ScreenDetector,
        region: ScreenRegion,
        generation: int,
        stop: threading.Event,
    ) -> Callable[[], None]:
        """Compose one run's poll loop, ready to be handed to a thread.

        ONE capture per tick, handed to ONE detector. That is not only cheaper:
        every verdict then describes the same instant of a moving screen rather
        than four moments of it, and a failed capture reaches all of them as the
        same ERROR instead of some seeing a frame and others not.

        Everything is read once, here: the region, the detector, the cadence and
        the stamp all describe the window this run was started for, and a run
        that re-read them mid-flight would drift onto another one.

        The detector is REMEMBERED as well as closed over, because
        :meth:`reset_trackers` swaps a tracker rather than clearing it in place
        and the object that has to end up holding the replacement is this one -
        the poller reads its trackers through ``detector.busy``/``.idle``/
        ``.stale`` every tick.
        """
        capture = self._capture
        poll_seconds = self._poll_seconds
        with self._tick_lock:
            self._detector = detector
            self._busy_tracker = detector.busy
            self._idle_tracker = detector.idle
            self._stale_tracker = detector.stale

        def loop() -> None:
            while not stop.is_set():
                try:
                    scene: RegionImage | None = capture(region)
                except CaptureError:
                    scene = None  # every detector hears about it the same way
                snapshot = detector.observe(scene)
                # Rolled BEFORE the tick is built, because the tick carries the
                # counts as of itself: a tick that reports the third big delta
                # says three, not two.
                arm_streak, changed_streak = self._roll_streaks(
                    generation, snapshot, detector.active_detectors
                )
                # Locations, not pixels (§2.2). A sighting knows where it was in
                # the FRAME; the tick says where it is on the real screen, which
                # is the only half a brain on another machine could use.
                self._deliver(
                    Tick(
                        seq=self._next_seq(),
                        generation=generation,
                        at=snapshot.at,
                        captured=snapshot.captured,
                        busy=snapshot.busy,
                        idle=snapshot.idle,
                        stale=snapshot.stale,
                        sightings={
                            kind: (None if sighting is None else sighting.rect(region))
                            for kind, sighting in snapshot.sightings.items()
                        },
                        active_detectors=detector.active_detectors,
                        stale_arm_streak=arm_streak,
                        changed_streak=changed_streak,
                    )
                )
                # The pictures, after the verdicts they illustrate, out of the
                # very frame the matches were verified against - and cut on the
                # thread that captured it, by whoever asked for them. A failed
                # capture recognised nothing and says nothing: an empty map
                # would blank rows a dropped frame is no evidence about.
                if scene is not None and snapshot.sightings:
                    self._deliver_frame(scene, snapshot.sightings)
                # Sleep in short increments so cancellation lands promptly.
                remaining = poll_seconds
                while remaining > 0 and not stop.is_set():
                    step = min(0.05, remaining)
                    time.sleep(step)
                    remaining -= step

        return loop

    def _start(self, loop: Callable[[], None], stop: threading.Event) -> DetectorPoller:
        """Run a composed loop on a fresh ``agentclip-detector`` thread.

        Daemon, like the watcher: an exit must never wait on a poll interval.
        The loop shares the run's stop flag, so starting one composed for a run
        that has since ended hands back a poller that dies on its first check.
        """
        thread = threading.Thread(target=loop, name="agentclip-detector", daemon=True)
        poller = DetectorPoller(thread, stop)
        self._poller = poller
        thread.start()
        return poller

    def _stop_poller(self) -> None:
        """End the running poller, without waiting for it.

        Deliberately no join: the caller is the event loop thread and the loop
        only notices between ticks, so joining would freeze the interface for up
        to a poll interval. Dropping the handle makes "stopped" true immediately
        for everything that asks, and the tick the thread it leaves is still
        finishing is filtered by its stamp, not by its thread.
        """
        if self._poller is not None:
            self._poller.cancel()
        self._poller = None

    def _next_seq(self) -> int:
        with self._tick_lock:
            self._seq += 1
            return self._seq

    def _roll_streaks(
        self,
        generation: int,
        snapshot: DetectionSnapshot,
        active_detectors: tuple[str, ...],
    ) -> tuple[int, int]:
        """Fold one observation into the two consecutive-tick counts.

        Under the lock because two threads reach these ints - the poller rolls
        them, and ``configure``/``reset_trackers`` zero them from the event loop
        - and it is bookkeeping, which is exactly what the lock is for (§4.4);
        the arithmetic itself is three comparisons.

        A run whose generation has been retargeted away rolls NOTHING and is
        told the current counts: its tick is a ghost that will be dropped a
        moment later (§4.2), and letting it advance a count on its way out would
        put a dead screen's evidence on the live screen's tally.
        """
        with self._tick_lock:
            if generation != self._generation:
                return self._stale_arm_streak, self._changed_streak
            min_diff = self._spec.send_arm_min_diff if self._spec is not None else 0.0
            self._stale_arm_streak = roll_arm_streak(
                self._stale_arm_streak, snapshot.stale, min_diff=min_diff
            )
            self._changed_streak = roll_changed_streak(
                self._changed_streak,
                busy=snapshot.busy,
                idle=snapshot.idle,
                stale=snapshot.stale,
                active_detectors=active_detectors,
            )
            return self._stale_arm_streak, self._changed_streak

    def _deliver(self, tick: Tick) -> None:
        """Publish one tick: ghost check, bookkeeping, then everyone else.

        The lock covers the ghost check AND the bookkeeping it guards, so a
        configure cannot land between them and leave the tick half-belonging to
        each run (§4.2). It covers nothing after that: the waiters are resolved
        through their own loop and the hooks are called on this thread, and a
        hook that blocked under the lock would stall the configure trying to
        retarget away from it (§4.4).

        A hook that RAISES is not allowed to kill the poller. Losing every tick
        after the first bad paint would look exactly like a frozen screen, so
        the exception is logged and the next hook still runs.
        """
        with self._tick_lock:
            if tick.generation != self._generation:
                return
            self._latest = tick
            due = [waiter for waiter in self._waiters if waiter.armed < tick.seq]
            if due:
                self._waiters = [waiter for waiter in self._waiters if waiter.armed >= tick.seq]
            hooks = tuple(self._hooks)
        for waiter in due:
            waiter.resolve(tick)
        for hook in hooks:
            try:
                hook(tick)
            except Exception:  # noqa: BLE001 - one bad subscriber, not one dead poller
                _log.exception("tick subscriber raised")

    def _deliver_frame(
        self, scene: RegionImage, sightings: Mapping[TemplateKind, Sighting | None]
    ) -> None:
        with self._tick_lock:
            hooks = tuple(self._frame_hooks)
        for hook in hooks:
            try:
                hook(scene, sightings)
            except Exception:  # noqa: BLE001 - same contract as a tick subscriber
                _log.exception("frame subscriber raised")

    # == the local-only tier ===================================================
    # Never on the wire (§2.2/§3): pixels, tracker identities and the OS adapter
    # itself. Used by the ELEMENTS panel's crops today and by the calibration
    # window from §6.4 on - both of which run where the pixels are.

    @property
    def ops(self) -> ScreenOps:
        """The monitor's hand on the machine, for a caller that needs a call the
        Protocol does not carry (a template search, a hover beat)."""
        return self._ops

    @property
    def detector(self) -> ScreenDetector | None:
        """The live detector, or None when nothing is configured or nothing was
        worth watching."""
        return self._detector

    def capture(self, region: ScreenRegion) -> RegionImage:
        """One frame of ``region``, through the same seam the poller captures
        with. Raises ``CaptureError`` like the primitive does."""
        return self._capture(region)

    def on_frame(self, hook: FrameHook) -> Callable[[], None]:
        """Every captured frame that recognised something, with its sightings.

        The pixel half of a tick, kept off the Protocol on purpose: this is how
        the ELEMENTS column cuts its crops, and a crop is a calibration surface
        that runs where the pixels are. Called on the POLLER thread, right after
        the tick the frame produced.
        """
        with self._tick_lock:
            self._frame_hooks.append(hook)

        def unsubscribe() -> None:
            with self._tick_lock:
                if hook in self._frame_hooks:
                    self._frame_hooks.remove(hook)

        return unsubscribe

    @property
    def busy_tracker(self) -> PresenceTracker | None:
        """The current run's busy tracker, or None."""
        return self._busy_tracker

    @property
    def idle_tracker(self) -> PresenceTracker | None:
        """The current run's idle tracker, or None."""
        return self._idle_tracker

    @property
    def stale_tracker(self) -> StaleTracker | None:
        """The current run's staleness tracker, or None."""
        return self._stale_tracker

    def reset_trackers(self) -> None:
        """Make every live tracker forget the frames it has seen.

        The debounce only, never the verdicts: what the trackers hold is a
        streak and a previous frame, and every caller (the paste, the send, the
        auto-copy flow) has just PRODUCED the frames behind that streak itself.

        **Swap, not clear** (§4.3). Every caller is on the event loop thread and
        the tracker being cleared is being polled on the detector thread, which
        reads the streak, spends a template search or a frame diff, and writes
        the streak back - so an in-place ``reset()`` landing inside that search
        is undone by the write that follows it, and the frames the flow produced
        stay in history. That is the one thing ``_tick_lock`` cannot fix at its
        own grain: the expensive halves of a tick are deliberately OUTSIDE it.
        So each tracker is replaced by a ``fresh()`` one of the same
        calibration, and the poll still in flight folds its frame into an object
        nobody will read again.

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
            # The streaks go with the debounce, for the same reason and by the
            # same argument: they are counts of what those trackers said about
            # frames the caller has just produced ITSELF - a paste, a send, the
            # auto-copy flow's own scrolling. A count that survived the reset
            # would be the flow's own mouse work, still being counted as the
            # model's.
            self._stale_arm_streak = 0
            self._changed_streak = 0

    @property
    def stale_arm_streak(self) -> int:
        """Consecutive ticks whose stale probe reported a big change. The live
        count; every tick carries its own value of it."""
        with self._tick_lock:
            return self._stale_arm_streak

    @property
    def changed_streak(self) -> int:
        """Consecutive ticks on which every active detector said "finished"."""
        with self._tick_lock:
            return self._changed_streak

    # -- the test seam ---------------------------------------------------------

    def stamp(
        self,
        *,
        captured: bool = True,
        busy: BusyProbe | None = None,
        idle: BusyProbe | None = None,
        stale: StaleProbe | None = None,
        sightings: Mapping[TemplateKind, ScreenRegion | None] | None = None,
        active_detectors: tuple[str, ...] = (),
        stale_arm_streak: int = 0,
        changed_streak: int = 0,
        generation: int | None = None,
        at: float | None = None,
    ) -> Tick:
        """Build a tick as the poll loop would have stamped it.

        The three fields nobody writing a test wants to keep straight - the
        sequence number, the run stamp and the clock - filled in from the live
        monitor, so a caller only spells out the reading it cares about. Pass
        ``generation`` explicitly to speak as a run that has been retargeted
        away, which is how a ghost is written.
        """
        with self._tick_lock:
            return Tick(
                seq=self._next_seq(),
                generation=self._generation if generation is None else generation,
                at=self._clock() if at is None else at,
                captured=captured,
                busy=busy,
                idle=idle,
                stale=stale,
                sightings=dict(sightings or {}),
                active_detectors=active_detectors,
                stale_arm_streak=stale_arm_streak,
                changed_streak=changed_streak,
            )

    def feed(self, tick: Tick) -> None:
        """Deliver one tick exactly as the poller would. The suites' one door.

        There is no probe to inject any more - the loop builds a whole tick and
        publishes it - so this is what ``feed_probe`` was: one reading, stamped
        with the run it belongs to, taking the same ghost check and reaching the
        same subscribers.
        """
        self._deliver(tick)

    # == the clipboard =========================================================

    @property
    def self_writes(self) -> SelfWriteSet:
        """Hashes of every clipboard write this monitor made. The watcher's
        filter and the delivery's register are one object, and this is it."""
        return self._self_writes

    @property
    def clipboard_kind(self) -> str | None:
        """Which backend is behind the clipboard verbs: the provider's name,
        ``"manual"`` for the write-only sentinel, None when the monitor was
        wired up without one at all."""
        return None if self._clipboard is None else self._clipboard.name

    async def read_clipboard(self) -> str | None:
        """The provider's text, or None - which is also what a monitor wired up
        without a provider says, because "nothing to read" is the same answer."""
        if self._clipboard is None:
            return None
        return self._clipboard.read_text()

    async def write_clipboard(self, text: str) -> None:
        """Put ``text`` on the clipboard and register it as OUR write, so the
        watcher polling the very same clipboard cannot ingest our own outbound
        back as if it were a reply (``clip.watcher.write_via``).

        Raises ``ClipboardUnavailable`` with no provider, exactly as the write
        through a backend that cannot write does, and into the same branch.
        """
        if self._clipboard is None:
            raise ClipboardUnavailable("no clipboard provider")
        write_via(self._clipboard, self._self_writes, text)

    def watch_clipboard(self, on: bool) -> bool:
        """Start or stop the watcher; returns whether one is polling now.

        Sync, alone among the verbs around it, because every caller is - and
        idempotent in both directions. No arming check of its own: whether the
        machine may be watched at all is policy, and policy stays in the brain
        (§2.3).

        ``False`` for an ``on=True`` there is nothing to honour. Two ways that
        happens and the caller can tell them apart through
        :attr:`clipboard_kind`: no backend at all (a monitor nobody wired one
        into - the headless tests), or the "manual" sentinel, which is what
        "no backend works" looks like to a user who copies and pastes by hand.
        There is nothing a poll could ever see through either.
        """
        if not on:
            self._stop_clip_watch()
            return False
        if self._watcher is not None:
            return True
        if self._clipboard is None or self._clipboard.name == "manual":
            return False
        stop = threading.Event()
        provider = self._clipboard
        interval = self._clip_poll_interval_ms
        accepts = self._clip_accepts
        self_writes = self._self_writes

        def loop() -> None:
            watch(
                provider,
                interval,
                should_stop=stop.is_set,
                accepts=accepts,
                on_capture=self._deliver_clip,
                self_writes=self_writes,
            )

        thread = threading.Thread(target=loop, name="agentclip-clipwatch", daemon=True)
        self._watcher = thread
        self._watcher_stop = stop
        thread.start()
        return True

    def _stop_clip_watch(self) -> None:
        """Stop watching, without waiting for it.

        Deliberately no join: the caller is the UI thread and the loop only
        notices between ticks, so joining would freeze the interface for up to a
        poll interval. Dropping the handles makes "stopped" true immediately for
        everything that asks, and a capture that lands in the window the last
        tick leaves open is a real capture the user really made.
        """
        if self._watcher_stop is not None:
            self._watcher_stop.set()
        self._watcher = None
        self._watcher_stop = None

    def _deliver_clip(self, text: str) -> None:
        """Fan one accepted capture out, on the watcher thread. Same contract as
        a tick subscriber: one hook that raises is not the end of the watch."""
        with self._tick_lock:
            hooks = tuple(self._clip_hooks)
        for hook in hooks:
            try:
                hook(text)
            except Exception:  # noqa: BLE001 - one bad subscriber, not one dead watcher
                _log.exception("clipboard subscriber raised")

    # == actions ===============================================================
    # Coroutines because the Protocol's are - the remote implementation's every
    # one of these is a round trip. Locally they are the synchronous ScreenOps
    # calls they always were, awaited without a thread hop: each is a single
    # user32 call plus at most its own settle, and a hop would buy nothing but a
    # context switch.

    async def focus_window(self, handle: int) -> bool:
        return self._ops.focus_window(handle)

    async def foreground_window(self) -> int | None:
        return self._ops.foreground_window()

    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        return self._ops.click(region, settle_s=settle_s)

    async def move_cursor(self, x: int, y: int) -> bool:
        return self._ops.move_cursor(x, y)

    async def scroll(self, region: ScreenRegion, detents: int) -> bool:
        return self._ops.scroll(region, detents)

    async def scroll_key(self, key: str, taps: int = 1) -> bool:
        return self._ops.scroll_key(key, taps)

    async def send_paste(self) -> bool:
        return self._ops.send_paste()

    async def send_enter(self) -> bool:
        return self._ops.send_enter()

    # == the pixel verdicts ====================================================
    # §2.3's half of ``click_profile_element`` and everything around it: capture
    # the configured region, search it with the configured profile, answer about
    # what is on it. The brain names a KIND and nothing else - not a template,
    # not a rectangle, not a tolerance - because none of those could cross the
    # wire, and because a caller that had to supply them would be a caller
    # keeping its own copy of the calibration.
    #
    # **The search runs off the event loop.** Each of these is a full-region
    # pixel comparison (and ``hover_scan`` is that plus a real cursor walk with
    # a settle at every stop, seconds of it), so the blocking half sits in a
    # ``_*_now`` method and the verb awaits it through ``asyncio.to_thread`` -
    # exactly where the controller put it before these moved. No thread this
    # object owns: the poller's is still the only one.
    #
    # **Never raises.** No spec, no region, no profile, a capture that failed,
    # nothing captured for the kind - all the same answer, because a caller that
    # may not click either way has the same next move for every one of them.

    def _search_context(self) -> tuple[MonitorSpec, ScreenRegion, ServiceProfile] | None:
        """The spec, the region and the profile the verbs answer against, or
        None when any of the three is missing.

        Read together and once per call, so a configure landing mid-verb cannot
        leave a search hunting one service's appearance inside another's
        rectangle. The profile is resolved per call rather than cached at
        configure time because it is EDITED while the app runs (the calibration
        window captures into it), and a verb that answered from a stale copy
        would ignore the capture the user just took to fix it.
        """
        with self._tick_lock:
            spec = self._spec
        if spec is None or spec.region is None:
            return None
        profile = self._profile_for(spec.service)
        if profile is None:
            return None
        return spec, spec.region, profile

    @staticmethod
    def _matcher(spec: MonitorSpec) -> CandidateSource:
        """How this service wants its appearances hunted for.

        The same pair (``spec.tolerance`` and this) the detector was built with,
        because a verb searching by another ruler than the poller would have the
        ELEMENTS column and the thing about to be clicked disagreeing about the
        same frame.
        """
        return select_matcher(spec.matcher).origins

    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]:
        ctx = self._search_context()
        if ctx is None:
            return ()
        spec, region, profile = ctx
        templates = profile.variants(kind)
        if not templates:
            return ()
        return await asyncio.to_thread(self._find_all_now, spec, region, kind, templates)

    def _find_all_now(
        self,
        spec: MonitorSpec,
        region: ScreenRegion,
        kind: TemplateKind,
        templates: tuple[Template, ...],
    ) -> tuple[ScreenRegion, ...]:
        try:
            scene = self._capture(region)
        except CaptureError:
            return ()
        return tuple(
            element_rects(
                self._ops.all_matches,
                templates,
                scene,
                region,
                max_diff=kind.max_diff,
                tolerance=spec.tolerance,
                matcher=self._matcher(spec),
            )
        )

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        ctx = self._search_context()
        if ctx is None:
            return Located(None, False, None)
        spec, region, profile = ctx
        templates = profile.variants(kind)
        if not templates:
            return Located(None, False, None)
        return await asyncio.to_thread(
            self._locate_now, spec, region, profile, kind, templates, exclude_kinds
        )

    def _locate_now(
        self,
        spec: MonitorSpec,
        region: ScreenRegion,
        profile: ServiceProfile,
        kind: TemplateKind,
        templates: tuple[Template, ...],
        exclude_kinds: tuple[TemplateKind, ...],
    ) -> Located:
        """One capture, and up to two searches of it.

        The lowest-match search comes FIRST and always, because it is the one
        that answers both halves of a miss - not there, and how close - and a
        miss is the expensive path this must not double: the auto-copy's hunt
        re-snaps and re-searches three times over, and every one of those rounds
        is a full-region comparison.

        The second search only ever runs on a HIT, and only to answer "were
        there several?". Nothing found is not ambiguous, so there is nothing to
        ask; and a caller holding a rectangle is about to click it, which is
        precisely when the number matters.
        """
        try:
            scene = self._capture(region)
        except CaptureError:
            return Located(None, False, None)
        matcher = self._matcher(spec)
        found, best_miss = lowest_match_scored(
            self._ops.lowest_match,
            templates,
            scene,
            max_diff=kind.max_diff,
            tolerance=spec.tolerance,
            matcher=matcher,
        )
        if found is None:
            return Located(None, False, best_miss)
        rect = match_rect(region, found[0], found[1])
        for other in exclude_kinds:
            other_templates = profile.variants(other)
            if not other_templates:
                continue
            forbidden = element_rects(
                self._ops.all_matches,
                other_templates,
                scene,
                region,
                max_diff=other.max_diff,
                tolerance=spec.tolerance,
                matcher=matcher,
            )
            if any(same_element(rect, taken) for taken in forbidden):
                # It IS on screen - it is just not this kind's element. Reported
                # as a miss rather than as an ambiguity because there is nothing
                # a redrawn window would fix.
                return Located(None, False, best_miss)
        rects = element_rects(
            self._ops.all_matches,
            templates,
            scene,
            region,
            max_diff=kind.max_diff,
            tolerance=spec.tolerance,
            matcher=matcher,
        )
        return Located(rect, len(rects) > 1, None)

    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick:
        ctx = self._search_context()
        if ctx is None:
            return ElementClick.MISMATCH
        spec, region, profile = ctx
        templates = profile.variants(kind)
        if not templates:
            return ElementClick.MISMATCH
        # ``_locate_now`` rather than ``locate``, so the search and the click
        # that follows it are aimed by ONE reading of the configuration: a
        # retarget landing between the two would otherwise have this pressing a
        # pixel found in the window the automation has just left.
        located = await asyncio.to_thread(
            self._locate_now, spec, region, profile, kind, templates, ()
        )
        if located.region is None:
            return ElementClick.MISMATCH
        if located.ambiguous:
            # Two of them under one drawn region is two conversations, and
            # picking one is a coin toss whose loser is a chat that gets clicked
            # on behalf of the other.
            return ElementClick.AMBIGUOUS
        target = click_point_region(located.region, *profile.click_point(kind))
        settle = ELEMENT_CLICK_SETTLE_S if settle_s is None else settle_s
        clicked = await self.click(target, settle_s=settle)
        return ElementClick.CLICKED if clicked else ElementClick.NOT_CLICKED

    async def hover_scan(self, kind: TemplateKind) -> ScreenRegion | None:
        ctx = self._search_context()
        if ctx is None:
            return None
        spec, region, profile = ctx
        templates = profile.variants(kind)
        if not templates:
            return None
        return await asyncio.to_thread(self._hover_scan_now, spec, region, kind, templates)

    def _hover_scan_now(
        self,
        spec: MonitorSpec,
        region: ScreenRegion,
        kind: TemplateKind,
        templates: tuple[Template, ...],
    ) -> ScreenRegion | None:
        """Move, settle, capture, search - and stop at the first frame it is on.

        Blocking by design: three OS calls and a pause per stop. The pause is
        read through ``ScreenOps`` rather than taken from a constant because a
        suite shrinks it, and a file full of scans that each waited out a real
        browser repaint is a file nobody runs.

        Any failure ends the scan: a cursor move the platform refused, or a
        capture that did not come back, means the scan cannot SEE - which is not
        the same fact as "it is not there" but has the same consequence, and
        pretending to keep looking would spend the rest of the walk moving the
        user's mouse for nothing.
        """
        matcher = self._matcher(spec)
        for x, y in hover_scan_points(region):
            if not self._ops.move_cursor(x, y):
                return None
            time.sleep(self._ops.hover_step_delay())
            try:
                scene = self._capture(region)
            except CaptureError:
                return None
            found = lowest_match(
                self._ops.lowest_match,
                templates,
                scene,
                max_diff=kind.max_diff,
                tolerance=spec.tolerance,
                matcher=matcher,
            )
            if found is not None:
                return match_rect(region, found[0], found[1])
        return None

    async def snap_to_bottom(self, action: str) -> None:
        with self._tick_lock:
            spec = self._spec
        region = None if spec is None else spec.region
        if action == SCROLL_PAGE_DOWN:
            await self.scroll_key("page_down", PAGE_DOWN_TAPS)
        elif action == SCROLL_END:
            await self.scroll_key("end")
        elif region is not None:
            # The wheel is the only one of the three that has to be AIMED - a
            # scroll key goes to whatever holds focus, a wheel detent goes where
            # the pointer is - so it is the only one a monitor with no region
            # drawn cannot do at all.
            await self.scroll(region, SNAP_WHEEL_DETENTS)


def _conforms(monitor: LocalUIMonitor) -> UIMonitor:
    """Structural pin: mypy fails HERE if this class drifts from the Protocol.

    The tests are not type-checked, and a Protocol nothing declares is a
    Protocol nothing enforces - so the check lives in the module it is about.
    Costs one function that is never called; buys a red type-check the day a
    signature on either side of the seam changes without the other.
    """
    return monitor
