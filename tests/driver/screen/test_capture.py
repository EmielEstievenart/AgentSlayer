"""Screen capture. The real-screen tests need Windows GDI and a live desktop."""

from __future__ import annotations

import sys

import pytest

from agentclip.driver.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.driver.screen.region import ScreenRegion

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows GDI capture")


@windows_only
def test_capture_returns_a_bgrx_frame_of_the_right_size() -> None:
    image = capture_region(ScreenRegion(0, 0, 16, 12))
    assert isinstance(image, RegionImage)
    assert (image.width, image.height) == (16, 12)
    assert len(image.pixels) == 16 * 12 * 4


@windows_only
def test_capture_is_repeatable_on_a_static_area() -> None:
    """Two back-to-back grabs of the same pixels must be comparable frames."""
    region = ScreenRegion(0, 0, 8, 8)
    first, second = capture_region(region), capture_region(region)
    assert (first.width, first.height) == (second.width, second.height)
    assert len(first.pixels) == len(second.pixels)


@pytest.mark.parametrize("size", [(0, 10), (10, 0), (-4, 4)])
def test_empty_region_raises(size: tuple[int, int]) -> None:
    with pytest.raises(CaptureError):
        capture_region(ScreenRegion(0, 0, *size))


@pytest.mark.skipif(sys.platform == "win32", reason="only other platforms refuse outright")
def test_capture_is_unavailable_off_windows() -> None:
    with pytest.raises(CaptureError):
        capture_region(ScreenRegion(0, 0, 16, 12))
