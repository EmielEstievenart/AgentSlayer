"""The 2D anchor search: is this appearance anywhere inside this region?

The whole recognition model rests on this one question, so the cases here are
the ways it can be got wrong: a template that is present but shifted, two
occurrences where only the lowest matters, a scene with no structure for
anchors to grip, quantisation damage taking whole rows out, and a template that
does not fit the scene at all. The last test is a budget rather than a
behaviour: this runs on a poll timer over a full browser window.
"""

from __future__ import annotations

import random
import time

import pytest

from agentclip.screen import template as template_module
from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import (
    ANCHOR_COUNT,
    ANCHOR_LEN,
    DEFAULT_TOLERANCE,
    RegionMatch,
    Template,
    find_all_in_region,
    find_in_region,
    find_lowest_in_region,
    find_lowest_with_best_miss,
    match_at_xy,
    match_rect,
)


def solid(width: int, height: int, colour: tuple[int, int, int]) -> RegionImage:
    """A uniformly coloured BGRX frame (the X byte is left at 0)."""
    blue, green, red = colour
    return RegionImage(width, height, bytes((blue, green, red, 0)) * (width * height))


def stack(*blocks: RegionImage) -> RegionImage:
    """Vertically concatenate same-width frames into one image."""
    width = blocks[0].width
    assert all(block.width == width for block in blocks)
    return RegionImage(
        width,
        sum(block.height for block in blocks),
        b"".join(block.pixels for block in blocks),
    )


def noise(width: int, height: int, seed: int = 1) -> RegionImage:
    """A frame of deterministic pseudo-random pixels.

    The realistic stand-in for a browser: structure in every row (so anchors
    have something to grip) and no self-similarity (so nothing matches by
    accident). A flat block would exercise neither.
    """
    rng = random.Random(seed)
    pixels = bytearray()
    for _ in range(width * height):
        pixels += bytes((rng.randrange(256), rng.randrange(256), rng.randrange(256), 0))
    return RegionImage(width, height, bytes(pixels))


def paste(scene: RegionImage, patch: RegionImage, x: int, y: int) -> RegionImage:
    """``scene`` with ``patch`` stamped in at ``(x, y)``."""
    pixels = bytearray(scene.pixels)
    row = patch.width * 4
    for ty in range(patch.height):
        start = ((y + ty) * scene.width + x) * 4
        pixels[start : start + row] = patch.pixels[ty * row : (ty + 1) * row]
    return RegionImage(scene.width, scene.height, bytes(pixels))


def tiled(patch: RegionImage, across: int, down: int) -> RegionImage:
    """``patch`` repeated edge to edge - a gallery of identical buttons."""
    row = patch.width * 4
    band = b"".join(patch.pixels[y * row : (y + 1) * row] * across for y in range(patch.height))
    return RegionImage(patch.width * across, patch.height * down, band * down)


def needle_banner(scene: RegionImage, template: Template, rows_each: int = 4) -> RegionImage:
    """``scene`` with every anchor's needle tiled across rows at the very top.

    The pathological page: a repetitive banner (a striped header, a table rule)
    whose quantised blue plane happens to carry the exact runs the anchors look
    for, hundreds of times per row, ABOVE the appearance that is really there.
    """
    pixels = bytearray(scene.pixels)
    row = 0
    for anchor in template.anchors:
        for _ in range(rows_each):
            for x in range(scene.width):
                # mid-bucket, so the byte quantises back to the needle's value
                pixels[(row * scene.width + x) * 4] = anchor.needle[x % ANCHOR_LEN] * 32 + 16
            row += 1
    return RegionImage(scene.width, scene.height, bytes(pixels))


def shifted(image: RegionImage, delta: int, rows: range | None = None) -> RegionImage:
    """``image`` with every channel of ``rows`` moved by ``delta`` (clamped)."""
    pixels = bytearray(image.pixels)
    for y in rows if rows is not None else range(image.height):
        for i in range(y * image.width * 4, (y + 1) * image.width * 4):
            if i % 4 != 3:
                pixels[i] = max(0, min(255, pixels[i] + delta))
    return RegionImage(image.width, image.height, bytes(pixels))


