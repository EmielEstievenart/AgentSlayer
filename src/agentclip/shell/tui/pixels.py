"""Draw a captured frame as terminal cells, with nothing but ``rich``.

"Is the tool looking at the right rectangle?" is a question about pixels, and
until now the only answer was a status line claiming a size. This module is the
picture: one terminal cell per TWO vertically stacked pixels, drawn as U+2580
UPPER HALF BLOCK with the top pixel as the foreground colour and the bottom
pixel as the background. A cell is roughly twice as tall as it is wide, so
half-blocks come out with roughly SQUARE pixels - a chat window keeps its
proportions instead of being squashed to a third of its height.

**The renderer for terminals that cannot do better.** The ELEMENTS column now
draws its close-ups as sixel where the terminal supports it (``tui.graphics``,
tui.md 1.7) - averaging a 24px icon into six cell rows answers *is that
squarish* and nothing sharper. This module is what everything else gets: the
service editor's template thumbnails (where a shape really is the question),
and every terminal, pipe and test run without sixel. It needs nothing from the
terminal and nothing from Pillow, which is exactly why it stays.

Everything here is a pure function over :class:`RegionImage`, with no Textual
in sight, so the sizing, the averaging and the colours are unit-testable
without a Pilot.

Sizing and averaging belong to the CALLER'S thread, not the UI's.
:func:`thumbnail` is meant to run wherever the frame was captured - the
detector poll worker already holds it - because a chat region is hundreds of
thousands of pixels and only the couple of hundred cells that survive
downsampling should ever cross into the message queue. :func:`half_block_text`
is the cheap half and runs on the UI thread, over the small image.
"""

from __future__ import annotations

from rich.color import Color
from rich.style import Style
from rich.text import Text

from agentclip.driver.screen.capture import RegionImage, crop

# Re-exported, not defined here any more. ``crop`` is the cutter both element
# panels are built on - the matched rectangle out of the frame it was found in -
# and it is a pure function over a captured buffer with nothing terminal about
# it, so it lives next to the type it cuts (``screen.capture``) now that a
# second shell needs it and the two shells may not import each other. Imported
# under this name so every existing caller and test keeps working.
__all__ = [
    "HALF_BLOCK",
    "crop",
    "downsample",
    "fit_cells",
    "half_block_text",
    "thumbnail",
]

# U+2580: the top half of the cell is painted in the foreground colour, the
# bottom half in the background colour. Two pixels per cell, and the only glyph
# this module draws.
HALF_BLOCK = "▀"

# How many source pixels each output pixel averages, per axis. A full box
# average over a 600x500 region is ~300k samples of pure-Python indexing per
# thumbnail; capping the grid at 4x4 costs ~8k for a 28x16 preview and is
# indistinguishable at this scale, while still being far more faithful than
# nearest-neighbour (which, on a screenshot of mostly-background text, samples
# the background and shows an empty rectangle).
_SAMPLES_PER_AXIS = 4


