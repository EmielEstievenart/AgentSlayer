"""A real capture of Copilot's chat input box, and the impostor problem the
salient sample set exists to solve.

The fixture is a genuine 1428x762 frame of an M365 Copilot window
(``copilot-frame.png``); the chat box is cut out of it at (368, 469) in each
test, so the search starts from pixels that cannot disagree for any reason
except the one being demonstrated.

What it demonstrates is the mirror image of the flat-background cliff
(test_template_flat_background). There the flat surface poisoned the ANCHORS
and the origin was never proposed; here the origins arrive fine and the flat
surface poisons the COMPARISON instead. The chat box is 97% one dark surface -
everything that makes it a chat box (a hairline border, "Message Copilot", two
icons) is the remaining 3% - and a uniform sample gives those pixels 3% of the
votes. So any similar dark rectangle on the page used to verify at ~0.06
against the chatbox kinds' 0.20 threshold, and a first-match search handed
back whichever dark patch its bottom-up candidate order visited first: the
avatar strip at the bottom of the sidebar, on this very frame.

Template.build now records the pixels that deviate from the template's
dominant shade, and stage 2 samples them with the same budget as the uniform
sweep. The tests below pin the recovery (the only thing found on this whole
frame is the chat box itself), the mechanism (what the salient set contains),
and the counterfactual (a template stripped of it still falls for the
impostor - so a regression here cannot pass silently).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from agentclip.screen.capture import RegionImage
from agentclip.screen.matchers import opencv_available, select_matcher
from agentclip.screen.png import decode_png
from agentclip.screen.template import (
    DEFAULT_TOLERANCE,
    MAX_SAMPLES,
    SALIENT_MIN,
    RegionMatch,
    Template,
    find_all_in_region,
    find_in_region,
    match_at_xy,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

needs_cv2 = pytest.mark.skipif(not opencv_available(), reason="the cv extra is not installed")

# Where the chat box sits in the frame. A fact about the fixture, asserted by
# the control test rather than trusted.
CHATBOX_AT = (368, 469)
CHATBOX_SIZE = (1030, 97)
# What screen.profile gives the chatbox kinds - the loosest threshold in the
# profile, which is exactly why flat impostors used to clear it. Spelled out
# so a change to the kind cannot quietly turn these tests into another claim.
CHATBOX_MAX_DIFF = 0.20
# A spot the OpenCV sweep used to hand back instead of the chat box: a flat
# dark stretch of the sidebar's avatar strip, near the bottom of the frame.
IMPOSTOR_AT = (39, 665)


@cache
def frame() -> RegionImage:
    """Decoded once for the whole module - decode_png walks a megapixel of
    scanlines in Python, and nothing here mutates what it returns."""
    return decode_png((FIXTURES / "copilot-frame.png").read_bytes())


def cut(scene: RegionImage, x: int, y: int, width: int, height: int) -> RegionImage:
    """``scene``'s pixels at that rectangle - a byte-identical crop, so every
    claim below is measured against a zero."""
    pixels = bytearray()
    for row in range(height):
        start = ((y + row) * scene.width + x) * 4
        pixels += scene.pixels[start : start + width * 4]
    return RegionImage(width, height, bytes(pixels))


def chatbox() -> RegionImage:
    return cut(frame(), *CHATBOX_AT, *CHATBOX_SIZE)


def stripped(template: Template) -> Template:
    """``template`` without its salient set - what verification used to see."""
    return Template(template.image, template.anchors, template.offset_anchors)


def solid(width: int, height: int, colour: tuple[int, int, int]) -> RegionImage:
    blue, green, red = colour
    return RegionImage(width, height, bytes((blue, green, red, 0)) * (width * height))


def bordered(width: int, height: int, surface: tuple[int, int, int], line: tuple[int, int, int]) -> RegionImage:
    """A flat box with a one-pixel border: the least appearance that still has
    a shape, and a miniature of what a chat input box is."""
    pixels = bytearray(solid(width, height, surface).pixels)
    edge = bytes((*line, 0))
    for x in range(width):
        pixels[x * 4 : x * 4 + 4] = edge
        at = ((height - 1) * width + x) * 4
        pixels[at : at + 4] = edge
    for y in range(height):
        pixels[y * width * 4 : y * width * 4 + 4] = edge
        at = (y * width + width - 1) * 4
        pixels[at : at + 4] = edge
    return RegionImage(width, height, bytes(pixels))


def paste(scene: RegionImage, patch: RegionImage, x: int, y: int) -> RegionImage:
    pixels = bytearray(scene.pixels)
    row = patch.width * 4
    for ty in range(patch.height):
        start = ((y + ty) * scene.width + x) * 4
        pixels[start : start + row] = patch.pixels[ty * row : (ty + 1) * row]
    return RegionImage(scene.width, scene.height, bytes(pixels))


# -- the control, and the recovery ---------------------------------------------


def test_the_chatbox_is_found_exactly_where_it_was_cut_from() -> None:
    """The whole pipeline on real pixels, at the kind's own threshold."""
    scene = frame()
    assert (scene.width, scene.height) == (1428, 762)
    template = Template.build(chatbox())
    match = find_in_region(template, scene, max_diff=CHATBOX_MAX_DIFF)
    assert match == RegionMatch(*CHATBOX_AT, 0.0)