def test_finds_the_template_at_a_known_position() -> None:
    patch = noise(20, 16, seed=2)
    scene = paste(noise(140, 90, seed=1), patch, 37, 24)
    assert find_in_region(Template.build(patch), scene) == RegionMatch(37, 24, 0.0)


def test_finds_the_template_flush_with_the_scene_origin() -> None:
    patch = noise(20, 16, seed=2)
    scene = paste(noise(140, 90, seed=1), patch, 0, 0)
    assert find_in_region(Template.build(patch), scene) == RegionMatch(0, 0, 0.0)


def test_finds_the_bottom_most_of_two_stacked_occurrences() -> None:
    """The copy button's question: every response stamps one, the newest is
    the lowest."""
    patch = noise(20, 16, seed=2)
    scene = paste(paste(noise(140, 90, seed=1), patch, 30, 10), patch, 30, 60)
    assert find_lowest_in_region(Template.build(patch), scene) == RegionMatch(30, 60, 0.0)


def test_finds_side_by_side_occurrences() -> None:
    patch = noise(20, 16, seed=2)
    scene = paste(paste(noise(140, 90, seed=1), patch, 5, 40), patch, 90, 40)
    found = find_all_in_region(Template.build(patch), scene)
    assert [(m.x, m.y) for m in found] == [(5, 40), (90, 40)]


def test_find_all_in_region_honours_its_limit() -> None:
    patch = noise(20, 16, seed=2)
    scene = noise(140, 90, seed=1)
    for x in (5, 40, 75, 110):
        scene = paste(scene, patch, x, 40)
    assert len(find_all_in_region(Template.build(patch), scene, limit=2)) == 2


def test_an_absent_template_is_not_found() -> None:
    template = Template.build(noise(20, 16, seed=2))
    assert find_in_region(template, noise(140, 90, seed=1)) is None
    assert find_lowest_in_region(template, noise(140, 90, seed=1)) is None
    assert find_all_in_region(template, noise(140, 90, seed=1)) == []


def test_a_flat_scene_that_every_anchor_hits_stays_bounded() -> None:
    """A blank page puts every anchor's needle at every position. The candidate
    cap plus the cheap probe must turn that into a fast "no", not a hang."""
    patch = solid(24, 20, (100, 0, 0))  # flat: all eight anchors are the same run
    scene = solid(400, 300, (100, 255, 255))  # same blue bucket, different colour
    started = time.monotonic()
    assert find_in_region(Template.build(patch), scene) is None
    assert time.monotonic() - started < 2.0


def test_noise_within_tolerance_still_matches_in_two_dimensions() -> None:
    patch = noise(20, 16, seed=2)
    scene = paste(noise(140, 90, seed=1), shifted(patch, 3), 37, 24)
    found = find_in_region(Template.build(patch), scene)
    assert found is not None and (found.x, found.y, found.diff) == (37, 24, 0.0)


def test_a_difference_beyond_max_diff_is_rejected() -> None:
    """Three of twenty rows repainted: still the same object to a loose
    threshold, not the same object to the default one."""
    patch = noise(20, 20, seed=2)
    damaged = paste(patch, noise(20, 3, seed=9), 0, 16)
    scene = paste(noise(140, 90, seed=1), damaged, 37, 24)
    template = Template.build(patch)
    assert find_in_region(template, scene) is None
    found = find_in_region(template, scene, max_diff=0.2)
    assert found is not None and (found.x, found.y) == (37, 24)
    assert found.diff == pytest.approx(3 / 20)


