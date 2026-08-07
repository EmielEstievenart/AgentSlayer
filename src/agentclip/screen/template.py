"""Find the lowest copy-button icon inside a tall vertical strip of the screen.

The user calibrates once by boxing a chat's copy button; that little icon is the
template. Every response since then stamps the same icon down the right-hand
gutter of the transcript, so to click the NEWEST one we capture a same-width
vertical band and look for the bottom-most place the template still matches
(largest y). Multiple matches are the normal case, not an error.

Pixel comparison is the house style from screen.busy: strided sampling with a
fixed budget, per-channel tolerance on B/G/R, the undefined X byte skipped.
Deliberately a pure function of its inputs - no capture, no ctypes, no OS - so
it is unit-testable anywhere and cheap enough to run once per response.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentclip.screen.capture import RegionImage

# A channel delta below this is anti-aliasing/theme noise, not a different icon.
DEFAULT_TOLERANCE = 24
# The template "matches" when no more than this fraction of sampled pixels differ.
# Looser than busy's threshold: the icon sits on whatever text happens to be
# behind it and hover states tint it.
DEFAULT_MAX_DIFF = 0.08
# Pixels compared per candidate offset. A tall band means hundreds of offsets,
# so the per-offset cost is what keeps the whole scan affordable.
MAX_SAMPLES = 1024


@dataclass(frozen=True, slots=True)
class TemplateMatch:
    """Where the template was found: ``y_offset`` is band-local (0 = band top)."""

    y_offset: int
    diff: float


def _validate(template: RegionImage, band: RegionImage) -> None:
    if template.width != band.width:
        raise ValueError(f"width mismatch: template {template.width}, band {band.width}")
    if template.width <= 0 or template.height <= 0:
        raise ValueError("template has no area")
    if band.height < template.height:
        raise ValueError(
            f"band ({band.height}px) is shorter than the template ({template.height}px)"
        )
    if len(template.pixels) < template.width * template.height * 4:
        raise ValueError("template pixel buffer is truncated")
    if len(band.pixels) < band.width * band.height * 4:
        raise ValueError("band pixel buffer is truncated")


def _diff_at(template: RegionImage, band: RegionImage, y_offset: int, tolerance: int) -> float:
    """Diff fraction between the template and the band slice at ``y_offset``.

    Inputs are assumed already validated. At most ``MAX_SAMPLES`` pixels are
    compared, picked with a uniform stride over the template's row-major pixel
    order - the same set of pixels at every offset, so the diffs are comparable.
    """
    total = template.width * template.height
    step = 1 if total <= MAX_SAMPLES else -(-total // MAX_SAMPLES)
    # Widths are equal, so a template pixel index maps onto the band by shifting
    # whole rows: band index = y_offset * width + template index.
    base = y_offset * band.width
    left, right = memoryview(template.pixels), memoryview(band.pixels)
    sampled = differing = 0
    for index in range(0, total, step):
        here = index * 4
        there = (base + index) * 4
        sampled += 1
        # Byte 3 of each BGRX pixel is undefined in a GDI capture - skip it.
        if (
            abs(left[here] - right[there]) > tolerance
            or abs(left[here + 1] - right[there + 1]) > tolerance
            or abs(left[here + 2] - right[there + 2]) > tolerance
        ):
            differing += 1
    return differing / sampled


def match_at(
    template: RegionImage,
    band: RegionImage,
    y_offset: int,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
) -> float:
    """Fraction of sampled pixels that differ between the template and the band
    slice starting at ``y_offset``. 0.0 is a pixel-perfect match.

    Raises ValueError if the widths differ, the template is empty, a pixel
    buffer is truncated, or the slice would run off either end of the band.
    """
    _validate(template, band)
    if y_offset < 0 or y_offset + template.height > band.height:
        raise ValueError(f"offset {y_offset} does not fit inside the band")
    return _diff_at(template, band, y_offset, tolerance)


def find_lowest_match(
    template: RegionImage,
    band: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
) -> TemplateMatch | None:
    """The bottom-most offset where the template matches, or None if it never does.

    Scanning bottom-up and returning the first hit is both the answer we want
    (the newest response's copy button) and the cheap way to get it: a match
    near the bottom ends the scan after a handful of offsets.
    """
    _validate(template, band)
    for y_offset in range(band.height - template.height, -1, -1):
        diff = _diff_at(template, band, y_offset, tolerance)
        if diff <= max_diff:
            return TemplateMatch(y_offset, diff)
    return None
