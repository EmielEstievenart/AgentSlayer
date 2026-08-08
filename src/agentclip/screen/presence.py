"""Is this appearance on screen right now? De-bounced, and asymmetrically so.

The finish detectors used to ask "does this fixed rectangle still look like it
did at calibration?", which forced the user to re-draw a box every time the
browser moved. This asks the question the way a person does - *is the stop
button visible anywhere in the chat?* - by searching a captured appearance
inside the whole chat region (screen.template).

One frame is not an answer. A tooltip, a scroll, a repaint mid-capture can all
hide a button that is really there, and the two possible mistakes are not
equally bad: believing the model is still generating when it has finished
costs a poll interval, while believing it has finished when it hasn't harvests
a truncated answer and feeds it to the engine as the whole response. So the
"still generating" verdict is adopted the instant one frame supports it, and
the "finished" verdict must survive ``required_ticks`` consecutive frames.

The verdict is reported as busy.py's BusyProbe so the sidebar and the TUI's
existing verdict readers keep working unchanged. Its polarity depends on which
appearance is being tracked, exactly mirroring today's two probes:

    found_is_busy   template found     template missing
    True (busy)     MATCH now          MATCH until N misses, then CHANGED
    False (idle)    CHANGED until N finds, then MATCH   CHANGED now

which reads the same both ways round: MATCH means "still generating" for a
busy-time appearance and "finished" for an idle-time one - the meanings
main.py's ``_busy_verdict`` / ``_idle_verdict`` already assign them.
"""

from __future__ import annotations

from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import RegionImage
from agentclip.screen.template import (
    DEFAULT_MAX_DIFF,
    DEFAULT_TOLERANCE,
    Template,
    find_in_region,
)

# Four ticks at the TUI's 0.5 s cadence: two seconds of agreement before a
# response is declared finished. Same default, and same reasoning, as the stale
# detector's DEFAULT_REQUIRED_TICKS.
DEFAULT_REQUIRED_TICKS = 4


class PresenceTracker:
    """Tracks one appearance across polls and reports a de-bounced verdict.

    Stateful, like StaleTracker and unlike probe_busy: "missing for N frames in
    a row" is a property of a sequence. The tracker owns no timer and no
    capture - the caller polls it with a frame - so one capture of the chat
    region can feed several trackers on the same tick, and they all judge the
    same instant.
    """

    def __init__(
        self,
        template: Template,
        *,
        found_is_busy: bool,
        required_ticks: int = DEFAULT_REQUIRED_TICKS,
        tolerance: int = DEFAULT_TOLERANCE,
        max_diff: float = DEFAULT_MAX_DIFF,
    ) -> None:
        self._template = template
        self._found_is_busy = found_is_busy
        self._required_ticks = max(1, required_ticks)
        self._tolerance = tolerance
        self._max_diff = max_diff
        # Consecutive frames arguing for the *finished* verdict. The generating
        # verdict needs no counter: it is adopted on sight.
        self._streak = 0

    @property
    def _generating(self) -> BusyState:
        return BusyState.MATCH if self._found_is_busy else BusyState.CHANGED

    @property
    def _finished(self) -> BusyState:
        return BusyState.CHANGED if self._found_is_busy else BusyState.MATCH

    def observe(self, scene: RegionImage | None) -> BusyProbe:
        """Fold one frame of the chat region into the verdict. Never raises.

        ``None`` (the caller's capture failed) reports ERROR and leaves the
        streak alone - the same blip-tolerance as StaleTracker.poll: a dropped
        frame midway through a run of misses must neither count toward
        "finished" nor throw away the progress already made toward it.
        """
        if scene is None:
            return BusyProbe(BusyState.ERROR, None)
        match = find_in_region(
            self._template, scene, tolerance=self._tolerance, max_diff=self._max_diff
        )
        diff = match.diff if match is not None else None
        if (match is not None) == self._found_is_busy:
            self._streak = 0
            return BusyProbe(self._generating, diff)
        self._streak += 1
        state = self._finished if self._streak >= self._required_ticks else self._generating
        return BusyProbe(state, diff)

    def reset(self) -> None:
        """Forget the streak, as if freshly built.

        No verdict survives this: the next frame decides on its own. For the
        TUI's flow suspension - the auto-copy flow clicks and scrolls the very
        window being watched, so the frames it produces say nothing about
        whether the model is generating.
        """
        self._streak = 0
