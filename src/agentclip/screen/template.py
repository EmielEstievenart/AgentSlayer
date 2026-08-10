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
at all - 256 shades collapse to 8 buckets, so the same button captured twice is
the same string. Its residual risk is a shade drifting ACROSS a bucket edge,
which rewrites the byte however small the drift was, and eight anchors on eight
different rows are the first half of the answer: such a drift tends to move a
whole row of the template at once, so the anchors sharing no row with the
damage still find it.

The second half is there because that fallback has a blind spot, and the blind
spot is not hypothetical. Gemini's dark sidebar is #1f1f1f - blue 31, one unit
below the edge at 32 - and a crop of a menu item on it is four fifths flat
background. Every anchor read from such a crop carries background bytes, so one
unit of surface tint (a hover, a Material elevation, a theme tweak) moves all
eight at once: row redundancy buys nothing when the damaged shade is on every
row. The button became unfindable while the comparison at its own origin still
called it perfect, and no tolerance could rescue it, because the origin was
never proposed.

So the anchors are measured against TWO RULERS. The plain one steps at 32, 64,
96; the offset one, ``(v + 16) >> 5``, steps at 16, 48, 80 - the same 32-wide
buckets, half a bucket along. Their edges are 16 apart, so a shade shifting by
up to 16 crosses at most one of the two, and the ruler it did not cross reads
exactly the byte it read at capture. Each ruler chooses and stores its own
eight anchors (how distinctive a window is depends on the bucketing, so the
choice cannot be shared), each is searched against its own plane of the scene,
and the candidates are the union - sixteen chances at the origin instead of
eight, whose failure modes no longer coincide. A shift of 17 or more can still
land on an edge of both; from 25 the per-channel tolerance below rejects the
pixels anyway, so what is left uncovered is the narrow 17-24 band rather than
the one-unit cliff that started this.

Pixel comparison is the house style from screen.busy: strided sampling with a
fixed budget, per-channel tolerance on B/G/R, the undefined X byte skipped.
Deliberately a pure function of its inputs - no capture, no ctypes, no OS - so
it is unit-testable anywhere.

Uniform sampling has its own blind spot, and it is the mirror image of the
anchors'. Copilot's chat input box is a 1030x97 crop of which 97% is one flat
dark surface; everything that makes it THAT box - the hairline border, the
placeholder text, two icons - is the remaining 3%. A uniform stride gives
those pixels 3% of the votes, so a comparison against any similar dark
rectangle elsewhere on the page disagrees on almost nothing it sampled: the
impostor scores ~0.06 against the chatbox's 0.20 threshold, and a search that
accepts the first verified candidate hands back whichever dark patch its
candidate order visited first. No threshold can fix that - tightening it below
the impostor's score would reject the real box the moment its placeholder text
moves. The fix is representation, not strictness: Template.build records which
pixels DEVIATE from the template's dominant shade (the border, the glyphs -
whatever survives SALIENT_DELTA), and stage 2 samples those alongside the
uniform sweep with equal budget. At the real box the salient pixels agree
almost perfectly; on a flat impostor they disagree almost totally, which
drags its pooled diff to ~0.5 and far past any threshold in use.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
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
# A pixel is salient when any channel sits further than this from the
# template's dominant shade. One quantisation bucket: closer than that and the
# pixel is the background with noise on it, which is exactly what the salient
# set exists to exclude. Measured against the real profiles this keeps the
# hairline border, the placeholder text and the icons of a chat input box
# (~3% of its area) and nothing of the flat surface.
SALIENT_DELTA = 32
# Fewer salient pixels than this and the set is noise, not structure - a
# near-flat template (a solid send button) simply has no distinguishing pixels
# to insist on, and insisting on a handful would let single-pixel flicker veto
# a match. Such a template skips the salient sampling entirely and behaves as
# it always did.
SALIENT_MIN = 16


