"""The search's arithmetic: from a frame and a stack of images to rectangles.

Pure functions over one captured frame. Nothing here captures anything, clicks
anything or decides anything - the search itself is passed in
(``ScreenOps.lowest_match`` / ``ScreenOps.all_matches``) precisely so this stays
arithmetic, and the two questions it answers are the whole search model:

* **Where is the NEWEST one?** :func:`lowest_match_scored` - the copy button's
  question. Every response stamps one icon and the newest is the lowest.
* **How many are there?** :func:`element_rects` - the "is it there / click it"
  question. An appearance belongs to the SERVICE, so a second window of the same
  service inside the drawn region carries the same button, and clicking whichever
  match came back first would click a different conversation's.

It lives in the monitor package because the monitor is what asks both questions
now (docs/design/ui-monitor.md §2.3: the pixel verdicts are the monitor's).
:mod:`agentclip.driver.automation.flow` re-exports every name under the spelling
its suites already reach for - both these functions had been written out by BOTH
shells before they came down here, because a shell may not import another shell,
and a second copy of the fold that stops two IMAGES of one control reading as two
windows is exactly the copy that drifts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.template import (
    DEFAULT_TOLERANCE,
    CandidateSource,
    RegionMatch,
    Template,
    match_rect,
    same_element,
)

# How many matches of one appearance are worth collecting. The question the
# search answers is "one, or more than one?", so anything past a handful is the
# same answer - and every extra candidate is a full pixel comparison.
MAX_MATCHES = 8

# One image's answer about one frame: the bottom-most match, and the closest
# candidate that was rejected. ``ScreenOps.lowest_match`` is the implementation;
# it is passed in so the two functions below stay arithmetic.
FindLowest = Callable[..., tuple[RegionMatch | None, float | None]]
# The same trick for the other question: every match of one image in one frame.
# ``ScreenOps.all_matches`` is the implementation.
FindAll = Callable[..., list[RegionMatch]]


def lowest_match_scored(
    find: FindLowest,
    templates: Sequence[Template],
    scene: RegionImage,
    *,
    max_diff: float,
    tolerance: int = DEFAULT_TOLERANCE,
    matcher: CandidateSource | None = None,
) -> tuple[tuple[Template, RegionMatch] | None, float | None]:
    """The bottom-most match of ANY of a kind's images, and how close the whole
    stack came to one.

    The copy button's question asked across a whole stack: every response stamps
    one icon, the newest is the lowest, and which of the service's captured
    pictures of it matched changes nothing about that - only the size of the
    rectangle to click.

    The second value is the smallest REJECTED diff across every image of the
    kind (None when no candidate was judged at all), and it is what lets a
    failed copy-button search say whether the button was absent or the capture
    has stopped matching it - see ``screen.template.find_lowest_with_best_miss``.
    Across the stack rather than per image, because the user captured several
    pictures of ONE control and "how close did we get" is a question about the
    control.
    """
    best: tuple[Template, RegionMatch] | None = None
    best_miss: float | None = None
    for template in templates:
        match, miss = find(template, scene, tolerance=tolerance, max_diff=max_diff, matcher=matcher)
        if miss is not None and (best_miss is None or miss < best_miss):
            best_miss = miss
        if match is None:
            continue
        if best is None or (match.y, match.x) > (best[1].y, best[1].x):
            best = (template, match)
    return best, best_miss


def lowest_match(
    find: FindLowest,
    templates: Sequence[Template],
    scene: RegionImage,
    *,
    max_diff: float,
    tolerance: int = DEFAULT_TOLERANCE,
    matcher: CandidateSource | None = None,
) -> tuple[Template, RegionMatch] | None:
    """:func:`lowest_match_scored` without the miss - the hover scan's question,
    which only ever asks "is it there yet"."""
    return lowest_match_scored(
        find, templates, scene, max_diff=max_diff, tolerance=tolerance, matcher=matcher
    )[0]


def distinct_rects(
    region: ScreenRegion, found: list[tuple[Template, RegionMatch]]
) -> list[ScreenRegion]:
    """Scene-local matches as absolute rectangles, one per physical element.

    Each match carries the image that produced it, because a kind holds several
    (screen.profile) and a rectangle to click is the size of the image that
    actually matched. Fed the whole union at once for the same reason: two
    images of one control - a send button and its greyed-out twin - land on the
    same pixels, and must fold into one rectangle rather than read as two
    windows of the same service.
    """
    kept: list[ScreenRegion] = []
    for template, match in found:
        rect = match_rect(region, template, match)
        if not any(same_element(rect, other) for other in kept):
            kept.append(rect)
    return kept


def element_rects(
    find: FindAll,
    templates: Sequence[Template],
    scene: RegionImage,
    region: ScreenRegion,
    *,
    max_diff: float,
    tolerance: int = DEFAULT_TOLERANCE,
    matcher: CandidateSource | None = None,
) -> list[ScreenRegion]:
    """Every place a kind is in one frame, as absolute rectangles - one each.

    ``lowest_match_scored``'s sibling and the other half of the search model:
    that one asks "where is the NEWEST one", this one asks "how many are
    there". All of them rather than the first, because that is the question
    callers actually have to answer - an appearance belongs to the SERVICE, so
    a second window of the same service inside the drawn region carries the
    same button, and clicking whichever match came back first would click a
    different conversation's.

    A plain OR-union over the kind's images: any of them being on screen means
    the control is. Sorted back into reading order across the union before the
    fold, so which image happened to be searched first cannot change which
    rectangle survives as a duplicate's representative - and then folded
    (``distinct_rects``), so a list longer than one really does mean two
    elements rather than two IMAGES of one (screen.profile's variant stacks).

    Arithmetic over a frame, like everything else here: the search is passed in
    (``ScreenOps.all_matches``) and the capture is the caller's.
    """
    found: list[tuple[Template, RegionMatch]] = []
    for template in templates:
        matches = find(
            template,
            scene,
            tolerance=tolerance,
            max_diff=max_diff,
            limit=MAX_MATCHES,
            matcher=matcher,
        )
        found.extend((template, match) for match in matches)
    found.sort(key=lambda pair: (pair[1].y, pair[1].x))
    return distinct_rects(region, found)
