"""The harness the ``LocalUIMonitor`` suites share: one wired monitor, real
threads, and a teardown that refuses to let one outlive its test.

Three files hang off this. ``test_local.py`` asserts what a tick IS and where it
goes; ``test_poller.py`` asserts the thread that produces them - ownership,
order, stamps and stopping; ``test_tracker_reset.py`` asserts the one race the
poll thread introduced, with a hand parked exactly where it used to be lost.

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
import threading
import time
from collections.abc import Callable, Iterator, Mapping

import pytest

from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.monitor import local as local_module
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.protocol import MonitorSpec, Tick
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import DetectionSnapshot, Sighting
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.stale import StaleProbe

# Fast enough that a test never waits on a tick, slow enough to still be a tick.
POLL_S = 0.005
TIMEOUT_S = 5.0

REGION = ScreenRegion(10, 20, 4, 4)

CaptureFn = Callable[[ScreenRegion], RegionImage]


def frame() -> RegionImage:
    """One captured frame: 2x2 BGRX, flat black. The loop hands it straight to
    the detector, so what is IN it only matters to the detector's own tests."""
    return RegionImage(2, 2, bytes(4 * 4))


async def await_until(
    predicate: Callable[[], bool], what: str, timeout: float = TIMEOUT_S
) -> None:
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
        hover_scan=False,
        scroll_action="wheel",
        snap_back=False,
        delivery="paste",
        auto_submit=False,
        send_arm_min_diff=0.02,
        send_arm_ticks=2,
        send_gate_timeout_ticks=240,
        send_gate_seen_timeout_ticks=20,
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


class Wiring:
    """One monitor, everything that came out of it, and the frames it asked for."""

    def __init__(self, monitor: LocalUIMonitor, captured: list[ScreenRegion]) -> None:
        self.monitor = monitor
        self.captured = captured
        self.ticks: list[Tick] = []
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
    ) -> Wiring:
        held = profile if profile is not None else ServiceProfile("svc")
        captured: list[ScreenRegion] = []
        shot = capture if capture is not None else (lambda _region: frame())

        def capturing(region: ScreenRegion) -> RegionImage:
            captured.append(region)
            return shot(region)

        monitor = LocalUIMonitor(
            profile_for=lambda key: held if key == "svc" else None,
            clipboard=clipboard,
            capture=capturing,
            poll_seconds=poll_seconds,
            clip_poll_interval_ms=clip_poll_interval_ms,
        )
        wiring = Wiring(monitor, captured)

        def on_tick(tick: Tick) -> None:
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
    for name in ("agentclip-detector", "agentclip-clipwatch"):
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
        monkeypatch.setattr(
            local_module, "build_detector", lambda *_args, **_kwargs: detector
        )

    return use