# Quantisation table for bytes.translate: 256 shades collapse to 8 buckets, so
# an EXACT byte search tolerates the anti-aliasing and theme dithering that
# would otherwise make every capture of the same button a different string.
QUANT = bytes(value >> 5 for value in range(256))
# The same ruler, its edges moved half a bucket along: 16, 48, 80 instead of
# 32, 64, 96. A shade cannot be within 16 of an edge on both rulers, which is
# the whole point (see module docstring). 240-255 land in a ninth bucket, and
# that is fine - it is a byte value like any other, distinct from the eight
# below it, and both sides of every comparison are read through this same
# table.
QUANT_OFFSET = bytes((value + 16) >> 5 for value in range(256))
# How far a shade can move before it is guaranteed to have crossed an edge on
# one of the two rulers. Documentation for the reader rather than a knob: it is
# the spacing of the two rulers' interleaved edges, so changing it means
# changing QUANT_OFFSET.
RULER_STAGGER = 16
# Anchor geometry. Eight bytes is long enough that a random 8-bucket run occurs
# roughly once per 16M pixels (i.e. essentially never in a 1080p scene) and
# short enough to survive a couple of pixels of sub-pixel shift at its edges.
ANCHOR_LEN = 8
# Eight anchors on eight different rows, PER RULER: the two fallbacks that keep
# a match findable when quantisation damage takes some rows out, or when it
# takes the background shade every row shares (see module docstring).
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
# Ceiling on stage-2 comparisons in one search, counted across BOTH rulers -
# which is what keeps the second ruler from costing a second search's worth of
# the expensive stage. The per-anchor cap already bounds the candidate list,
# but a scene tiled with the template (a gallery of identical copy buttons)
# makes every one of those candidates a REAL match that the cheap probe rightly
# waves through, and 4096 full comparisons is a second of a poll interval.
# Candidates arrive bottom-most first, so the budget is spent where the answer
# to both questions this module is asked - is it there? where is the newest
# one? - actually lives.
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


# The seam between the two halves of a search. A CandidateSource answers only
# "which origins are worth comparing?"; everything after it - the sniff test,
# the strided comparison, the per-channel tolerance, the diff on the
# RegionMatch, the MAX_VERIFICATIONS budget - is shared by every source there
# is. That division is what lets a user pick a different way of FINDING things
# without changing what a match means: RegionMatch.diff is computed by the same
# code either way, so one tolerance setting governs both and two backends can
# be compared against each other honestly.
#
# The alternative source (an OpenCV correlation sweep) lives in
# screen.matchers, not here: this module is stdlib-only and must stay
# importable on a machine with no cv2, so the dependency direction runs
# matchers -> template and never back.
CandidateSource = Callable[["Template", RegionImage], list[tuple[int, int]]]


def _quantised_plane(image: RegionImage, ruler: bytes) -> bytes:
    """The blue channel of every pixel, bucketed by ``ruler``, row-major.

    One byte per pixel, so a flat index into it IS ``y * width + x`` - which is
    what makes ``bytes.find`` usable as a 2D search. ``ruler`` is QUANT or
    QUANT_OFFSET; a template's anchors and the scene they are hunted in must be
    read through the same one, or the bytes mean different things.
    """
    return image.pixels[0 : image.width * image.height * 4 : 4].translate(ruler)


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


def _salient_indices(image: RegionImage) -> tuple[int, ...]:
    """Row-major indices of the pixels that distinguish this template from a
    flat rectangle of its own dominant shade (see the module docstring).

    The dominant shade is the per-channel mode - not the mean, which a bright
    logo on a dark surface would drag off both of them. A pixel is kept when
    ANY channel strays beyond SALIENT_DELTA, via one translate per channel and
    a big-int OR so the scan over 100k pixels of a chat-box-sized template
    stays in C. Capped by an even stride at MAX_SAMPLES so a busy template
    (where most pixels deviate) costs no more than the uniform sweep it
    supplements; empty below SALIENT_MIN, which callers read as "nothing to
    insist on".
    """
    total = image.width * image.height
    marks = 0
    for channel in range(3):
        plane = image.pixels[channel : total * 4 : 4]
        mode = Counter(plane).most_common(1)[0][0]
        table = bytes(1 if abs(value - mode) > SALIENT_DELTA else 0 for value in range(256))
        marks |= int.from_bytes(plane.translate(table), "big")
    flags = marks.to_bytes(total, "big")
    salient = [index for index, flag in enumerate(flags) if flag]
    if len(salient) < SALIENT_MIN:
        return ()
    return tuple(salient[index] for index in _spread(len(salient), MAX_SAMPLES))


