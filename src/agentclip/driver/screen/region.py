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
