"""Staleness detection: the frame-to-frame stability tracker (screen/stale.py).

Pure tests with synthetic frames and an injected capture callable - the streak
logic is the whole detector, so it is pinned down without a screen. The frames
are fed as a script: each poll consumes the next entry, ``None`` meaning "the
capture failed this tick".
"""

from __future__ import annotations

import pytest

from agentclip.screen import busy
from agentclip.screen.busy import diff_fraction
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.region import ScreenRegion
from agentclip.screen.stale import (
    STALE_MAX_DIFF,
    STALE_MAX_SAMPLES,
    StaleProbe,
    StaleState,
    StaleTracker,
)

REGION = ScreenRegion(400, 300, 200, 120)


def solid(colour: tuple[int, int, int] = (0, 0, 0), width: int = 8, height: int = 6) -> RegionImage:
    """A uniformly coloured BGRX frame (the X byte is left at 0)."""
    blue, green, red = colour
    return RegionImage(width, height, bytes((blue, green, red, 0)) * (width * height))


def scripted(frames: list[RegionImage | None]) -> StaleTracker:
    """A tracker whose captures replay ``frames`` in order (None = CaptureError)."""
    iterator = iter(frames)

    def capture(region: ScreenRegion) -> RegionImage:
        frame = next(iterator)
        if frame is None:
            raise CaptureError("scripted failure")
        return frame

    return StaleTracker(REGION, required_ticks=2, capture=capture)


BLACK = solid((0, 0, 0))
WHITE = solid((255, 255, 255))


def test_the_first_frame_is_changing_with_no_diff() -> None:
    """With nothing to compare against, claiming stillness would let a chat
    that was idle at calibration time read as finished without any generation
    ever observed."""
    tracker = scripted([BLACK])
    assert tracker.poll() == StaleProbe(StaleState.CHANGING, None, 0)


def test_the_streak_accumulates_to_stale_at_required_ticks() -> None:
    tracker = scripted([BLACK, BLACK, BLACK, BLACK])
    assert tracker.poll().state is StaleState.CHANGING  # first frame
    assert tracker.poll() == StaleProbe(StaleState.CHANGING, 0.0, 1)
    assert tracker.poll() == StaleProbe(StaleState.STALE, 0.0, 2)
    # And it stays stale while nothing moves.
    assert tracker.poll() == StaleProbe(StaleState.STALE, 0.0, 3)


def test_required_ticks_one_means_the_second_quiet_poll_is_already_stale() -> None:
    iterator = iter([BLACK, BLACK])
    tracker = StaleTracker(REGION, required_ticks=1, capture=lambda region: next(iterator))
    assert tracker.poll().state is StaleState.CHANGING
    assert tracker.poll() == StaleProbe(StaleState.STALE, 0.0, 1)


def test_a_change_resets_the_streak_to_zero() -> None:
    tracker = scripted([BLACK, BLACK, WHITE, WHITE, WHITE])
    tracker.poll()  # first frame
    assert tracker.poll().stable_ticks == 1
    changed = tracker.poll()  # black -> white: everything moved
    assert changed == StaleProbe(StaleState.CHANGING, 1.0, 0)
    # The streak rebuilds from the new content.
    assert tracker.poll().stable_ticks == 1
    assert tracker.poll() == StaleProbe(StaleState.STALE, 0.0, 2)


def test_a_capture_error_keeps_the_streak_and_the_stored_frame() -> None:
    """The busy prober's blip-tolerance, mirrored: one bad frame must not
    silently restart the stillness clock on an in-flight finish."""
    tracker = scripted([BLACK, BLACK, None, BLACK])
    tracker.poll()
    assert tracker.poll().stable_ticks == 1
    assert tracker.poll() == StaleProbe(StaleState.ERROR, None, 1)
    # The next good frame still compares against the pre-error frame.
    assert tracker.poll() == StaleProbe(StaleState.STALE, 0.0, 2)


def test_frames_compare_to_the_previous_frame_not_an_anchor() -> None:
    """A slow stream must read as changing on EVERY tick: each frame differs
    from the one before it, even though consecutive pairs never repeat."""
    tracker = scripted([solid((v, v, v)) for v in (0, 60, 120, 180, 240)])
    tracker.poll()
    for _ in range(4):
        probe = tracker.poll()
        assert probe == StaleProbe(StaleState.CHANGING, 1.0, 0)


def test_reset_forgets_the_frame_and_the_streak() -> None:
    tracker = scripted([BLACK, BLACK, BLACK, BLACK])
    tracker.poll()
    tracker.poll()
    tracker.reset()
    # Back to first-frame semantics: no diff, no streak, no verdict.
    assert tracker.poll() == StaleProbe(StaleState.CHANGING, None, 0)
    assert tracker.poll().stable_ticks == 1


def test_noise_below_max_diff_still_counts_as_quiet() -> None:
    """A blinking caret or a hover glow is a handful of pixels in a whole
    response region - well under STALE_MAX_DIFF, so it must not hold the
    verdict open forever."""
    quiet = solid((100, 100, 100), width=100, height=100)
    pixels = bytearray(quiet.pixels)
    pixels[0] = 255  # one pixel out of 10,000 changed hard
    barely = RegionImage(100, 100, bytes(pixels))
    tracker = scripted([quiet, barely, quiet])
    tracker.poll()
    assert tracker.poll().stable_ticks == 1
    assert tracker.poll() == StaleProbe(StaleState.STALE, pytest.approx(0.0001), 2)


# -- the denser sampling that makes the sensitive threshold trustworthy --------


def _with_appended_line(base: RegionImage) -> RegionImage:
    """``base`` plus one 8-px-tall stripe of new 'text' at the bottom."""
    line_rows = 8
    pixels = bytearray(base.pixels)
    start = (base.height - line_rows) * base.width * 4
    pixels[start:] = bytes((255, 255, 255, 0)) * (line_rows * base.width)
    return RegionImage(base.width, base.height, bytes(pixels))


def test_a_single_appended_text_line_registers_at_the_stale_sample_budget() -> None:
    before = solid((0, 0, 0), width=200, height=200)
    after = _with_appended_line(before)
    diff = diff_fraction(before, after, max_samples=STALE_MAX_SAMPLES)
    assert diff > STALE_MAX_DIFF  # the whole point: one line must not slip through


def test_diff_fraction_default_sampling_is_unchanged() -> None:
    """The ``max_samples`` keyword must not have moved busy.py's behaviour: the
    default IS the old module constant, so every existing caller samples the
    exact same pixels as before."""
    assert busy.MAX_SAMPLES == 4096
    a = solid((0, 0, 0), width=500, height=500)
    half = bytes((0, 0, 0, 0)) * (500 * 500 // 2)
    b = RegionImage(500, 500, half + bytes((255, 255, 255, 0)) * (500 * 500 // 2))
    assert diff_fraction(a, b) == diff_fraction(a, b, max_samples=busy.MAX_SAMPLES)
    # And a denser budget still lands on the same true fraction here.
    assert diff_fraction(a, b, max_samples=STALE_MAX_SAMPLES) == pytest.approx(0.5, abs=0.1)