def test_the_search_ignores_the_undefined_x_byte() -> None:
    patch = noise(20, 16, seed=2)
    opaque = RegionImage(140, 90, bytes(bytearray(paste(noise(140, 90), patch, 10, 10).pixels)))
    pixels = bytearray(opaque.pixels)
    pixels[3::4] = b"\xff" * (opaque.width * opaque.height)
    scene = RegionImage(opaque.width, opaque.height, bytes(pixels))
    assert find_in_region(Template.build(patch), scene) == RegionMatch(10, 10, 0.0)


def test_a_scene_smaller_than_the_template_is_simply_not_a_match() -> None:
    """A resized browser window is a normal runtime condition, not an error."""
    template = Template.build(noise(40, 40, seed=2))
    assert find_in_region(template, noise(20, 90, seed=1)) is None
    assert find_in_region(template, noise(90, 20, seed=1)) is None
    assert find_lowest_in_region(template, noise(20, 20, seed=1)) is None
    assert find_all_in_region(template, noise(20, 20, seed=1)) == []


def test_match_at_xy_scores_a_known_origin() -> None:
    patch = noise(20, 16, seed=2)
    scene = paste(noise(140, 90, seed=1), patch, 37, 24)
    assert match_at_xy(patch, scene, 37, 24) == 0.0
    assert match_at_xy(patch, scene, 0, 0) > 0.5


def test_match_at_xy_rejects_an_origin_that_does_not_fit() -> None:
    patch = noise(20, 16, seed=2)
    scene = noise(40, 40, seed=1)
    with pytest.raises(ValueError):
        match_at_xy(patch, scene, 25, 0)  # 25 + 20 > 40
    with pytest.raises(ValueError):
        match_at_xy(patch, scene, 0, -1)


def test_match_rect_maps_a_scene_local_match_onto_the_screen() -> None:
    template = Template.build(noise(20, 16, seed=2))
    region = ScreenRegion(-300, 40, 500, 400)  # a monitor left of the primary
    rect = match_rect(region, template, RegionMatch(37, 24, 0.0))
    assert rect == ScreenRegion(-263, 64, 20, 16)


def test_build_picks_anchors_on_distinct_rows() -> None:
    """The whole point of eight anchors: quantisation damage travels by row."""
    template = Template.build(noise(40, 40, seed=2))
    assert len(template.anchors) == ANCHOR_COUNT
    assert len({anchor.dy for anchor in template.anchors}) == ANCHOR_COUNT
    assert all(len(anchor.needle) == ANCHOR_LEN for anchor in template.anchors)
    assert (template.width, template.height) == (40, 40)


def test_build_reuses_rows_when_the_template_is_too_short() -> None:
    template = Template.build(noise(40, 3, seed=2))
    assert len(template.anchors) == ANCHOR_COUNT
    assert len({(a.dx, a.dy) for a in template.anchors}) == ANCHOR_COUNT


def test_build_rejects_an_image_it_cannot_anchor() -> None:
    with pytest.raises(ValueError):
        Template.build(RegionImage(0, 0, b""))
    with pytest.raises(ValueError):
        Template.build(RegionImage(20, 20, b"\x00" * 16))  # truncated
    with pytest.raises(ValueError):
        Template.build(noise(ANCHOR_LEN - 1, 20))  # narrower than one anchor


def test_a_shade_crossing_a_quantisation_bucket_edge_is_still_found() -> None:
    """The residual risk of quantised anchors, and the fallback that covers it.

    Every pixel of this template's BLUE channel - the one the anchors read -
    sits exactly on a bucket boundary (96 = the bottom of bucket 3), so a
    three-shade drift on a row moves that row into a different bucket and
    silently destroys any anchor living on it. Spreading the anchors over eight
    rows is what saves the match: the drift here hits the top four rows, and
    the anchors below them still propose the right origin - where the
    per-channel tolerance (3 << 24) sees a perfect match. (The green channel
    steps per row AND per pixel only so that the right origin is the ONLY one
    that verifies - a horizontally flat patch would also verify one column to
    either side of it.)
    """

    def band(row: int) -> RegionImage:
        return RegionImage(
            24, 1, b"".join(bytes((96, (row * 30 + x * 40) % 256, 96, 0)) for x in range(24))
        )

    patch = stack(*[band(row) for row in range(20)])
    drifted = shifted(patch, -3, rows=range(4))
    scene = paste(solid(200, 150, (200, 40, 40)), drifted, 60, 30)
    assert find_in_region(Template.build(patch), scene) == RegionMatch(60, 30, 0.0)


