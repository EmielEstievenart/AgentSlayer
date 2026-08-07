"""The region wire format (picker child stdout -> ScreenRegion) and geometry."""

from __future__ import annotations

import pytest

from agentclip.screen.region import ScreenRegion, format_region, parse_region


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
