"""The detector poll thread, driven for real - no Textual, no screen, no mouse.

The sibling of ``test_clipboard_watcher.py`` and the same bargain: what the
detectors SEE has its own unit tests (``tests/screen/test_detector.py``), and
what a probe MEANS is still the shell's (``tests/tui/``, unchanged by this
slice). What is asserted here is the half that just moved down from
``MainScreen``: *ownership of the producing thread*. Who is polling, what a
reading carries on its way out, which run it belongs to, and that a stop really
ends it - the last one with a genuine ``join``, because a poller that only
*looks* stopped is exactly the bug this slice could introduce and the one a
mocked thread would hide. So the threads are real and every test joins its own.

Nothing here consumes: the controller pushes probes out through the five sinks
and never looks at them again. The generation stamp is the visible half of that
split - the counter is here because the threads are, and every comparison
against it is somebody else's.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, cast

import pytest

from agentclip.automation.controller import AutomationController
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.detector import (
    DetectionSnapshot,
    ScreenDetector,
    Sighting,
    build_detector,
)
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.stale import StaleProbe, StaleState

from .conftest import FakeAutomationView

# Fast enough that a test never waits on a tick, slow enough to still be a tick.
POLL_S = 0.005
TIMEOUT_S = 5.0

REGION = ScreenRegion(10, 20, 4, 4)
OTHER_REGION = ScreenRegion(900, 0, 4, 4)


def _frame() -> RegionImage:
    """One captured frame: 2x2 BGRX, flat black. The loop hands it straight to
    the detector, so what is IN it only matters to the detector's own tests."""
    return RegionImage(2, 2, bytes(4 * 4))


