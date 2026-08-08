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

The second half of this module answers the harder question the same way but in
two dimensions: "is this little appearance anywhere inside this whole chat
region?" - the region is now the browser's chat area rather than a strip whose
width the user matched to the icon, so nothing is pinned to a column and a
brute-force sweep of every (x, y) is millions of offsets. :class:`Template`
precomputes a handful of ANCHORs (short, visually busy byte runs from the
quantised blue plane) and the search lets ``bytes.find`` locate them at C
speed; only the few origins an anchor vouches for are ever compared pixel by
pixel. Quantisation (v >> 5) is what makes an exact byte search survive
anti-aliasing at all - and its residual risk is why there are eight anchors on
eight different rows: a shade drifting across a bucket edge tends to move a
whole row of the template at once, so the anchors that share no row with the
damage still find it.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion

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


# -- 2D search: find an appearance anywhere inside a region --------------------

# Quantisation table for bytes.translate: 256 shades collapse to 8 buckets, so
# an EXACT byte search tolerates the anti-aliasing and theme dithering that
# would otherwise make every capture of the same button a different string.
QUANT = bytes(value >> 5 for value in range(256))
# Anchor geometry. Eight bytes is long enough that a random 8-bucket run occurs
# roughly once per 16M pixels (i.e. essentially never in a 1080p scene) and
# short enough to survive a couple of pixels of sub-pixel shift at its edges.
ANCHOR_LEN = 8
# Eight anchors on eight different rows: the fallback that keeps a match
# findable when quantisation damage takes some rows out (see module docstring).
ANCHOR_COUNT = 8
# Stage-1 sniff test on a candidate origin: 16 pixels, at most 4 allowed to
# differ. Cheap enough to run on thousands of candidates, selective enough that
# almost none of them reach the full comparison.
PROBE_COUNT = 16
PROBE_FAIL_MAX = 4
# A flat scene (a blank page, a solid panel) makes every anchor match almost
# everywhere. The cap turns that pathological case into bounded work instead of
# a hang; a real appearance is found long before 512 hits of one anchor.
MAX_CANDIDATES_PER_ANCHOR = 512
# How much of the template is inspected when choosing anchors. Bounds
# Template.build for a wide template (a chat input box is ~800px across)
# without changing what it picks for a small icon.
_ANCHOR_ROW_SAMPLES = 128
_ANCHOR_X_SAMPLES = 16


@dataclass(frozen=True, slots=True)
class Anchor:
    """A short quantised byte run at ``(dx, dy)`` inside the template.

    Finding ``needle`` at some point in the scene proposes exactly one template
    origin - the point minus ``(dx, dy)`` - which is the whole trick: a C-level
    substring search replaces the sweep over every candidate offset.
    """

    dx: int
    dy: int
    needle: bytes


@dataclass(frozen=True, slots=True)
class RegionMatch:
    """Where the template sits in the scene: ``(x, y)`` is its top-left corner,
    scene-local (0, 0 = the scene's own top-left)."""

    x: int
    y: int
    diff: float


def _quantised_plane(image: RegionImage) -> bytes:
    """The blue channel of every pixel, quantised to 8 buckets, row-major.

    One byte per pixel, so a flat index into it IS ``y * width + x`` - which is
    what makes ``bytes.find`` usable as a 2D search.
    """
    return image.pixels[0 : image.width * image.height * 4 : 4].translate(QUANT)


def _window_score(window: bytes) -> int:
    """How distinctive an anchor candidate is: distinct buckets + transitions.

    Both terms matter. Distinct buckets rejects a flat run of background;
    transitions prefers an edge (a glyph stroke, a button border) over a smooth
    gradient that any other gradient in the scene would also satisfy.
    """
    distinct = len(set(window))
    transitions = sum(1 for i in range(1, len(window)) if window[i] != window[i - 1])
    return distinct + transitions


