"""Staleness detection: has the response region stopped changing?

The third finish detector, and the only service-agnostic one. The busy/idle
detectors (screen/busy.py) compare against a *calibrated* baseline, which means
trusting one service's particular pixel cues - a stop square, a send button -
and some chat UIs simply have no reliable cue to draw a box around. Frame-to-
frame stability needs no cue at all: while the model streams its answer the
response region keeps changing, and once it has looked the same for long
enough the response is done. The user only draws the region where the answer
text appears.

Stateful where busy.py is stateless, necessarily: "stopped changing for N
seconds" is a property of a *sequence* of frames, so :class:`StaleTracker`
keeps the previous frame and a stability streak between polls. Each poll is
compared to the frame before it (rolled forward), never to a fixed anchor - a
response that streams slowly must read as changing on every tick, not as
"still similar to where it started". One tracker per detector run: the TUI
builds a fresh one whenever its poller (re)starts, and ``reset()`` lets it
forget mid-run (the auto-copy flow scrolls the very region being watched, so
the frame it left behind must not count as history).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from agentclip.screen.busy import DEFAULT_TOLERANCE, diff_fraction
from agentclip.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.screen.region import ScreenRegion

# Far stricter than busy's DEFAULT_MAX_DIFF (0.02): the drawn region is the
# whole response area, so one newly appended text line is a *tiny* fraction of
# its pixels - and noticing exactly that line is the detector's entire job.
STALE_MAX_DIFF = 0.002
# A denser sample budget than busy's MAX_SAMPLES (4096), same reasoning: at
# 4096 samples over a full response region the stride is so wide that a whole
# appended line of text can fall between sampled pixels and read as "no
# change". 16384 keeps a poll cheap while sampling every few pixels.
STALE_MAX_SAMPLES = 16_384
# How many consecutive quiet polls count as "done" when the caller does not
# say: 4 polls at the TUI's 0.5 s cadence is the default 2 s of stillness.
DEFAULT_REQUIRED_TICKS = 4


class StaleState(Enum):
    STALE = "stale"  # unchanged for required_ticks polls -> response finished
    CHANGING = "changing"  # still moving (or too recently) -> generating
    ERROR = "error"  # capture failed; no verdict


@dataclass(frozen=True, slots=True)
class StaleProbe:
    """One poll's verdict. ``diff`` is the frame-to-frame differing fraction
    (None on the first frame and on ERROR); ``stable_ticks`` is the current
    run of consecutive quiet polls, so the UI can show progress toward STALE."""

    state: StaleState
    diff: float | None
    stable_ticks: int


class StaleTracker:
    """Polls a screen region and reports STALE once it stops changing.

    ``required_ticks`` (not seconds) on purpose: the tracker has no clock and
    no timer - the caller owns the poll cadence, so the caller converts its
    "stable for N seconds" wish into a tick count. ``capture`` is injectable
    the same way busy.py's tests monkeypatch ``capture_region``: the streak
    logic is pure and worth testing without a screen.
    """

    def __init__(
        self,
        region: ScreenRegion,
        *,
        tolerance: int = DEFAULT_TOLERANCE,
        max_diff: float = STALE_MAX_DIFF,
        required_ticks: int = DEFAULT_REQUIRED_TICKS,
        capture: Callable[[ScreenRegion], RegionImage] = capture_region,
    ) -> None:
        self._region = region
        self._tolerance = tolerance
        self._max_diff = max_diff
        self._required_ticks = max(1, required_ticks)
        self._capture = capture
        self._last: RegionImage | None = None
        self._streak = 0

    def poll(self) -> StaleProbe:
        """Capture the region and fold it into the streak. Never raises.

        A capture failure keeps the streak AND the stored frame - the same
        blip-tolerance as the busy prober: one bad frame must not silently
        restart the stillness clock on an in-flight finish. The first frame is
        CHANGING by definition: with nothing to compare against, claiming
        stillness would let a chat that was idle at calibration time read as
        "finished" without any generation ever observed.
        """
        try:
            current = self._capture(self._region)
        except CaptureError:
            return StaleProbe(StaleState.ERROR, None, self._streak)
        previous, self._last = self._last, current  # roll the frame forward
        if previous is None:
            self._streak = 0
            return StaleProbe(StaleState.CHANGING, None, 0)
        diff = diff_fraction(
            previous, current, tolerance=self._tolerance, max_samples=STALE_MAX_SAMPLES
        )
        self._streak = self._streak + 1 if diff <= self._max_diff else 0
        state = StaleState.STALE if self._streak >= self._required_ticks else StaleState.CHANGING
        return StaleProbe(state, diff, self._streak)

    def reset(self) -> None:
        """Forget the last frame and the streak, as if freshly built.

        For the TUI's flow suspension: the auto-copy flow scrolls and hover-
        scans the browser, mutating the watched region itself - so when the
        flow ends, both the streak and the frame it would be compared against
        describe a screen the flow produced, not one the model did.
        """
        self._last = None
        self._streak = 0
