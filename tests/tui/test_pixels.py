"""The half-block renderer, tested as what it is: arithmetic over bytes.

No Pilot, no Textual, no screen capture - :mod:`agentclip.tui.pixels` is five
pure functions over ``RegionImage``, and the whole point of factoring it out of
the sidebar was that the sizing, the cutting, the averaging and the BGRX
channel swap could be pinned here rather than inferred from a rendered
terminal.

The colours are read back off ``Text.spans``, which is exact: the renderer
appends one styled run per CELL in reading order, so span N is cell N and its
style's ``color``/``bgcolor`` are literally the top and bottom pixel.
"""

from __future__ import annotations

from rich.style import Style

from agentclip.driver.screen.capture import RegionImage
from agentclip.tui.pixels import (
    HALF_BLOCK,
    crop,
    downsample,
    fit_cells,
    half_block_text,
    thumbnail,
)


def image(rows: list[list[tuple[int, int, int]]]) -> RegionImage:
    """Build a capture from RGB rows - stored BGRX, exactly as GDI hands it over."""
    height = len(rows)
    width = len(rows[0]) if height else 0
    pixels = bytearray()
    for row in rows:
        for red, green, blue in row:
            pixels += bytes((blue, green, red, 0))
    return RegionImage(width, height, bytes(pixels))


