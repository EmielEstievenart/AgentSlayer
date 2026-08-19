"""The region wire format (picker child stdout -> ScreenRegion) and geometry."""

from __future__ import annotations

import pytest

from agentclip.driver.screen.region import (
    ScreenRegion,
    click_point_region,
    format_region,
    parse_region,
)


def test_roundtrip() -> None:
    region = ScreenRegion(1050, 340, 812, 540)
    assert parse_region(format_region(region)) == region


def test_roundtrip_negative_origin() -> None:
    """A monitor left of / above the primary puts the region at negative coords."""
    region = ScreenRegion(-1920, -200, 640, 480)
    assert parse_region(format_region(region)) == region


def test_parse_tolerates_whitespace() -> None:
    assert parse_region("  10 20 300 400\r\n") == ScreenRegion(10, 20, 300, 400)


@pytest.mark.parametrize(
    "text",
    [
        "",  # cancelled overlay: child prints nothing
        "not a region",
        "10 20 300",  # too few fields
        "10 20 300 400 500",  # too many
        "10 20 300 4.5",  # non-integer
        "10 20 0 400",  # empty box
        "10 20 300 -400",  # negative size
    ],
)
def test_parse_rejects(text: str) -> None:
    assert parse_region(text) is None


def test_center() -> None:
    assert ScreenRegion(100, 200, 10, 20).center == (105, 210)
    assert ScreenRegion(-100, 0, 50, 51).center == (-75, 25)


def test_describe_is_humane() -> None:
    assert ScreenRegion(1050, 340, 812, 540).describe() == "812×540 at (1050, 340)"


@pytest.mark.parametrize(
    "region",
    [
        ScreenRegion(1050, 340, 812, 540),
        ScreenRegion(0, 0, 1, 1),  # a template one pixel across
        ScreenRegion(-40, -7, 6, 6),  # the width banker's rounding gets wrong
        ScreenRegion(10, 20, 25, 3),
    ],
)
def test_the_middle_of_a_region_is_exactly_its_centre(region: ScreenRegion) -> None:
    """50/50 has to be the old behaviour to the pixel, for every size: it is
    what every click did before the point was adjustable, and a click that
    moved by one when nobody touched the setting is a regression nobody would
    look for."""
    assert click_point_region(region, 50, 50).center == region.center


def test_the_corners_are_the_regions_own_corner_pixels() -> None:
    """0% and 100% span the PIXELS, not the edges - so the extremes are inside
    the picture rather than one past its bottom-right corner."""
    region = ScreenRegion(1050, 340, 812, 540)

    assert click_point_region(region, 0, 0) == ScreenRegion(1050, 340, 1, 1)
    assert click_point_region(region, 100, 100) == ScreenRegion(1861, 879, 1, 1)


def test_a_one_pixel_template_has_nowhere_else_to_land() -> None:
    """Every percentage of a 1x1 capture is the same single pixel."""
    region = ScreenRegion(700, 800, 1, 1)

    for percent in (0, 50, 100):
        assert click_point_region(region, percent, percent) == ScreenRegion(700, 800, 1, 1)


def test_the_two_axes_are_read_separately() -> None:
    """A chat box clicked near its left edge and a little below the middle."""
    region = ScreenRegion(100, 200, 201, 101)

    assert click_point_region(region, 10, 75) == ScreenRegion(120, 275, 1, 1)
