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
one control reading as two windows is exactly the copy that drifts. Phase 2 moved
them on again, to :mod:`agentclip.driver.monitor.search`, along with the two
scroll sizes and the element-click settle (:mod:`agentclip.driver.monitor.beats`):
the monitor is what captures a frame, searches it and scrolls the page now
(docs/design/ui-monitor.md §2.3), and none of that arithmetic may sit above the
seam it is asked across.

What is still WRITTEN here is what the brain still asks for itself: where to
click before a keyboard scroll (``above_chatbox``), how a failed hunt reports
itself (``how_close``), and the sizes of the hunt's own retries.

Every name stays reachable under the shell's old address (``main.py``
re-imports them), because the suites size their assertions off them.
"""

from __future__ import annotations

from agentclip.driver.monitor.beats import (  # noqa: F401
    ELEMENT_CLICK_SETTLE_S,
    PAGE_DOWN_TAPS,
    SNAP_WHEEL_DETENTS,
)

# The search's arithmetic MOVED to the monitor in phase 2
# (docs/design/ui-monitor.md §2.3): it is the monitor that captures a frame and
# asks these two questions now, so they live where the pixels are. Re-exported
# here because the brain and every suite name them at this address.
from agentclip.driver.monitor.search import (  # noqa: F401
    MAX_MATCHES,
    FindAll,
    FindLowest,
    distinct_rects,
    element_rects,
    lowest_match,
    lowest_match_scored,
)
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion

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
# Small offsets from the pixel the click was aimed at, still inside a ~24 px
# icon, and how hard the verification looks for a clipboard that changed.
COPY_CLICK_OFFSETS = ((0, 0), (-3, -3), (3, 3))
COPY_VERIFY_READS = 6
COPY_VERIFY_INTERVAL_S = 0.2


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