def cell_colours(text) -> list[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    """(foreground, background) per cell, in reading order."""
    out = []
    for span in text.spans:
        style = span.style
        assert isinstance(style, Style)
        assert style.color is not None and style.bgcolor is not None
        fg, bg = style.color.triplet, style.bgcolor.triplet
        assert fg is not None and bg is not None
        out.append(((fg.red, fg.green, fg.blue), (bg.red, bg.green, bg.blue)))
    return out


# -- fit_cells --------------------------------------------------------------


def test_fit_cells_uses_the_full_width_when_the_aspect_allows() -> None:
    """A cell is two pixels tall, so a 4:1 strip 28 cells wide is 4 rows deep."""
    assert fit_cells(width=112, height=56, max_cols=28, max_rows=8) == (28, 7)


def test_fit_cells_gives_up_width_rather_than_overflow_the_row_budget() -> None:
    """A tall region cannot use the full width: the box would be deeper than
    the caller reserved, and the widget below it would be pushed off."""
    cols, rows = fit_cells(width=100, height=400, max_cols=28, max_rows=8)
    assert rows == 8
    assert cols == 4  # 8 rows = 16 px tall; 16 * 100/400 = 4 px wide


def test_fit_cells_keeps_a_wide_region_square_on_screen() -> None:
    """Half-blocks have square pixels, so a square region comes out half as
    many rows as columns - not the same number, which would draw it stretched."""
    assert fit_cells(width=200, height=200, max_cols=20, max_rows=20) == (20, 10)


def test_fit_cells_never_returns_a_zero_dimension_for_a_real_region() -> None:
    cols, rows = fit_cells(width=3, height=1000, max_cols=28, max_rows=8)
    assert cols >= 1 and rows >= 1


def test_fit_cells_reports_nothing_to_draw() -> None:
    """Every degenerate input collapses to the same "show your resting line"."""
    assert fit_cells(0, 10, 28, 8) == (0, 0)
    assert fit_cells(10, 0, 28, 8) == (0, 0)
    assert fit_cells(10, 10, 0, 8) == (0, 0)
    assert fit_cells(10, 10, 28, 0) == (0, 0)


# -- downsample -------------------------------------------------------------


def test_downsample_averages_rather_than_samples() -> None:
    """A 2x2 of three blacks and one white averages to a quarter-lit grey.

    Nearest-neighbour would answer 0 or 255 here, which is exactly the failure
    this exists to avoid: a chat window is mostly background, and one sampled
    pixel per cell draws an empty rectangle.
    """
    source = image([[(0, 0, 0), (255, 255, 255)], [(0, 0, 0), (0, 0, 0)]])
    small = downsample(source, 1, 1)
    assert (small.width, small.height) == (1, 1)
    blue, green, red = small.pixels[0], small.pixels[1], small.pixels[2]
    assert (red, green, blue) == (63, 63, 63)  # 255 // 4


def test_downsample_keeps_channels_apart() -> None:
    source = image([[(200, 100, 40), (200, 100, 40)]])
    small = downsample(source, 1, 1)
    assert small.pixels[:3] == bytes((40, 100, 200))  # B, G, R


def test_downsample_to_its_own_size_is_the_identity() -> None:
    source = image([[(1, 2, 3), (4, 5, 6)], [(7, 8, 9), (10, 11, 12)]])
    assert downsample(source, 2, 2).pixels == source.pixels


def test_downsample_enlarges_by_repeating_rather_than_failing() -> None:
    """Asking for more pixels than there are is legal - a captured icon can be
    smaller than the preview box - and each output pixel simply reads the
    source pixel it lands on."""
    source = image([[(10, 20, 30), (40, 50, 60)]])
    big = downsample(source, 4, 2)
    assert (big.width, big.height) == (4, 2)
    assert cell_colours(half_block_text(big)) == [
        ((10, 20, 30), (10, 20, 30)),
        ((10, 20, 30), (10, 20, 30)),
        ((40, 50, 60), (40, 50, 60)),
        ((40, 50, 60), (40, 50, 60)),
    ]


def test_downsample_refuses_nothing_and_raises_nothing() -> None:
    """No area, no target and a truncated buffer are all "an empty image"."""
    real = image([[(1, 2, 3)]])
    assert downsample(real, 0, 4) == RegionImage(0, 0, b"")
    assert downsample(real, 4, 0) == RegionImage(0, 0, b"")
    assert downsample(RegionImage(0, 0, b""), 4, 4) == RegionImage(0, 0, b"")
    truncated = RegionImage(8, 8, b"\x00" * 12)
    assert downsample(truncated, 2, 2) == RegionImage(0, 0, b"")


# -- half_block_text --------------------------------------------------------


def test_half_block_text_pairs_two_pixels_into_one_cell() -> None:
    """Top pixel is the foreground, bottom is the background, BGRX swapped back."""
    source = image([[(255, 0, 0)], [(0, 0, 255)]])
    text = half_block_text(source)
    assert text.plain == HALF_BLOCK
    assert cell_colours(text) == [((255, 0, 0), (0, 0, 255))]


def test_half_block_text_lays_rows_out_in_reading_order() -> None:
    source = image(
        [
            [(1, 1, 1), (2, 2, 2)],
            [(3, 3, 3), (4, 4, 4)],
            [(5, 5, 5), (6, 6, 6)],
            [(7, 7, 7), (8, 8, 8)],
        ]
    )
    text = half_block_text(source)
    assert text.plain == f"{HALF_BLOCK * 2}\n{HALF_BLOCK * 2}"
    assert cell_colours(text) == [
        ((1, 1, 1), (3, 3, 3)),
        ((2, 2, 2), (4, 4, 4)),
        ((5, 5, 5), (7, 7, 7)),
        ((6, 6, 6), (8, 8, 8)),
    ]


def test_half_block_text_pairs_an_odd_last_row_with_itself() -> None:
    """A half-lit final line reads as a rendering bug; the repeat is invisible."""
    source = image([[(1, 1, 1)], [(2, 2, 2)], [(3, 3, 3)]])
    text = half_block_text(source)
    assert text.plain == f"{HALF_BLOCK}\n{HALF_BLOCK}"
    assert cell_colours(text) == [((1, 1, 1), (2, 2, 2)), ((3, 3, 3), (3, 3, 3))]


def test_half_block_text_draws_a_single_pixel() -> None:
    text = half_block_text(image([[(9, 8, 7)]]))
    assert text.plain == HALF_BLOCK
    assert cell_colours(text) == [((9, 8, 7), (9, 8, 7))]


def test_half_block_text_of_nothing_is_empty_text() -> None:
    assert half_block_text(RegionImage(0, 0, b"")).plain == ""
    assert half_block_text(RegionImage(4, 4, b"\x00" * 8)).plain == ""  # truncated


def test_half_block_text_adds_no_trailing_newline() -> None:
    """A Static sized to exactly ELEMENT_CROP_ROWS has no row to spare."""
    text = half_block_text(image([[(0, 0, 0)], [(0, 0, 0)]]))
    assert text.plain.count("\n") == 0


# -- thumbnail --------------------------------------------------------------


def test_thumbnail_produces_exactly_the_grid_the_cells_will_draw() -> None:
    """Two pixels per cell row, so the pixel height is always even and the
    UI thread never has to size anything."""
    source = image([[(0, 0, 0)] * 64 for _ in range(64)])
    small = thumbnail(source, max_cols=28, max_rows=8)
    assert small is not None
    assert (small.width, small.height) == (16, 16)  # 8 rows x 2 px
    assert half_block_text(small).plain.count("\n") == 7


def test_thumbnail_of_an_empty_frame_is_none() -> None:
    """One answer for "there is nothing to show", so callers test one thing."""
    assert thumbnail(RegionImage(0, 0, b""), 28, 8) is None
    assert thumbnail(image([[(1, 2, 3)]]), 0, 8) is None
    assert thumbnail(RegionImage(8, 8, b"\x00" * 4), 28, 8) is None  # truncated


# -- crop -------------------------------------------------------------------
#
# The cutter the ELEMENTS column is built on: a template search says WHERE the
# appearance is, and this turns that answer back into the pixels that matched.
# Its contract is deliberately total - clamp, never raise - because it runs on
# a poll timer over rectangles a moving browser produced.

GRID = image(
    [
        [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)],
        [(0, 1, 0), (1, 1, 0), (2, 1, 0), (3, 1, 0)],
        [(0, 2, 0), (1, 2, 0), (2, 2, 0), (3, 2, 0)],
        [(0, 3, 0), (1, 3, 0), (2, 3, 0), (3, 3, 0)],
    ]
)


