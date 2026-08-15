"""The find-all search's arithmetic: from scene-local matches to click targets.

``flow.element_rects`` and the fold under it are what every "is it there / click
it" question is answered from, and until this commit both shells spelled them
out for themselves. The cases here are the ways that fold can be got wrong: two
IMAGES of one control (a send button and its greyed-out twin) landing on the
same pixels and reading as two windows, a genuine second window folding into
the first, and the union of a kind's images coming back in whatever order the
images happened to be searched in.

No pixels are compared: the search is passed in (``ScreenOps.all_matches``)
precisely so this stays arithmetic, so a stub answers for it here.
"""

from __future__ import annotations

import random

from agentclip.automation.flow import MAX_MATCHES, distinct_rects, element_rects
from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import RegionMatch, Template

# The drawn chat window every match below is scene-local to, deliberately not at
# the origin: a rectangle handed to a click is absolute, and an offset of (0, 0)
# would pass whether or not the translation happens at all.
REGION = ScreenRegion(1000, 500, 400, 300)


def noise(width: int, height: int, seed: int = 1) -> RegionImage:
    """A frame of deterministic pseudo-random pixels - enough structure for
    ``Template.build`` to take it."""
    rng = random.Random(seed)
    return RegionImage(width, height, bytes(rng.randrange(256) for _ in range(width * height * 4)))


def template(width: int = 20, height: int = 16, seed: int = 1) -> Template:
    return Template.build(noise(width, height, seed))


def test_two_images_of_one_control_fold_into_one_rectangle() -> None:
    """The whole point of the fold: a kind holds several pictures of the same
    button, and both of them matching means one button, not two windows."""
    icon, greyed = template(seed=1), template(seed=2)
    found = [(icon, RegionMatch(30, 40, 0.0)), (greyed, RegionMatch(31, 41, 0.01))]
    assert distinct_rects(REGION, found) == [ScreenRegion(1030, 540, 20, 16)]


def test_a_second_window_survives_the_fold() -> None:
    """The case the fold may NOT swallow: the same button in another window of
    the same service, which is what makes a click ambiguous."""
    icon = template()
    found = [(icon, RegionMatch(0, 0, 0.0)), (icon, RegionMatch(0, 200, 0.0))]
    assert distinct_rects(REGION, found) == [
        ScreenRegion(1000, 500, 20, 16),
        ScreenRegion(1000, 700, 20, 16),
    ]


def test_a_rectangle_is_the_size_of_the_image_that_matched() -> None:
    """A kind's images are not all one size, and a click target is the size of
    the picture that actually matched."""
    wide = template(width=80, height=24, seed=3)
    assert distinct_rects(REGION, [(wide, RegionMatch(5, 5, 0.0))]) == [
        ScreenRegion(1005, 505, 80, 24)
    ]


def test_the_union_is_sorted_into_reading_order_before_the_fold() -> None:
    """Which image was searched FIRST may not decide which rectangle survives a
    duplicate: the whole union is put back into reading order first, so the
    representative is the topmost match rather than the first-searched one."""
    first, second = template(seed=1), template(seed=2)
    scene = noise(400, 300)
    answers = {
        first: [RegionMatch(0, 100, 0.0)],
        second: [RegionMatch(1, 99, 0.0), RegionMatch(0, 250, 0.0)],
    }

    def find(tpl: Template, _scene: RegionImage, **_kw: object) -> list[RegionMatch]:
        return answers[tpl]

    assert element_rects(find, [first, second], scene, REGION, max_diff=0.1) == [
        ScreenRegion(1001, 599, 20, 16),  # the topmost of the two overlapping hits
        ScreenRegion(1000, 750, 20, 16),
    ]


def test_every_image_of_the_kind_is_searched_with_the_same_ruler() -> None:
    """An OR-union over the kind's images, each searched with the caller's
    tolerance/matcher and capped at ``MAX_MATCHES`` - anything past a handful
    is the same answer to "one, or more than one?" and costs a full compare."""
    calls: list[dict[str, object]] = []

    def find(tpl: Template, _scene: RegionImage, **kw: object) -> list[RegionMatch]:
        calls.append(kw)
        return []

    images = [template(seed=1), template(seed=2)]
    assert element_rects(find, images, noise(40, 40), REGION, max_diff=0.2, tolerance=7) == []
    assert len(calls) == len(images)
    assert all(call["limit"] == MAX_MATCHES for call in calls)
    assert all(call["tolerance"] == 7 and call["max_diff"] == 0.2 for call in calls)
