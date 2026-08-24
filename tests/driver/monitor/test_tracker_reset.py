"""Clearing a tracker's debounce while the poller is polling it.

The one race the poll thread introduced, and the one this file exists to keep
closed. Until the detectors got a thread of their own they were polled from the
message pump, so ``reset_trackers`` - the paste, the send, the end of the
auto-copy flow - could not possibly land in the middle of a poll. Now it can,
and a tracker is exactly the wrong shape for that: both ``PresenceTracker.
observe`` and ``StaleTracker._fold`` READ the streak, spend a template search or
a full-region diff, and then WRITE the streak back. A reset landing inside that
gap is read-modify-written away a frame later, and what comes back is the
history the reset existed to throw out - the frames AgentClip produced by
clicking, scrolling and pasting into the very window being watched.

``_tick_lock`` cannot close this at its own grain: the expensive halves of a
tick are deliberately outside it (§4.4), and an event-loop thread blocking on a
template search is the stall the whole split exists to remove. So the fix is to
SWAP rather than clear (§4.3), and these tests are about the swap - with a real
poller thread, parked with a hand on it exactly where the lost update used to
happen.

The frames are real ones: noise the template matcher can genuinely anchor on,
with a "button" pasted into it, because a swap that quietly forgot its
calibration would pass every assertion a stub detector could make and then
report "finished" forever on a chat it is no longer even searching.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable

from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.detector import ScreenDetector
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.stale import StaleTracker
from agentclip.driver.screen.template import RegionMatch, Template

from .conftest import TIMEOUT_S, Wiring, await_until, spec


def _noise(width: int, height: int, seed: int) -> RegionImage:
    """A frame varied enough for a template search to anchor on."""
    rng = random.Random(seed)
    pixels = bytearray()
    for _ in range(width * height):
        pixels += bytes((rng.randrange(256), rng.randrange(256), rng.randrange(256), 0))
    return RegionImage(width, height, bytes(pixels))


def _paste(scene: RegionImage, patch: RegionImage, x: int, y: int) -> RegionImage:
    pixels = bytearray(scene.pixels)
    row = patch.width * 4
    for ty in range(patch.height):
        start = ((y + ty) * scene.width + x) * 4
        pixels[start : start + row] = patch.pixels[ty * row : (ty + 1) * row]
    return RegionImage(scene.width, scene.height, bytes(pixels))


BUTTON = _noise(20, 16, seed=2)
TEMPLATE = Template.build(BUTTON)
ABSENT = _noise(140, 90, seed=1)  # the chat region with the appearance missing
PRESENT = _paste(ABSENT, BUTTON, 60, 40)  # ...and with it on screen
CHAT = ScreenRegion(0, 0, ABSENT.width, ABSENT.height)


def _busy_tracker() -> PresenceTracker:
    return PresenceTracker((TEMPLATE,), found_is_busy=True)


class ParkedTracker(PresenceTracker):
    """A tracker that stops inside the frame search and waits to be let go.

    The race's window, with a hand on it. ``observe`` reads nothing before
    ``_find`` and writes everything after it, so a thread parked here is a poll
    that has already decided what it is going to write and has not written it
    yet - the only moment a reset can be lost.
    """

    def __init__(self, templates: tuple[Template, ...]) -> None:
        super().__init__(templates, found_is_busy=True)
        self.searching = threading.Event()
        self.release = threading.Event()

    def _find(self, scene: RegionImage) -> tuple[Template, RegionMatch] | None:
        self.searching.set()
        assert self.release.wait(TIMEOUT_S), "the test never released the parked poll"
        return super()._find(scene)


def _dead_capture(_region: ScreenRegion) -> RegionImage:
    """A capture that always fails. An ERROR leaves every tracker's streak AND
    stored frame untouched, so a monitor wired to this one has a genuine poller
    running and no tick of it can touch what the test is measuring."""
    raise CaptureError("nothing to see")


async def _quiet(wiring: Wiring) -> None:
    """Stop the run and join its thread, so what follows is the only thing
    touching the trackers. A suspend rather than a close: the detector and the
    trackers have to survive, because they are the subject."""
    poller = wiring.monitor.poller
    await wiring.monitor.suspend()
    if poller is not None:
        poller.thread.join(TIMEOUT_S)
        assert not poller.thread.is_alive()


# == the race itself ===========================================================


async def test_a_reset_is_not_undone_by_a_poll_that_is_already_searching(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """A poll parked mid-search, a reset, and then the poll lands.

    Clearing the tracker in place fails here: the parked poll writes its sighting
    back over the reset and the monitor is still pointing at that object, so the
    frame AgentClip's own paste produced is back in the history the reset existed
    to throw out. Swapping cannot fail that way - the write lands in the tracker
    that was REPLACED, and both the monitor and the detector have already moved
    on to one that has genuinely seen nothing.

    The suspend before the release is what keeps the assertion about the reset
    rather than about the next tick: with the run ended, the parked poll is the
    last frame this detector will ever fold.
    """
    wiring = wire(capture=lambda _region: PRESENT)
    parked = ParkedTracker((TEMPLATE,))
    detector = ScreenDetector(CHAT, busy=parked)
    compose_with(detector)
    await wiring.monitor.configure(spec(region=CHAT))
    await await_until(parked.searching.is_set, "the poll reaching its search")

    # The event-loop thread, mid-search: a paste, a send, or the flow ending.
    wiring.monitor.reset_trackers()
    poller = wiring.monitor.poller
    assert poller is not None
    await wiring.monitor.suspend()  # ...so the parked poll is this run's LAST frame
    parked.release.set()
    poller.thread.join(TIMEOUT_S)
    assert not poller.thread.is_alive()

    live = wiring.monitor.busy_tracker
    assert live is not None
    # The behaviour first: what the reset threw away has to stay thrown away.
    assert live.last_sighting is None, "the parked poll wrote its frame back over the reset"
    # Then the mechanism that makes it true - and it is not that the frame
    # vanished: it landed in the tracker nobody reads any more.
    assert live is not parked, "the reset cleared in place instead of swapping"
    assert parked.last_sighting is not None
    # The poller reads its trackers through the DETECTOR, so a swap that does
    # not reach it leaves the next tick folding into the tracker just replaced.
    assert detector.busy is live


# == what a swapped tracker is =================================================


async def test_the_swapped_tracker_still_knows_what_it_is_looking_for(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """A swap is a reset, not a rebuild: same calibration, empty history.

    Otherwise the cure is worse than the disease - a tracker that forgot its
    templates finds nothing on every frame and so reports "finished" forever, on
    a chat it is no longer even searching.
    """
    wiring = wire(capture=_dead_capture)
    detector = ScreenDetector(CHAT, busy=_busy_tracker())
    compose_with(detector)
    await wiring.monitor.configure(spec(region=CHAT))
    await _quiet(wiring)
    original = wiring.monitor.busy_tracker
    assert original is not None
    original.observe(PRESENT)

    wiring.monitor.reset_trackers()

    live = wiring.monitor.busy_tracker
    assert live is not None and live is not original
    assert live.last_sighting is None
    # The same appearance, found the same way, on the frame that carries it.
    assert live.observe(PRESENT).generating_now is True
    assert live.last_sighting is not None
    assert live.observe(ABSENT).diff is None


async def test_the_stale_tracker_is_swapped_too_and_forgets_its_frame(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The staleness detector has the same read-diff-write shape and the same
    cure - and the frame it holds matters more than the streak: comparing a
    frame the flow scrolled against the one after it is how a harvest reads as
    stillness."""
    wiring = wire(capture=_dead_capture)
    detector = ScreenDetector(CHAT, stale=StaleTracker(CHAT, required_ticks=2))
    compose_with(detector)
    await wiring.monitor.configure(spec(region=CHAT))
    await _quiet(wiring)
    original = wiring.monitor.stale_tracker
    assert original is not None
    original.observe(ABSENT)
    assert original.observe(ABSENT).stable_ticks == 1

    wiring.monitor.reset_trackers()

    live = wiring.monitor.stale_tracker
    assert live is not None and live is not original
    assert detector.stale is live
    # No previous frame, so the first one after the reset is CHANGING by
    # definition - the streak did not carry across.
    assert live.observe(ABSENT).stable_ticks == 0


