"""The harness the ``LocalUIMonitor`` suites share: one wired monitor, real
threads, and a teardown that refuses to let one outlive its test.

Five files hang off this. ``test_local.py`` asserts what a tick IS and where it
goes; ``test_poller.py`` asserts the thread that produces them - ownership,
order, stamps and stopping; ``test_tracker_reset.py`` asserts the one race the
poll thread introduced, with a hand parked exactly where it used to be lost;
``test_verbs.py`` asserts the pixel verdicts, against a hand
(:class:`ScriptedOps`) that answers instead of touching a machine; and
``test_streaks.py`` asserts the two consecutive-tick counts the monitor carries
on every tick.

Two things are deliberate here and worth not undoing:

*Real threads.* A poller that only *looks* stopped is exactly the bug these
files exist to catch, and a mocked thread would hide it. So :func:`wire` builds
monitors that genuinely poll, and the teardown joins every ``agentclip-detector``
and ``agentclip-clipwatch`` thread **by name** rather than by handle - a test
that reconfigures leaves earlier runs' threads behind on purpose (that is what a
ghost tick is), and those are precisely the ones a handle-keeping teardown would
miss.

*A real detector where the reading matters, a scripted one where the SHAPE
does.* :func:`spec` goes through the real composer (``build_detector``), because
the loop under test is a bridge and the thing it bridges should be the thing
that ships. :class:`ScriptedDetector` is for the tick's shape - which probes ride
one tick, in what order the frame follows it - where a real detector would need
a calibrated service behind every reading. It is installed through
:func:`compose_with`, which is the only seam that can reach inside ``configure``.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence

import pytest

from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.monitor import local as local_module
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.ops import ScreenOps
from agentclip.driver.monitor.protocol import MonitorSpec, Tick
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.detector import DetectionSnapshot, Sighting
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.stale import StaleProbe
from agentclip.driver.screen.template import CandidateSource, RegionMatch, Template

# Fast enough that a test never waits on a tick, slow enough to still be a tick.
POLL_S = 0.005
TIMEOUT_S = 5.0

REGION = ScreenRegion(10, 20, 4, 4)

CaptureFn = Callable[[ScreenRegion], RegionImage]


def frame() -> RegionImage:
    """One captured frame: 2x2 BGRX, flat black. The loop hands it straight to
    the detector, so what is IN it only matters to the detector's own tests."""
    return RegionImage(2, 2, bytes(4 * 4))