# -- repetitive scenes: what the candidate cap must not cost --------------------


def test_a_repetitive_banner_does_not_hide_the_match_below_it() -> None:
    """The cap is a cost bound, not a search order. Hundreds of anchor hits in
    a banner across the top of the page are all junk - they propose origins the
    scene cannot even hold - and they must not spend the budget that the real
    appearance, lower down the page, needs."""
    patch = noise(40, 40, seed=2)
    template = Template.build(patch)
    scene = needle_banner(paste(noise(1200, 900, seed=1), patch, 1000, 800), template)
    assert match_at_xy(patch, scene, 1000, 800) == 0.0  # it is right there
    assert find_in_region(template, scene) == RegionMatch(1000, 800, 0.0)
    assert find_lowest_in_region(template, scene) == RegionMatch(1000, 800, 0.0)


def test_find_lowest_in_a_scene_tiled_with_the_template() -> None:
    """625 occurrences, one candidate budget: the ones that survive it have to
    be the bottom-most, because the bottom-most is the whole question."""
    patch = noise(40, 40, seed=2)
    scene = tiled(patch, 25, 25)
    found = find_lowest_in_region(Template.build(patch), scene)
    assert found is not None and (found.x, found.y) == (960, 960)


def test_a_tiled_scene_stays_within_a_poll_interval() -> None:
    """Every candidate in a tiled scene is a real match, so the cheap probe
    cannot thin them out - the stage-2 budget is what keeps this affordable."""
    patch = noise(40, 40, seed=2)
    scene = tiled(patch, 25, 25)
    template = Template.build(patch)
    started = time.monotonic()
    find_lowest_in_region(template, scene)
    assert time.monotonic() - started < 1.0


def test_the_cheap_probe_rejects_a_candidate_whose_right_half_is_wrong() -> None:
    """Stage 1 earns its place by rejecting, and it only can if its 16 pixels
    spread over the template: a probe walking the row-major order at a stride
    of the width would sniff column 0 sixteen times and wave this through."""
    icon = noise(24, 16, seed=2)
    half_wrong = paste(icon, noise(12, 16, seed=9), 12, 0)
    assert template_module._probe_at(icon, icon, 0, 0, DEFAULT_TOLERANCE)
    assert not template_module._probe_at(icon, half_wrong, 0, 0, DEFAULT_TOLERANCE)


# -- sampling: the strided comparison must actually see the whole template ------


def test_a_chatbox_sized_template_is_compared_across_every_column() -> None:
    """A chat input box is ~640px across, where a 1024-sample stride works out
    at 40 - a divisor of 640, so the "spread" sample walks 16 of the 640
    columns. A scene that repaints everything between them is not a match."""
    chatbox = solid(640, 64, (10, 10, 10))
    pixels = bytearray(chatbox.pixels)
    for index in range(640 * 64):
        if index % 640 % 8:  # every column the coarsest aliased lattice skips
            pixels[index * 4 : index * 4 + 3] = bytes((110, 110, 110))
    repainted = RegionImage(640, 64, bytes(pixels))
    assert match_at_xy(chatbox, repainted, 0, 0) > 0.5


def test_build_picks_distinct_needles_for_a_two_colour_icon() -> None:
    """Eight anchors are eight independent chances only if they are eight
    different needles. A two-tone glyph's most distinctive window is the same
    alternating run wherever it is read, and eight copies of it search one
    pattern eight times over."""
    pixels = bytearray()
    for y in range(20):
        for x in range(40):
            on = ((x * 3 + y * 5) % 11) < 5
            pixels += bytes((255, 255, 255, 0)) if on else bytes((0, 0, 0, 0))
    icon = Template.build(RegionImage(40, 20, bytes(pixels)))
    assert len({anchor.needle for anchor in icon.anchors}) == ANCHOR_COUNT


