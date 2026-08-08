"""How different are two captures of the same rectangle (``diff_fraction``).

The sampler every finish detector compares with. Pure - no screen, no state -
so the tolerance, the sample budget and the ignored X byte are all pinned down
here; the detectors that actually look at the screen are tested in
test_presence.py and test_stale.py.
"""

from __future__ import annotations

import pytest

from agentclip.screen.busy import DEFAULT_TOLERANCE, diff_fraction
from agentclip.screen.capture import RegionImage


def solid(width: int, height: int, colour: tuple[int, int, int] = (0, 0, 0)) -> RegionImage:
    """A uniformly coloured BGRX frame (the X byte is left at 0)."""
    blue, green, red = colour
    return RegionImage(width, height, bytes((blue, green, red, 0)) * (width * height))


def test_identical_images_do_not_differ() -> None:
    assert diff_fraction(solid(32, 24, (10, 20, 30)), solid(32, 24, (10, 20, 30))) == 0.0


def test_fully_different_images() -> None:
    assert diff_fraction(solid(32, 24, (0, 0, 0)), solid(32, 24, (255, 255, 255))) == 1.0


def test_noise_below_tolerance_is_not_a_difference() -> None:
    baseline = solid(32, 24, (100, 100, 100))
    noisy = solid(32, 24, (100 + DEFAULT_TOLERANCE, 100 - 5, 100 + 3))
    assert diff_fraction(baseline, noisy) == 0.0


def test_noise_above_tolerance_is_a_difference() -> None:
    baseline = solid(32, 24, (100, 100, 100))
    changed = solid(32, 24, (100, 100, 100 + DEFAULT_TOLERANCE + 1))
    assert diff_fraction(baseline, changed) == 1.0


def test_x_byte_is_ignored() -> None:
    """GDI leaves the 4th byte of each pixel undefined - it must not count."""
    baseline = RegionImage(2, 1, bytes([10, 20, 30, 0, 10, 20, 30, 0]))
    other = RegionImage(2, 1, bytes([10, 20, 30, 255, 10, 20, 30, 255]))
    assert diff_fraction(baseline, other) == 0.0


@pytest.mark.parametrize("size", [(33, 24), (32, 25)])
def test_dimension_mismatch_is_a_full_difference(size: tuple[int, int]) -> None:
    assert diff_fraction(solid(32, 24), solid(*size)) == 1.0


def test_large_region_is_sampled_but_stays_accurate() -> None:
    """A big box must not be compared pixel by pixel, yet still read ~50% here."""
    width = height = 500
    baseline = solid(width, height, (0, 0, 0))
    half = bytes((0, 0, 0, 0)) * (width * height // 2)
    changed = RegionImage(width, height, half + bytes((255, 255, 255, 0)) * (width * height // 2))
    assert diff_fraction(baseline, changed) == pytest.approx(0.5, abs=0.1)
