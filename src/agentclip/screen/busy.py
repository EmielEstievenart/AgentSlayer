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
from math import gcd

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
    """One poll's verdict; ``diff`` is the differing-pixel fraction (None on ERROR).

    ``state`` is de-bounced: it is a reading of a *sequence* of frames, and
    during the settling window after a reset it reports the "still generating"
    default with no frame behind it at all (see
    :mod:`agentclip.screen.presence`). That default is the right bias for a
    finish decision and a disastrous one for anything that treats a single
    verdict as proof, so the raw per-frame fact travels alongside it:

    ``generating_now`` is True only when THIS frame's own template search
    directly says the model is generating - never on a grace-period default,
    never on an ERROR. Callers that need "the model is visibly generating right
    now" (arming the auto-copy, overriding the ready-to-send gate) read this;
    callers deciding "has it finished?" read ``state`` and keep the debounce.
    """

    state: BusyState
    diff: float | None
    generating_now: bool = False


def sample_step(total: int, budget: int, width: int) -> int:
    """The stride that walks ``total`` row-major pixels in at most ``budget``
    samples - nudged up until it is coprime with ``width``.

    Coprimality is the whole point, and skipping it is a silent sensor
    failure rather than a rounding error. A stride that shares a factor with
    the row length lands on the same handful of columns forever: over a
    1280-wide region at 16384 samples the raw stride is 80, and 80 divides
    1280, so the "spread over the whole image" sample is 16 of the 1280
    columns - a change confined to the other 1264 reads as no change at all
    (a paragraph appearing in a chat, to the stale detector). With the stride
    bumped to the next coprime value the walk visits every column in turn, for
    the price of a few samples.

    Only the image geometry decides it, never the caller's position in it, so
    two frames - or two candidate origins - always compare the same pixels.
    """
    if total <= budget:
        return 1
    step = -(-total // budget)  # ceil: at most `budget` samples
    while gcd(step, width) > 1:
        step += 1
    return step


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
    whole image (and is the same set of pixels on every probe - see
    :func:`sample_step` for why that stride is coprime with the width). The
    stale detector passes a denser budget: it must notice a single appended
    text line inside a full response region, which the default stride can step
    right over.
    """
    if a.width != b.width or a.height != b.height:
        return 1.0
    total = a.width * a.height
    if total <= 0 or len(a.pixels) < total * 4 or len(b.pixels) < total * 4:
        return 1.0

    step = sample_step(total, max_samples, a.width)
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


