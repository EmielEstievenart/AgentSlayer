"""The poll thread the monitor owns, driven for real - no Textual, no screen.

The descendant of ``tests/driver/automation/test_detector_poller.py``: when the
loop moved out of ``AutomationController`` and into ``LocalUIMonitor`` the
subject came with it, and so did the reason it is tested with real threads. A
poller that only *looks* stopped is exactly the bug this file could introduce,
and a mocked thread would hide it - so every test here starts a genuine
``agentclip-detector`` thread and the fixture joins it by name.

What is asserted is the PRODUCER, not the reading: who is polling, what one
capture turns into, which run a tick belongs to, and that stopping really stops.
What a tick MEANS is the brain's (``tests/driver/automation/``); what the
detectors SEE is ``tests/driver/screen/test_detector.py``'s.

Four things changed shape in the move and each has a test here that would fail
if they slid back:

* the five ``consume_*`` calls became ONE ``Tick`` - so "busy then idle then
  stale" is no longer an order between deliveries, it is a single object that
  has to carry all three;
* a ghost is DROPPED rather than delivered with a stale stamp (§4.2) - the old
  loop pushed it and let the consumer check, this one never publishes it;
* ``suspend`` and ``configure`` came apart - a stop that has not MOVED the
  monitor must not bump the generation;
* the lock inverted. The old controller's ``_tick_lock`` deliberately BLOCKED a
  retarget that landed mid-probe, because the probe's ghost check and the
  bookkeeping it guarded had to stay one act. Here the expensive halves of a
  tick are outside the lock by construction (§4.4), so a ``configure`` racing an
  in-flight ``observe`` must not wait for it at all - it bumps the generation
  and the tick still in flight is dropped on its stamp.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

from agentclip.driver.monitor.protocol import Tick
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.detector import Sighting
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.stale import StaleProbe, StaleState
from agentclip.driver.screen.template import RegionMatch, Template

from .conftest import (
    REGION,
    TIMEOUT_S,
    ScriptedDetector,
    Wiring,
    await_until,
    snapshot,
    spec,
)


def _boom(_region: ScreenRegion) -> RegionImage:
    raise CaptureError("no display")


SIGHTING_W, SIGHTING_H = 8, 2  # the narrowest a real Template is allowed to be


def _sighting_at(kind: TemplateKind, x: int, y: int) -> Sighting:
    """A match found at ``(x, y)`` INSIDE the frame, on an 8x2 appearance.

    Real objects rather than stubs, because ``rect`` is the arithmetic under
    test: it takes the origin off the match and the size off the template, and
    a stand-in for either would be a stand-in for the thing being asserted.
    """
    template = Template.build(
        RegionImage(SIGHTING_W, SIGHTING_H, bytes(SIGHTING_W * SIGHTING_H * 4))
    )
    return Sighting(kind=kind, template=template, match=RegionMatch(x, y, 0.0), at=0.0)


def _live_pollers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "agentclip-detector" and t.is_alive()]


def _whole_tick() -> ScriptedDetector:
    """A detector whose every frame answers all three finish signals at once,
    and recognises something as well - the fullest tick the loop can build."""
    return ScriptedDetector(
        snapshot(
            busy=BusyProbe(BusyState.MATCH, 0.2, True),
            idle=BusyProbe(BusyState.CHANGED, 0.4),
            stale=StaleProbe(StaleState.CHANGING, 0.5, 0),
            sightings={TemplateKind.COPY: None},
        )
    )


# == who is polling ============================================================


async def test_the_poller_runs_on_its_own_named_daemon_thread(
    wire: Callable[..., Wiring],
) -> None:
    """Daemon, like the clipboard watcher: an exit must never wait on a poll
    interval. Named, because that is how a leak is found - by the fixture here,
    and by anyone reading a stack dump of the frozen exe."""
    wiring = wire()
    await wiring.monitor.configure(spec())

    poller = wiring.monitor.poller
    assert poller is not None
    assert poller.thread.name == "agentclip-detector"
    assert poller.thread.daemon is True
    assert poller.thread.is_alive()
    assert poller.is_cancelled is False


async def test_a_spec_the_monitor_cannot_watch_leaves_no_thread_behind(
    wire: Callable[..., Wiring],
) -> None:
    """The three ways ``configure`` returns without a run - no region, no
    profile, nothing worth watching - and none of them may leak a thread. The
    generation still moves every time: a retarget onto nothing has to invalidate
    the ticks the previous run has in flight just as much as one onto a window."""
    wiring = wire()
    before = len(_live_pollers())

    assert await wiring.monitor.configure(spec(region=None)) == 1
    assert await wiring.monitor.configure(spec(service="nobody-has-this")) == 2
    assert wiring.monitor.poller is None
    assert len(_live_pollers()) == before


async def test_each_configure_replaces_the_poller_rather_than_adding_one(
    wire: Callable[..., Wiring],
) -> None:
    """A second window must not leave two loops watching two windows.

    The old run is cancelled and dropped on the spot; the thread it leaves is
    joined here rather than by ``configure``, because joining on the event loop
    would freeze the interface for up to a poll interval - which is exactly why
    the stamp exists to filter what that thread is still finishing.
    """
    wiring = wire()
    before = len(_live_pollers())
    await wiring.monitor.configure(spec())
    first = wiring.monitor.poller
    assert first is not None

    await wiring.monitor.configure(spec())
    second = wiring.monitor.poller
    assert second is not None and second is not first
    assert first.is_cancelled is True

    first.thread.join(timeout=TIMEOUT_S)
    assert not first.thread.is_alive()
    assert len(_live_pollers()) == before + 1, "a configure left the old loop polling"


# == what one capture turns into ===============================================


async def test_one_capture_becomes_one_tick_carrying_busy_idle_and_stale(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The shape the move imposed. Three consumers used to be called in a fixed
    order - busy, then idle, then stale - and the order mattered because the
    LAST calibrated one closed the tick. There is no order to keep any more:
    one capture is handed to one detector and all three verdicts ride out on one
    object, so every reading describes the same instant of a moving screen and a
    consumer can see the whole tick at once. ``active_detectors`` is what tells
    it which of the three were ever going to be there.
    """
    wiring = wire()
    detector = _whole_tick()
    compose_with(detector)
    await wiring.monitor.configure(spec())

    await await_until(lambda: bool(wiring.ticks), "the first tick")
    tick = wiring.ticks[0]
    assert wiring.captured[0] == REGION
    assert tick.busy is not None and tick.busy.state is BusyState.MATCH
    assert tick.idle is not None and tick.idle.state is BusyState.CHANGED
    assert tick.stale is not None and tick.stale.state is StaleState.CHANGING
    assert tick.active_detectors == ("busy", "idle", "stale")
    assert detector.frames[0] is not None, "the detector was polled without a frame"


