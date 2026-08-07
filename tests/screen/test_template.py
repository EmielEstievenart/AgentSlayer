"""Vertical template matching: finding the lowest copy-button icon in a band."""

from __future__ import annotations

import pytest

from agentclip.screen.capture import RegionImage
from agentclip.screen.template import (
    DEFAULT_TOLERANCE,
    TemplateMatch,
    find_lowest_match,
    match_at,
)

WIDTH = 4
ICON = (0, 0, 255)  # BGR red - stands in for the copy-button icon
PAGE = (30, 30, 30)  # the transcript behind it


def solid(width: int, height: int, colour: tuple[int, int, int]) -> RegionImage:
    """A uniformly coloured BGRX frame (the X byte is left at 0)."""
    blue, green, red = colour
    return RegionImage(width, height, bytes((blue, green, red, 0)) * (width * height))


def stack(*blocks: RegionImage) -> RegionImage:
    """Vertically concatenate same-width frames into one band."""
    width = blocks[0].width
    assert all(block.width == width for block in blocks)
    return RegionImage(
        width,
        sum(block.height for block in blocks),
        b"".join(block.pixels for block in blocks),
    )


def test_finds_the_lowest_of_several_matches() -> None:
    """Every response stamps an icon; only the bottom-most one is the new one."""
    template = solid(WIDTH, 6, ICON)
    band = stack(
        solid(WIDTH, 10, PAGE),
        template,
        solid(WIDTH, 8, PAGE),
        template,
        solid(WIDTH, 5, PAGE),
    )
    assert find_lowest_match(template, band) == TemplateMatch(10 + 6 + 8, 0.0)


def test_finds_a_match_flush_with_the_band_bottom() -> None:
    template = solid(WIDTH, 6, ICON)
    band = stack(solid(WIDTH, 12, PAGE), template)
    assert find_lowest_match(template, band) == TemplateMatch(12, 0.0)


def test_finds_a_match_at_the_very_top() -> None:
    template = solid(WIDTH, 6, ICON)
    band = stack(template, solid(WIDTH, 9, PAGE))
    assert find_lowest_match(template, band) == TemplateMatch(0, 0.0)


def test_band_equal_to_the_template_is_a_single_offset() -> None:
    template = solid(WIDTH, 6, ICON)
    assert find_lowest_match(template, template) == TemplateMatch(0, 0.0)


def test_no_match_returns_none() -> None:
    template = solid(WIDTH, 6, ICON)
    assert find_lowest_match(template, solid(WIDTH, 40, PAGE)) is None


def test_a_partial_overlap_is_not_a_match() -> None:
    """One icon in the band must yield exactly one offset, not a fuzzy range."""
    template = solid(WIDTH, 6, ICON)
    band = stack(solid(WIDTH, 10, PAGE), template, solid(WIDTH, 10, PAGE))
    match = find_lowest_match(template, band)
    assert match is not None and match.y_offset == 10
    assert match_at(template, band, 11) > 0.0


def test_noise_within_tolerance_still_matches() -> None:
    blue, green, red = ICON
    template = solid(WIDTH, 6, ICON)
    shaded = solid(WIDTH, 6, (blue + DEFAULT_TOLERANCE, green + 5, red - DEFAULT_TOLERANCE))
    band = stack(solid(WIDTH, 10, PAGE), shaded)
    assert find_lowest_match(template, band) == TemplateMatch(10, 0.0)


def test_noise_beyond_tolerance_does_not_match() -> None:
    blue, green, red = ICON
    template = solid(WIDTH, 6, ICON)
    shifted = solid(WIDTH, 6, (blue, green + DEFAULT_TOLERANCE + 1, red))
    band = stack(solid(WIDTH, 10, PAGE), shifted)
    assert find_lowest_match(template, band) is None
    assert find_lowest_match(template, band, tolerance=DEFAULT_TOLERANCE + 1) is not None


def test_max_diff_controls_how_much_may_differ() -> None:
    """One differing row out of six is 16% of the pixels - past the default."""
    template = solid(WIDTH, 6, ICON)
    band = stack(solid(WIDTH, 10, PAGE), solid(WIDTH, 5, ICON), solid(WIDTH, 1, PAGE))
    assert find_lowest_match(template, band) is None
    match = find_lowest_match(template, band, max_diff=0.2)
    assert match is not None and match.y_offset == 10
    assert match.diff == pytest.approx(1 / 6)


def test_match_at_a_known_offset() -> None:
    template = solid(WIDTH, 6, ICON)
    band = stack(solid(WIDTH, 10, PAGE), template, solid(WIDTH, 4, PAGE))
    assert match_at(template, band, 10) == 0.0
    assert match_at(template, band, 0) == 1.0


def test_match_at_ignores_the_undefined_x_byte() -> None:
    template = RegionImage(2, 1, bytes([10, 20, 30, 0, 10, 20, 30, 0]))
    band = RegionImage(2, 1, bytes([10, 20, 30, 255, 10, 20, 30, 255]))
    assert match_at(template, band, 0) == 0.0


def test_match_at_rejects_an_offset_outside_the_band() -> None:
    template = solid(WIDTH, 6, ICON)
    band = solid(WIDTH, 10, PAGE)
    with pytest.raises(ValueError):
        match_at(template, band, 5)  # 5 + 6 > 10
    with pytest.raises(ValueError):
        match_at(template, band, -1)


def test_width_mismatch_raises() -> None:
    template = solid(WIDTH, 6, ICON)
    band = solid(WIDTH + 1, 20, PAGE)
    with pytest.raises(ValueError):
        find_lowest_match(template, band)
    with pytest.raises(ValueError):
        match_at(template, band, 0)


def test_band_shorter_than_the_template_raises() -> None:
    template = solid(WIDTH, 6, ICON)
    band = solid(WIDTH, 5, ICON)
    with pytest.raises(ValueError):
        find_lowest_match(template, band)
    with pytest.raises(ValueError):
        match_at(template, band, 0)


def test_an_empty_template_raises() -> None:
    with pytest.raises(ValueError):
        find_lowest_match(RegionImage(0, 0, b""), RegionImage(0, 10, b""))


def test_a_truncated_buffer_raises() -> None:
    template = RegionImage(WIDTH, 6, b"\x00" * 8)
    with pytest.raises(ValueError):
        find_lowest_match(template, solid(WIDTH, 20, PAGE))


def test_a_tall_band_finds_the_icon_at_a_realistic_size() -> None:
    """A 24x10 stand-in for the icon, in a 1500px band, lands on the exact row.

    Each row is a different shade, so - unlike a flat block - no shifted offset
    can pass: real icons have that kind of vertical structure.
    """
    template = stack(*[solid(24, 1, (0, 5 + row * 25, 255)) for row in range(10)])
    band = stack(solid(24, 900, PAGE), template, solid(24, 600, PAGE))
    assert find_lowest_match(template, band) == TemplateMatch(900, 0.0)
