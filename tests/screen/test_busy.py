"""Busy detection: comparing a fresh capture against the calibration baseline."""

from __future__ import annotations

import pytest

from agentclip.screen import busy
from agentclip.screen.busy import (
    DEFAULT_TOLERANCE,
    BusyProbe,
    BusyState,
    diff_fraction,
    probe_busy,
)
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.region import ScreenRegion

REGION = ScreenRegion(120, 240, 32, 24)


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


def test_probe_matches_when_the_region_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = solid(32, 24, (7, 7, 7))
    monkeypatch.setattr(busy, "capture_region", lambda region: solid(32, 24, (7, 7, 7)))
    assert probe_busy(baseline, REGION) == BusyProbe(BusyState.MATCH, 0.0)


def test_probe_reports_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = solid(32, 24, (0, 0, 0))
    monkeypatch.setattr(busy, "capture_region", lambda region: solid(32, 24, (255, 255, 255)))
    probe = probe_busy(baseline, REGION)
    assert probe.state is BusyState.CHANGED
    assert probe.diff == 1.0


def test_probe_reports_error_when_capture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("no display")

    monkeypatch.setattr(busy, "capture_region", boom)
    assert probe_busy(solid(32, 24), REGION) == BusyProbe(BusyState.ERROR, None)


def test_probe_honours_max_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """One differing pixel in a small region is enough to exceed the default."""
    baseline = solid(4, 4, (0, 0, 0))
    pixels = bytearray(baseline.pixels)
    pixels[0] = 255
    monkeypatch.setattr(busy, "capture_region", lambda region: RegionImage(4, 4, bytes(pixels)))
    assert probe_busy(baseline, REGION).state is BusyState.CHANGED
    assert probe_busy(baseline, REGION, max_diff=0.5).state is BusyState.MATCH
