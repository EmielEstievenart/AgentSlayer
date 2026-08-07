"""Busy detection: does a screen region still look like it did at calibration?

The user calibrates WHILE the model is generating (the region typically shows
the chat's stop-button square). As long as a fresh capture matches the
calibration baseline the model is still reasoning; when it stops matching the
response has finished. Nothing acts on the verdict yet - the TUI only displays
it (tui.md, sidebar).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentclip.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.screen.region import ScreenRegion

# A channel delta below this is anti-aliasing/theme noise, not a real change.
DEFAULT_TOLERANCE = 24
# The region "changed" when more than this fraction of sampled pixels differ.
DEFAULT_MAX_DIFF = 0.02
# Upper bound on pixels compared per probe. The user may draw a huge box, and
# this runs on a poll timer - a fixed sample budget keeps every probe cheap.
MAX_SAMPLES = 4096


class BusyState(Enum):
    MATCH = "match"  # still looks calibrated -> reasoning ongoing
    CHANGED = "changed"  # region differs -> response finished
    ERROR = "error"  # capture failed; no verdict


@dataclass(frozen=True, slots=True)
class BusyProbe:
    """One poll's verdict; ``diff`` is the differing-pixel fraction (None on ERROR)."""

    state: BusyState
    diff: float | None


def diff_fraction(a: RegionImage, b: RegionImage, *, tolerance: int = DEFAULT_TOLERANCE) -> float:
    """Fraction of (sampled) pixels whose B/G/R delta exceeds ``tolerance``.

    Mismatched dimensions are a full mismatch (1.0).

    At most ``MAX_SAMPLES`` pixels are compared, picked with a uniform stride
    over the row-major pixel order so the sample spreads across the whole image
    (and is the same set of pixels on every probe).
    """
    if a.width != b.width or a.height != b.height:
        return 1.0
    total = a.width * a.height
    if total <= 0 or len(a.pixels) < total * 4 or len(b.pixels) < total * 4:
        return 1.0

    step = 1 if total <= MAX_SAMPLES else -(-total // MAX_SAMPLES)
    left, right = memoryview(a.pixels), memoryview(b.pixels)
    sampled = differing = 0
    for index in range(0, total, step):
        offset = index * 4
        sampled += 1
        # Byte 3 of each BGRX pixel is undefined in a GDI capture - skip it.
        if (
            abs(left[offset] - right[offset]) > tolerance
            or abs(left[offset + 1] - right[offset + 1]) > tolerance
            or abs(left[offset + 2] - right[offset + 2]) > tolerance
        ):
            differing += 1
    return differing / sampled


def probe_busy(
    baseline: RegionImage,
    region: ScreenRegion,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
) -> BusyProbe:
    """Capture ``region`` now and compare against ``baseline``. Never raises."""
    try:
        current = capture_region(region)
    except CaptureError:
        return BusyProbe(BusyState.ERROR, None)
    diff = diff_fraction(baseline, current, tolerance=tolerance)
    state = BusyState.MATCH if diff <= max_diff else BusyState.CHANGED
    return BusyProbe(state, diff)
