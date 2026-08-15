"""De-bounced appearance presence (screen/presence.py).

Both polarities are walked tick by tick, because the asymmetry IS the design:
"still generating" is believed on sight, "finished" has to be earned. The
scenes are synthetic - a noise background with the appearance stamped in or
left out - so the whole truth table is pinned down without a screen.
"""

from __future__ import annotations

import random

from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.template import Template


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
    return PresenceTracker((TEMPLATE,), found_is_busy=True, required_ticks=required_ticks)


def idle_tracker(required_ticks: int = 3) -> PresenceTracker:
    """A tracker for an idle-time appearance: found means finished."""
    return PresenceTracker((TEMPLATE,), found_is_busy=False, required_ticks=required_ticks)


def states(tracker: PresenceTracker, scenes: list[RegionImage | None]) -> list[BusyState]:
    return [tracker.observe(scene).state for scene in scenes]


# -- the busy-time appearance (the stop button) -------------------------------


def test_a_visible_busy_appearance_is_believed_immediately() -> None:
    probe = busy_tracker().observe(PRESENT)
    assert probe == BusyProbe(BusyState.MATCH, 0.0, True)


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


# -- the raw per-frame fact, next to the de-bounced verdict -------------------
#
# ``generating_now`` exists because the two are not the same claim: for the
# whole settling window after a reset the VERDICT says "generating" with
# nothing on screen behind it, and the TUI armed its auto-copy off exactly that
# (tests/shell/tui/test_finish_signal_ui.py pins the screen's half). Everything above
# this line is the verdict's behaviour and is deliberately unchanged.


def test_a_busy_match_this_frame_is_evidence() -> None:
    assert busy_tracker().observe(PRESENT).generating_now is True


def test_the_busy_graces_generating_default_is_not_evidence() -> None:
    """THE regression: three of these ticks read "generating" on nothing at all.

    The button is gone from the first frame; the verdict withholds "finished"
    until the streak completes, which is the whole design - but no frame in that
    window ever saw a reasoning icon, and none of them may claim to have.
    """
    tracker = busy_tracker(required_ticks=3)
    for _ in range(2):
        probe = tracker.observe(ABSENT)
        assert probe.state is BusyState.MATCH  # the verdict still says generating
        assert probe.generating_now is False  # ...on no evidence, and says so
    assert tracker.observe(ABSENT).state is BusyState.CHANGED


def test_a_busy_miss_and_an_error_are_never_evidence() -> None:
    tracker = busy_tracker(required_ticks=1)
    tracker.observe(PRESENT)
    assert tracker.observe(ABSENT).generating_now is False
    assert tracker.observe(None).generating_now is False


def test_an_idle_appearance_missing_from_the_start_is_not_evidence() -> None:
    """A composer mid-layout on a fresh page looks exactly like this, and a
    still, unanswered chat can hold the look for ever - so absence alone may
    not arm anything, however loudly the verdict reads "generating"."""
    tracker = idle_tracker()
    for _ in range(5):
        probe = tracker.observe(ABSENT)
        assert probe.state is BusyState.CHANGED  # "generating", as ever
        assert probe.generating_now is False


def test_an_idle_appearance_watched_to_go_is_evidence() -> None:
    """Once it has genuinely been there, its absence is a transition - the send
    button that was on screen is not any more, which is a send."""
    tracker = idle_tracker()
    assert tracker.observe(PRESENT).generating_now is False  # present = not generating
    assert tracker.observe(ABSENT).generating_now is True


def test_reset_forgets_the_sighting_too() -> None:
    """It is a claim about the frames since the reset. An idle appearance seen
    before a paste says nothing about the turn the paste starts."""
    tracker = idle_tracker()
    tracker.observe(PRESENT)
    tracker.reset()
    assert tracker.observe(ABSENT).generating_now is False
    tracker.observe(PRESENT)
    assert tracker.observe(ABSENT).generating_now is True


def test_a_scene_too_small_to_hold_the_appearance_reads_as_missing() -> None:
    """A resized window is a runtime condition, not a crash."""
    tracker = busy_tracker(required_ticks=1)
    tracker.observe(PRESENT)
    assert tracker.observe(noise(10, 10, seed=3)).state is BusyState.CHANGED


# -- a kind is a stack of images, ORed -------------------------------------------

SECOND_BUTTON = noise(20, 16, seed=9)  # the same control, drawn a second way
SECOND_TEMPLATE = Template.build(SECOND_BUTTON)
SECOND_PRESENT = paste(ABSENT, SECOND_BUTTON, 30, 20)


