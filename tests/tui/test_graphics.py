"""The sixel path, tested as the only part of it a headless run can see.

Sixel output cannot be asserted from a terminal that is not there, and the
agent that wrote this renderer could not look at it. So everything the renderer
DECIDES is a pure function in :mod:`agentclip.tui.graphics`, and this module
pins all of it: the startup probe's verdict and its caching, the mode
selection, the scaling policy, the BGRX->RGB swap, and the padding that stops
textual-image resizing a crop a second time.

What is left unpinned here is the escape sequence itself, which is
textual-image's job, and the fact that the widget draws it inside Textual -
that one is pinned in test_elements_panel_ui.py, where a Pilot can read the
strips back.
"""

from __future__ import annotations

import pytest
from PIL import Image as PILImage

from agentclip.screen.capture import RegionImage
from agentclip.tui import graphics as graphics_mod
from agentclip.tui.graphics import (
    CROP_MAX_ROWS,
    CROP_MIN_ROWS,
    DEFAULT_CELL_HEIGHT,
    DEFAULT_CELL_WIDTH,
    NO_SIXEL,
    TerminalGraphics,
    crop_picture,
    crop_rows,
    fit_pixels,
    pad_to_box,
    probe_terminal,
    region_to_pil,
    scale_crop,
    set_terminal_graphics,
    sixel_image_class,
    terminal_graphics,
)

CELL = TerminalGraphics(sixel=True, cell_width=10, cell_height=20)


def capture(width: int, height: int, colour: tuple[int, int, int] = (10, 20, 30)) -> RegionImage:
    """A capture buffer as GDI hands it over: four bytes per pixel, blue first."""
    red, green, blue = colour
    return RegionImage(width, height, bytes((blue, green, red, 0)) * (width * height))


# -- the verdict --------------------------------------------------------------


def test_an_unprobed_process_draws_half_blocks() -> None:
    """Everything that does not go through cli.main - every test, the
    --pick-region child - must land on the renderer that needs no terminal."""
    assert NO_SIXEL.sixel is False
    assert NO_SIXEL.mode == "half-block"
    assert TerminalGraphics(sixel=True).mode == "sixel"


