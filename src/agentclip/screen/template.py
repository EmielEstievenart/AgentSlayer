"""Is this little appearance anywhere inside this captured region, and where?

The one question the whole recognition model rests on. The user captures what a
thing looks like once (a copy icon, a stop button, a chat input box) and draws
one box around the browser window; everything after that is this search, run
against a fresh capture of that window - so nothing is ever pinned to a
remembered coordinate and the browser is free to move.

A brute-force sweep of every (x, y) origin in a 1000×800 region is millions of
offsets, which is not affordable on a poll timer. :class:`Template` therefore
precomputes a handful of ANCHORs (short, visually busy byte runs from the
quantised blue plane) and the search lets ``bytes.find`` locate them at C
speed; only the few origins an anchor vouches for are ever compared pixel by
pixel, in two stages - a 16-pixel sniff test, then the full strided comparison.

Quantisation (v >> 5) is what makes an exact byte search survive anti-aliasing
at all, and its residual risk is why there are eight anchors on eight different
rows: a shade drifting across a bucket edge tends to move a whole row of the
template at once, so the anchors sharing no row with the damage still find it.

Pixel comparison is the house style from screen.busy: strided sampling with a
fixed budget, per-channel tolerance on B/G/R, the undefined X byte skipped.
Deliberately a pure function of its inputs - no capture, no ctypes, no OS - so
it is unit-testable anywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from agentclip.screen.busy import sample_step
from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion

# A channel delta below this is anti-aliasing/theme noise, not a different icon.
DEFAULT_TOLERANCE = 24
# The template "matches" when no more than this fraction of sampled pixels
# differ. Looser than busy's threshold: an icon sits on whatever text happens to
# be behind it and hover states tint it. Each TemplateKind overrides it with a
# value suited to what it is (screen.profile).
DEFAULT_MAX_DIFF = 0.08
# Pixels compared per verified candidate. A whole browser window yields many
# anchor hits, so the per-candidate cost is what keeps a poll affordable.
MAX_SAMPLES = 1024


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
# Stage-1 sniff test on a candidate origin: 16 pixels on a 4x4 grid over the
# template, at most 4 allowed to differ. Cheap enough to run on thousands of
# candidates, selective enough that almost none of them reach the full
# comparison - as long as the 16 pixels actually spread out. A grid rather than
# a strided walk of the row-major order for exactly that reason: any stride
# sharing a factor with the template width probes one column sixteen times
# (a 24x16 icon, at a stride of 24, sniffs nothing but column 0), and half of
# an icon can then be wrong without a single probe noticing.
_PROBE_GRID = 4
PROBE_COUNT = _PROBE_GRID * _PROBE_GRID
PROBE_FAIL_MAX = 4
# A flat scene (a blank page, a solid panel) makes every anchor match almost
# everywhere. The cap turns that pathological case into bounded work instead of
# a hang; a real appearance is found long before 512 hits of one anchor. Only
# candidates that survive the cheap rejections count against it (see
# _candidate_origins): junk hits must not be able to spend the budget.
MAX_CANDIDATES_PER_ANCHOR = 512
# Ceiling on stage-2 comparisons in one search. The per-anchor cap already
# bounds the candidate list, but a scene tiled with the template (a gallery of
# identical copy buttons) makes every one of those candidates a REAL match that
# the cheap probe rightly waves through, and 4096 full comparisons is a second
# of a poll interval. Candidates arrive bottom-most first, so the budget is
# spent where the answer to both questions this module is asked - is it there?
# where is the newest one? - actually lives.
MAX_VERIFICATIONS = 256
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

        def needle_at(y: int, x: int) -> bytes:
            start = y * width + x
            return plane[start : start + ANCHOR_LEN]

        # Eight anchors are eight independent chances to find the appearance,
        # which they only are if they are eight different needles: a two-colour
        # icon's most "distinctive" window is the same alternating run wherever
        # it is read, and eight anchors carrying it search one place eight
        # times. So a needle already taken disqualifies the window - without
        # spending the row, so the row's next-best window still gets a turn.
        anchors: list[Anchor] = []
        needles: set[bytes] = set()
        taken: set[tuple[int, int]] = set()
        taken_rows: set[int] = set()

        def sweep(*, fresh_row: bool, fresh_needle: bool) -> bool:
            """Take the best-scoring windows that meet both conditions.
            True once ANCHOR_COUNT anchors have been chosen."""
            for _, y, x in scored:
                if (y, x) in taken or (fresh_row and y in taken_rows):
                    continue
                needle = needle_at(y, x)
                if fresh_needle and needle in needles:
                    continue
                taken.add((y, x))
                taken_rows.add(y)
                needles.add(needle)
                anchors.append(Anchor(x, y, needle))
                if len(anchors) == ANCHOR_COUNT:
                    return True
            return False

        # In preference order: a new row AND a new needle; then a new needle on
        # a row already used (a short template runs out of rows); then a new
        # row carrying a needle already taken; then anything left. The last two
        # are the flat panel and the two-tone glyph, which simply do not
        # contain ANCHOR_COUNT distinct needles - and a duplicate anchor on a
        # fresh row still buys the row redundancy quantisation damage needs.
        for fresh_needle in (True, False):
            for fresh_row in (True, False):
                if sweep(fresh_row=fresh_row, fresh_needle=fresh_needle):
                    return cls(image, tuple(anchors))
        return cls(image, tuple(anchors))


def _fits(template: RegionImage, scene: RegionImage, x: int, y: int) -> bool:
    return 0 <= x <= scene.width - template.width and 0 <= y <= scene.height - template.height


def _probe_at(template: RegionImage, scene: RegionImage, x: int, y: int, tolerance: int) -> bool:
    """Stage 1: do the PROBE_COUNT grid pixels agree at this origin?

    The grid is the centre of each cell of a _PROBE_GRID x _PROBE_GRID division
    of the template, so both halves and both bands are always represented -
    whatever the template's shape. Returns as soon as too many have failed: on
    a flat scene thousands of anchor hits are rejected here, and each must cost
    a handful of comparisons, not a full sample budget.
    """
    left, right = memoryview(template.pixels), memoryview(scene.pixels)
    failed = 0
    for row in range(_PROBE_GRID):
        ty = (2 * row + 1) * template.height // (2 * _PROBE_GRID)
        for column in range(_PROBE_GRID):
            tx = (2 * column + 1) * template.width // (2 * _PROBE_GRID)
            here = (ty * template.width + tx) * 4
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
    the template's row-major order), so diffs stay comparable between offsets -
    including the stride's coprimality with the width, which for a template as
    wide as a chat input box is the difference between reading every column and
    reading sixteen of them (busy.sample_step).
    """
    total = template.width * template.height
    step = sample_step(total, MAX_SAMPLES, template.width)
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
    """Every template origin the anchors vouch for, bottom-most first.

    Two properties earn their keep here, and both are about what happens when
    MAX_CANDIDATES_PER_ANCHOR binds - which is precisely when the scene is
    repetitive enough for the search to need help:

    * The sweep runs BACKWARDS (``bytes.rfind``), so the candidates that
      survive the cap are the lowest ones. That is the answer
      find_lowest_in_region is looking for outright, and for find_in_region it
      biases the search toward the bottom of a chat, where the buttons live.
    * Only candidates that survive the cheap rejections - row-wrapped runs,
      origins the scene cannot hold, origins another anchor already proposed -
      count against the cap. A repetitive banner across the top of a page
      produces thousands of hits that are nothing; letting them spend the
      budget blinds the search to the real appearance below them.

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
        # Outside this band the anchor's own offset already puts the template
        # off the edge of the scene, so a hit there cannot become a candidate
        # however the rest of it looks - skip the two dead bands wholesale
        # rather than examining a flat scene's worth of hits inside them.
        first = anchor.dy * width + anchor.dx
        last = (anchor.dy + scene.height - template.height) * width + (
            anchor.dx + scene.width - template.width
        )
        end = min(len(plane), last + ANCHOR_LEN)
        kept = 0
        while kept < MAX_CANDIDATES_PER_ANCHOR:
            index = plane.rfind(anchor.needle, first, end)
            if index < 0:
                break
            # One short of this hit, so overlapping runs are still found and
            # the window always shrinks (no way to sit on the same hit twice).
            end = index + ANCHOR_LEN - 1
            y, x = divmod(index, width)
            if x + ANCHOR_LEN > width:
                continue  # the run straddles two rows: not a horizontal match
            origin = (x - anchor.dx, y - anchor.dy)
            if origin in seen:
                continue
            seen.add(origin)  # including the misfits: judged once, not per anchor
            if not _fits(template.image, scene, *origin):
                continue
            origins.append(origin)
            kept += 1
    return origins


def _verify(
    template: Template, scene: RegionImage, tolerance: int, max_diff: float
) -> Iterator[RegionMatch]:
    """Candidates that pass both stages, lazily, in candidate order.

    Lazy so the presence question can stop at the first one, and shared so the
    stage-2 budget (MAX_VERIFICATIONS) means the same thing to every caller.
    """
    budget = MAX_VERIFICATIONS
    for x, y in _candidate_origins(template, scene):
        if not _probe_at(template.image, scene, x, y, tolerance):
            continue
        if budget <= 0:
            return
        budget -= 1
        diff = _diff_at_xy(template.image, scene, x, y, tolerance)
        if diff <= max_diff:
            yield RegionMatch(x, y, diff)


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

    "First" is candidate order - each anchor's hits from the bottom up - not
    reading order: callers that only ask *is it there?* (is the stop button on
    screen?) want the early exit, and for a presence question any verified
    occurrence answers it.
    """
    return next(_verify(template, scene, tolerance, max_diff), None)


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
    candidate has been judged - which is why the candidates arrive bottom-most
    first: if a repetitive scene exhausts a budget, what is left is the part of
    the scene this question cares about.
    """
    matches = list(_verify(template, scene, tolerance, max_diff))
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
    matches = list(_verify(template, scene, tolerance, max_diff))
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
