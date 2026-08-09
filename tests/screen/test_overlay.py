"""The `/identify` overlay's annotation maths, without a screen anywhere near it.

`screen.overlay` is two tkinter entry points around a small amount of pure
arithmetic: which colour each element gets, which badge it carries, and where
that badge lands. Those are the parts that can be silently wrong - a legend that
numbers the boxes differently from the badges on them is worse than no legend -
so they live in ``_identify_colours`` / ``_identify_labels``, which take element
lists and return data.

Nothing here imports tkinter, builds a root or spawns the overlay child: the
drawing is a handful of ``create_text`` calls over this data, and a test rig for
a fullscreen topmost window would be a worse test of it than reading the code.
"""

from __future__ import annotations

from agentclip.screen.identify import CHAT_REGION_LABEL, IdentifiedElement
from agentclip.screen.overlay import (
    _BADGE_NUDGE_PX,
    IDENTIFY_COLOURS,
    IDENTIFY_REGION_BADGE,
    IDENTIFY_REGION_COLOUR,
    _identify_colours,
    _identify_labels,
)
from agentclip.screen.region import ScreenRegion

REGION = ScreenRegion(1000, 500, 320, 240)


def _element(label: str, left: int, top: int, diff: float | None = 0.01) -> IdentifiedElement:
    return IdentifiedElement(label, ScreenRegion(left, top, 40, 20), diff)


def _scene() -> list[IdentifiedElement]:
    """The shape ``identify_elements`` returns: the region, then the finds."""
    return [
        IdentifiedElement(CHAT_REGION_LABEL, REGION),
        _element("copy", 1100, 600, 0.013),
        _element("copy", 1100, 700, 0.021),
        _element("new-chat", 1200, 540, 0.004),
    ]


# -- colours ------------------------------------------------------------------


def test_one_colour_per_label_shared_by_every_instance() -> None:
    labels = _identify_labels(_scene())
    assert labels[1].colour == labels[2].colour  # two copies are one family
    assert labels[3].colour != labels[1].colour


def test_region_colour_is_off_the_cycle() -> None:
    colours = _identify_colours(_scene())
    assert colours[CHAT_REGION_LABEL] == IDENTIFY_REGION_COLOUR
    assert IDENTIFY_REGION_COLOUR not in IDENTIFY_COLOURS


def test_colours_follow_first_appearance_order() -> None:
    colours = _identify_colours([_element("copy", 0, 100), _element("new-chat", 0, 200)])
    assert list(colours) == ["copy", "new-chat"]
    assert list(colours.values()) == [IDENTIFY_COLOURS[0], IDENTIFY_COLOURS[1]]


def test_more_kinds_than_colours_cycles_rather_than_failing() -> None:
    many = [_element(f"kind-{index}", 0, index * 100) for index in range(len(IDENTIFY_COLOURS) + 2)]
    colours = _identify_colours(many)
    assert colours["kind-0"] == colours[f"kind-{len(IDENTIFY_COLOURS)}"]


# -- badges and legend rows ---------------------------------------------------


def test_badges_number_the_finds_and_mark_the_region() -> None:
    badges = [label.badge for label in _identify_labels(_scene())]
    assert badges == [IDENTIFY_REGION_BADGE, "#1", "#2", "#3"]


def test_legend_row_carries_badge_label_and_diff() -> None:
    labels = _identify_labels(_scene())
    assert labels[1].legend == "#1  copy 0.013"
    assert labels[3].legend == "#3  new-chat 0.004"


def test_region_legend_row_has_no_diff() -> None:
    assert _identify_labels(_scene())[0].legend == f"{IDENTIFY_REGION_BADGE}  {CHAT_REGION_LABEL}"


def test_labels_are_one_per_element_in_order() -> None:
    elements = _scene()
    assert len(_identify_labels(elements)) == len(elements)


# -- badge placement ----------------------------------------------------------


def test_badge_sits_just_above_its_box_in_window_coordinates() -> None:
    label = _identify_labels([_element("copy", 1100, 600)], left=1000, top=500)[0]
    assert (label.x, label.y) == (102, 91)  # 1100-1000+2, 600-500-9


def test_badge_flips_inside_a_box_flush_with_the_top_of_the_screen() -> None:
    label = _identify_labels([_element("copy", 1100, 502)], left=1000, top=500)[0]
    assert label.y == 11  # 2 + 9, drawn inside the box rather than off-screen


def test_identical_rectangles_get_their_badges_side_by_side() -> None:
    labels = _identify_labels([_element("copy", 400, 300), _element("copy", 400, 300)])
    assert labels[0].y == labels[1].y
    assert labels[1].x - labels[0].x == _BADGE_NUDGE_PX


def test_a_third_stacked_badge_clears_both_of_the_others() -> None:
    labels = _identify_labels([_element("copy", 400, 300)] * 3)
    assert [label.x for label in labels] == [402, 402 + _BADGE_NUDGE_PX, 402 + 2 * _BADGE_NUDGE_PX]


def test_near_identical_rectangles_nudge_too() -> None:
    """Two matches of one appearance are boxes a couple of pixels apart."""
    labels = _identify_labels([_element("copy", 400, 300), _element("copy", 402, 301)])
    assert labels[1].x - labels[0].x == _BADGE_NUDGE_PX + 2


def test_boxes_far_enough_apart_keep_their_natural_positions() -> None:
    labels = _identify_labels([_element("copy", 400, 300), _element("copy", 400, 400)])
    assert [label.x for label in labels] == [402, 402]  # different lines, no collision


def test_no_elements_is_no_labels() -> None:
    assert _identify_labels([]) == []