def test_the_probe_caches_what_the_terminal_said(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probed ONCE, before Textual starts, and read from the cache forever
    after: the query cannot be repeated once Textual owns stdin."""
    import textual_image._terminal as terminal_mod
    import textual_image.renderable.sixel as sixel_mod

    monkeypatch.setattr(sixel_mod, "query_terminal_support", lambda: True)
    monkeypatch.setattr(terminal_mod, "get_cell_size", lambda: terminal_mod.CellSize(8, 17))

    assert probe_terminal() == TerminalGraphics(sixel=True, cell_width=8, cell_height=17)
    assert terminal_graphics() == TerminalGraphics(sixel=True, cell_width=8, cell_height=17)


def test_a_terminal_that_says_no_gets_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import textual_image._terminal as terminal_mod
    import textual_image.renderable.sixel as sixel_mod

    monkeypatch.setattr(sixel_mod, "query_terminal_support", lambda: False)
    monkeypatch.setattr(terminal_mod, "get_cell_size", lambda: terminal_mod.CellSize(9, 18))

    assert probe_terminal() == TerminalGraphics(sixel=False, cell_width=9, cell_height=18)


def test_a_probe_that_blows_up_is_not_a_failed_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is a working renderer behind the probe, so a terminal that answers
    nonsense (or a missing textual-image) costs the pictures, not the app."""
    import textual_image.renderable.sixel as sixel_mod

    def boom() -> bool:
        raise OSError("no terminal here")

    monkeypatch.setattr(sixel_mod, "query_terminal_support", boom)
    assert probe_terminal() == NO_SIXEL
    assert terminal_graphics() == NO_SIXEL


def test_a_terminal_that_will_not_say_its_cell_size_gets_the_vt340_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import textual_image._terminal as terminal_mod
    import textual_image.renderable.sixel as sixel_mod

    monkeypatch.setattr(sixel_mod, "query_terminal_support", lambda: True)
    monkeypatch.setattr(terminal_mod, "get_cell_size", lambda: terminal_mod.CellSize(0, 0))

    graphics = probe_terminal()
    assert (graphics.cell_width, graphics.cell_height) == (
        DEFAULT_CELL_WIDTH,
        DEFAULT_CELL_HEIGHT,
    )


def test_the_sixel_widget_is_the_dedicated_one_not_the_auto_alias() -> None:
    """The auto alias is picked by import-time detection, which is the bug this
    module exists to route around - so the panel asks for the sixel widget by
    name."""
    from textual_image.widget.sixel import Image as SixelImage

    assert sixel_image_class() is SixelImage


def test_setting_the_verdict_is_how_a_test_reaches_the_sixel_path() -> None:
    set_terminal_graphics(CELL)
    assert terminal_graphics() is CELL
    set_terminal_graphics(NO_SIXEL)
    assert graphics_mod.terminal_graphics() is NO_SIXEL


# -- how tall a row is --------------------------------------------------------


def test_a_crop_row_is_a_pixel_budget_divided_by_the_terminals_cell() -> None:
    """A cell is 16px tall in one font and 24 in another; what is being
    reserved room for is an icon, so the budget is in pixels."""
    assert crop_rows(20) == 3  # 56px of budget, 60px of rows
    assert crop_rows(16) == 4
    assert crop_rows(28) == 2


def test_a_crop_row_is_clamped_at_both_ends() -> None:
    assert crop_rows(1000) == CROP_MIN_ROWS
    assert crop_rows(1) == CROP_MAX_ROWS
    assert crop_rows(0) == CROP_MIN_ROWS


# -- what size a crop is drawn at ---------------------------------------------


def test_a_crop_that_fits_is_drawn_life_size() -> None:
    """The whole reason for sixel: the send button at exactly the size the
    screenshot has it, with no resampling to soften it."""
    assert fit_pixels(120, 40, 160, 60) == (120, 40)
    assert fit_pixels(160, 60, 160, 60) == (160, 60)


def test_a_tiny_appearance_is_doubled_so_it_can_be_seen() -> None:
    """A 12px stop icon drawn 1:1 in a row that reserved 56 is unreadable."""
    assert fit_pixels(12, 12, 160, 60) == (24, 24)
    # 24px is not tiny, and is left alone.
    assert fit_pixels(24, 24, 160, 60) == (24, 24)


def test_nothing_is_ever_drawn_more_than_twice_life_size() -> None:
    """Past 2x it stops looking like a picture of a screen and starts looking
    like a rendering bug - so a small icon in a tall box still stops at 2x."""
    assert fit_pixels(8, 8, 1600, 600) == (16, 16)
    # ... and a double that would not fit is not attempted at all.
    assert fit_pixels(12, 12, 160, 20) == (12, 12)


def test_a_crop_bigger_than_its_box_is_fitted_keeping_its_aspect() -> None:
    assert fit_pixels(320, 120, 160, 60) == (160, 60)
    assert fit_pixels(320, 60, 160, 60) == (160, 30)  # width binds
    assert fit_pixels(80, 240, 160, 60) == (20, 60)  # height binds


def test_a_degenerate_crop_is_nothing_to_draw() -> None:
    """The same contract pixels.fit_cells has: the caller's cue to show its
    resting line instead."""
    assert fit_pixels(0, 10, 160, 60) == (0, 0)
    assert fit_pixels(10, 0, 160, 60) == (0, 0)
    assert fit_pixels(10, 10, 0, 60) == (0, 0)
    assert fit_pixels(10, 10, 160, 0) == (0, 0)


def test_a_fitted_crop_never_collapses_to_nothing() -> None:
    """An extreme aspect ratio rounds toward one pixel, not zero."""
    assert fit_pixels(4000, 10, 160, 60) == (160, 1)


# -- the channel swap ---------------------------------------------------------


def test_a_capture_is_read_bgrx_not_bgra() -> None:
    """The fourth byte of a GDI capture is UNDEFINED, and zero in practice. Read
    as alpha it would make every crop fully transparent - i.e. invisible."""
    source = region_to_pil(capture(2, 1, colour=(200, 100, 50)))
    assert source is not None
    assert source.mode == "RGB"
    assert list(source.getdata()) == [(200, 100, 50), (200, 100, 50)]


def test_an_empty_or_truncated_capture_is_none_not_an_exception() -> None:
    """This runs on a poll timer over rectangles a moving browser produced."""
    assert region_to_pil(RegionImage(0, 0, b"")) is None
    assert region_to_pil(RegionImage(4, 4, b"\x00" * 8)) is None


# -- the scaling itself -------------------------------------------------------


def test_a_life_size_crop_is_handed_over_untouched() -> None:
    source = PILImage.new("RGB", (24, 24), (1, 2, 3))
    assert scale_crop(source, 160, 60) is source


def test_a_shrunk_crop_keeps_the_strokes_a_nearest_shrink_would_eat() -> None:
    """Lanczos going down: an icon is thin strokes on a flat field, and a
    nearest shrink lands on the field almost every time."""
    source = PILImage.new("RGB", (320, 120), (0, 0, 0))
    source.putpixel((0, 0), (255, 255, 255))
    scaled = scale_crop(source, 160, 60)
    assert scaled is not None
    assert scaled.size == (160, 60)
    assert scaled.getpixel((0, 0)) != (0, 0, 0)


def test_a_doubled_crop_stays_blocky() -> None:
    """NEAREST going up: a doubled pixel should still be a pixel, not a guess
    at what was between two of them."""
    source = PILImage.new("RGB", (2, 2), (0, 0, 0))
    source.putpixel((0, 0), (255, 255, 255))
    scaled = scale_crop(source, 160, 60)
    assert scaled is not None
    assert scaled.size == (4, 4)
    assert set(scaled.getdata()) == {(0, 0, 0), (255, 255, 255)}


def test_scaling_nothing_is_none() -> None:
    assert scale_crop(PILImage.new("RGB", (24, 24)), 0, 60) is None


# -- the padding --------------------------------------------------------------


def test_padding_centres_the_crop_in_the_exact_cell_box() -> None:
    """Exact, because textual-image resizes whatever it is given to fill the
    styled cell box - the padding is what makes that second resize a no-op
    rather than a stretch off the aspect ratio."""
    padded = pad_to_box(PILImage.new("RGB", (20, 20), (9, 9, 9)), 160, 60)
    assert padded.size == (160, 60)
    assert padded.getpixel((70 + 5, 20 + 5)) == (9, 9, 9, 255)


def test_the_padding_is_transparent_so_the_panel_shows_through() -> None:
    """Fully transparent pixels are skipped by the sixel encoder outright, so
    the row keeps whatever background the live theme paints."""
    padded = pad_to_box(PILImage.new("RGB", (20, 20), (9, 9, 9)), 160, 60)
    assert padded.mode == "RGBA"
    assert padded.getpixel((0, 0)) == (0, 0, 0, 0)


# -- end to end ---------------------------------------------------------------


def test_a_capture_becomes_exactly_the_cell_box_the_panel_reserved() -> None:
    picture = crop_picture(capture(24, 24), 16, 3, CELL)
    assert picture is not None
    assert picture.size == (16 * CELL.cell_width, 3 * CELL.cell_height)


def test_a_wide_appearance_is_fitted_to_the_column_not_squashed_into_it() -> None:
    picture = crop_picture(capture(400, 50), 16, 3, CELL)
    assert picture is not None
    assert picture.size == (160, 60)  # the box
    # 400x50 fitted to 160 wide is 160x20, centred - so the top row is padding.
    assert picture.getpixel((80, 0))[3] == 0
    assert picture.getpixel((80, 30))[3] == 255


def test_nothing_to_draw_stays_nothing_to_draw() -> None:
    assert crop_picture(RegionImage(0, 0, b""), 16, 3, CELL) is None
    assert crop_picture(capture(24, 24), 0, 3, CELL) is None


def test_the_default_verdict_is_used_when_none_is_passed() -> None:
    set_terminal_graphics(CELL)
    try:
        picture = crop_picture(capture(24, 24), 16, 3)
        assert picture is not None
        assert picture.size == (160, 60)
    finally:
        set_terminal_graphics(NO_SIXEL)
