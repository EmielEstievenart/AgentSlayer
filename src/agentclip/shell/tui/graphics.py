"""What the terminal can actually draw, decided once, before Textual starts.

The ELEMENTS column (tui.md 1.7) shows the pixels the detectors recognised. Half
blocks - one cell per two stacked pixels, ``tui.pixels`` - were the first answer
and they are honest about *shape* and nothing else: a 24x24 icon squeezed into a
16x6 cell budget is twelve rows of averaged mush, which tells you a copy button
is squarish and never that it is a clipboard. **Sixel** draws the crop at its
real pixel size, and on the terminal this tool is written for (Windows Terminal
1.22+) that is the difference between a smear and the icon.

**Why the probe lives here, at startup, and not at compose time.**
``textual_image.renderable``'s auto-detection runs `WHEN THE MODULE IS FIRST
IMPORTED`: it writes a DA1 query to the terminal and reads the reply off stdin.
Import it lazily - from a widget's ``compose``, say - and Textual is already
running its own stdin reader thread, which swallows the reply; the detection
concludes "no sixel", silently picks half-cell rendering, and the sixel widget
you asked for renders as blocks. That is exactly what the pixel-quality
prototype hit. So :func:`probe_terminal` runs from ``cli.main`` BEFORE
``app.run()``, does every terminal round trip there is to do (support, cell
size), imports the sixel widget module while it is still safe to, and caches the
verdict. After that nothing in the UI ever queries a terminal again.

Everything below the probe is a pure function over :class:`RegionImage` and
Pillow images, so the mode selection, the scaling policy and the BGRX->RGB swap
are unit-testable without a terminal - which matters more here than anywhere
else in the app, because sixel output cannot be asserted from a headless test
and cannot be seen by the agent that wrote it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PIL import Image as PILImage

from agentclip.driver.screen.capture import RegionImage

# The cell size textual-image itself falls back to when the terminal will not
# say (VT340). Used for the same reason: it has to be *some* number, and every
# consumer here treats it as a budget rather than a truth.
DEFAULT_CELL_WIDTH = 10
DEFAULT_CELL_HEIGHT = 20

# How tall a crop row is allowed to be, in PIXELS rather than cells, because a
# cell is a different number of pixels on every font size. ~56px holds a
# doubled small icon (2x24) and any wide appearance once it has been fitted to
# the column's width, without leaving half the column empty.
CROP_BOX_HEIGHT_PX = 56
CROP_MIN_ROWS = 2
CROP_MAX_ROWS = 8

# Below this, an appearance is small enough that drawing it 1:1 wastes the room
# the row already reserved - a 12px stop icon is four cells of nothing. Doubled
# with NEAREST, so it stays the crisp blocky thing the detector matched rather
# than a smoothed guess at one.
UPSCALE_BELOW_PX = 20
MAX_UPSCALE = 2

# The thinnest a shrunk crop may end up on either axis. An appearance can be
# extremely lopsided - a captured chat input box is ~848x5 once it is a strip of
# border - and fitting that to sixteen columns lands its height on a fraction of
# a pixel, i.e. a one-pixel hairline that reads as an empty row. Two pixels is
# the fewest that reads as a *line the tool drew*, and at these aspect ratios
# the distortion it costs is imperceptible. Never applied above the source's own
# size or the box's, so a genuinely 1px-tall capture is still drawn 1px tall.
MIN_DRAW_PX = 2


@dataclass(frozen=True, slots=True)
class TerminalGraphics:
    """The startup verdict: can this terminal draw sixels, and how big is a cell.

    One value for the whole process. It is a *dataclass* rather than three
    module globals so a test can hand a fabricated verdict to the pure
    functions below - the sixel path is the one path no headless test can
    observe, so every decision it makes has to be decidable from this.
    """

    sixel: bool
    cell_width: int = DEFAULT_CELL_WIDTH
    cell_height: int = DEFAULT_CELL_HEIGHT

    @property
    def mode(self) -> str:
        """``"sixel"`` or ``"half-block"`` - what the panel writes on itself."""
        return "sixel" if self.sixel else "half-block"


# What a terminal that was never probed gets: the drawing mode that needs
# nothing from the terminal. Tests, ``--pick-region`` children and anything that
# imports the widget without going through cli.main all land here, which is why
# the half-block path has to keep working forever rather than being a
# transitional courtesy.
NO_SIXEL = TerminalGraphics(sixel=False)

_graphics: TerminalGraphics = NO_SIXEL


def terminal_graphics() -> TerminalGraphics:
    """The cached startup verdict (``NO_SIXEL`` until :func:`probe_terminal`)."""
    return _graphics


def set_terminal_graphics(graphics: TerminalGraphics) -> None:
    """Pin the verdict. The probe's own setter, and the tests' way in.

    A test cannot make a headless pytest run answer a DA1 query, so the way it
    exercises the sixel path is to declare one - which is sound precisely
    because everything downstream reads the verdict rather than the terminal.
    """
    global _graphics
    _graphics = graphics


def interactive_terminal() -> bool:
    """Is there a real console on both ends to hold a conversation with?

    Asked FIRST, and it is not a nicety. Probing writes an escape sequence and
    reads the reply back off the file descriptor; ``textual_image``'s own
    support query checks only that stdout *exists*, so pointed at a pipe or at
    NUL it writes the query, reads end-of-file, appends the empty string to the
    response it is accumulating, and does that forever - a spin, not a block,
    and not something a timeout saves you from. AgentClip is piped often enough
    (a smoke test, a ``| tee``, an agent running it) that this has to be a
    guard rather than a warning, and a piped run wants half blocks anyway.

    ``sys.__stdout__``/``sys.__stdin__`` rather than the current ones, because
    those are what the query actually writes to and reads from.
    """
    streams = (sys.__stdout__, sys.__stdin__)
    return all(stream is not None and stream.isatty() for stream in streams)


def probe_terminal() -> TerminalGraphics:
    """Ask the terminal what it can do, once, before Textual owns the terminal.

    Call this from the entry point and nowhere else. It:

    * refuses outright when there is no console on both ends
      (:func:`interactive_terminal`) - a query written into a pipe never comes
      back, and the reader spins on end-of-file rather than timing out;
    * imports ``textual_image.renderable``, whose module body performs the
      sixel/TGP detection - doing that here rather than lazily is the whole
      point of this function (see the module docstring);
    * asks ``query_terminal_support()`` itself, because a module-level global
      picked by import-order is not an API and the sixel verdict is the one
      thing this app actually needs (a TGP-only terminal must read as "no");
    * imports ``textual_image.widget.sixel`` while terminal queries are still
      safe - its package ``__init__`` calls ``get_cell_size()``, which would
      otherwise fire under a running Textual;
    * caches the cell size, which is what turns the panel's cell budget into
      the pixel budget the crops are scaled to.

    Any failure at all - no textual-image, no stdout, a terminal that answers
    nonsense - is answered with :data:`NO_SIXEL`. There is a working renderer
    behind this, so an unusable probe is never a reason to fail a launch.
    """
    if not interactive_terminal():
        set_terminal_graphics(NO_SIXEL)
        return NO_SIXEL
    try:
        import textual_image.widget.sixel  # noqa: F401  (imported for its side effects)
        from textual_image._terminal import get_cell_size
        from textual_image.renderable.sixel import query_terminal_support

        supported = bool(query_terminal_support())
        cell = get_cell_size()
        graphics = TerminalGraphics(
            sixel=supported,
            cell_width=cell.width if cell.width > 0 else DEFAULT_CELL_WIDTH,
            cell_height=cell.height if cell.height > 0 else DEFAULT_CELL_HEIGHT,
        )
    except Exception:
        graphics = NO_SIXEL
    set_terminal_graphics(graphics)
    return graphics


def sixel_image_class() -> type | None:
    """textual-image's Textual widget for sixel, or ``None`` if it is not usable.

    Deliberately ``textual_image.widget.sixel.Image`` - the DEDICATED sixel
    widget - rather than ``textual_image.widget.Image``, which is an alias
    picked by the import-time auto-detection this module exists to stop
    trusting. Sixel cannot go through the ordinary renderable path (Textual
    composites printable segments and sixel data is not printable), so the
    package ships a widget that injects the escape sequence at ``render_lines``
    time and moves the cursor itself; the auto alias resolves to that widget
    only when the detection happened to succeed.

    The import is lazy because importing this package QUERIES A TERMINAL - its
    ``__init__`` chain runs the auto-detection and ``get_cell_size()`` - and
    merely importing the TUI must not do that. ``--list-services`` and the
    ``--pick-region`` child import the TUI and never draw a crop. By the time
    the panel asks, :func:`probe_terminal` has already imported the module at
    the one moment those queries were safe, so nothing here touches anything.
    """
    try:
        from textual_image.widget.sixel import Image as SixelImage
    except Exception:
        return None
    return SixelImage


def crop_rows(cell_height: int) -> int:
    """How many cell rows one crop reserves, at this terminal's cell height.

    Fixed for the session, and reserved whether or not anything matched: a row
    that grew when its element appeared would make every row below it jump on a
    0.5 s timer, which is the same rule the half-block budget already obeys.
    Derived from a PIXEL budget rather than fixed at a cell count because a cell
    is 16px tall in one font and 24 in another, and the thing being reserved
    room for is an icon.
    """
    if cell_height <= 0:
        return CROP_MIN_ROWS
    rows = -(-CROP_BOX_HEIGHT_PX // cell_height)  # ceil
    return max(CROP_MIN_ROWS, min(CROP_MAX_ROWS, rows))


def fit_pixels(width: int, height: int, box_width: int, box_height: int) -> tuple[int, int]:
    """The pixel size a crop is drawn at inside a ``box_width x box_height`` box.

    Three cases, and the middle one is the one the user asked for:

    * **bigger than the box** - shrunk to fit, aspect kept (Lanczos, see
      :func:`scale_crop`);
    * **fits** - drawn 1:1. A send button rendered at exactly the size the
      screenshot has it is the crisp, true-size picture sixel exists to give,
      and any resampling at all is a downgrade;
    * **tiny** (under :data:`UPSCALE_BELOW_PX` on both axes, and doubling still
      fits) - doubled with NEAREST, because a 12px icon drawn 1:1 in a row that
      reserved 56px is unreadably small. Never more than 2x: past that it stops
      looking like a picture of a screen and starts looking like a bug.

    A shrink never takes an axis below :data:`MIN_DRAW_PX` (or the source's own
    size, whichever is smaller): the chat-box appearances are hundreds of pixels
    wide and a handful tall, and rounding one of those down to a single pixel
    draws a hairline nobody can tell from a row that found nothing.

    ``(0, 0)`` for anything degenerate, which is the caller's cue to draw the
    resting line instead - the same contract ``pixels.fit_cells`` has.
    """
    if width <= 0 or height <= 0 or box_width <= 0 or box_height <= 0:
        return (0, 0)
    if width <= box_width and height <= box_height:
        if (
            max(width, height) < UPSCALE_BELOW_PX
            and width * MAX_UPSCALE <= box_width
            and height * MAX_UPSCALE <= box_height
        ):
            return (width * MAX_UPSCALE, height * MAX_UPSCALE)
        return (width, height)
    scale = min(box_width / width, box_height / height)
    return (
        _at_least_visible(int(width * scale), width, box_width),
        _at_least_visible(int(height * scale), height, box_height),
    )


def _at_least_visible(size: int, source: int, box: int) -> int:
    """``size``, floored at :data:`MIN_DRAW_PX` without inventing pixels.

    The floor is capped by the source and the box, so it can only ever rescue a
    rounding loss: a 1px-tall capture stays 1px tall, and a box with one pixel
    to spare on an axis is not overflowed to satisfy it.
    """
    return max(size, min(MIN_DRAW_PX, source, box))


def region_to_pil(image: RegionImage) -> PILImage.Image | None:
    """A captured BGRX buffer as an RGB Pillow image, or ``None`` if there is none.

    A capture is four bytes per pixel, blue first, with the fourth byte
    undefined (``screen.capture``) - so the raw decoder mode is ``BGRX`` and NOT
    ``BGRA``: read as alpha, that undefined byte is zero and the whole crop
    would encode as fully transparent, i.e. invisible.

    ``None`` rather than an exception for an empty or truncated buffer, for the
    same reason ``pixels.downsample`` returns an empty image: this runs on a
    poll timer over rectangles a moving browser produced.
    """
    if image.width <= 0 or image.height <= 0:
        return None
    if len(image.pixels) < image.width * image.height * 4:
        return None
    return PILImage.frombytes("RGB", (image.width, image.height), image.pixels, "raw", "BGRX")


def scale_crop(source: PILImage.Image, box_width: int, box_height: int) -> PILImage.Image | None:
    """Resize a crop to :func:`fit_pixels`, with the filter that case deserves.

    Lanczos going down (an icon is thin strokes on a flat field; a box or
    nearest shrink eats the strokes), NEAREST going up (a doubled pixel should
    stay a pixel), and no resampling at all at 1:1.
    """
    width, height = fit_pixels(source.width, source.height, box_width, box_height)
    if not width or not height:
        return None
    if (width, height) == (source.width, source.height):
        return source
    resample = (
        PILImage.Resampling.NEAREST
        if width > source.width
        else PILImage.Resampling.LANCZOS
    )
    return source.resize((width, height), resample)


def pad_to_box(image: PILImage.Image, box_width: int, box_height: int) -> PILImage.Image:
    """Centre ``image`` on a transparent ``box_width x box_height`` canvas.

    The crop has to arrive at the sixel widget at EXACTLY the pixel size of the
    cell box it occupies, or textual-image scales it again to fill that box -
    which stretches it off its aspect ratio and undoes the careful fit above
    (``widget.sixel._ImageSixelImpl._scale_image`` resizes to the styled cell
    size in pixels, unconditionally). Padding rather than stretching is what
    makes that second resize a no-op.

    Transparent rather than a colour: the sixel encoder skips fully transparent
    pixels outright, so the padding is not drawn at all and the panel's own
    background shows through whatever theme is live.
    """
    canvas = PILImage.new("RGBA", (max(1, box_width), max(1, box_height)), (0, 0, 0, 0))
    canvas.paste(image, ((box_width - image.width) // 2, (box_height - image.height) // 2))
    return canvas


def crop_picture(
    image: RegionImage, cols: int, rows: int, graphics: TerminalGraphics | None = None
) -> PILImage.Image | None:
    """A captured crop as the exact ``cols x rows`` cell box the panel draws.

    The whole sixel path in one call: BGRX -> Pillow, fit to the cell box in
    pixels, pad back out to it. ``None`` when there is nothing to draw.
    """
    graphics = graphics or terminal_graphics()
    source = region_to_pil(image)
    if source is None:
        return None
    box_width = cols * graphics.cell_width
    box_height = rows * graphics.cell_height
    scaled = scale_crop(source, box_width, box_height)
    if scaled is None:
        return None
    return pad_to_box(scaled, box_width, box_height)
