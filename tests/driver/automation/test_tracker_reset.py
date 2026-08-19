"""Clearing a tracker's debounce while the poller is polling it.

The one race the phase-0 extraction surfaced. Until slice 4 the detectors were
polled from the message pump, so ``reset_trackers`` - the paste, the send, the
end of the auto-copy flow - could not possibly land in the middle of a poll.
Since slice 4 it can, and a tracker is exactly the wrong shape for that: both
``PresenceTracker.observe`` and ``StaleTracker._fold`` READ the streak, spend a
template search or a full-region diff, and then WRITE the streak back. A reset
landing inside that gap is read-modify-written away a frame later, and what
comes back is the history the reset existed to throw out - the frames AgentClip
produced by clicking, scrolling and pasting into the very window being watched.

``_tick_lock`` cannot close this at its own grain: the expensive halves of a
tick are deliberately outside it (``automation/controller.py``'s module
docstring), and a UI thread blocking on a template search is the stall the whole
split exists to remove. So the fix is to swap rather than clear, and these tests
are about the swap - with a real second thread, parked with a hand on it exactly
where the lost update used to happen.
"""

from __future__ import annotations

import random
import threading

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import ScreenDetector
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.stale import StaleTracker
from agentclip.driver.screen.template import RegionMatch, Template

TIMEOUT_S = 5.0


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
REGION = ScreenRegion(0, 0, ABSENT.width, ABSENT.height)


class ParkedTracker(PresenceTracker):
    """A tracker that stops inside the frame search and waits to be let go.

    The race's window, with a hand on it. ``observe`` reads nothing before
    ``_find`` and writes everything after it, so a thread parked here is a poll
    that has already decided what it is going to write and has not written it
    yet - which is the only moment a reset can be lost.
    """

    def __init__(self, templates: tuple[Template, ...]) -> None:
        super().__init__(templates, found_is_busy=True)
        self.searching = threading.Event()
        self.release = threading.Event()

    def _find(self, scene: RegionImage) -> tuple[Template, RegionMatch] | None:
        self.searching.set()
        assert self.release.wait(TIMEOUT_S), "the test never released the parked poll"
        return super()._find(scene)


def _install(automation: AutomationController, detector: ScreenDetector) -> None:
    """Wire a run up the way ``MainScreen._start_detector_worker`` does.

    The trackers under their own names, then the detector handed over as the
    loop is composed. The loop itself is thrown away - what is under test is
    what a reset does to the composition, not the polling.
    """
    automation.busy_tracker = detector.busy
    automation.idle_tracker = detector.idle
    automation.stale_tracker = detector.stale
    automation.detector_loop(detector, REGION, capture=lambda _region: PRESENT, poll_seconds=0.0)


def test_a_reset_is_not_undone_by_a_poll_that_is_already_searching(
    automation: AutomationController,
) -> None:
    """The race itself: a poll parked mid-search, a reset, then the poll lands.

    Clearing the tracker in place fails here - the parked poll writes its
    sighting back over the reset and the controller is still pointing at it.
    Swapping cannot: the frame the flow produced lands in the tracker that was
    replaced, and both the controller and the detector already moved on.
    """
    parked = ParkedTracker((TEMPLATE,))
    detector = ScreenDetector(REGION, busy=parked)
    _install(automation, detector)

    poll = threading.Thread(target=lambda: detector.observe(PRESENT), daemon=True)
    poll.start()
    assert parked.searching.wait(TIMEOUT_S), "the poll never reached the search"

    # The UI thread, mid-search: a paste, a send, or the auto-copy flow ending.
    automation.reset_trackers()

    parked.release.set()
    poll.join(TIMEOUT_S)
    assert not poll.is_alive()

    live = automation.busy_tracker
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


def test_the_swapped_tracker_still_knows_what_it_is_looking_for(
    automation: AutomationController,
) -> None:
    """A swap is a reset, not a rebuild: same calibration, empty history.

    Otherwise the cure is worse - a tracker that forgot its templates reports
    "finished" forever, on a chat it is no longer even searching.
    """
    detector = ScreenDetector(REGION, busy=PresenceTracker((TEMPLATE,), found_is_busy=True))
    _install(automation, detector)
    original = automation.busy_tracker
    assert original is not None
    original.observe(PRESENT)

    automation.reset_trackers()

    live = automation.busy_tracker
    assert live is not None and live is not original
    assert live.last_sighting is None
    # The same appearance, found the same way, on the frame that carries it.
    assert live.observe(PRESENT).generating_now is True
    assert live.last_sighting is not None
    assert live.observe(ABSENT).diff is None


def test_the_stale_tracker_is_swapped_too_and_forgets_its_frame(
    automation: AutomationController,
) -> None:
    """The staleness detector has the same read-diff-write shape and the same
    cure - and the frame it holds matters more than the streak: comparing a
    frame the flow scrolled against the one after it is how a harvest reads as
    stillness."""
    detector = ScreenDetector(REGION, stale=StaleTracker(REGION, required_ticks=2))
    _install(automation, detector)
    original = automation.stale_tracker
    assert original is not None
    original.observe(ABSENT)
    assert original.observe(ABSENT).stable_ticks == 1

    automation.reset_trackers()

    live = automation.stale_tracker
    assert live is not None and live is not original
    assert detector.stale is live
    # No previous frame, so the first one after the reset is CHANGING by
    # definition - the streak did not carry across.
    assert live.observe(ABSENT).stable_ticks == 0


def test_a_tracker_the_detector_does_not_hold_is_swapped_on_its_own(
    automation: AutomationController,
) -> None:
    """The identity guard. A test (or a shell mid-rebuild) can hand this object
    a tracker the live detector knows nothing about; the swap must replace that
    one and leave the detector's composition exactly as it was."""
    detector = ScreenDetector(REGION, busy=PresenceTracker((TEMPLATE,), found_is_busy=True))
    _install(automation, detector)
    detectors_own = detector.busy
    borrowed = PresenceTracker((TEMPLATE,), found_is_busy=True)
    automation.busy_tracker = borrowed

    automation.reset_trackers()

    assert automation.busy_tracker is not borrowed
    assert detector.busy is detectors_own
