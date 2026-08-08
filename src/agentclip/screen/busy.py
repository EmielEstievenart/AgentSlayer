"""How different are two captures of the same rectangle, and what a verdict is.

The pixel comparison (``diff_fraction``) and the vocabulary every finish
detector answers in (``BusyState`` / ``BusyProbe``) live here. Nothing in this
module looks at the screen any more: the detectors that do are
:mod:`agentclip.screen.presence` (is this appearance visible anywhere in the
chat region?) and :mod:`agentclip.screen.stale` (has the chat region stopped
changing?), and both report their verdicts in these types.

``BusyState`` keeps its historical names because the polarity is the caller's
to assign: MATCH means "still generating" for a busy-time appearance and
"finished" for an idle-time one, which is exactly the reading the TUI's
``_busy_verdict`` / ``_idle_verdict`` already give them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentclip.screen.capture import RegionImage

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


def diff_fraction(
    a: RegionImage,
    b: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_samples: int = MAX_SAMPLES,
) -> float:
    """Fraction of (sampled) pixels whose B/G/R delta exceeds ``tolerance``.

    Mismatched dimensions are a full mismatch (1.0).

    At most ``max_samples`` pixels are compared (default ``MAX_SAMPLES``, so
    every existing caller keeps its exact sampling), picked with a uniform
    stride over the row-major pixel order so the sample spreads across the
    whole image (and is the same set of pixels on every probe). The stale
    detector passes a denser budget: it must notice a single appended text
    line inside a full response region, which the default stride can step
    right over.
    """
    if a.width != b.width or a.height != b.height:
        return 1.0
    total = a.width * a.height
    if total <= 0 or len(a.pixels) < total * 4 or len(b.pixels) < total * 4:
        return 1.0

    step = 1 if total <= max_samples else -(-total // max_samples)
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