async def test_a_tracker_the_detector_does_not_hold_is_left_where_it_is(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The identity guard. The monitor holds the three trackers under their own
    names AND the detector holds them as its composition; a reset patches the
    detector's slot only when what is in it is the very object the monitor is
    replacing. Anything else in there belongs to somebody else - a rebuild in
    flight, a calibration window - and overwriting it with a tracker that
    somebody else's poll is not reading would leave two objects each convinced
    it is the live one.
    """
    wiring = wire(capture=_dead_capture)
    detector = ScreenDetector(CHAT, busy=_busy_tracker())
    compose_with(detector)
    await wiring.monitor.configure(spec(region=CHAT))
    await _quiet(wiring)
    mine = wiring.monitor.busy_tracker
    assert mine is not None and detector.busy is mine

    # ...and now something else puts its own tracker on the live detector.
    borrowed = _busy_tracker()
    detector.busy = borrowed

    wiring.monitor.reset_trackers()

    assert detector.busy is borrowed, "the reset overwrote a tracker that was not its own"
    swapped = wiring.monitor.busy_tracker
    assert swapped is not mine and swapped is not borrowed
    # The slot the detector does NOT hold is still swapped rather than cleared:
    # a poll in flight on the old one has to land somewhere unread either way.
    assert isinstance(swapped, PresenceTracker)


async def test_a_reset_with_nothing_configured_is_harmless(
    wire: Callable[..., Wiring],
) -> None:
    """The flows call this unconditionally, and a monitor watching nothing is an
    ordinary state (no region, no profile, nothing worth watching)."""
    wiring = wire()
    wiring.monitor.reset_trackers()
    await wiring.monitor.configure(spec(region=None))
    wiring.monitor.reset_trackers()

    assert wiring.monitor.busy_tracker is None
    assert wiring.monitor.idle_tracker is None
    assert wiring.monitor.stale_tracker is None