def test_any_of_a_kinds_images_being_on_screen_counts_as_found() -> None:
    """The motivating case: a send button greyed out while a file uploads is a
    different picture of the same control, and either one means it is there."""
    tracker = PresenceTracker(
        (TEMPLATE, SECOND_TEMPLATE), found_is_busy=True, required_ticks=1
    )
    assert tracker.observe(SECOND_PRESENT).state is BusyState.MATCH
    assert tracker.observe(PRESENT).state is BusyState.MATCH
    assert tracker.observe(ABSENT).state is BusyState.CHANGED


def test_a_tracker_with_no_images_finds_nothing() -> None:
    """Not a crash: the caller decides whether an empty stack is worth polling."""
    tracker = PresenceTracker((), found_is_busy=True, required_ticks=1)
    assert tracker.observe(PRESENT) == BusyProbe(BusyState.CHANGED, None)


# -- where it was, not only whether it was --------------------------------------
#
# ``last_sighting`` is the spatial counterpart of ``generating_now``: the one
# frame's own answer, next to the verdict about the sequence. Nothing in the
# detector reads it - it exists so the TUI can show the matched pixels - so what
# is pinned here is that it says the truth about the LAST searched frame and
# never leaks a claim from an older one.


def test_a_fresh_tracker_has_seen_nothing() -> None:
    assert busy_tracker().last_sighting is None


def test_the_sighting_names_the_variant_and_where_it_matched() -> None:
    """Both halves are needed to cut the crop: the match carries the corner and
    the winning template carries the size."""
    tracker = busy_tracker()
    tracker.observe(PRESENT)
    sighting = tracker.last_sighting
    assert sighting is not None
    template, match = sighting
    assert template is TEMPLATE
    assert (match.x, match.y) == (60, 40)  # where `paste` put it


def test_a_frame_without_the_appearance_clears_the_sighting() -> None:
    """Per-frame, not cumulative - a picture of where the button used to be is
    exactly the lie the panel must not tell."""
    tracker = busy_tracker()
    tracker.observe(PRESENT)
    tracker.observe(ABSENT)
    assert tracker.last_sighting is None


def test_the_sighting_survives_a_failed_capture() -> None:
    """An ERROR frame is not a frame: it leaves the streak alone (the whole
    blip-tolerance design) and leaves this alone for the same reason."""
    tracker = busy_tracker()
    tracker.observe(PRESENT)
    tracker.observe(None)
    sighting = tracker.last_sighting
    assert sighting is not None
    assert (sighting[1].x, sighting[1].y) == (60, 40)


def test_the_sighting_names_whichever_variant_actually_matched() -> None:
    """A kind is a stack, and the crop's size is the size of the image that won."""
    tracker = PresenceTracker(
        (TEMPLATE, SECOND_TEMPLATE), found_is_busy=True, required_ticks=1
    )
    tracker.observe(SECOND_PRESENT)
    sighting = tracker.last_sighting
    assert sighting is not None
    assert sighting[0] is SECOND_TEMPLATE
    assert (sighting[1].x, sighting[1].y) == (30, 20)


def test_reset_forgets_where_it_was() -> None:
    """Same reason as the streak and the sighting flag: a rectangle from before
    the reset is not a rectangle from now."""
    tracker = busy_tracker()
    tracker.observe(PRESENT)
    tracker.reset()
    assert tracker.last_sighting is None


def test_an_idle_tracker_reports_its_sighting_the_same_way() -> None:
    """Polarity is the verdict's business, not the picture's: the send button
    being on screen is a sighting whichever way it is being read."""
    tracker = idle_tracker()
    tracker.observe(PRESENT)
    assert tracker.last_sighting is not None
    tracker.observe(ABSENT)
    assert tracker.last_sighting is None


def test_fresh_is_a_reset_that_hands_back_a_new_object() -> None:
    """The swap spelling of ``reset``, for a caller on another thread.

    Same calibration - the images, the polarity, the thresholds - and none of
    the history: the tracker still in flight keeps whatever it was writing, and
    the one the caller installs remembers nothing (automation/controller.py's
    ``reset_trackers``).
    """
    tracker = busy_tracker(required_ticks=3)
    tracker.observe(PRESENT)
    states(tracker, [ABSENT, ABSENT])  # two of the three misses banked

    spare = tracker.fresh()

    assert spare is not tracker
    assert spare.last_sighting is None
    # The streak did not come with it: three misses from scratch, not one.
    assert states(spare, [ABSENT, ABSENT]) == [BusyState.MATCH, BusyState.MATCH]
    assert spare.observe(ABSENT).state is BusyState.CHANGED
    # ...and it is still looking for the same appearance, the same way round.
    assert spare.observe(PRESENT).generating_now is True
    # The original is untouched - handing back a copy is what lets a poll that
    # is still mid-search finish into an object nobody will read again.
    assert tracker.observe(ABSENT).state is BusyState.CHANGED
