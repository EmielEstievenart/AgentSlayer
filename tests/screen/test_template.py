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

from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import (
    ANCHOR_COUNT,
    ANCHOR_LEN,
    RegionMatch,
    Template,
    find_all_in_region,
    find_in_region,
    find_lowest_in_region,
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
    steps per row only so that the right origin is the ONLY one that verifies.)
    """
    patch = stack(*[solid(24, 1, (96, (row * 30) % 256, 96)) for row in range(20)])
    drifted = shifted(patch, -3, rows=range(4))
    scene = paste(solid(200, 150, (200, 40, 40)), drifted, 60, 30)
    assert find_in_region(Template.build(patch), scene) == RegionMatch(60, 30, 0.0)


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