def _choose_anchors(plane: bytes, width: int, height: int) -> tuple[Anchor, ...]:
    """The ANCHOR_COUNT most distinctive windows of one quantised plane.

    Run once per ruler, on that ruler's plane, because "distinctive" is a fact
    about a bucketing and not about the image: a pair of shades straddling one
    ruler's edge is two buckets to it and one bucket to the other, so the two
    rulers genuinely disagree about where the interesting windows are. Sharing
    a choice between them would hand the offset ruler positions that carry no
    contrast on its own plane.
    """
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
                return tuple(anchors)
    return tuple(anchors)


@dataclass(frozen=True, slots=True)
class Template:
    """An appearance to look for, plus the anchors that make looking cheap.

    Built once (at capture, or when a profile is loaded from disk) and reused
    by every later search - anchor selection is the expensive part and it does
    not depend on the scene.

    Two sets of anchors, one per ruler (see the module docstring). ``anchors``
    is the plain ``v >> 5`` set and ``offset_anchors`` the ``(v + 16) >> 5``
    one; neither is a fallback for the other, they are simply both searched.
    ``offset_anchors`` defaults to empty so a Template can still be assembled
    by hand from one ruler's worth of anchors, which is what a test that wants
    to watch a single ruler fail needs.

    ``salient`` is the third precomputed fact: the row-major indices of the
    pixels that distinguish this appearance from a flat rectangle of its own
    background (see _salient_indices). Like the anchors it depends only on the
    template, never the scene, so it is paid for once. Defaults to empty -
    a hand-built Template verifies exactly as it did before the salient stage
    existed.
    """

    image: RegionImage
    anchors: tuple[Anchor, ...]
    offset_anchors: tuple[Anchor, ...] = ()
    salient: tuple[int, ...] = ()

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    @property
    def rulers(self) -> tuple[tuple[bytes, tuple[Anchor, ...]], ...]:
        """Each quantisation table paired with the anchors read through it.

        The one place the pairing lives, because getting it wrong is silent:
        hunting an offset-ruler needle in a plain-ruler plane finds nothing and
        looks exactly like the appearance not being on screen.
        """
        return ((QUANT, self.anchors), (QUANT_OFFSET, self.offset_anchors))

    @classmethod
    def build(cls, image: RegionImage) -> Template:
        """Choose this image's anchors, on both rulers. Raises ValueError if it
        cannot be one: no area, a truncated buffer, or narrower than a single
        anchor.

        Nothing about this is stored in a profile - a saved appearance is a PNG
        and the anchors are rebuilt from it on load - so a template captured
        before the second ruler existed gets it for free.
        """
        width, height = image.width, image.height
        if width <= 0 or height <= 0:
            raise ValueError("template has no area")
        if len(image.pixels) < width * height * 4:
            raise ValueError("template pixel buffer is truncated")
        if width < ANCHOR_LEN:
            raise ValueError(f"template is narrower than an anchor ({width} < {ANCHOR_LEN}px)")

        return cls(
            image,
            _choose_anchors(_quantised_plane(image, QUANT), width, height),
            _choose_anchors(_quantised_plane(image, QUANT_OFFSET), width, height),
            _salient_indices(image),
        )


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


