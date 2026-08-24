"""``LocalUIMonitor``, driven for real - no Textual, no screen, no mouse.

The sibling of ``tests/driver/automation/test_detector_poller.py``, and the same
bargain: what the detectors SEE has its own unit tests
(``tests/driver/screen/test_detector.py``), and what a tick MEANS is the brain's
(``tests/driver/automation/``). What is asserted here is what the monitor OWNS -
who is polling, what comes out of a tick, which run it belongs to, when a parked
``observe`` is allowed to wake up, and that a stop really ends a thread.

So the threads are real and every test joins its own: a poller that only *looks*
stopped is exactly the bug this file could hide, and a mocked thread would hide
it. The detector is the real composer's (``build_detector``) over fake frames,
because the loop under test is a bridge and the thing it bridges should be the
thing that ships.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.monitor.local import POLL_SECONDS, LocalUIMonitor, required_ticks
from agentclip.driver.monitor.protocol import MonitorSpec, Tick
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion

# Fast enough that a test never waits on a tick, slow enough to still be a tick.
POLL_S = 0.005
TIMEOUT_S = 5.0

REGION = ScreenRegion(10, 20, 4, 4)


def _frame() -> RegionImage:
    """One captured frame: 2x2 BGRX, flat black. The loop hands it straight to
    the detector, so what is IN it only matters to the detector's own tests."""
    return RegionImage(2, 2, bytes(4 * 4))


