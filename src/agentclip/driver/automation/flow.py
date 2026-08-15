"""The automation's numbers and its pure questions about a frame.

Everything here was a module-level constant or a module-level function of the
Textual ``MainScreen`` until the OS-acting sequences came down into
:class:`~agentclip.driver.automation.controller.AutomationController` (docs/design/gui.md
§1, slice 6). None of it decides anything on its own: the sizes are tuning, and
the functions are arithmetic over a frame - which rectangle to click, how close
the search came, how many of an appearance are on screen, where "just above the
chat box" is. The sequences that read them live on the controller.

``distinct_rects`` and ``element_rects`` arrived later and by the other route:
BOTH shells had spelled them out, because a shell may not import another shell
(tests/test_layering.py), and a second copy of the fold that stops two IMAGES of
one control reading as two windows is exactly the copy that drifts.

The constants stay reachable under the shell's old names (``main.py``
re-imports them), because the suites size their assertions off them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.template import (
    DEFAULT_TOLERANCE,
    CandidateSource,
    RegionMatch,
    Template,
    match_rect,
    same_element,
)

# The wheel flick's size, in detents, for the auto-copy flow's snap to the
# bottom. Deliberately far more than it takes to cross one screenful: a flick
# that stops short leaves the newest reply's copy button above the fold and the
# harvest hunts a transcript that is not showing the answer, which is a silent
# fall to MANUAL_COPY. Over-shooting costs nothing at all - the page is already
# at its bottom and the extra detents land on a wall - so the number is chosen
# for the worst long response rather than the typical one.
SNAP_WHEEL_DETENTS = -100
# How many Page Down taps a "page_down" scroll action sends in one burst
# (ServicePreset.scroll_action). Sized like the wheel flick above and for the
# same reason: a generous over-shoot that stops at the bottom, because the flow
# wants the newest reply on screen, not a measured scroll. Twelve taps is
# roughly a dozen screenfuls, which comfortably covers a long reply the user
# scrolled away from. End needs no such count - one tap is the bottom by
# definition, which is why it is left at one.
PAGE_DOWN_TAPS = 12
# How many times the flow will snap-and-search before giving up on the static
# frame. Pages that stream a reply keep growing while the flow is already
# running, and a lazily rendered transcript can still be laying out the last
# response when the first capture is taken - so one snap that comes up empty is
# weak evidence. Each extra round re-scrolls (the same action, the same settle)
# and re-searches; the pointer and the focus stay where round 1 put them.
COPY_SNAP_ROUNDS = 3
# Beat between the snap and the capture, for the page to render what it just
# scrolled to. Paid once per round.
SNAP_SETTLE_S = 0.4
# How far ABOVE the chat box a keyboard-scroll focus click lands (see
# ``above_chatbox``). The gap between the transcript and the input box is
# padding, and this is deliberately a small number: a few pixels higher is the
# last response itself, where a click could land on a link or select text.
ABOVE_CHATBOX_PX = 10
# Hover pause before clicking a calibrated element, for the same reason the copy
# click settles: web UIs paint their buttons on hover.
ELEMENT_CLICK_SETTLE_S = 0.05
# Small offsets from the matched rect, still inside a ~24 px icon, and how hard
# the verification looks for a clipboard that changed.
COPY_CLICK_OFFSETS = ((0, 0), (-3, -3), (3, 3))
COPY_VERIFY_READS = 6
COPY_VERIFY_INTERVAL_S = 0.2

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


def above_chatbox(box: ScreenRegion, region: ScreenRegion) -> ScreenRegion | None:
    """A one-pixel click target in the padding just above ``box``, horizontally
    centred on it - or None when that point is not inside ``region``.

    For the focus click that precedes a KEYBOARD snap to the bottom. Clicking
    the chat box itself puts a caret in the text field, and End there moves the
    caret to the end of the line - the transcript never scrolls at all. The
    padding strip above the box focuses the same page with nothing typable
    under the pointer.

    None is the "keep clicking the box" answer, and it covers the case that
    matters: with no chat box calibrated ``chatbox_region`` hands back the
    whole drawn window, whose top edge has no padding above it inside the
    region. Aiming above THAT would click outside the chat entirely.
    """
    x, _ = box.center
    y = box.top - ABOVE_CHATBOX_PX
    if not (region.left <= x < region.left + region.width):
        return None
    if not (region.top <= y < region.top + region.height):
        return None
    return ScreenRegion(x, y, 1, 1)


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
        match, miss = find(
            template, scene, tolerance=tolerance, max_diff=max_diff, matcher=matcher
        )
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


def how_close(best_miss: float | None) -> str:
    """The one number that turns "not found" into an actionable report.

    A near miss says the capture has drifted and wants recapturing (F2), while
    nothing judged at all says the icon simply was not on the frame. Written
    once and read by every round of the auto-copy flow's hunt, so the retry
    lines and the final verdict phrase the same fact the same way.
    """
    if best_miss is None:
        return "no candidate cleared the first-stage sniff test"
    return f"best candidate diff {best_miss:.2f}, needs ≤ {TemplateKind.COPY.max_diff:.2f}"
