"""An in-memory :class:`~agentclip.driver.monitor.protocol.UIMonitor` for suites
that drive the brain without a screen.

The sibling of ``driver/clip/fake.py`` and the same bargain. Everything the
controller asks the machine now goes through one object, so a suite that hands
it *this* one gets the whole automation core in microseconds: no poll thread,
no template search, no mouse. Ticks are pushed by hand (:meth:`FakeUIMonitor.feed`,
with :meth:`FakeUIMonitor.make_tick` to write one in a line) - that is the
``tick_feed`` seam docs/design/ui-monitor.md §6.1 names as ``feed_probe``'s
replacement, and it is where the ghost filter is enforced: a tick stamped with
a generation the last :meth:`FakeUIMonitor.configure` has moved past reaches
nobody.

It lives in ``src`` rather than in ``tests`` for the reason ``clip/fake.py``
does: two test packages (``tests/driver/automation`` and, once the shells are
rewired, ``tests/shell/*``) drive the same seam, and a double that only one of
them can import is a double the other one reimplements.

Three things it is deliberately NOT strict about, because the suites that use
it are about the brain and not about the machine:

* **Actions delegate.** Every verb records ``(name, args)`` on :attr:`calls`
  and then, unless :attr:`answers` scripts a reply for it, calls straight
  through to :attr:`ops`. That keeps working every suite that substitutes the
  machine the way it always did - a :class:`~agentclip.driver.monitor.ops.ScreenOps`
  subclass handed in here, or a monkeypatch of
  ``agentclip.driver.monitor.ops``' own names.
* **The clipboard is real enough.** With no provider it is a string in memory;
  hand one in (``FakeClipboard``) and reads and writes go through it, self-write
  registration included. ``has_clipboard=False`` is the third case: the machine
  that has no clipboard at all, which raises
  :class:`~agentclip.driver.clip.base.ClipboardUnavailable` exactly as the real
  one does.
* **The local-only tier is here too** (§3): the trackers, the detector behind
  them, ``capture`` and the frame hook. Those never cross the wire, and this
  phase's controller still reaches for them - see
  :class:`~agentclip.driver.automation.controller.MonitorLike`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable, Mapping
from typing import TypeVar, cast

from agentclip.driver.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.driver.clip.watcher import SelfWriteSet, write_via
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
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.detector import ScreenDetector, Sighting
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.stale import StaleProbe, StaleTracker

# One frame and what was recognised in it, for the panel that draws crops. The
# local-only tier's hook (§2.2: pixels are a calibration surface, so they never
# ride the wire) - and the reason the ELEMENTS column is fed beside the tick
# rather than out of it.
FrameHook = Callable[[RegionImage, Mapping[TemplateKind, Sighting | None]], None]

T = TypeVar("T")


class FakeUIMonitor:
    """A ``UIMonitor`` made of lists. Nothing runs; everything is recorded."""

    def __init__(
        self,
        *,
        ops: ScreenOps | None = None,
        clipboard: ClipboardProvider | None = None,
        has_clipboard: bool = True,
        self_writes: SelfWriteSet | None = None,
    ) -> None:
        # The OS adapter every action falls through to. A real one by default,
        # because that is what the suites monkeypatch at ITS use site.
        self.ops = ops if ops is not None else ScreenOps()
        self.clipboard = clipboard
        self.has_clipboard = has_clipboard or clipboard is not None
        self.self_writes = self_writes if self_writes is not None else SelfWriteSet()
        # -- configuration ----------------------------------------------------
        self.spec: MonitorSpec | None = None
        self.specs: list[MonitorSpec] = []
        # What the real monitor's region store would hold (regions.py): every
        # region this fake was ever configured with, by service key. RECORDED
        # rather than acted on - the fake never fills a region-less spec from it,
        # because a suite that hands over ``region=None`` is saying "nothing is
        # calibrated" and a double that quietly disagreed would hide the branch
        # under test. It is here so a caller can assert the save happened.
        self.saved_regions: dict[str, ScreenRegion] = {}
        # ...unless a suite says otherwise. Opt-in mirror of the real store rule
        # (local.py:_remember_region), for the one story that needs the double
        # to BEHAVE like a far monitor: the brain has no box, the monitor's
        # machine remembers one, and ``configured_region`` hands it back.
        self.fills_from_store = False
        # THE generation: bumped by every ``configure``, stamped into ticks,
        # and what makes one a ghost. Public because a test writes scenarios in
        # terms of it ("...and now a delegation starts").
        self.generations = 0
        # -- observation -------------------------------------------------------
        self.seq = 0
        self.ticks: list[Tick] = []
        self.ghosts: list[Tick] = []
        self._latest: Tick | None = None
        self._tick_hooks: list[TickHook] = []
        self._clip_hooks: list[ClipHook] = []
        self._frame_hooks: list[FrameHook] = []
        self._waiters: list[asyncio.Future[Tick]] = []
        # -- actions -----------------------------------------------------------
        # Every verb, in the order it was asked for, and the scripted replies
        # that override the fall-through to ``ops``.
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.answers: dict[str, object] = {}
        self.written: list[str] = []
        self.clipboard_text: str | None = None
        self.watching = False
        # -- lifecycle ---------------------------------------------------------
        self.suspends = 0
        self.resumes = 0
        self.closed = False
        # -- the local-only tier (§3) ------------------------------------------
        self.frames: list[RegionImage] = []
        self.detector: ScreenDetector | None = None
        self.busy_tracker: PresenceTracker | None = None
        self.idle_tracker: PresenceTracker | None = None
        self.stale_tracker: StaleTracker | None = None
        self.resets = 0

    # == lifecycle / configuration ============================================

    async def configure(self, spec: MonitorSpec) -> int:
        """Retarget, exactly as the real one does: record the spec, bump the
        generation, and every tick taken before this instant is a ghost."""
        return self.retarget(spec)

    def retarget(self, spec: MonitorSpec | None = None) -> int:
        """What ``configure`` does, minus the ``await`` - so a synchronous
        scenario can say "and now the automation moved to another window"
        without an event loop. Test-only; nothing in ``src`` calls it."""
        if spec is not None:
            # The real one's store rule (local.py:_remember_region), in one
            # line each way: a spec with a box is remembered, a spec without
            # one is filled from what was remembered.
            if spec.region is not None:
                self.saved_regions[spec.service] = spec.region
            elif self.fills_from_store and spec.service in self.saved_regions:
                spec = dataclasses.replace(spec, region=self.saved_regions[spec.service])
            self.spec = spec
            self.specs.append(spec)
        self.generations += 1
        return self.generations

    def saved_region(self, service: str) -> ScreenRegion | None:
        """The local tier's region-store read, answered from :attr:`saved_regions`."""
        return self.saved_regions.get(service)

    async def configured_region(self) -> ScreenRegion | None:
        self.calls.append(("configured_region", ()))
        return self.spec.region if self.spec is not None else None

    async def suspend(self) -> None:
        self.suspends += 1
        self.calls.append(("suspend", ()))

    async def resume(self) -> None:
        self.resumes += 1
        self.calls.append(("resume", ()))

    async def close(self) -> None:
        self.closed = True
        self.watching = False
        self.calls.append(("close", ()))

    # == observation ==========================================================

    @property
    def generation(self) -> int:
        return self.generations

    @property
    def latest(self) -> Tick | None:
        return self._latest

    async def observe(self) -> Tick:
        """The next tick :meth:`feed` delivers - never the cached one."""
        waiter: asyncio.Future[Tick] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        return await waiter

    def subscribe(self, hook: TickHook) -> Callable[[], None]:
        self._tick_hooks.append(hook)

        def drop() -> None:
            if hook in self._tick_hooks:
                self._tick_hooks.remove(hook)

        return drop

    def on_clip(self, hook: ClipHook) -> Callable[[], None]:
        self._clip_hooks.append(hook)

        def drop() -> None:
            if hook in self._clip_hooks:
                self._clip_hooks.remove(hook)

        return drop

    # -- the tick feed (the test seam ``feed_probe`` became) ------------------

    def make_tick(
        self,
        *,
        seq: int | None = None,
        generation: int | None = None,
        at: float | None = None,
        captured: bool = True,
        busy: BusyProbe | None = None,
        idle: BusyProbe | None = None,
        stale: StaleProbe | None = None,
        sightings: Mapping[TemplateKind, ScreenRegion | None] | None = None,
        active_detectors: tuple[str, ...] | None = None,
        stale_arm_streak: int = 0,
        changed_streak: int = 0,
    ) -> Tick:
        """One tick, with everything a caller did not care about filled in.

        The defaults are the quiet ones: captured, no probe of any kind, nothing
        searched for, neither streak running. ``active_detectors`` follows the
        last spec's ``finish_signals``, so a configured monitor's ticks say what
        they run without every call site repeating it.

        The two streaks are written OUT rather than counted here: the real
        monitor rolls them from the probes it is stamping, and a double that
        recomputed them would be a suite asserting against a second
        implementation of the thing under test. A scenario says "and this is the
        third big delta" by saying three.
        """
        return Tick(
            seq=self.seq + 1 if seq is None else seq,
            generation=self.generations if generation is None else generation,
            at=time.monotonic() if at is None else at,
            captured=captured,
            busy=busy,
            idle=idle,
            stale=stale,
            sightings=dict(sightings or {}),
            active_detectors=(
                (self.spec.finish_signals if self.spec is not None else ())
                if active_detectors is None
                else active_detectors
            ),
            stale_arm_streak=stale_arm_streak,
            changed_streak=changed_streak,
        )

    # The name ``LocalUIMonitor`` gives the same seam, so one test helper can be
    # pointed at either double without knowing which it has.
    stamp = make_tick

    def feed(self, tick: Tick) -> None:
        """Deliver one tick to ``latest``, the subscribers and any pending
        :meth:`observe` - unless it is a ghost, in which case it reaches none of
        them and lands on :attr:`ghosts` instead (§4.2)."""
        if tick.generation != self.generations:
            self.ghosts.append(tick)
            return
        self.seq = max(self.seq, tick.seq)
        self.ticks.append(tick)
        self._latest = tick
        for hook in list(self._tick_hooks):
            hook(tick)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(tick)

    def push_clip(self, text: str) -> None:
        """One clipboard change the watcher accepted, as the watcher would."""
        for hook in list(self._clip_hooks):
            hook(text)

    # == actions ==============================================================

    def _answer(self, verb: str, args: tuple[object, ...], fallback: Callable[[], T]) -> T:
        self.calls.append((verb, args))
        if verb in self.answers:
            return cast("T", self.answers[verb])
        return fallback()

    async def focus_window(self, handle: int) -> bool:
        return self._answer("focus_window", (handle,), lambda: self.ops.focus_window(handle))

    async def foreground_window(self) -> int | None:
        return self._answer("foreground_window", (), self.ops.foreground_window)

    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        return self._answer(
            "click", (region, settle_s), lambda: self.ops.click(region, settle_s=settle_s)
        )

    async def move_cursor(self, x: int, y: int) -> bool:
        return self._answer("move_cursor", (x, y), lambda: self.ops.move_cursor(x, y))

    async def scroll(self, region: ScreenRegion, detents: int) -> bool:
        return self._answer("scroll", (region, detents), lambda: self.ops.scroll(region, detents))

    async def scroll_key(self, key: str, taps: int = 1) -> bool:
        return self._answer("scroll_key", (key, taps), lambda: self.ops.scroll_key(key, taps))

    async def send_paste(self) -> bool:
        return self._answer("send_paste", (), self.ops.send_paste)

    async def send_enter(self) -> bool:
        return self._answer("send_enter", (), self.ops.send_enter)

    # -- the pixel verdicts ----------------------------------------------------
    # No screen, so no fall-through: each one records the ask and hands back
    # either what :attr:`answers` scripts or the "there is nothing there"
    # answer - which is the same answer the real monitor gives for a machine
    # with no region drawn, so a brain driven by this double takes the branch it
    # would take against an uncalibrated screen unless a test says otherwise.

    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]:
        return self._answer("find_all", (kind,), tuple)

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        return self._answer("locate", (kind, exclude_kinds), lambda: Located(None, False, None))

    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick:
        return self._answer("click_element", (kind, settle_s), lambda: ElementClick.MISMATCH)

    async def hover_scan(self, kind: TemplateKind) -> ScreenRegion | None:
        return self._answer("hover_scan", (kind,), lambda: None)

    async def snap_to_bottom(self, action: str) -> None:
        self._answer("snap_to_bottom", (action,), lambda: None)

    # -- the clipboard ---------------------------------------------------------

    @property
    def clipboard_kind(self) -> str | None:
        if not self.has_clipboard:
            return None
        return self.clipboard.name if self.clipboard is not None else "fake"

    async def read_clipboard(self) -> str | None:
        self.calls.append(("read_clipboard", ()))
        if not self.has_clipboard:
            raise ClipboardUnavailable("no clipboard provider")
        if self.clipboard is not None:
            return self.clipboard.read_text()
        return self.clipboard_text

    async def write_clipboard(self, text: str) -> None:
        self.calls.append(("write_clipboard", (text,)))
        if not self.has_clipboard:
            raise ClipboardUnavailable("no clipboard provider")
        self.written.append(text)
        self.clipboard_text = text
        if self.clipboard is not None:
            write_via(self.clipboard, self.self_writes, text)
        else:
            self.self_writes.note(text)

    def watch_clipboard(self, on: bool) -> bool:
        self.calls.append(("watch_clipboard", (on,)))
        self.watching = bool(on) and self.clipboard_kind not in (None, "manual")
        return self.watching

    # == the local-only tier ==================================================

    def on_frame(self, hook: FrameHook) -> Callable[[], None]:
        """Every captured frame and what was recognised in it, AFTER the tick
        that describes it (the order the ELEMENTS column is drawn in)."""
        self._frame_hooks.append(hook)

        def drop() -> None:
            if hook in self._frame_hooks:
                self._frame_hooks.remove(hook)

        return drop

    def push_frame(
        self,
        sightings: Mapping[TemplateKind, Sighting | None],
        *,
        scene: RegionImage | None = None,
        generation: int | None = None,
    ) -> None:
        """One frame's recognitions, as the poll loop would hand them over.

        ``generation`` is the run the caller means to speak as - a stamp the
        monitor has moved past reaches nobody, exactly as a ghost tick does.
        Frames themselves carry no stamp (the hook takes two arguments, not
        three): they are delivered on the monitor's own thread right after the
        tick they belong to, so the run they describe is the live one.
        """
        stamp = self.generations if generation is None else generation
        if stamp != self.generations:
            return
        frame = scene if scene is not None else RegionImage(1, 1, bytes(4))
        for hook in list(self._frame_hooks):
            hook(frame, sightings)

    def capture(self, region: ScreenRegion) -> RegionImage:
        """One frame of ``region`` for the calibration surface. Raises like the
        real thing unless :attr:`frames` scripts one."""
        self.calls.append(("capture", (region,)))
        if not self.frames:
            raise CaptureError("no frame scripted")
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def reset_trackers(self) -> None:
        """Swap every live tracker for a fresh one of the same calibration -
        never clear one in place (§4.3), and reach the detector that is being
        polled through it."""
        self.calls.append(("reset_trackers", ()))
        self.resets += 1
        detector = self.detector
        if self.busy_tracker is not None:
            spare = self.busy_tracker.fresh()
            if detector is not None and detector.busy is self.busy_tracker:
                detector.busy = spare
            self.busy_tracker = spare
        if self.idle_tracker is not None:
            spare = self.idle_tracker.fresh()
            if detector is not None and detector.idle is self.idle_tracker:
                detector.idle = spare
            self.idle_tracker = spare
        if self.stale_tracker is not None:
            stale_spare = self.stale_tracker.fresh()
            if detector is not None and detector.stale is self.stale_tracker:
                detector.stale = stale_spare
            self.stale_tracker = stale_spare


def _conforms(monitor: FakeUIMonitor) -> UIMonitor:
    """Structural pin: mypy fails HERE if the double drifts from the contract.

    The same trick ``shell/app/remote_link.py`` uses, and for the same reason -
    a double that has quietly stopped answering what the real one answers is a
    suite that passes against nothing.
    """
    return monitor