async def test_the_sequence_number_rises_and_never_repeats(
    wire: Callable[..., Wiring],
) -> None:
    """``seq`` is what ``observe`` arms against, so it counts every tick the
    monitor ever produced and only ever goes up - across runs too, because a
    reconfigure restarts the generation's meaning and not the count."""
    wiring = wire()
    await wiring.monitor.configure(spec())
    await await_until(lambda: len(wiring.ticks) >= 3, "three ticks")

    await wiring.monitor.configure(spec())
    await await_until(
        lambda: any(tick.generation == 2 for tick in wiring.ticks), "the second run's ticks"
    )

    seqs = [tick.seq for tick in wiring.ticks]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


async def test_the_frame_is_published_after_the_tick_it_illustrates(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The pixels come last, on the poller thread, out of the very frame the
    verdicts were taken from. After, because a crop illustrates a reading rather
    than making one - and on this thread, because a crop is a calibration
    surface and it has to be cut where the pixels are (§2.2)."""
    wiring = wire()
    compose_with(_whole_tick())
    await wiring.monitor.configure(spec())

    await await_until(lambda: ("frame", 1) in wiring.published, "the first frame")
    assert wiring.published[:2] == [("tick", 1), ("frame", 1)]


async def test_a_failed_capture_reaches_the_detector_as_a_missing_frame(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """One capture per tick is handed to one detector, so a failure has to reach
    every reading the same way instead of letting some see a frame.

    And a dropped frame recognised nothing, so it SAYS nothing: no pixels go
    out, because an empty crop map would blank rows the tick is no evidence
    about.
    """
    wiring = wire(capture=_boom)
    detector = ScriptedDetector(
        snapshot(stale=StaleProbe(StaleState.ERROR, None, 0), captured=False),
        active_detectors=("stale",),
    )
    compose_with(detector)
    await wiring.monitor.configure(spec())

    await await_until(lambda: bool(wiring.ticks), "the error tick")
    assert detector.frames[0] is None
    tick = wiring.ticks[0]
    assert tick.captured is False
    assert dict(tick.sightings) == {}
    assert not [what for what, _seq in wiring.published if what == "frame"]


async def test_a_sighting_leaves_as_a_screen_rectangle_not_a_crop(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """§2.2 at the seam that translates: the detector locates a match inside the
    FRAME, and the tick says where it is on the real screen - the only half a
    brain on another machine could ever act on."""
    wiring = wire()
    found = _sighting_at(TemplateKind.COPY, 1, 2)
    compose_with(ScriptedDetector(snapshot(sightings={TemplateKind.COPY: found})))
    await wiring.monitor.configure(spec())

    await await_until(lambda: bool(wiring.ticks), "the first tick")
    located = wiring.ticks[0].locate(TemplateKind.COPY)
    assert located == ScreenRegion(REGION.left + 1, REGION.top + 2, SIGHTING_W, SIGHTING_H)


# == which run a tick belongs to ===============================================


async def test_a_tick_from_a_superseded_run_reaches_nobody(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """§4.2, with the ghost produced for real rather than stamped by hand.

    A stop is a flag, not a join: the loop a ``configure`` interrupts still
    finishes the tick it was in, and that tick describes the window the monitor
    has just been pointed away from. So it is dropped on its stamp at all three
    exits at once - it never lands in ``latest``, never reaches a subscriber,
    and above all never answers a recipe parked in ``observe`` waiting to see
    the NEW window.

    The retarget goes to a spec with no region on purpose: nothing new starts,
    so anything that arrives after it can only be the ghost.
    """
    wiring = wire()
    inside = threading.Event()
    release = threading.Event()

    def park() -> None:
        inside.set()
        release.wait(TIMEOUT_S)

    ticking = snapshot(stale=StaleProbe(StaleState.CHANGING, 0.5, 0))
    compose_with(ScriptedDetector(ticking, on_observe=park))
    await wiring.monitor.configure(spec())
    await await_until(inside.is_set, "the poller reaching its detector")

    assert await wiring.monitor.configure(spec(region=None)) == 2
    parked = asyncio.ensure_future(wiring.monitor.observe())
    await asyncio.sleep(0)

    release.set()
    await await_until(lambda: not _live_pollers(), "the superseded loop ending")

    assert wiring.ticks == [], "a ghost tick reached a subscriber"
    assert wiring.monitor.latest is None, "a ghost tick became the latest reading"
    assert not parked.done(), "a ghost tick woke a parked observe"

    # ...and the observe is still armed for the run that is live NOW.
    live = wiring.monitor.stamp()
    wiring.monitor.feed(live)
    assert await asyncio.wait_for(parked, TIMEOUT_S) is live


async def test_a_configure_never_waits_for_a_probe_that_is_already_in_flight(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The inversion, and §4.4's whole point.

    While the consumer ran on the message pump, a retarget landing mid-probe was
    made to WAIT: the probe's ghost check and the bookkeeping it guarded had to
    stay one act, and the lock is what kept them one. Here the expensive halves
    of a tick - the capture and ``detector.observe`` - are outside the lock by
    construction, because a UI thread blocking on a template search is the stall
    the whole split exists to remove. So the same race must now resolve the
    other way: the configure walks straight past the probe in flight, and the
    tick that probe eventually produces is dropped on its stamp instead.

    Driven from a thread of its own with a bounded wait, because the failure
    being ruled out is a *hang* - a deadlocked configure on the event loop would
    take the test session with it rather than fail it.
    """
    wiring = wire()
    inside = threading.Event()
    release = threading.Event()

    def park() -> None:
        inside.set()
        release.wait(TIMEOUT_S)

    ticking = snapshot(stale=StaleProbe(StaleState.CHANGING, 0.5, 0))
    compose_with(ScriptedDetector(ticking, on_observe=park))
    await wiring.monitor.configure(spec())
    await await_until(inside.is_set, "the poller reaching its detector")

    landed = threading.Event()

    def retarget() -> None:
        asyncio.run(wiring.monitor.configure(spec(region=None)))
        landed.set()

    threading.Thread(target=retarget, name="fake-ui", daemon=True).start()
    walked_past = landed.wait(2.0)
    release.set()
    assert walked_past, "configure blocked on a probe that was still in flight"

    await await_until(lambda: not _live_pollers(), "the superseded loop ending")
    assert wiring.monitor.generation == 2
    assert wiring.ticks == [], "the stale run's tick was published anyway"


async def test_a_subscriber_that_blocks_cannot_stall_a_configure(
    wire: Callable[..., Wiring],
) -> None:
    """The other half of §4.4. Hooks are called outside the lock on purpose -
    the GUI's detection panel and the automation's own bookkeeping both hang off
    this seam - so a slow one delays the next tick and nothing else. Under the
    lock it would stall the very ``configure`` trying to retarget away from it,
    which is the freeze a user would report as "the app hung when I clicked".
    """
    wiring = wire()
    inside = threading.Event()
    release = threading.Event()

    def slow(_tick: Tick) -> None:
        inside.set()
        release.wait(TIMEOUT_S)

    wiring.monitor.subscribe(slow)
    await wiring.monitor.configure(spec())
    await await_until(inside.is_set, "the subscriber being called")

    landed = threading.Event()

    def retarget() -> None:
        asyncio.run(wiring.monitor.configure(spec(region=None)))
        landed.set()

    threading.Thread(target=retarget, name="fake-ui", daemon=True).start()
    walked_past = landed.wait(2.0)
    release.set()
    assert walked_past, "configure blocked behind a subscriber that was still painting"


# == stopping ==================================================================


async def test_suspend_cancels_the_run_without_bumping_the_generation(
    wire: Callable[..., Wiring],
) -> None:
    """A suspend has not MOVED the monitor anywhere - the capture overlay is
    about to own the screen and the window is where it was - so the ticks the
    interrupted loop is still finishing are honest readings of it and dropping
    them as ghosts would be a lie. Hence: cancel the thread, leave the counter
    alone, and let the resume put the SAME detector back on a new one.
    """
    wiring = wire()
    await wiring.monitor.configure(spec())
    poller = wiring.monitor.poller
    assert poller is not None
    await await_until(lambda: bool(wiring.ticks), "the first tick")

    await wiring.monitor.suspend()

    assert poller.is_cancelled is True
    assert wiring.monitor.poller is None
    assert wiring.monitor.generation == 1, "a suspend counted as a retarget"
    poller.thread.join(timeout=TIMEOUT_S)
    assert not poller.thread.is_alive()

    # A stop flag nobody reads would keep ticking here.
    quiet = len(wiring.ticks)
    await asyncio.sleep(0.05)
    assert len(wiring.ticks) == quiet


async def test_close_stops_the_poller_for_good(wire: Callable[..., Wiring]) -> None:
    """Idempotent, and final: after a close there is no thread left holding the
    screen, and a second close is not an error - shutdown paths run twice."""
    wiring = wire()
    before = len(_live_pollers())
    await wiring.monitor.configure(spec())
    poller = wiring.monitor.poller
    assert poller is not None

    await wiring.monitor.close()
    await wiring.monitor.close()

    poller.thread.join(timeout=TIMEOUT_S)
    assert not poller.thread.is_alive()
    assert len(_live_pollers()) == before
    quiet = len(wiring.ticks)
    await asyncio.sleep(0.05)
    assert len(wiring.ticks) == quiet


async def test_a_cancelled_loop_exits_within_a_sleep_slice(
    wire: Callable[..., Wiring],
) -> None:
    """The reason the loop sleeps in 0.05s steps instead of one ``poll_seconds``.

    A poller watching a real window sleeps half a second between ticks, and a
    cancel that had to wait that out would be felt at every retarget and at
    every exit. So the wait is chopped, and this is the test that would catch it
    being un-chopped: a five-second cadence, cancelled, and joined in a fraction
    of one tick.
    """
    wiring = wire(poll_seconds=5.0)
    await wiring.monitor.configure(spec())
    poller = wiring.monitor.poller
    assert poller is not None
    await await_until(lambda: bool(wiring.ticks), "the first tick")

    started = time.monotonic()
    await wiring.monitor.suspend()
    poller.thread.join(timeout=1.0)

    assert not poller.thread.is_alive(), "the loop slept through its own cancellation"
    assert time.monotonic() - started < 1.0