def _diff_at_xy(
    template: RegionImage,
    scene: RegionImage,
    x: int,
    y: int,
    tolerance: int,
    salient: tuple[int, ...] = (),
) -> float:
    """Stage 2: the strided full comparison of ``_diff_at``, at a 2D origin.

    Same sampling discipline (at most MAX_SAMPLES pixels, uniform stride over
    the template's row-major order), so diffs stay comparable between offsets -
    including the stride's coprimality with the width, which for a template as
    wide as a chat input box is the difference between reading every column and
    reading sixteen of them (busy.sample_step).

    ``salient`` indices are sampled on top of the uniform stride, into the same
    pooled fraction. Equal budgets, so a template that is nearly all flat
    surface still casts half its votes on the pixels that make it recognisable
    (see the module docstring) - and a template with an empty salient set gets
    exactly the historical uniform diff. Some pixels are counted by both walks;
    that is the weighting, not an accident.
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
    for index in salient:
        ty, tx = divmod(index, template.width)
        here = index * 4
        there = ((y + ty) * scene.width + (x + tx)) * 4
        sampled += 1
        if (
            abs(left[here] - right[there]) > tolerance
            or abs(left[here + 1] - right[there + 1]) > tolerance
            or abs(left[here + 2] - right[there + 2]) > tolerance
        ):
            differing += 1
    return differing / sampled


def _candidate_origins(template: Template, scene: RegionImage) -> list[tuple[int, int]]:
    """Every template origin either ruler's anchors vouch for, bottom-most first.

    The union of the two searches, deduplicated: on an undisturbed scene both
    rulers propose the same origin and it is judged once, and when a shade has
    drifted across one ruler's bucket edge the other one is still proposing it.
    The plain ruler goes first so the candidate order an undamaged search sees
    is exactly the order it saw when there was only one ruler.

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

    width = scene.width
    origins: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()  # shared by both rulers: judged once
    for ruler, anchors in template.rulers:
        if not anchors:
            continue  # a hand-built template that carries only one ruler
        plane = _quantised_plane(scene, ruler)  # once per ruler, not per anchor
        for anchor in anchors:
            # Outside this band the anchor's own offset already puts the
            # template off the edge of the scene, so a hit there cannot become
            # a candidate however the rest of it looks - skip the two dead
            # bands wholesale rather than examining a flat scene's worth of
            # hits inside them.
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
                # One short of this hit, so overlapping runs are still found
                # and the window always shrinks (no way to sit on the same hit
                # twice).
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


def _scored(
    template: Template,
    scene: RegionImage,
    tolerance: int,
    matcher: CandidateSource | None = None,
) -> Iterator[RegionMatch]:
    """Every candidate that survived stage 1, with the diff stage 2 gave it.

    Judged but NOT filtered, which is the difference between this and
    :func:`_verify`. A caller that only wants matches takes the filter; a caller
    that has to explain a MISS needs the numbers behind it, because "not found"
    and "the closest thing on screen was 0.21 against a 0.08 threshold" send the
    user to two entirely different fixes (the button is not there / the capture
    no longer looks like it).

    Lazy so the presence question can still stop at the first hit, and the
    stage-2 budget (MAX_VERIFICATIONS) is spent here so it means the same thing
    to every caller.
    """
    budget = MAX_VERIFICATIONS
    propose = _candidate_origins if matcher is None else matcher
    for x, y in propose(template, scene):
        if not _probe_at(template.image, scene, x, y, tolerance):
            continue
        if budget <= 0:
            return
        budget -= 1
        yield RegionMatch(
            x, y, _diff_at_xy(template.image, scene, x, y, tolerance, template.salient)
        )