async def _await_until(
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


def _spec(
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


class Wiring:
    """One monitor, everything that came out of it, and the frames it asked for."""

    def __init__(self, monitor: LocalUIMonitor, captured: list[ScreenRegion]) -> None:
        self.monitor = monitor
        self.captured = captured
        self.ticks: list[Tick] = []
        self.clips: list[str] = []


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
    ) -> Wiring:
        held = profile if profile is not None else ServiceProfile("svc")
        captured: list[ScreenRegion] = []

        def capture(region: ScreenRegion) -> RegionImage:
            captured.append(region)
            return _frame()

        monitor = LocalUIMonitor(
            profile_for=lambda key: held if key == "svc" else None,
            clipboard=clipboard,
            capture=capture,
            poll_seconds=poll_seconds,
            clip_poll_interval_ms=clip_poll_interval_ms,
        )
        wiring = Wiring(monitor, captured)
        monitor.subscribe(wiring.ticks.append)
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


# == what a tick is, and where it goes =========================================


async def test_a_tick_reaches_the_subscribers_and_the_latest_read(
    wire: Callable[..., Wiring],
) -> None:
    """The whole of phase 1's plumbing in one assertion: the poller captures the
    configured window, folds the frame into a tick, and publishes it."""
    wiring = wire()
    generation = await wiring.monitor.configure(_spec())

    await _await_until(lambda: bool(wiring.ticks), "the first tick")
    tick = wiring.ticks[0]
    assert wiring.captured[0] == REGION
    assert tick.generation == generation == 1
    assert tick.seq == 1
    assert tick.captured is True
    assert tick.stale is not None  # the one signal this spec ticks
    assert tick.active_detectors == ("stale",)
    assert wiring.monitor.latest is tick

    await _await_until(lambda: len(wiring.ticks) > 1, "a second tick")
    assert wiring.ticks[1].seq == 2  # never repeats, and never goes backwards


async def test_a_spec_with_nothing_to_watch_starts_no_thread(
    wire: Callable[..., Wiring],
) -> None:
    """Configured and idle, deliberately: a poll loop with no window to watch -
    or a service this machine has no profile for - is pure cost."""
    wiring = wire()

    assert await wiring.monitor.configure(_spec(region=None)) == 1
    assert wiring.monitor.poller is None
    assert await wiring.monitor.configure(_spec(service="nobody-has-this")) == 2
    assert wiring.monitor.poller is None
    assert wiring.monitor.latest is None


async def test_the_sightings_are_screen_rectangles_not_pixels(
    wire: Callable[..., Wiring],
) -> None:
    """§2.2: locations and booleans, never a crop. A service with no captures
    searches for nothing, so the map is empty and every kind reads "no answer"
    rather than "not on screen"."""
    wiring = wire()
    await wiring.monitor.configure(_spec())

    await _await_until(lambda: bool(wiring.ticks), "the first tick")
    tick = wiring.ticks[0]
    assert dict(tick.sightings) == {}
    assert tick.present(TemplateKind.COPY) is None
    assert tick.searched(TemplateKind.COPY) is False


# == observe: the next tick, never the cached one ==============================


async def test_observe_waits_for_a_tick_taken_after_the_call(
    wire: Callable[..., Wiring],
) -> None:
    """The bug this exists to stop is reading the screen from before the scroll.
    A tick that was already in hand when the call was made resolves nothing."""
    wiring = wire()
    await wiring.monitor.configure(_spec(region=None))  # ticks come from feed only
    wiring.monitor.feed(wiring.monitor.stamp())
    stale = wiring.monitor.latest
    assert stale is not None

    task = asyncio.ensure_future(wiring.monitor.observe())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done(), "the tick from before the call must not resolve it"

    wiring.monitor.feed(wiring.monitor.stamp())
    tick = await asyncio.wait_for(task, TIMEOUT_S)
    assert tick.seq > stale.seq


async def test_observe_is_woken_from_the_poller_thread(wire: Callable[..., Wiring]) -> None:
    """The hand-over that makes ``observe`` usable at all: the future belongs to
    the event loop and the tick arrives on a thread of its own."""
    wiring = wire()
    await wiring.monitor.configure(_spec())
    tick = await asyncio.wait_for(wiring.monitor.observe(), TIMEOUT_S)
    assert tick.generation == 1


async def test_a_ghost_tick_is_dropped_and_never_resolves_an_observe(
    wire: Callable[..., Wiring],
) -> None:
    """§4.2. A stop is a flag, not a join: the loop a reconfigure interrupts
    still finishes the tick it was in, and that tick describes the window the
    monitor has just been pointed away from. It is dropped on its stamp - it
    never lands in ``latest``, never reaches a subscriber, and above all never
    answers a recipe that is waiting to see the NEW window.
    """
    wiring = wire()
    await wiring.monitor.configure(_spec(region=None))
    in_flight = wiring.monitor.stamp()  # taken under generation 1

    assert await wiring.monitor.configure(_spec(region=None)) == 2
    task = asyncio.ensure_future(wiring.monitor.observe())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    wiring.monitor.feed(in_flight)
    await asyncio.sleep(0)
    assert wiring.ticks == []
    assert wiring.monitor.latest is None
    assert not task.done()

    live = wiring.monitor.stamp()
    wiring.monitor.feed(live)
    assert await asyncio.wait_for(task, TIMEOUT_S) is live
    assert wiring.monitor.latest is live


# == the trackers ==============================================================


async def test_reset_trackers_swaps_the_identities_and_the_detector_sees_them(
    wire: Callable[..., Wiring],
) -> None:
    """§4.3, swap not clear. The replacement has to reach the DETECTOR, because
    that is what the poller folds its frames through - a reset the detector
    never heard about is a reset the very next tick undoes."""
    wiring = wire()
    await wiring.monitor.configure(_spec())
    detector = wiring.monitor.detector
    assert detector is not None
    before = wiring.monitor.stale_tracker
    assert before is not None and detector.stale is before

    wiring.monitor.reset_trackers()

    after = wiring.monitor.stale_tracker
    assert after is not None
    assert after is not before, "the tracker was cleared in place, not swapped"
    assert detector.stale is after


# == suspend and resume ========================================================


async def test_suspend_stops_the_thread_and_resume_restarts_it(
    wire: Callable[..., Wiring],
) -> None:
    """§6.4's pair. A capture overlay is about to own the screen and nothing has
    MOVED, so the generation stands: the ticks the interrupted loop is still
    finishing are honest readings of the same window, and the resume puts the
    same detector back on a thread rather than rebuilding it."""
    wiring = wire()
    await wiring.monitor.configure(_spec())
    poller = wiring.monitor.poller
    assert poller is not None
    await _await_until(lambda: bool(wiring.ticks), "the first tick")
    detector = wiring.monitor.detector

    await wiring.monitor.suspend()
    poller.thread.join(timeout=TIMEOUT_S)
    assert not poller.thread.is_alive(), "a suspended poller kept polling"
    assert wiring.monitor.poller is None
    quiet = len(wiring.ticks)
    assert wiring.monitor.generation == 1

    await wiring.monitor.resume()
    assert wiring.monitor.poller is not None
    assert wiring.monitor.detector is detector, "resume rebuilt the detector"
    await _await_until(lambda: len(wiring.ticks) > quiet, "ticks after the resume")
    assert wiring.monitor.generation == 1, "a suspend/resume is not a retarget"
    assert wiring.ticks[-1].generation == 1


async def test_resume_while_polling_is_a_no_op(wire: Callable[..., Wiring]) -> None:
    wiring = wire()
    await wiring.monitor.configure(_spec())
    poller = wiring.monitor.poller

    await wiring.monitor.resume()

    assert wiring.monitor.poller is poller


async def test_close_ends_the_poller(wire: Callable[..., Wiring]) -> None:
    wiring = wire()
    await wiring.monitor.configure(_spec())
    poller = wiring.monitor.poller
    assert poller is not None

    await wiring.monitor.close()
    await wiring.monitor.close()  # idempotent

    poller.thread.join(timeout=TIMEOUT_S)
    assert not poller.thread.is_alive()


# == the cadence ===============================================================


def test_stable_seconds_converts_against_the_monitors_own_tick_rate() -> None:
    """§2.10: the brain ships raw seconds and the monitor owns the poll rate.
    Never zero - a service configured faster than one poll still has to see the
    screen hold still ONCE."""
    assert POLL_SECONDS == 0.5
    assert required_ticks(0.5) == 1
    assert required_ticks(2.0) == 4
    assert required_ticks(0.0) == 1
    assert required_ticks(0.1) == 1
    assert required_ticks(1.0, poll_seconds=0.25) == 4


async def test_configure_hands_the_converted_ticks_to_the_detector(
    wire: Callable[..., Wiring], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conversion is not just a function that exists: it is what the
    detector is actually built with, and no shell computes it any more."""
    import agentclip.driver.monitor.local as local

    seen: dict[str, object] = {}
    real = local.build_detector

    def spy(region: ScreenRegion, profile: ServiceProfile, **kwargs: object):
        seen.update(kwargs)
        return real(region, profile, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(local, "build_detector", spy)
    await wire(poll_seconds=POLL_SECONDS).monitor.configure(_spec(stable_seconds=2.0))

    assert seen["required_ticks"] == 4
    assert seen["signals"] == ("stale",)
    assert seen["tolerance"] == 24
    assert seen["matcher"] == "anchors"

    # And against the monitor's OWN rate, not the module constant: a monitor
    # polling faster needs proportionally more quiet ticks to have watched the
    # same two seconds of screen. This is the half a shell could never get
    # right, and the reason the seconds ship raw.
    await wire(poll_seconds=0.25).monitor.configure(_spec(stable_seconds=2.0))
    assert seen["required_ticks"] == 8


# == the clipboard =============================================================


async def test_the_watcher_delivers_an_external_copy_but_never_our_own_write(
    wire: Callable[..., Wiring],
) -> None:
    """§2.11. The watcher and the writer share one ``SelfWriteSet``, which is
    the whole reason AgentClip's own outbound payload does not come straight
    back in as if the user had copied a reply."""
    clipboard = FakeClipboard()
    wiring = wire(clipboard=clipboard)
    assert wiring.monitor.clipboard_kind == "fake"
    assert wiring.monitor.watch_clipboard(True) is True
    assert wiring.monitor.watch_clipboard(True) is True  # idempotent

    clipboard.set_text("a reply the user copied")
    await _await_until(lambda: bool(wiring.clips), "the external copy")

    await wiring.monitor.write_clipboard("our own outbound payload")
    assert wiring.monitor.self_writes.contains_text("our own outbound payload")
    assert await wiring.monitor.read_clipboard() == "our own outbound payload"

    # The proof it was SKIPPED rather than merely late: a later external copy
    # lands, and the watcher polls one clipboard in order.
    clipboard.set_text("the next reply")
    await _await_until(lambda: len(wiring.clips) > 1, "the second external copy")
    assert wiring.clips == ["a reply the user copied", "the next reply"]

    assert wiring.monitor.watch_clipboard(False) is False
    assert wiring.monitor.watch_clipboard(False) is False  # idempotent both ways


async def test_a_manual_clipboard_is_never_polled() -> None:
    """The manual provider is the sentinel for "no backend works": the user
    copies and pastes by hand, and there is nothing a poll could ever see."""

    class Manual:
        name = "manual"

        def read_text(self) -> str | None:
            return None

        def write_text(self, text: str) -> None:
            raise AssertionError("nothing should write through the manual provider")

        def healthcheck(self) -> bool:
            return False

    monitor = LocalUIMonitor(profile_for=lambda _key: None, clipboard=Manual())
    assert monitor.clipboard_kind == "manual"
    assert monitor.watch_clipboard(True) is False
    assert not [t for t in threading.enumerate() if t.name == "agentclip-clipwatch"]
    assert await monitor.read_clipboard() is None
    await monitor.close()


async def test_a_monitor_with_no_provider_reads_nothing(wire: Callable[..., Wiring]) -> None:
    wiring = wire()
    assert wiring.monitor.clipboard_kind is None
    assert await wiring.monitor.read_clipboard() is None
    assert wiring.monitor.watch_clipboard(True) is False
    assert not [t for t in threading.enumerate() if t.name == "agentclip-clipwatch"]


# == the subscribers ===========================================================


async def test_one_subscriber_that_raises_does_not_stop_the_next(
    wire: Callable[..., Wiring],
) -> None:
    """Losing every tick after the first bad paint would look exactly like a
    frozen screen, so a hook that throws is logged and the fan-out goes on."""
    wiring = wire()
    await wiring.monitor.configure(_spec(region=None))
    seen: list[Tick] = []

    def boom(_tick: Tick) -> None:
        raise RuntimeError("a subscriber with a bug in it")

    wiring.monitor.subscribe(boom)
    wiring.monitor.subscribe(seen.append)

    first = wiring.monitor.stamp()
    wiring.monitor.feed(first)
    wiring.monitor.feed(wiring.monitor.stamp())

    assert seen[0] is first
    assert len(seen) == 2, "the poller stopped publishing after a hook threw"
    assert len(wiring.ticks) == 2  # the fixture's own hook, registered first


async def test_unsubscribe_stops_the_delivery(wire: Callable[..., Wiring]) -> None:
    wiring = wire()
    await wiring.monitor.configure(_spec(region=None))
    seen: list[Tick] = []
    off = wiring.monitor.subscribe(seen.append)

    wiring.monitor.feed(wiring.monitor.stamp())
    off()
    wiring.monitor.feed(wiring.monitor.stamp())

    assert len(seen) == 1


async def test_the_frame_hook_carries_the_pixels_the_tick_never_does(
    wire: Callable[..., Wiring],
) -> None:
    """The local-only tier (§2.2): a crop is a calibration surface, so the raw
    scene leaves through a hook of its own and never through a tick. A frame
    that recognised nothing says nothing - which is what a profile with no
    captures produces, so this one asserts the silence."""
    wiring = wire()
    frames: list[RegionImage] = []
    wiring.monitor.on_frame(lambda scene, _sightings: frames.append(scene))
    await wiring.monitor.configure(_spec())

    await _await_until(lambda: len(wiring.ticks) > 1, "a couple of ticks")
    assert frames == [], "a frame with no sightings is no evidence about anything"