def rgb_rows(source: RegionImage) -> list[list[tuple[int, int, int]]]:
    """The image back as RGB rows - the inverse of ``image``."""
    out = []
    for y in range(source.height):
        row = []
        for x in range(source.width):
            i = (y * source.width + x) * 4
            row.append((source.pixels[i + 2], source.pixels[i + 1], source.pixels[i]))
        out.append(row)
    return out


def test_crop_cuts_the_requested_rectangle() -> None:
    """The pixel at (x, y) of the result is the pixel at (x + left, y + top)."""
    out = crop(GRID, 1, 2, 2, 2)
    assert (out.width, out.height) == (2, 2)
    assert rgb_rows(out) == [[(1, 2, 0), (2, 2, 0)], [(1, 3, 0), (2, 3, 0)]]


def test_crop_of_the_whole_image_is_the_whole_image() -> None:
    assert rgb_rows(crop(GRID, 0, 0, 4, 4)) == rgb_rows(GRID)


def test_crop_of_one_pixel() -> None:
    """The smallest real answer a match can produce; not a special case."""
    out = crop(GRID, 3, 1, 1, 1)
    assert (out.width, out.height) == (1, 1)
    assert rgb_rows(out) == [[(3, 1, 0)]]


def test_crop_clamps_a_rectangle_that_runs_off_the_edge() -> None:
    """A match reported near the border, or a template a pixel wider than the
    frame still holds: the caller gets the part that exists, not an exception."""
    out = crop(GRID, 2, 2, 10, 10)
    assert (out.width, out.height) == (2, 2)
    assert rgb_rows(out) == [[(2, 2, 0), (3, 2, 0)], [(2, 3, 0), (3, 3, 0)]]


def test_crop_clamps_a_negative_origin() -> None:
    """Same rule on the other two sides - the overlap, and only the overlap."""
    out = crop(GRID, -2, -1, 4, 3)
    assert (out.width, out.height) == (2, 2)
    assert rgb_rows(out) == [[(0, 0, 0), (1, 0, 0)], [(0, 1, 0), (1, 1, 0)]]


def test_crop_with_no_overlap_is_empty() -> None:
    """Entirely outside, zero-sized, or asked for backwards - one answer, and
    it is the same one the panel already has for "nothing matched"."""
    assert crop(GRID, 9, 9, 2, 2) == RegionImage(0, 0, b"")
    assert crop(GRID, -9, 0, 2, 2) == RegionImage(0, 0, b"")
    assert crop(GRID, 1, 1, 0, 4) == RegionImage(0, 0, b"")
    assert crop(GRID, 1, 1, 4, -1) == RegionImage(0, 0, b"")


def test_crop_of_an_empty_or_truncated_source_is_empty() -> None:
    """The house rule from downsample: a short buffer is a runtime condition."""
    assert crop(RegionImage(0, 0, b""), 0, 0, 2, 2) == RegionImage(0, 0, b"")
    assert crop(RegionImage(4, 4, b"\x00" * 8), 0, 0, 2, 2) == RegionImage(0, 0, b"")


def test_a_crop_feeds_thumbnail_without_any_arithmetic_in_between() -> None:
    """The two are used as one step in the poll worker, so the seam is pinned:
    whatever crop returns, thumbnail either sizes it for the cell box or says
    outright that there is nothing to draw."""
    icon = image([[(x * 8, y * 8, 0) for x in range(24)] for y in range(24)])
    small = thumbnail(crop(icon, 4, 4, 16, 16), max_cols=16, max_rows=6)
    assert small is not None
    assert (small.width, small.height) == (12, 12)  # 6 rows x 2 px, aspect kept
    # A match that does not overlap the frame crops to nothing, and "nothing to
    # draw" is the one answer the panel already knows how to show.
    assert thumbnail(crop(icon, 40, 40, 16, 16), max_cols=16, max_rows=6) is None
