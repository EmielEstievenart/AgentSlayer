"""What `/identify` believes it can see, computed from hand-built scenes.

The pure half of the command (``agentclip.driver.screen.identify``): a drawn rectangle,
a service profile and one captured frame in, a labelled list of absolute screen
rectangles out. No capture, no tkinter, no OS - the scenes here are bytes with
templates planted at known offsets, so "the box is where the button is" is an
exact assertion rather than a screenshot to squint at.

The wire format is tested here too, next to the thing it carries: it is how the
list reaches the drawing child process, and a round trip that loses a coordinate
would put every box in the wrong place with nothing to catch it.
"""

from __future__ import annotations

import pytest

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.identify import (
    CHAT_REGION_LABEL,
    IdentifiedElement,
    format_payload,
    identify_elements,
    parse_payload,
    summarise,
)
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion

REGION = ScreenRegion(1000, 500, 320, 240)


def _noise(width: int, height: int, seed: int = 1) -> RegionImage:
    """A background no template will accidentally match.

    A plain LCG rather than a repeating pattern: the matcher's anchors are exact
    8-byte runs of the quantised blue plane, and any periodic filler is a run
    that repeats all over the scene.
    """
    state = seed
    out = bytearray(width * height * 4)
    for index in range(len(out)):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out[index] = (state >> 16) & 0xFF
    return RegionImage(width, height, bytes(out))


def _patch(width: int, height: int, seed: int = 11) -> RegionImage:
    """A small appearance varied enough for ``Template.build`` to anchor on.

    Same generator as the background, off a different seed - so two appearances
    are genuinely two pictures. A cyclic filler will not do: a phase-shifted
    cycle is the SAME picture translated, and the two variants below would then
    match each other one row apart and fold into one element.
    """
    return _noise(width, height, seed)


def _plant(scene: RegionImage, patch: RegionImage, x: int, y: int) -> RegionImage:
    """``patch`` copied into ``scene`` at scene-local ``(x, y)``."""
    pixels = bytearray(scene.pixels)
    for row in range(patch.height):
        start = ((y + row) * scene.width + x) * 4
        source = row * patch.width * 4
        pixels[start : start + patch.width * 4] = patch.pixels[source : source + patch.width * 4]
    return RegionImage(scene.width, scene.height, bytes(pixels))


def _profile(**kinds: RegionImage) -> ServiceProfile:
    profile = ServiceProfile("test-service")
    for name, image in kinds.items():
        profile.put(TemplateKind(name.replace("_", "-")), image)
    return profile


def _by_label(elements: list[IdentifiedElement], label: str) -> list[IdentifiedElement]:
    return [element for element in elements if element.label == label]


def test_the_drawn_region_is_always_the_first_element() -> None:
    """Where the tool was LOOKING is half the answer - and with an empty profile
    it is the whole of it, which is exactly the case a user runs this to see."""
    elements = identify_elements(REGION, ServiceProfile("empty"), _noise(320, 240))
    assert [element.label for element in elements] == [CHAT_REGION_LABEL]
    assert elements[0].rect == REGION
    assert elements[0].diff is None  # nothing was matched to find it


def test_a_planted_appearance_comes_back_at_the_absolute_screen_offset() -> None:
    """The whole contract: scene-local match + the region's own origin. A box
    drawn a region's width off would land on the desktop next to the browser."""
    copy = _patch(24, 24)
    scene = _plant(_noise(320, 240), copy, 40, 90)
    elements = identify_elements(REGION, _profile(copy=copy), scene)

    found = _by_label(elements, "copy")
    assert len(found) == 1
    assert found[0].rect == ScreenRegion(REGION.left + 40, REGION.top + 90, 24, 24)
    assert found[0].diff == pytest.approx(0.0)  # planted verbatim
    assert found[0].describe() == "copy 0.000"


def test_every_occurrence_of_a_kind_is_reported() -> None:
    """Two copy buttons is a real finding (usually: two chats inside one drawn
    box), and the debug view's job is to show it, not to pick one."""
    copy = _patch(24, 24)
    scene = _plant(_plant(_noise(320, 240), copy, 20, 30), copy, 200, 150)
    elements = identify_elements(REGION, _profile(copy=copy), scene)

    rects = [element.rect for element in _by_label(elements, "copy")]
    assert rects == [
        ScreenRegion(REGION.left + 20, REGION.top + 30, 24, 24),
        ScreenRegion(REGION.left + 200, REGION.top + 150, 24, 24),
    ]


