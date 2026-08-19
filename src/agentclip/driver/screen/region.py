"""ScreenRegion: a user-drawn rectangle in virtual-screen pixel coordinates.

Coordinates come from the overlay (screen.overlay) and feed the focus click
(screen.focus). On a multi-monitor desktop ``left``/``top`` can be negative
(monitors left of / above the primary), so parsing must accept signed ints.

The wire format - ``left top width height`` on one line - is how the picker
child process reports its result over stdout (screen.picker).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScreenRegion:
    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def describe(self) -> str:
        return f"{self.width}×{self.height} at ({self.left}, {self.top})"


def _offset(size: int, percent: int) -> int:
    """How far into ``size`` pixels ``percent`` lands, rounded half up.

    Half up rather than Python's round-half-to-even, so that 50% is the
    region's own centre for EVERY size: ``floor((size - 1) / 2 + 0.5)`` is
    ``size // 2``, which is what :attr:`ScreenRegion.center` uses. Banker's
    rounding misses it on a six-pixel-wide icon.
    """
    return int((size - 1) * percent / 100 + 0.5)


def click_point_region(region: ScreenRegion, x_percent: int, y_percent: int) -> ScreenRegion:
    """The one pixel inside ``region`` a click aimed at ``x``%/``y``% lands on.

    A ScreenRegion rather than a pair, because that is what every click in the
    app takes - and a 1x1 rectangle is the one whose centre is the pixel
    itself, so the point survives ``click_region``'s own centring.

    The percentages span the region's pixels rather than its edges: 0/0 is the
    top-left pixel, 100/100 the bottom-right one, and 50/50 is exactly
    ``region.center``. Chat boxes that are clickable end to end are why this
    exists - the middle of the box is a fine place to aim only until a service
    puts an attachment tray there.
    """
    return ScreenRegion(
        region.left + _offset(region.width, x_percent),
        region.top + _offset(region.height, y_percent),
        1,
        1,
    )


def format_region(region: ScreenRegion) -> str:
    return f"{region.left} {region.top} {region.width} {region.height}"


def parse_region(text: str) -> ScreenRegion | None:
    """The wire format back into a region; None for anything else (a cancelled
    overlay prints nothing, so empty/garbage input is a normal outcome)."""
    parts = text.split()
    if len(parts) != 4:
        return None
    try:
        left, top, width, height = (int(p) for p in parts)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return ScreenRegion(left, top, width, height)