def test_a_full_screen_search_is_fast_enough_to_poll() -> None:
    """1080p is the real scene size and this runs on a poll timer."""
    rng = random.Random(7)
    scene = RegionImage(1920, 1080, rng.randbytes(1920 * 1080 * 4))
    patch = noise(40, 40, seed=5)
    scene = paste(scene, patch, 1500, 900)
    template = Template.build(patch)
    started = time.monotonic()
    found = find_in_region(template, scene)
    elapsed = time.monotonic() - started
    assert found == RegionMatch(1500, 900, 0.0)
    assert elapsed < 3.0, f"a full-screen search took {elapsed:.2f}s"


# -- the near-miss report -----------------------------------------------------
# ``find_lowest_with_best_miss`` exists so a failed copy-button search can say
# HOW it failed. "Not found" has two causes that need opposite fixes from the
# user - the button was not on screen, or the capture has stopped looking like
# it - and only a number tells them apart. The harness log prints it (`/log`).


def test_the_best_miss_is_none_when_the_template_is_simply_found() -> None:
    patch = noise(20, 16, seed=2)
    scene = paste(noise(140, 90, seed=1), patch, 37, 24)
    match, best_miss = find_lowest_with_best_miss(Template.build(patch), scene)
    assert match == RegionMatch(37, 24, 0.0)
    assert best_miss is None  # nothing was judged and rejected


def test_a_rejected_candidate_reports_how_close_it_came() -> None:
    """The capture-has-drifted case: the appearance IS there, repainted enough
    to fail the threshold. A bare None would send the user hunting for a button
    that is on their screen."""
    patch = noise(20, 20, seed=2)
    damaged = paste(patch, noise(20, 3, seed=9), 0, 16)  # 3 of 20 rows repainted
    scene = paste(noise(140, 90, seed=1), damaged, 37, 24)
    match, best_miss = find_lowest_with_best_miss(Template.build(patch), scene)
    assert match is None
    assert best_miss == pytest.approx(3 / 20)
    # ...and a threshold that accepts it gets the match instead.
    match, _ = find_lowest_with_best_miss(Template.build(patch), scene, max_diff=0.2)
    assert match is not None and (match.x, match.y) == (37, 24)


def test_an_empty_region_reports_no_candidate_at_all() -> None:
    """The other cause: nothing in the scene is even shaped like the template,
    so there is no number to report and the log says so in words instead."""
    template = Template.build(noise(20, 16, seed=2))
    assert find_lowest_with_best_miss(template, noise(140, 90, seed=1)) == (None, None)


def test_the_plain_search_is_the_same_search() -> None:
    """``find_lowest_in_region`` is now the one-line view of it, so the two can
    never disagree about what a match is."""
    patch = noise(20, 16, seed=2)
    scene = paste(paste(noise(140, 90, seed=1), patch, 30, 10), patch, 30, 60)
    template = Template.build(patch)
    assert find_lowest_with_best_miss(template, scene)[0] == find_lowest_in_region(template, scene)


def test_the_lowest_match_and_a_near_miss_come_back_together() -> None:
    """A clean occurrence above a damaged one: the search still answers with the
    clean match, and still says how close the damaged one came."""
    patch = noise(20, 20, seed=2)
    damaged = paste(patch, noise(20, 3, seed=9), 0, 16)
    scene = paste(paste(noise(140, 200, seed=1), patch, 30, 20), damaged, 30, 120)
    match, best_miss = find_lowest_with_best_miss(Template.build(patch), scene)
    assert match is not None and (match.x, match.y) == (30, 20)
    assert best_miss == pytest.approx(3 / 20)