def _verify(
    template: Template,
    scene: RegionImage,
    tolerance: int,
    max_diff: float,
    matcher: CandidateSource | None = None,
) -> Iterator[RegionMatch]:
    """Candidates that pass both stages, lazily, in candidate order."""
    return (
        match
        for match in _scored(template, scene, tolerance, matcher)
        if match.diff <= max_diff
    )


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
    matcher: CandidateSource | None = None,
) -> RegionMatch | None:
    """The first place the template matches, or None. Never raises.

    "First" is candidate order - hits from the bottom up - not reading order:
    callers that only ask *is it there?* (is the stop button on screen?) want
    the early exit, and for a presence question any verified occurrence answers
    it.

    ``matcher`` chooses how candidate origins are proposed (see
    :data:`CandidateSource` and screen.matchers); None is the built-in anchor
    search. It changes what gets COMPARED, never what a comparison decides.
    """
    return next(_verify(template, scene, tolerance, max_diff, matcher), None)


def find_lowest_in_region(
    template: Template,
    scene: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
    matcher: CandidateSource | None = None,
) -> RegionMatch | None:
    """The bottom-most match (largest y), or None. Never raises.

    The copy button's question: every response stamps one, and the newest is
    the lowest. No early exit is possible - the answer is only known once every
    candidate has been judged - which is why the candidates arrive bottom-most
    first: if a repetitive scene exhausts a budget, what is left is the part of
    the scene this question cares about.
    """
    return find_lowest_with_best_miss(
        template, scene, tolerance=tolerance, max_diff=max_diff, matcher=matcher
    )[0]


def find_lowest_with_best_miss(
    template: Template,
    scene: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
    matcher: CandidateSource | None = None,
) -> tuple[RegionMatch | None, float | None]:
    """:func:`find_lowest_in_region`, plus how close the search came to a hit.

    ``(match, best_miss)``. ``best_miss`` is the smallest diff among the
    candidates that were judged and REJECTED - None when nothing got as far as
    being judged, which is the honest answer for "the scene contains nothing
    even shaped like this" and a materially different report from "the closest
    thing was almost it".

    It exists for the harness log, and specifically for the one question a
    failed auto-copy always raises: was the copy button not on screen, or has
    the capture stopped matching it? A bare None cannot tell those apart and a
    number can. The diff is reported alongside a match too (a match's own diff
    is on the ``RegionMatch``), so a caller never has to ask twice.

    Same traversal and same budget as the plain search - this is where it is
    implemented and ``find_lowest_in_region`` is the one-line view of it - so
    the extra answer costs nothing.
    """
    matches: list[RegionMatch] = []
    best_miss: float | None = None
    for candidate in _scored(template, scene, tolerance, matcher):
        if candidate.diff <= max_diff:
            matches.append(candidate)
        elif best_miss is None or candidate.diff < best_miss:
            best_miss = candidate.diff
    if not matches:
        return None, best_miss
    return max(matches, key=lambda match: (match.y, match.x)), best_miss


def find_all_in_region(
    template: Template,
    scene: RegionImage,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
    limit: int = 64,
    matcher: CandidateSource | None = None,
) -> list[RegionMatch]:
    """Every match, top-to-bottom then left-to-right, at most ``limit``. Never raises."""
    matches = list(_verify(template, scene, tolerance, max_diff, matcher))
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


def same_element(one: ScreenRegion, other: ScreenRegion) -> bool:
    """Are these two matches the same physical thing on screen?

    A template routinely matches its own element at several neighbouring
    origins - a pixel or two of drift is well inside the diff threshold - so a
    raw match count says more about anti-aliasing than about how many buttons
    are on screen. Overlapping rectangles are one element: a genuine second
    copy of a button cannot be drawn on top of the first.

    Per axis, not as one radius, because a template can be very lopsided. A
    chat input box is ~800x90, and "within max(width, height)" would fold two
    input boxes 400px apart - the two windows this whole check exists to tell
    apart - into one.

    Lives here, beside ``match_rect``, because both callers of it are asking the
    same question about the same rectangles: the automation folding a kind's
    matches down to one click target (``MainScreen._find_all``) and the
    /identify overlay folding them down to one drawn box (screen.identify).
    """
    return abs(one.left - other.left) < one.width and abs(one.top - other.top) < one.height