@needs_cv2
def test_the_opencv_sweep_agrees_on_the_frame_it_used_to_get_wrong() -> None:
    """The regression as the user hit it: the sweep's candidates arrive bottom
    up, its first flat patch used to verify, and the chat box - present in the
    very same candidate list - was never reached."""
    template = Template.build(chatbox())
    match = find_in_region(
        template,
        frame(),
        max_diff=CHATBOX_MAX_DIFF,
        matcher=select_matcher("opencv").origins,
    )
    assert match == RegionMatch(*CHATBOX_AT, 0.0)


def test_nothing_else_on_the_whole_frame_is_the_chatbox() -> None:
    """The frame is full of dark rectangles - a sidebar, an avatar strip, the
    page itself - and every one of them used to verify. Now every match
    overlaps the one real chat box."""
    template = Template.build(chatbox())
    matches = find_all_in_region(template, frame(), max_diff=CHATBOX_MAX_DIFF)
    assert matches, "the control above found it, so this cannot be empty"
    x, y = CHATBOX_AT
    width, height = CHATBOX_SIZE
    for match in matches:
        assert abs(match.x - x) < width and abs(match.y - y) < height


def test_a_flat_impostor_passes_the_uniform_comparison_alone() -> None:
    """Why no threshold could fix this: measured the old way - a uniform
    sample, which is what match_at_xy still is - the flat patch really does
    look like the chat box. The salient votes are what tells them apart, not
    a stricter number."""
    diff = match_at_xy(chatbox(), frame(), *IMPOSTOR_AT, tolerance=DEFAULT_TOLERANCE)
    assert diff < CHATBOX_MAX_DIFF


@needs_cv2
def test_a_template_without_its_salient_set_still_falls_for_the_impostor() -> None:
    """The counterfactual that keeps the salient stage load-bearing: strip it
    and the old wrong answer comes straight back, far from the chat box."""
    template = stripped(Template.build(chatbox()))
    match = find_in_region(
        template,
        frame(),
        max_diff=CHATBOX_MAX_DIFF,
        matcher=select_matcher("opencv").origins,
    )
    assert match is not None
    assert abs(match.y - CHATBOX_AT[1]) >= CHATBOX_SIZE[1]


# -- the mechanism, in miniature -----------------------------------------------


SURFACE = (36, 36, 36)  # the chat box's real dominant shade
LINE = (120, 120, 120)


def test_the_salient_set_is_the_border_and_none_of_the_surface() -> None:
    patch = bordered(40, 20, SURFACE, LINE)
    expected = [
        index
        for index in range(40 * 20)
        if index // 40 in (0, 19) or index % 40 in (0, 39)
    ]
    assert list(Template.build(patch).salient) == expected


def test_a_box_on_a_page_of_its_own_surface_is_found_once_and_exactly() -> None:
    """The fixture case in miniature: a bordered box on a page of nothing but
    its own background. The border is 11% of the box - under the chatbox
    threshold, so every flat offset used to verify - and the salient votes now
    reject all of them without rejecting the box."""
    patch = bordered(60, 24, SURFACE, LINE)
    scene = paste(solid(200, 100, SURFACE), patch, 30, 40)
    template = Template.build(patch)
    assert find_in_region(template, scene, max_diff=CHATBOX_MAX_DIFF) == RegionMatch(30, 40, 0.0)
    matches = find_all_in_region(template, scene, max_diff=CHATBOX_MAX_DIFF)
    for match in matches:
        assert abs(match.x - 30) < 60 and abs(match.y - 40) < 24
    # And the uniform comparison alone would have taken the flat page.
    assert match_at_xy(patch, scene, 100, 10) < CHATBOX_MAX_DIFF


def test_a_near_flat_template_has_nothing_to_insist_on() -> None:
    """Below SALIENT_MIN the set is noise, not structure: a solid template
    keeps its historical behaviour instead of letting a handful of pixels veto
    every match."""
    patch = solid(40, 20, SURFACE)
    template = Template.build(patch)
    assert template.salient == ()
    scene = paste(solid(200, 100, (200, 200, 200)), patch, 30, 40)
    found = find_in_region(template, scene, max_diff=CHATBOX_MAX_DIFF)
    # A flat template matches itself at any overlapping origin - the point is
    # that it still matches at all, not where exactly.
    assert found is not None and abs(found.x - 30) < 40 and abs(found.y - 40) < 20
    assert SALIENT_MIN > 1  # the guard exists, whatever its exact value


def test_the_salient_set_is_capped_like_the_uniform_one() -> None:
    """A busy template (noise deviates almost everywhere) must not turn the
    salient walk into a second full sweep - the poll budget is the point."""
    import random

    rng = random.Random(7)
    pixels = bytes(
        byte
        for _ in range(64 * 32)
        for byte in (rng.randrange(256), rng.randrange(256), rng.randrange(256), 0)
    )
    template = Template.build(RegionImage(64, 32, pixels))
    assert 0 < len(template.salient) <= MAX_SAMPLES
