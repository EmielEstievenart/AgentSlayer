"""De-bounced appearance presence (screen/presence.py).

Both polarities are walked tick by tick, because the asymmetry IS the design:
"still generating" is believed on sight, "finished" has to be earned. The
scenes are synthetic - a noise background with the appearance stamped in or
left out - so the whole truth table is pinned down without a screen.
"""

from __future__ import annotations

import random

from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import RegionImage
from agentclip.screen.presence import PresenceTracker
from agentclip.screen.template import Template


def noise(width: int, height: int, seed: int = 1) -> RegionImage:
    rng = random.Random(seed)
    pixels = bytearray()
    for _ in range(width * height):
        pixels += bytes((rng.randrange(256), rng.randrange(256), rng.randrange(256), 0))
    return RegionImage(width, height, bytes(pixels))


def paste(scene: RegionImage, patch: RegionImage, x: int, y: int) -> RegionImage:
    pixels = bytearray(scene.pixels)
    row = patch.width * 4
    for ty in range(patch.height):
        start = ((y + ty) * scene.width + x) * 4
        pixels[start : start + row] = patch.pixels[ty * row : (ty + 1) * row]
    return RegionImage(scene.width, scene.height, bytes(pixels))


BUTTON = noise(20, 16, seed=2)
TEMPLATE = Template.build(BUTTON)
ABSENT = noise(140, 90, seed=1)  # the chat region with no button in it
PRESENT = paste(ABSENT, BUTTON, 60, 40)  # ...and with one


def busy_tracker(required_ticks: int = 3) -> PresenceTracker:
    """A tracker for a busy-time appearance: found means still generating."""
    return PresenceTracker(TEMPLATE, found_is_busy=True, required_ticks=required_ticks)


def idle_tracker(required_ticks: int = 3) -> PresenceTracker:
    """A tracker for an idle-time appearance: found means finished."""
    return PresenceTracker(TEMPLATE, found_is_busy=False, required_ticks=required_ticks)


def states(tracker: PresenceTracker, scenes: list[RegionImage | None]) -> list[BusyState]:
    return [tracker.observe(scene).state for scene in scenes]


# -- the busy-time appearance (the stop button) -------------------------------


def test_a_visible_busy_appearance_is_believed_immediately() -> None:
    probe = busy_tracker().observe(PRESENT)
    assert probe == BusyProbe(BusyState.MATCH, 0.0)


def test_the_busy_appearance_must_be_missing_for_the_whole_streak() -> None:
    """Losing the stop button for one frame is a repaint, not an answer."""
    tracker = busy_tracker(required_ticks=3)
    assert states(tracker, [PRESENT, ABSENT, ABSENT]) == [
        BusyState.MATCH,
        BusyState.MATCH,
        BusyState.MATCH,
    ]
    assert tracker.observe(ABSENT) == BusyProbe(BusyState.CHANGED, None)
    # ...and it stays finished while the button stays gone.
    assert tracker.observe(ABSENT).state is BusyState.CHANGED


def test_the_busy_appearance_reappearing_restarts_the_streak() -> None:
    tracker = busy_tracker(required_ticks=3)
    assert states(tracker, [ABSENT, ABSENT, PRESENT]) == [
        BusyState.MATCH,
        BusyState.MATCH,
        BusyState.MATCH,
    ]
    assert states(tracker, [ABSENT, ABSENT]) == [BusyState.MATCH, BusyState.MATCH]
    assert tracker.observe(ABSENT).state is BusyState.CHANGED


# -- the idle-time appearance (the send button) -------------------------------


def test_a_missing_idle_appearance_means_generating_immediately() -> None:
    assert idle_tracker().observe(ABSENT) == BusyProbe(BusyState.CHANGED, None)


def test_the_idle_appearance_must_be_present_for_the_whole_streak() -> None:
    tracker = idle_tracker(required_ticks=3)
    assert states(tracker, [PRESENT, PRESENT]) == [BusyState.CHANGED, BusyState.CHANGED]
    assert tracker.observe(PRESENT) == BusyProbe(BusyState.MATCH, 0.0)
    assert tracker.observe(PRESENT).state is BusyState.MATCH


def test_the_idle_appearance_vanishing_restarts_the_streak() -> None:
    tracker = idle_tracker(required_ticks=3)
    states(tracker, [PRESENT, PRESENT])
    assert tracker.observe(ABSENT).state is BusyState.CHANGED
    assert states(tracker, [PRESENT, PRESENT]) == [BusyState.CHANGED, BusyState.CHANGED]
    assert tracker.observe(PRESENT).state is BusyState.MATCH


# -- shared behaviour ---------------------------------------------------------


def test_required_ticks_of_one_decides_on_the_next_frame() -> None:
    busy = busy_tracker(required_ticks=1)
    assert busy.observe(PRESENT).state is BusyState.MATCH
    assert busy.observe(ABSENT).state is BusyState.CHANGED
    idle = idle_tracker(required_ticks=1)
    assert idle.observe(ABSENT).state is BusyState.CHANGED
    assert idle.observe(PRESENT).state is BusyState.MATCH


def test_a_failed_capture_is_an_error_that_leaves_the_streak_intact() -> None:
    """StaleTracker's blip handling, mirrored: a dropped frame mid-streak must
    neither count toward "finished" nor throw away the progress made."""
    tracker = busy_tracker(required_ticks=3)
    tracker.observe(PRESENT)
    assert states(tracker, [ABSENT, ABSENT]) == [BusyState.MATCH, BusyState.MATCH]
    assert tracker.observe(None) == BusyProbe(BusyState.ERROR, None)
    assert tracker.observe(ABSENT).state is BusyState.CHANGED  # the third miss, not the fourth


def test_an_error_alone_never_produces_a_verdict() -> None:
    tracker = busy_tracker(required_ticks=1)
    for _ in range(5):
        assert tracker.observe(None) == BusyProbe(BusyState.ERROR, None)


def test_the_diff_is_reported_when_the_appearance_is_found() -> None:
    """The sidebar shows it; it only exists when there was something to match."""
    assert busy_tracker().observe(PRESENT).diff == 0.0
    assert busy_tracker().observe(ABSENT).diff is None


def test_reset_forgets_the_streak() -> None:
    tracker = busy_tracker(required_ticks=3)
    states(tracker, [ABSENT, ABSENT])
    tracker.reset()
    assert states(tracker, [ABSENT, ABSENT]) == [BusyState.MATCH, BusyState.MATCH]
    assert tracker.observe(ABSENT).state is BusyState.CHANGED


def test_a_scene_too_small_to_hold_the_appearance_reads_as_missing() -> None:
    """A resized window is a runtime condition, not a crash."""
    tracker = busy_tracker(required_ticks=1)
    tracker.observe(PRESENT)
    assert tracker.observe(noise(10, 10, seed=3)).state is BusyState.CHANGED