def fit_cells(width: int, height: int, max_cols: int, max_rows: int) -> tuple[int, int]:
    """The largest ``(cols, rows)`` cell box inside the budget that keeps the aspect.

    Two pixels stack in one cell, so a box of ``cols x rows`` cells holds
    ``cols x (rows * 2)`` pixels - which is why the height is halved here and
    nowhere else. Returns ``(0, 0)`` when there is nothing to draw (no source
    area, or no room to draw it in), which is the caller's cue to show its
    resting line instead.
    """
    if width <= 0 or height <= 0 or max_cols <= 0 or max_rows <= 0:
        return (0, 0)
    cols = max_cols
    rows = max(1, -(-(cols * height) // (width * 2)))  # ceil(cols * h / (w * 2))
    if rows > max_rows:
        rows = max_rows
        cols = max(1, min(max_cols, round(rows * 2 * width / height)))
    return (cols, rows)


def downsample(image: RegionImage, width: int, height: int) -> RegionImage:
    """Box-average ``image`` down to ``width x height`` BGRX pixels.

    Averaged rather than sampled: a chat window is mostly background with thin
    text on it, and a nearest-neighbour shrink of one lands on the background
    almost every time and draws a blank rectangle. The average keeps the
    message blocks visible as bands, which is exactly the "yes, that is my
    chat" cue the preview exists for.

    A zero-size target, a zero-size source or a truncated buffer all produce an
    empty image rather than an exception: this is a preview, and the callers'
    answer to all three is the same.
    """
    src_w, src_h = image.width, image.height
    if width <= 0 or height <= 0 or src_w <= 0 or src_h <= 0:
        return RegionImage(0, 0, b"")
    src = image.pixels
    if len(src) < src_w * src_h * 4:
        return RegionImage(0, 0, b"")

    # Column sample sets are the same for every output row, so they are built
    # once instead of width*height times.
    x_samples = [_axis_samples(tx, width, src_w) for tx in range(width)]
    out = bytearray(width * height * 4)
    for ty in range(height):
        ys = _axis_samples(ty, height, src_h)
        for tx in range(width):
            xs = x_samples[tx]
            blue = green = red = 0
            for sy in ys:
                row = sy * src_w * 4
                for sx in xs:
                    i = row + sx * 4
                    blue += src[i]
                    green += src[i + 1]
                    red += src[i + 2]
            count = len(ys) * len(xs)
            o = (ty * width + tx) * 4
            out[o] = blue // count
            out[o + 1] = green // count
            out[o + 2] = red // count  # byte 3 stays 0: X is undefined in a capture
    return RegionImage(width, height, bytes(out))


def _axis_samples(index: int, out_size: int, src_size: int) -> list[int]:
    """Which source coordinates output pixel ``index`` averages, along one axis."""
    lo = index * src_size // out_size
    hi = max(lo + 1, (index + 1) * src_size // out_size)
    span = hi - lo
    if span <= _SAMPLES_PER_AXIS:
        return list(range(lo, hi))
    return [lo + (k * span) // _SAMPLES_PER_AXIS for k in range(_SAMPLES_PER_AXIS)]


def thumbnail(image: RegionImage, max_cols: int, max_rows: int) -> RegionImage | None:
    """Shrink ``image`` to fit a ``max_cols x max_rows`` cell box, aspect kept.

    The worker-thread entry point: :func:`fit_cells` then :func:`downsample`,
    with ``None`` for "there is nothing to show" so a caller never has to test
    two things. The result is already the exact pixel grid
    :func:`half_block_text` will draw - the UI thread does no arithmetic on the
    full frame.
    """
    cols, rows = fit_cells(image.width, image.height, max_cols, max_rows)
    if not cols or not rows:
        return None
    small = downsample(image, cols, rows * 2)
    return small if small.width else None


def half_block_text(image: RegionImage) -> Text:
    """Render an (already small) frame as ``rows = ceil(height / 2)`` lines.

    One :meth:`Text.append` per cell, in reading order, so the spans are the
    cells - which is what makes the colours assertable in a test without going
    near a terminal. An odd final row has no bottom pixel to pair with, so it
    pairs with itself: a half-lit last line reads as a rendering bug, and the
    repeat is invisible.

    An empty or truncated image renders as empty text, not an exception (see
    :func:`downsample`).
    """
    width, height = image.width, image.height
    text = Text(no_wrap=True, overflow="crop", end="")
    if width <= 0 or height <= 0:
        return text
    src = image.pixels
    if len(src) < width * height * 4:
        return text

    for row in range((height + 1) // 2):
        if row:
            text.append("\n")
        top_off = (row * 2) * width * 4
        bottom_off = min(row * 2 + 1, height - 1) * width * 4
        for x in range(width):
            i = top_off + x * 4
            j = bottom_off + x * 4
            text.append(
                HALF_BLOCK,
                Style(
                    color=Color.from_rgb(src[i + 2], src[i + 1], src[i]),
                    bgcolor=Color.from_rgb(src[j + 2], src[j + 1], src[j]),
                ),
            )
    return text
