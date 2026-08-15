"""A real capture of Gemini's "Nieuw gesprek" button, and the cliff the second
quantisation ruler exists to walk it back from.

The fixtures are a genuine 1411x740 GDI frame of a Gemini window
(``gemini-sidebar-frame.png``) and the new-chat button cut out of it
(``gemini-new-chat-crop.png``). The crop is byte-identical to the frame's
pixels at (20, 68), so every test here starts from a search that cannot fail
for any reason except the one being demonstrated.

What it demonstrates is the case ``test_a_shade_crossing_a_quantisation_bucket_edge_is_still_found``
(test_template) does not reach. That one drifts four ROWS of a template and the
anchors on the other rows still find it: the fallback works because the damage
travels by row. Here the shade that drifts is the BACKGROUND, which is on every
row - 82.8% of this crop is the flat sidebar surface, so every anchor read from
it carries background bytes and eight rows of redundancy buy nothing. Gemini's
dark sidebar is #1f1f1f, blue 31, exactly one unit below the ``v >> 5`` edge at
32, so a single unit of surface tint used to move the whole template's plane
and take the button off the map - while the pixel comparison at its own origin
still called it perfect, and no tolerance could say so, because the origin was
never proposed.

The second ruler, ``(v + 16) >> 5``, buckets at 16/48/80 instead of 32/64/96.
Blue 31 and blue 40 are the same byte to it, so it keeps proposing the origin
that the plain ruler has lost, and the comparison - which was always willing -
finally gets to answer. The tests below pin the recovery, the boundary of the
guarantee (a background shift of up to RULER_STAGGER), and the narrow band
above it that is still uncovered.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from agentclip.driver.screen import template as template_module
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.png import decode_png
from agentclip.driver.screen.template import (
    DEFAULT_TOLERANCE,
    QUANT,
    QUANT_OFFSET,
    RULER_STAGGER,
    RegionMatch,
    Template,
    find_in_region,
    find_lowest_with_best_miss,
    match_at_xy,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Where the crop sits in the frame, and the surface it is drawn on. Both are
# facts about the fixtures, asserted below rather than trusted.
BUTTON_AT = (20, 68)
SIDEBAR = (31, 31, 31)
# What screen.profile gives TemplateKind.NEW_CHAT. Spelled out so a change to
# the kind's threshold cannot quietly turn these tests into a different claim.
NEW_CHAT_MAX_DIFF = 0.10


@cache
def load(name: str) -> RegionImage:
    """Decoded once for the whole module - decode_png walks a megapixel of
    scanlines in Python, and nothing here mutates what it returns."""
    return decode_png((FIXTURES / name).read_bytes())


def frame() -> RegionImage:
    return load("gemini-sidebar-frame.png")


def crop() -> RegionImage:
    return load("gemini-new-chat-crop.png")


def retinted(image: RegionImage, delta: int) -> RegionImage:
    """``image`` with every pixel of the flat sidebar surface moved by ``delta``.

    A blunter instrument than a real hover - a browser would re-blend the text's
    anti-aliasing against the new surface too - and deliberately so: what
    survives this is a statement about the background shade alone, which is
    what puts every anchor of this crop in one basket.
    """
    shade = SIDEBAR[0] + delta
    assert 0 <= shade <= 255
    pixels = bytearray(image.pixels)
    for index in range(image.width * image.height):
        at = index * 4
        if tuple(pixels[at : at + 3]) == SIDEBAR:
            pixels[at : at + 3] = bytes((shade, shade, shade))
    return RegionImage(image.width, image.height, bytes(pixels))


def background_share(image: RegionImage, colour: tuple[int, int, int]) -> float:
    count = sum(
        1
        for index in range(image.width * image.height)
        if tuple(image.pixels[index * 4 : index * 4 + 3]) == colour
    )
    return count / (image.width * image.height)


def one_ruler(template: Template, ruler: bytes) -> Template:
    """``template`` with only one ruler's anchors - what the search used to be."""
    if ruler is QUANT:
        return Template(template.image, template.anchors)
    return Template(template.image, (), template.offset_anchors)


# -- the control: nothing is wrong with the capture, the codec or the search ---


def test_the_new_chat_button_is_found_on_the_frame_it_was_cut_from() -> None:
    """The whole pipeline on real pixels: decode, build, search, at the kind's
    own threshold. A perfect 0.0 - the crop IS the frame's pixels there."""
    scene, patch = frame(), crop()
    assert (scene.width, scene.height) == (1411, 740)
    assert (patch.width, patch.height) == (171, 34)
    match = find_in_region(Template.build(patch), scene, max_diff=NEW_CHAT_MAX_DIFF)
    assert match == RegionMatch(*BUTTON_AT, 0.0)


def test_the_crop_is_byte_identical_to_the_frame_beneath_it() -> None:
    """Guard against checkout/tooling re-encoding the fixtures: every claim
    below is measured against a zero, and a lossy round trip would move it."""
    scene, patch = frame(), crop()
    x, y = BUTTON_AT
    for row in range(patch.height):
        start = ((y + row) * scene.width + x) * 4
        assert scene.pixels[start : start + patch.width * 4] == (
            patch.pixels[row * patch.width * 4 : (row + 1) * patch.width * 4]
        )


# -- why one ruler was one point of failure ------------------------------------


