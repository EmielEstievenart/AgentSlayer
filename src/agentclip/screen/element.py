"""A screen element the user calibrated once: where it is *and* what it looked like.

Several detectors ask the same question - the two chat input boxes (a fresh
chat centres its box, an ongoing one docks it at the bottom), the browser's
"new chat" control, and later the sub-agent slots: *is the thing I was pointed
at still there?* That is always a region plus the pixels it held at calibration
time, so it is one type instead of two parallel attributes on every caller.

The comparison reuses screen.busy's strided sampler rather than a second
implementation; only the threshold differs. It is looser than busy's because a
chat input box grows a caret, loses a placeholder and picks up hover tinting
without ceasing to be that chat input box - while a box that has moved off
screen (the fresh-chat layout replaced by the ongoing one) differs wholesale.

``CalibratedElement.matches`` is pure - no capture, no OS - so the decision is
unit-testable anywhere; ``probe_element`` is the thin "look at the screen now"
wrapper the TUI monkeypatches in tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentclip.screen.busy import DEFAULT_TOLERANCE, diff_fraction
from agentclip.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.screen.region import ScreenRegion

# The element "is still there" when no more than this fraction of sampled
# pixels differ from the calibration snapshot.
DEFAULT_MAX_DIFF = 0.10


@dataclass(frozen=True, slots=True)
class CalibratedElement:
    """A region the user boxed, plus the frame it held at that moment."""

    region: ScreenRegion
    template: RegionImage

    def matches(
        self,
        current: RegionImage,
        *,
        tolerance: int = DEFAULT_TOLERANCE,
        max_diff: float = DEFAULT_MAX_DIFF,
    ) -> bool:
        """Does ``current`` still look like the calibration snapshot?

        Pure. A frame of different dimensions never matches (``diff_fraction``
        reports 1.0), which is what a re-drawn or rescaled window looks like.
        """
        return diff_fraction(self.template, current, tolerance=tolerance) <= max_diff

    def describe(self) -> str:
        return self.region.describe()


def probe_element(
    element: CalibratedElement,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    max_diff: float = DEFAULT_MAX_DIFF,
) -> bool:
    """Capture the element's region right now and compare. Never raises.

    A failed capture reads as "not there" (False) on purpose: every caller uses
    this to decide whether it is safe to click, and a screen we cannot see is
    not a screen we should be clicking blind.
    """
    try:
        current = capture_region(element.region)
    except CaptureError:
        return False
    return element.matches(current, tolerance=tolerance, max_diff=max_diff)