def test_near_duplicate_hits_on_one_element_fold_into_one_box() -> None:
    """A template matches its own element at several neighbouring origins, and a
    stack of boxes a pixel apart is unreadable. The fold is ``same_element``, the
    same one the automation uses to count windows."""
    copy = _patch(24, 24)
    # A second, shifted plant that overlaps the first: one physical button.
    scene = _plant(_plant(_noise(320, 240), copy, 60, 60), copy, 62, 61)
    elements = identify_elements(REGION, _profile(copy=copy), scene)
    assert len(_by_label(elements, "copy")) == 1


def test_two_variants_of_one_kind_report_both_places_but_not_one_place_twice() -> None:
    """A kind holds a STACK of appearances (a send button and its greyed twin).
    Both are searched, and the union is folded once - so two variants sitting on
    one element are one box, and two elements are two."""
    first, second = _patch(24, 24), _patch(24, 24, seed=97)
    profile = _profile()
    profile.put(TemplateKind.SEND_READY, first)
    profile.put(TemplateKind.SEND_READY, second)

    scene = _plant(_plant(_noise(320, 240), first, 30, 30), second, 180, 120)
    elements = identify_elements(REGION, profile, scene)
    rects = [element.rect for element in _by_label(elements, "send-ready")]
    assert rects == [
        ScreenRegion(REGION.left + 30, REGION.top + 30, 24, 24),
        ScreenRegion(REGION.left + 180, REGION.top + 120, 24, 24),
    ]


def test_kinds_are_reported_separately_even_when_they_land_on_each_other() -> None:
    """One picture captured under two kinds is a calibration mistake worth
    seeing, so the fold is per kind and never across them."""
    shared = _patch(24, 24)
    scene = _plant(_noise(320, 240), shared, 70, 70)
    elements = identify_elements(REGION, _profile(copy=shared, new_chat=shared), scene)

    labels = [element.label for element in elements]
    assert labels == [CHAT_REGION_LABEL, "copy", "new-chat"]
    assert elements[1].rect == elements[2].rect


def test_an_appearance_that_is_not_on_screen_is_simply_absent() -> None:
    copy = _patch(24, 24)
    elements = identify_elements(REGION, _profile(copy=copy), _noise(320, 240, seed=7))
    assert [element.label for element in elements] == [CHAT_REGION_LABEL]


def test_the_summary_counts_by_kind_and_ignores_the_region_itself() -> None:
    copy = _patch(24, 24)
    scene = _plant(_plant(_noise(320, 240), copy, 20, 30), copy, 200, 150)
    elements = identify_elements(REGION, _profile(copy=copy), scene)
    assert summarise(elements) == "identified 2 elements: copy×2"


def test_the_summary_says_nothing_was_found_rather_than_one() -> None:
    """The drawn region is always in the list, so counting it would report the
    empty result - the one people run this to diagnose - as a success."""
    text = summarise(identify_elements(REGION, ServiceProfile("empty"), _noise(320, 240)))
    assert "identified nothing" in text
    assert "F2" in text  # ...and says where appearances come from


# -- the wire format ----------------------------------------------------------


def test_the_payload_round_trips_every_field() -> None:
    elements = [
        IdentifiedElement(CHAT_REGION_LABEL, REGION),
        IdentifiedElement("copy", ScreenRegion(-40, -8, 24, 24), 0.0125),
    ]
    assert parse_payload(format_payload(elements)) == elements


def test_the_payload_survives_negative_origins() -> None:
    """A monitor left of or above the primary makes both coordinates negative,
    and the overlay spans the whole virtual desktop."""
    element = IdentifiedElement("new-chat", ScreenRegion(-1920, -120, 80, 30), 0.02)
    assert parse_payload(format_payload([element]))[0].rect == element.rect


def test_an_empty_list_round_trips() -> None:
    assert parse_payload(format_payload([])) == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json at all",
        "[]",  # a bare list is not the envelope
        '{"elements": 3}',
        '{"elements": ["copy"]}',
        '{"elements": [{"label": "copy"}]}',  # no rectangle
        '{"elements": [{"label": "copy", "left": "x", "top": 0, "width": 1, "height": 1}]}',
    ],
)
def test_a_malformed_payload_is_a_value_error_not_a_blank_overlay(text: str) -> None:
    """The child turns this into one stderr line and a non-zero exit, which the
    parent reports - better than a fullscreen window with nothing drawn on it."""
    with pytest.raises(ValueError):
        parse_payload(text)
