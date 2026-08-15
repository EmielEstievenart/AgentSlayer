"""Hover-scan geometry: the cursor stops for one bottom-up sweep of a band.

Pure arithmetic - no screen, no cursor - which is exactly why the step
sequence lives in its own module.
"""

from __future__ import annotations

import pytest

from agentclip.driver.screen.hover import MAX_STEPS, STEP_PX, hover_scan_points
from agentclip.driver.screen.region import ScreenRegion

BAND = ScreenRegion(1800, 200, 24, 400)


def test_scan_starts_at_the_bottom_row() -> None:
    """The newest response is at the bottom, so that is where hovering starts."""
    points = hover_scan_points(BAND)
    assert points[0] == (BAND.left + BAND.width // 2, BAND.top + BAND.height - 1)


def test_every_stop_is_on_the_bands_horizontal_centre() -> None:
    centre = BAND.left + BAND.width // 2
    assert {x for x, _y in hover_scan_points(BAND)} == {centre}


def test_stops_climb_by_one_step_each() -> None:
    points = hover_scan_points(BAND, step_px=10)
    ys = [y for _x, y in points]
    assert ys[:4] == [599, 589, 579, 569]
    assert all(earlier > later for earlier, later in zip(ys, ys[1:], strict=False))


def test_the_scan_is_bounded_by_the_top_of_the_band() -> None:
    """It stops AT the top row - never above it, and never short of it."""
    points = hover_scan_points(BAND, step_px=STEP_PX)
    ys = [y for _x, y in points]
    assert min(ys) == BAND.top
    assert ys[-1] == BAND.top


def test_a_step_that_overshoots_still_lands_on_the_top_row() -> None:
    band = ScreenRegion(0, 100, 24, 30)
    assert hover_scan_points(band, step_px=1000) == [(12, 129), (12, 100)]


def test_a_one_pixel_band_is_a_single_stop() -> None:
    assert hover_scan_points(ScreenRegion(0, 50, 24, 1)) == [(12, 50)]


def test_a_tall_band_is_capped_and_gives_up_below_the_top() -> None:
    """A full-screen band must not turn one response into a cursor crawl: the
    cap wins over covering the whole band, and the covered part is the bottom
    (where the newest response is)."""
    band = ScreenRegion(0, 0, 24, 10_000)
    points = hover_scan_points(band, step_px=10, max_steps=5)
    assert points == [(12, 9999), (12, 9989), (12, 9979), (12, 9969), (12, 9959)]


def test_the_default_cap_covers_a_tall_monitor() -> None:
    band = ScreenRegion(0, 0, 24, 1440)
    points = hover_scan_points(band)
    assert len(points) < MAX_STEPS  # not clipped
    assert points[-1] == (12, 0)


@pytest.mark.parametrize(
    "band, step, cap",
    [
        (ScreenRegion(0, 0, 0, 100), STEP_PX, MAX_STEPS),
        (ScreenRegion(0, 0, 24, 0), STEP_PX, MAX_STEPS),
        (ScreenRegion(0, 0, 24, 100), 0, MAX_STEPS),
        (ScreenRegion(0, 0, 24, 100), STEP_PX, 0),
    ],
)
def test_degenerate_inputs_produce_no_stops(band: ScreenRegion, step: int, cap: int) -> None:
    """Callers loop over the result directly, so nonsense must be an empty
    list rather than an exception or a single bogus stop."""
    assert hover_scan_points(band, step_px=step, max_steps=cap) == []