def _wait_until(predicate: Callable[[], bool], what: str, timeout: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _stale_detector(region: ScreenRegion = REGION, *, required_ticks: int = 1) -> ScreenDetector:
    """The detector a freshly drawn window gets: staleness and nothing else.

    A real one, from the real composer, because the loop under test is a bridge
    and the thing it bridges should be the thing that ships.
    """
    return build_detector(
        region,
        ServiceProfile("svc"),
        signals=("stale",),
        required_ticks=required_ticks,
    )


class _ScriptedDetector:
    """A detector that answers with a snapshot the test wrote out by hand.

    For the tick's SHAPE - which readings are pushed, in which order - where a
    real detector would need a calibrated service behind every one of them.
    Structural, like ``FakeAutomationView``: the loop only calls ``observe`` and
    ``searches``.
    """

    def __init__(self, snapshot: DetectionSnapshot, *, searches: tuple[TemplateKind, ...]) -> None:
        self._snapshot = snapshot
        self._searches = searches
        self.frames: list[RegionImage | None] = []

    def observe(self, scene: RegionImage | None) -> DetectionSnapshot:
        self.frames.append(scene)
        return self._snapshot

    def searches(self, kind: TemplateKind) -> bool:
        return kind in self._searches


def _snapshot(
    *,
    busy: BusyProbe | None = None,
    idle: BusyProbe | None = None,
    stale: StaleProbe | None = None,
    sightings: Mapping[TemplateKind, Sighting | None] | None = None,
    captured: bool = True,
) -> DetectionSnapshot:
    return DetectionSnapshot(
        at=0.0,
        captured=captured,
        busy=busy,
        idle=idle,
        stale=stale,
        sightings=dict(sightings or {}),
    )


class Wiring:
    """One controller with a stubbed capture, plus everything that came out."""

    def __init__(
        self,
        automation: AutomationController,
        pushed: list[tuple[str, Any, int]],
        captured: list[ScreenRegion],
    ) -> None:
        self.automation = automation
        # Every push, in the order the loop made it: (what, payload, generation).
        # One list rather than five, because the ORDER across the five sinks is
        # itself part of the contract (busy -> idle -> stale -> send -> crops).
        self.pushed = pushed
        self.captured = captured

    def stamps(self, what: str) -> list[int]:
        return [generation for kind, _payload, generation in self.pushed if kind == what]

    def kinds(self) -> list[str]:
        return [kind for kind, _payload, _generation in self.pushed]

    def capture(self, region: ScreenRegion) -> RegionImage:
        self.captured.append(region)
        return _frame()


@pytest.fixture
def wire(view: FakeAutomationView) -> Iterator[Callable[[], Wiring]]:
    """Build wired controllers, and make sure no poller outlives the test.

    The teardown is the point, exactly as it is for the watcher: these are real
    threads capturing (a stub of) the user's screen twice a second, so one a
    test forgot would leak into every test after it. Stopping is asked for here
    even when the test already did it, and then joined - so a loop that ignored
    its stop flag fails the test that started it rather than some later one.

    Joined BY NAME rather than by handle: a test that retargets leaves earlier
    runs' threads behind on purpose (that is what a ghost probe is), and those
    are exactly the ones a handle-keeping teardown would miss.
    """
    built: list[AutomationController] = []

    def build() -> Wiring:
        pushed: list[tuple[str, Any, int]] = []
        automation = AutomationController(
            view=view,
            on_busy_probe=lambda probe, gen: pushed.append(("busy", probe, gen)),
            on_idle_probe=lambda probe, gen: pushed.append(("idle", probe, gen)),
            on_stale_probe=lambda probe, gen: pushed.append(("stale", probe, gen)),
            on_send_ready_probe=lambda found, gen: pushed.append(("send", found, gen)),
            on_elements=lambda scene, sightings, gen: pushed.append(
                ("elements", dict(sightings), gen)
            ),
        )
        built.append(automation)
        return Wiring(automation, pushed, [])

    yield build

    for automation in built:
        automation.stop_detectors()
    for leftover in [t for t in threading.enumerate() if t.name == "agentclip-detector"]:
        leftover.join(timeout=TIMEOUT_S)
        assert not leftover.is_alive(), "a detector thread outlived its test"


def _start(wiring: Wiring, detector: Any, region: ScreenRegion = REGION) -> None:
    """The three calls a rebuild is made of, as ``MainScreen`` makes them."""
    wiring.automation.retarget_detectors()
    wiring.automation.start_detectors(
        wiring.automation.detector_loop(
            cast(ScreenDetector, detector),
            region,
            capture=wiring.capture,
            poll_seconds=POLL_S,
        )
    )


# == what starts a thread, and what does not ===================================


def test_nothing_polls_until_a_shell_starts_one(wire: Callable[[], Wiring]) -> None:
    """Construction wires the sinks up; it does not watch anything. Nothing is
    calibrated yet, and a poller with no window to watch is pure cost."""
    wiring = wire()
    assert wiring.automation.detectors_running is False
    assert wiring.automation.detector_poller is None
    assert wiring.automation.detector_generation == 0


def test_a_retarget_alone_starts_nothing(wire: Callable[[], Wiring]) -> None:
    """The two exits that watch nothing (no drawn window, nothing calibrated)
    still retarget: the run they end had probes in flight."""
    wiring = wire()
    assert wiring.automation.retarget_detectors() == 1
    assert wiring.automation.detectors_running is False


def test_a_composed_loop_that_is_never_started_never_polls(wire: Callable[[], Wiring]) -> None:
    """The seam a shell freezes to observe a composition without polling it."""
    wiring = wire()
    wiring.automation.retarget_detectors()
    wiring.automation.detector_loop(
        _stale_detector(), REGION, capture=wiring.capture, poll_seconds=POLL_S
    )
    time.sleep(0.05)
    assert wiring.captured == []
    assert wiring.automation.detectors_running is False


def test_the_poller_runs_on_its_own_named_daemon_thread(wire: Callable[[], Wiring]) -> None:
    wiring = wire()
    _start(wiring, _stale_detector())

    poller = wiring.automation.detector_poller
    assert poller is not None
    assert poller.thread.name == "agentclip-detector"
    assert poller.thread.daemon is True
    assert poller.is_cancelled is False


# == what a tick pushes ========================================================


def test_a_probe_reaches_the_callback_with_the_window_it_was_taken_from(
    wire: Callable[[], Wiring],
) -> None:
    """The whole point of the thread: the live window was looked at, and the
    shell hears the verdict on the callback it handed in."""
    wiring = wire()
    _start(wiring, _stale_detector())

    _wait_until(lambda: bool(wiring.stamps("stale")), "the first stale probe")
    assert wiring.captured[0] == REGION
    probe = wiring.pushed[0][1]
    assert isinstance(probe, StaleProbe)


def test_it_keeps_polling_until_it_is_stopped(wire: Callable[[], Wiring]) -> None:
    """A poll loop, not a one-shot: the readout is refreshed every tick."""
    wiring = wire()
    _start(wiring, _stale_detector())

    _wait_until(lambda: len(wiring.stamps("stale")) >= 3, "three ticks")


def test_the_whole_tick_is_pushed_in_the_reading_order(wire: Callable[[], Wiring]) -> None:
    """busy -> idle -> stale, then the send button, then the pictures.

    The first three are the order the tick-closing rule downstream reads (the
    LAST calibrated one closes the tick); the last two close nothing and fold
    into no verdict, which is why they come after the verdicts they illustrate.
    """
    wiring = wire()
    detector = _ScriptedDetector(
        _snapshot(
            busy=BusyProbe(BusyState.MATCH, 0.2, True),
            idle=BusyProbe(BusyState.CHANGED, 0.4),
            stale=StaleProbe(StaleState.CHANGING, 0.5, 0),
            sightings={TemplateKind.COPY: None},
        ),
        searches=(TemplateKind.SEND_READY,),
    )
    _start(wiring, detector)

    _wait_until(lambda: len(wiring.pushed) >= 5, "a whole tick")
    assert wiring.kinds()[:5] == ["busy", "idle", "stale", "send", "elements"]


def test_the_send_button_is_only_asked_about_when_it_is_calibrated(
    wire: Callable[[], Wiring],
) -> None:
    """No capture of it means no gate at all - and the loop is not the thing
    that decides that: it asks the detector what it searches for."""
    wiring = wire()
    detector = _ScriptedDetector(
        _snapshot(stale=StaleProbe(StaleState.CHANGING, 0.5, 0)), searches=()
    )
    _start(wiring, detector)

    _wait_until(lambda: bool(wiring.stamps("stale")), "a tick")
    assert "send" not in wiring.kinds()


def test_a_failed_capture_reaches_the_detector_as_a_missing_frame(
    wire: Callable[[], Wiring],
) -> None:
    """One capture per tick is handed to one detector, so a failure has to reach
    every reading the same way instead of letting some see a frame."""
    wiring = wire()
    detector = _ScriptedDetector(
        _snapshot(stale=StaleProbe(StaleState.ERROR, None, 0), captured=False),
        searches=(TemplateKind.SEND_READY,),
    )

    def boom(region: ScreenRegion) -> RegionImage:
        wiring.captured.append(region)
        raise CaptureError("no display")

    wiring.automation.retarget_detectors()
    wiring.automation.start_detectors(
        wiring.automation.detector_loop(
            cast(ScreenDetector, detector), REGION, capture=boom, poll_seconds=POLL_S
        )
    )

    _wait_until(lambda: bool(wiring.stamps("stale")), "the error probe")
    assert detector.frames[0] is None
    # A dropped frame recognised nothing, and says nothing: an empty map would
    # blank rows the tick is no evidence about.
    assert "elements" not in wiring.kinds()
    # ...the send gate still gets its three-valued "no answer" though.
    found = next(payload for kind, payload, _gen in wiring.pushed if kind == "send")
    assert found is None


def test_the_crops_cross_as_the_frame_they_were_verified_against(
    wire: Callable[[], Wiring],
) -> None:
    """The loop pushes the frame and its sightings, never a picture: cutting one
    down to panel size depends on the renderer, which is the shell's business -
    it just happens on this thread (see ``MainScreen._post_element_crops``)."""
    wiring = wire()
    detector = _ScriptedDetector(
        _snapshot(sightings={TemplateKind.COPY: None, TemplateKind.SEND_READY: None}),
        searches=(),
    )
    _start(wiring, detector)

    _wait_until(lambda: bool(wiring.kinds()), "a tick")
    kind, sightings, _generation = wiring.pushed[0]
    assert kind == "elements"
    assert set(sightings) == {TemplateKind.COPY, TemplateKind.SEND_READY}


# == which run a probe belongs to ==============================================


def test_every_probe_carries_the_run_that_produced_it(wire: Callable[[], Wiring]) -> None:
    wiring = wire()
    _start(wiring, _stale_detector())
    _wait_until(lambda: bool(wiring.stamps("stale")), "the first run's probes")
    assert set(wiring.stamps("stale")) == {1}

    wiring.automation.stop_detectors()
    _start(wiring, _stale_detector(OTHER_REGION), region=OTHER_REGION)
    _wait_until(lambda: 2 in wiring.stamps("stale"), "the second run's probes")
    assert wiring.automation.detector_generation == 2


def test_a_retarget_leaves_the_in_flight_tick_speaking_for_the_old_window(
    wire: Callable[[], Wiring],
) -> None:
    """The reason the stamp exists. A stop is a flag, not a join: the loop it
    interrupts still finishes the tick it was in, so its verdicts land AFTER the
    automation has been pointed at another browser window. They describe the OLD
    one - and a consumer can only tell because the counter had already moved on
    when they were taken.
    """
    wiring = wire()
    in_the_tick = threading.Event()
    release = threading.Event()

    def slow_capture(region: ScreenRegion) -> RegionImage:
        wiring.captured.append(region)
        in_the_tick.set()
        release.wait(TIMEOUT_S)
        return _frame()

    wiring.automation.retarget_detectors()
    wiring.automation.start_detectors(
        wiring.automation.detector_loop(
            _stale_detector(), REGION, capture=slow_capture, poll_seconds=POLL_S
        )
    )
    assert in_the_tick.wait(TIMEOUT_S), "the poller never reached its capture"

    # ...a delegation starts (or /abort ends one) while that tick is in flight.
    assert wiring.automation.retarget_detectors() == 2
    release.set()

    _wait_until(lambda: bool(wiring.stamps("stale")), "the ghost probe")
    assert wiring.stamps("stale") == [1]  # the run it was taken in, not the live one
    assert wiring.automation.detector_generation == 2


def test_a_stop_is_not_a_retarget(wire: Callable[[], Wiring]) -> None:
    """A modal suspending the poller has not moved the automation anywhere, so
    the counter stays put: the REBUILD that resumes it opens the next run, and a
    suspend/resume pair therefore costs exactly one new stamp rather than two."""
    wiring = wire()
    _start(wiring, _stale_detector())
    assert wiring.automation.detector_generation == 1

    wiring.automation.stop_detectors()
    assert wiring.automation.detector_generation == 1
    assert wiring.automation.detectors_running is False

    _start(wiring, _stale_detector())
    assert wiring.automation.detector_generation == 2


# == stopping ==================================================================


def test_a_stopped_poller_really_stops(wire: Callable[[], Wiring]) -> None:
    """``stop_detectors`` returns before the thread does (joining would freeze a
    UI thread for up to a poll interval), so the guarantee is that it ends
    *soon* - and this is the test that would catch a stop flag nobody reads."""
    wiring = wire()
    _start(wiring, _stale_detector())
    poller = wiring.automation.detector_poller
    assert poller is not None
    _wait_until(lambda: bool(wiring.captured), "the first capture")

    wiring.automation.stop_detectors()
    assert wiring.automation.detectors_running is False  # true for every reader at once
    assert poller.is_cancelled is True
    poller.thread.join(timeout=TIMEOUT_S)
    assert not poller.thread.is_alive()

    ticks = len(wiring.pushed)
    time.sleep(POLL_S * 10)
    assert len(wiring.pushed) == ticks


def test_stopping_twice_is_harmless(wire: Callable[[], Wiring]) -> None:
    wiring = wire()
    _start(wiring, _stale_detector())
    wiring.automation.stop_detectors()
    wiring.automation.stop_detectors()
    assert wiring.automation.detectors_running is False


def test_a_retarget_replaces_the_poller_rather_than_adding_one(
    wire: Callable[[], Wiring],
) -> None:
    """A second drawn window must not leave two loops watching two windows."""
    wiring = wire()
    _start(wiring, _stale_detector())
    first = wiring.automation.detector_poller
    assert first is not None

    _start(wiring, _stale_detector(OTHER_REGION), region=OTHER_REGION)
    second = wiring.automation.detector_poller
    assert second is not None and second is not first
    assert first.is_cancelled is True
    first.thread.join(timeout=TIMEOUT_S)
    assert not first.thread.is_alive()

    _wait_until(lambda: OTHER_REGION in wiring.captured, "the new window being watched")