async def await_until(predicate: Callable[[], bool], what: str, timeout: float = TIMEOUT_S) -> None:
    """Wait for something the POLLER thread will do, without blocking the event
    loop a parked ``observe`` is going to be woken through."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def spec(
    *,
    region: ScreenRegion | None = REGION,
    service: str = "svc",
    finish_signals: tuple[str, ...] = ("stale",),
    stable_seconds: float = 0.5,
) -> MonitorSpec:
    """The §2.10 payload, with everything a test is not about held still."""
    return MonitorSpec(
        service=service,
        region=region,
        finish_signals=finish_signals,
        stable_seconds=stable_seconds,
        tolerance=24,
        matcher="anchors",
    )


def snapshot(
    *,
    busy: BusyProbe | None = None,
    idle: BusyProbe | None = None,
    stale: StaleProbe | None = None,
    sightings: Mapping[TemplateKind, Sighting | None] | None = None,
    captured: bool = True,
    at: float = 0.0,
) -> DetectionSnapshot:
    """One detector answer, written out by hand. What :class:`ScriptedDetector`
    hands back every tick."""
    return DetectionSnapshot(
        at=at,
        captured=captured,
        busy=busy,
        idle=idle,
        stale=stale,
        sightings=dict(sightings or {}),
    )


class ScriptedDetector:
    """A detector that answers with a snapshot the test wrote out by hand.

    Structural, not a subclass: the monitor only ever touches ``watching``,
    ``active_detectors``, the three tracker slots and ``observe`` - so those are
    the whole of what a stand-in owes it, and pinning that list here is itself
    part of the contract.

    ``on_observe`` is the hook a test parks a thread on: it runs *inside* the
    call the tick lock is deliberately not held across (§4.4), which is the only
    moment a ``configure`` can race a probe in flight.
    """

    def __init__(
        self,
        answer: DetectionSnapshot,
        *,
        active_detectors: tuple[str, ...] = ("busy", "idle", "stale"),
        on_observe: Callable[[], None] | None = None,
    ) -> None:
        self._answer = answer
        self.active_detectors = active_detectors
        self.watching = True
        self.busy = None
        self.idle = None
        self.stale = None
        self.frames: list[RegionImage | None] = []
        self._on_observe = on_observe

    def observe(self, scene: RegionImage | None) -> DetectionSnapshot:
        self.frames.append(scene)
        if self._on_observe is not None:
            self._on_observe()
        return self._answer


def noise(width: int, height: int, seed: int = 1) -> RegionImage:
    """A frame of deterministic pseudo-random pixels - enough structure for
    ``Template.build`` to take it."""
    rng = random.Random(seed)
    return RegionImage(width, height, bytes(rng.randrange(256) for _ in range(width * height * 4)))


def template(width: int = 20, height: int = 16, seed: int = 1) -> Template:
    """One captured appearance. What it looks LIKE never matters in the verb
    suites - the search is scripted - but its SIZE does: a located rectangle is
    the size of the image that matched, and a click aims inside it."""
    return Template.build(noise(width, height, seed))


def profile_with(*kinds: TemplateKind, key: str = "svc") -> ServiceProfile:
    """A service that has captured exactly these appearances, one image each.

    One image per kind on purpose: the union over a stack is
    ``monitor/search.py``'s business and is tested there, so a verb suite that
    stacked two would only be re-asserting it - and would make every scripted
    search answer twice.
    """
    held = ServiceProfile(key)
    for index, kind in enumerate(kinds):
        held.templates[kind] = [template(seed=index + 1)]
    return held


class ScriptedOps(ScreenOps):
    """A hand on a machine that answers instead of touching one.

    Every OS call the pixel verbs make, recorded and scripted. A subclass rather
    than a stub because what the verbs are ABOUT is which of these they make, in
    what order, with what arguments - so anything left unoverridden reaching the
    real desktop would be the bug this cannot afford to hide.

    Each script is a queue whose last entry repeats, the way ``FakeUIMonitor``'s
    frames do: a test that cares about one answer writes one, and a test about a
    walk up the screen writes the sequence it expects to be walked.
    """

    def __init__(
        self,
        *,
        lowest: Sequence[tuple[RegionMatch | None, float | None]] = ((None, None),),
        all_matches: Sequence[Sequence[RegionMatch]] = ((),),
        capture_error: bool = False,
        move_ok: bool = True,
        click_ok: bool = True,
    ) -> None:
        self._lowest = list(lowest)
        self._all = list(all_matches)
        self._capture_error = capture_error
        self._move_ok = move_ok
        self._click_ok = click_ok
        self.captures: list[ScreenRegion] = []
        self.moves: list[tuple[int, int]] = []
        self.clicks: list[tuple[ScreenRegion, float | None]] = []
        self.scrolls: list[tuple[ScreenRegion, int]] = []
        self.keys: list[tuple[str, int]] = []
        # Which searches were spent, in order. The two questions cost the same
        # full-region comparison, so "how many of each" is a claim the verbs
        # make about their own cost and one this file pins.
        self.searches: list[str] = []

    @staticmethod
    def _next(queue: list[object]) -> object:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def capture(self, region: ScreenRegion) -> RegionImage:
        self.captures.append(region)
        if self._capture_error:
            raise CaptureError("nothing to see")
        return frame()

    def move_cursor(self, x: int, y: int) -> bool:
        self.moves.append((x, y))
        return self._move_ok

    def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        self.clicks.append((region, settle_s))
        return self._click_ok

    def scroll(self, region: ScreenRegion, detents: int) -> bool:
        self.scrolls.append((region, detents))
        return True

    def scroll_key(self, key: str, taps: int = 1) -> bool:
        self.keys.append((key, taps))
        return True

    def lowest_match(
        self,
        template: Template,
        scene: RegionImage,
        *,
        tolerance: int,
        max_diff: float,
        matcher: CandidateSource | None,
    ) -> tuple[RegionMatch | None, float | None]:
        self.searches.append("lowest")
        answer = self._next(self._lowest)
        assert isinstance(answer, tuple)
        return answer

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
        self.searches.append("all")
        answer = self._next(self._all)
        assert isinstance(answer, (list, tuple))
        return list(answer)

    def hover_step_delay(self) -> float:
        """No pause at all: the beat is a real browser's repaint and a suite
        that waited one out per stop is a suite nobody runs."""
        return 0.0

    def forget(self) -> None:
        """Drop the record, keep the script.

        For the moment between "the monitor is configured" and "the verb under
        test was called": a configured monitor POLLS, and a poll captures
        through this same hand, so whatever it managed before the suspend is
        noise against an assertion about which calls a verb made. The scripts
        are untouched because a poll never consumes one - the detector's
        trackers search through ``driver/screen`` directly, and only the verbs
        come through here.
        """
        self.captures.clear()
        self.moves.clear()
        self.clicks.clear()
        self.scrolls.clear()
        self.keys.clear()
        self.searches.clear()


class Wiring:
    """One monitor, everything that came out of it, and the frames it asked for."""

    def __init__(self, monitor: LocalUIMonitor, captured: list[ScreenRegion]) -> None:
        self.monitor = monitor
        self.captured = captured
        # OBSERVATIONS only. A notice (ui-monitor.md 11.10 - the tick a
        # ``configure`` publishes for its own sake when it started no poller)
        # is a statement about the RUN, not about the screen, and every
        # assertion in these files that counts ticks is counting what the
        # poller saw. They are kept apart rather than filtered at each use so
        # that "the poller published nothing" stays spellable as ``== []``.
        self.ticks: list[Tick] = []
        self.notices: list[Tick] = []
        self.clips: list[str] = []
        # Every publication in the order the poller made it: ``("tick", seq)``
        # for a subscriber call and ``("frame", seq_of_the_tick_it_follows)``
        # for the pixel hook. One list rather than two, because the ORDER
        # between them is the half of the contract two lists cannot show.
        self.published: list[tuple[str, int]] = []


@pytest.fixture
def wire() -> Iterator[Callable[..., Wiring]]:
    """Build wired monitors, and make sure no thread outlives the test.

    The teardown is the point: these are real threads capturing (a stub of) the
    user's screen, so one a test forgot would leak into every test after it.
    Joined BY NAME rather than by handle, because a test that reconfigures
    leaves earlier runs' threads behind on purpose - that is what a ghost tick
    is - and those are exactly the ones a handle-keeping teardown would miss.
    """
    built: list[Wiring] = []

    def build(
        *,
        clipboard: FakeClipboard | None = None,
        profile: ServiceProfile | None = None,
        poll_seconds: float = POLL_S,
        clip_poll_interval_ms: int = 5,
        capture: CaptureFn | None = None,
        ops: ScreenOps | None = None,
        service: str = "svc",
    ) -> Wiring:
        held = profile if profile is not None else ServiceProfile("svc")
        captured: list[ScreenRegion] = []
        # A monitor handed an ``ops`` captures THROUGH it by default, the way
        # the real one does (``self._ops.capture``): the pixel verbs and the
        # poll loop go through one seam, and a suite that split them would be
        # asserting against a machine no deployment has.
        shot: CaptureFn
        if capture is not None:
            shot = capture
        elif ops is not None:
            shot = ops.capture
        else:
            shot = lambda _region: frame()  # noqa: E731

        def capturing(region: ScreenRegion) -> RegionImage:
            captured.append(region)
            return shot(region)

        monitor = LocalUIMonitor(
            profile_for=lambda key: held if key == service else None,
            ops=ops,
            clipboard=clipboard,
            capture=capturing,
            poll_seconds=poll_seconds,
            clip_poll_interval_ms=clip_poll_interval_ms,
        )
        wiring = Wiring(monitor, captured)

        def on_tick(tick: Tick) -> None:
            if tick.notice:
                wiring.notices.append(tick)
                return
            wiring.ticks.append(tick)
            wiring.published.append(("tick", tick.seq))

        def on_frame(_scene: RegionImage, _sightings: object) -> None:
            wiring.published.append(("frame", wiring.ticks[-1].seq if wiring.ticks else -1))

        monitor.subscribe(on_tick)
        monitor.on_frame(on_frame)
        monitor.on_clip(wiring.clips.append)
        built.append(wiring)
        return wiring

    yield build

    for wiring in built:
        asyncio.run(wiring.monitor.close())
    for name in ("agentclip-detector", "agentclip-clipwatch", "agentclip-notice"):
        for leftover in [t for t in threading.enumerate() if t.name == name]:
            leftover.join(timeout=TIMEOUT_S)
            assert not leftover.is_alive(), f"a {name} thread outlived its test"


@pytest.fixture
def compose_with(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], None]:
    """Make the next ``configure`` poll THIS detector instead of building one.

    ``configure`` composes its own detector on purpose (the cadence conversion
    and the profile lookup are its business, and ``test_local.py`` pins both),
    so a test that needs a detector it can park a thread inside - or one whose
    answer it wrote by hand - has to reach the composer. This is that reach, and
    the only one: nothing else here touches the monitor's insides.
    """

    def use(detector: object) -> None:
        monkeypatch.setattr(local_module, "build_detector", lambda *_args, **_kwargs: detector)

    return use