def _spread(count: int, budget: int) -> range:
    """At most ``budget`` indices spread evenly over ``range(count)``."""
    if count <= budget:
        return range(count)
    return range(0, count, -(-count // budget))


@dataclass(frozen=True, slots=True)
class Template:
    """An appearance to look for, plus the anchors that make looking cheap.

    Built once (at capture, or when a profile is loaded from disk) and reused
    by every later search - anchor selection is the expensive part and it does
    not depend on the scene.
    """

    image: RegionImage
    anchors: tuple[Anchor, ...]

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    @classmethod
    def build(cls, image: RegionImage) -> Template:
        """Choose this image's anchors. Raises ValueError if it cannot be one:
        no area, a truncated buffer, or narrower than a single anchor."""
        width, height = image.width, image.height
        if width <= 0 or height <= 0:
            raise ValueError("template has no area")
        if len(image.pixels) < width * height * 4:
            raise ValueError("template pixel buffer is truncated")
        if width < ANCHOR_LEN:
            raise ValueError(f"template is narrower than an anchor ({width} < {ANCHOR_LEN}px)")

        plane = _quantised_plane(image)
        offsets = _spread(width - ANCHOR_LEN + 1, _ANCHOR_X_SAMPLES)
        # Negated score so a plain sort puts the most distinctive window first
        # and breaks ties by position - anchor choice must be deterministic, or
        # a reloaded profile would search differently than the captured one.
        scored: list[tuple[int, int, int]] = []
        for y in _spread(height, _ANCHOR_ROW_SAMPLES):
            base = y * width
            for x in offsets:
                scored.append((-_window_score(plane[base + x : base + x + ANCHOR_LEN]), y, x))
        scored.sort()

        def anchor_at(y: int, x: int) -> Anchor:
            start = y * width + x
            return Anchor(x, y, plane[start : start + ANCHOR_LEN])

        anchors: list[Anchor] = []
        taken_rows: set[int] = set()
        for _, y, x in scored:  # one per row first: spread the bets vertically
            if y in taken_rows:
                continue
            taken_rows.add(y)
            anchors.append(anchor_at(y, x))
            if len(anchors) == ANCHOR_COUNT:
                return cls(image, tuple(anchors))
        taken = {(a.dy, a.dx) for a in anchors}
        for _, y, x in scored:  # a short template runs out of rows: reuse them
            if (y, x) in taken:
                continue
            taken.add((y, x))
            anchors.append(anchor_at(y, x))
            if len(anchors) == ANCHOR_COUNT:
                break
        return cls(image, tuple(anchors))


def _fits(template: RegionImage, scene: RegionImage, x: int, y: int) -> bool:
    return 0 <= x <= scene.width - template.width and 0 <= y <= scene.height - template.height


def _probe_at(template: RegionImage, scene: RegionImage, x: int, y: int, tolerance: int) -> bool:
    """Stage 1: do PROBE_COUNT spread-out pixels agree at this origin?

    Returns as soon as too many have failed - on a flat scene thousands of
    anchor hits are rejected here, and each must cost a handful of comparisons,
    not a full sample budget.
    """
    total = template.width * template.height
    step = max(1, total // PROBE_COUNT)
    left, right = memoryview(template.pixels), memoryview(scene.pixels)
    failed = 0
    for probe in range(PROBE_COUNT):
        index = probe * step
        if index >= total:
            break
        ty, tx = divmod(index, template.width)
        here = index * 4
        there = ((y + ty) * scene.width + (x + tx)) * 4
        if (
            abs(left[here] - right[there]) > tolerance
            or abs(left[here + 1] - right[there + 1]) > tolerance
            or abs(left[here + 2] - right[there + 2]) > tolerance
        ):
            failed += 1
            if failed > PROBE_FAIL_MAX:
                return False
    return True


def _diff_at_xy(template: RegionImage, scene: RegionImage, x: int, y: int, tolerance: int) -> float:
    """Stage 2: the strided full comparison of ``_diff_at``, at a 2D origin.

    Same sampling discipline (at most MAX_SAMPLES pixels, uniform stride over
    the template's row-major order), so diffs stay comparable between offsets.
    """
    total = template.width * template.height
    step = 1 if total <= MAX_SAMPLES else -(-total // MAX_SAMPLES)
    left, right = memoryview(template.pixels), memoryview(scene.pixels)
    sampled = differing = 0
    for index in range(0, total, step):
        ty, tx = divmod(index, template.width)
        here = index * 4
        there = ((y + ty) * scene.width + (x + tx)) * 4
        sampled += 1
        # Byte 3 of each BGRX pixel is undefined in a GDI capture - skip it.
        if (
            abs(left[here] - right[there]) > tolerance
            or abs(left[here + 1] - right[there + 1]) > tolerance
            or abs(left[here + 2] - right[there + 2]) > tolerance
        ):
            differing += 1
    return differing / sampled


def _candidate_origins(template: Template, scene: RegionImage) -> list[tuple[int, int]]:
    """Every template origin the anchors vouch for, in anchor then scan order.

    Empty (rather than an exception) when the scene cannot hold the template: a
    resized browser window is a normal runtime condition, not a bug.
    """
    if scene.width < template.width or scene.height < template.height:
        return []
    if len(scene.pixels) < scene.width * scene.height * 4:
        return []

    plane = _quantised_plane(scene)  # once per search, not once per anchor
    width = scene.width
    origins: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for anchor in template.anchors:
        hits = 0
        position = 0
        while hits < MAX_CANDIDATES_PER_ANCHOR:
            index = plane.find(anchor.needle, position)
            if index < 0:
                break
            position = index + 1
            hits += 1
            y, x = divmod(index, width)
            if x + ANCHOR_LEN > width:
                continue  # the run straddles two rows: not a horizontal match
            origin = (x - anchor.dx, y - anchor.dy)
            if origin in seen or not _fits(template.image, scene, *origin):
                continue
            seen.add(origin)
            origins.append(origin)
    return origins


def _verified(
    template: Template, scene: RegionImage, tolerance: int, max_diff: float
) -> list[RegionMatch]:
    matches: list[RegionMatch] = []
    for x, y in _candidate_origins(template, scene):
        if not _probe_at(template.image, scene, x, y, tolerance):
            continue
        diff = _diff_at_xy(template.image, scene, x, y, tolerance)
        if diff <= max_diff:
            matches.append(RegionMatch(x, y, diff))
    return matches


def match_at_xy(
    template: RegionImage,
    scene: RegionImage,
    x: int,
    y: int,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
) -> float:
    """Diff fraction between the template and the scene at origin ``(x, y)``.
    0.0 is a pixel-perfect match.

    Raises ValueError if the template is empty, a pixel buffer is truncated, or
    the template would not fit inside the scene at that origin.
    """
    if template.width <= 0 or template.height <= 0:
        raise ValueError("template has no area")
    if len(template.pixels) < template.width * template.height * 4:
        raise ValueError("template pixel buffer is truncated")
    if len(scene.pixels) < scene.width * scene.height * 4:
        raise ValueError("scene pixel buffer is truncated")
    if not _fits(template, scene, x, y):
        raise ValueError(f"({x}, {y}) does not fit inside the scene")
    return _diff_at_xy(template, scene, x, y, tolerance)


def find_in_region(
    template: Template,
    scene: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
) -> RegionMatch | None:
    """The first place the template matches, or None. Never raises.

    "First" is anchor order, not reading order - callers that only ask *is it
    there?* (is the stop button on screen?) want the early exit, and for a
    presence question any verified occurrence answers it.
    """
    for x, y in _candidate_origins(template, scene):
        if not _probe_at(template.image, scene, x, y, tolerance):
            continue
        diff = _diff_at_xy(template.image, scene, x, y, tolerance)
        if diff <= max_diff:
            return RegionMatch(x, y, diff)
    return None


def find_lowest_in_region(
    template: Template,
    scene: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
) -> RegionMatch | None:
    """The bottom-most match (largest y), or None. Never raises.

    The copy button's question: every response stamps one, and the newest is
    the lowest. No early exit is possible - the answer is only known once every
    candidate has been judged.
    """
    matches = _verified(template, scene, tolerance, max_diff)
    if not matches:
        return None
    return max(matches, key=lambda match: (match.y, match.x))


def find_all_in_region(
    template: Template,
    scene: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
    limit: int = 64,
) -> list[RegionMatch]:
    """Every match, top-to-bottom then left-to-right, at most ``limit``. Never raises."""
    matches = _verified(template, scene, tolerance, max_diff)
    matches.sort(key=lambda match: (match.y, match.x))
    return matches[:limit]


def match_rect(region: ScreenRegion, template: Template, match: RegionMatch) -> ScreenRegion:
    """A scene-local match back into absolute screen coordinates - what a click
    needs. ``region`` is the screen rectangle the scene was captured from."""
    return ScreenRegion(
        region.left + match.x,
        region.top + match.y,
        template.width,
        template.height,
    )