def test_a_glyph_on_a_flat_surface_puts_the_background_in_every_anchor() -> None:
    """The structural reason row redundancy cannot help here, and it is
    unchanged by the fix: four fifths of the crop is one flat colour, so every
    needle - on either ruler, wherever it is read - runs through it. What the
    second ruler changes is not this, but whether one shade moving can damage
    both readings of it at once."""
    patch = crop()
    assert background_share(patch, SIDEBAR) > 0.8
    template = Template.build(patch)
    for ruler, anchors in template.rulers:
        assert len(anchors) == template_module.ANCHOR_COUNT
        assert all(ruler[SIDEBAR[0]] in anchor.needle for anchor in anchors)
    # And the two rulers really do read that shade differently, which is the
    # only reason a shift can be survivable at all.
    assert QUANT[31] == 0 and QUANT[32] == 1  # the cliff: one unit, new bucket
    assert QUANT_OFFSET[31] == 1 and QUANT_OFFSET[32] == 1  # not on this ruler


def test_the_plain_ruler_alone_still_loses_the_button_to_one_unit() -> None:
    """The regression this file was opened for, preserved as the thing the
    second ruler is measured against: with only ``v >> 5`` to go on, a surface
    one unit lighter proposes no origin within three pixels of a button that is
    demonstrably right there."""
    template = one_ruler(Template.build(crop()), QUANT)
    lighter = retinted(frame(), +1)
    origins = template_module._candidate_origins(template, lighter)
    assert not [o for o in origins if abs(o[0] - 20) <= 3 and abs(o[1] - 68) <= 3]
    assert find_in_region(template, lighter, max_diff=NEW_CHAT_MAX_DIFF) is None


# -- the recovery --------------------------------------------------------------


def test_a_surface_one_unit_lighter_is_still_found_on_the_second_ruler() -> None:
    """31 -> 32 crosses the plain ruler's edge and not the offset one's, so the
    origin is proposed anyway and the comparison calls it perfect."""
    template = Template.build(crop())
    lighter = retinted(frame(), +1)
    assert find_in_region(template, lighter, max_diff=NEW_CHAT_MAX_DIFF) == RegionMatch(
        *BUTTON_AT, 0.0
    )


def test_the_hover_tint_the_tolerance_exists_for_is_found_on_the_second_ruler() -> None:
    """A +9 surface tint is well inside DEFAULT_TOLERANCE - the comparison was
    always going to call this a perfect match - and now the search agrees with
    it instead of returning None over an origin it never proposed."""
    patch = crop()
    tinted = retinted(frame(), +9)
    assert DEFAULT_TOLERANCE > 9
    assert match_at_xy(patch, tinted, *BUTTON_AT, tolerance=DEFAULT_TOLERANCE) == 0.0
    assert find_in_region(Template.build(patch), tinted, max_diff=NEW_CHAT_MAX_DIFF) == RegionMatch(
        *BUTTON_AT, 0.0
    )


def test_a_surface_one_unit_darker_stays_in_its_bucket_and_is_still_found() -> None:
    """The other direction, which was never broken - kept so a change that
    trades one ruler's coverage for the other's cannot pass silently."""
    template = Template.build(crop())
    darker = retinted(frame(), -1)
    assert find_in_region(template, darker, max_diff=NEW_CHAT_MAX_DIFF) == RegionMatch(
        *BUTTON_AT, 0.0
    )


# -- the guarantee, and the band it does not reach -----------------------------


def test_any_background_shift_up_to_the_ruler_stagger_survives_one_of_them() -> None:
    """The property the two rulers buy, on the worst-case shade for it.

    The rulers' edges interleave every RULER_STAGGER units, so a shade moving
    by no more than that crosses at most one of them - and the ruler it did not
    cross reads exactly the byte it read at capture. Blue 31 is the worst case
    there is: it sits one unit below a plain edge, so the plain ruler is lost
    to +1 and every one of these shifts is riding on the offset ruler alone.
    """
    template = Template.build(crop())
    # Both ends of the band, both sides of the plain ruler's edge at 32, and a
    # couple in between. Retinting a megapixel in Python is the cost here, so
    # this samples the range rather than walking all 33 of it.
    for delta in (-RULER_STAGGER, -15, -1, 0, 1, 2, 9, 15, RULER_STAGGER):
        scene = retinted(frame(), delta)
        found = find_in_region(template, scene, max_diff=NEW_CHAT_MAX_DIFF)
        assert found == RegionMatch(*BUTTON_AT, 0.0), f"lost the button at a shift of {delta:+d}"


def test_a_shift_of_17_to_24_is_the_residual_gap_two_rulers_do_not_close() -> None:
    """Honest about what is still uncovered, and about how narrow it is.

    Past RULER_STAGGER a shade can land on an edge of both rulers at once, and
    blue 31 is exactly such a shade: +17 moves it over 32 (plain) and 48
    (offset) together. The band only runs to +24 because at +25 the per-channel
    tolerance rejects these pixels anyway - four fifths of the template is this
    surface - so from there the anchors are no longer the weakest link and a
    miss is the right answer rather than a blind spot.
    """
    patch = crop()
    template = Template.build(patch)
    for delta in (17, 24):
        scene = retinted(frame(), delta)
        # The comparison would still say yes; nothing asks it.
        assert match_at_xy(patch, scene, *BUTTON_AT, tolerance=DEFAULT_TOLERANCE) == 0.0
        assert find_in_region(template, scene, max_diff=NEW_CHAT_MAX_DIFF) is None

    beyond = retinted(frame(), 25)
    assert match_at_xy(patch, beyond, *BUTTON_AT, tolerance=DEFAULT_TOLERANCE) > 0.8


def test_the_best_miss_over_the_gap_still_describes_somewhere_else() -> None:
    """What the harness log says when the gap does bite, so nobody reads the
    number as a statement about the button: the closest thing JUDGED is
    elsewhere on the page, because the button was never a candidate."""
    template = Template.build(crop())
    scene = retinted(frame(), 17)
    match, best_miss = find_lowest_with_best_miss(template, scene, max_diff=NEW_CHAT_MAX_DIFF)
    assert match is None
    assert best_miss is not None and best_miss > 0.2
