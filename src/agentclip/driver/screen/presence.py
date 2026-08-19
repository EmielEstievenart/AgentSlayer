"""Is this appearance on screen right now? De-bounced, and asymmetrically so.

The finish detectors used to ask "does this fixed rectangle still look like it
did at calibration?", which forced the user to re-draw a box every time the
browser moved. This asks the question the way a person does - *is the stop
button visible anywhere in the chat?* - by searching the service's captured
images of that appearance inside the whole chat region (screen.template). Every
image of the kind, ORed: one control can be drawn several ways (greyed out
mid-upload, tinted on hover), and any of them being there is the same answer.

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

Read that table again for what it does NOT say. The "still generating" cell in
the top-right and bottom-left corners is two different things wearing one
value: a genuine same-frame reading, and the grace-period default a
freshly-reset tracker reports while its streak climbs toward
``required_ticks``. Biasing the *verdict* that way is the whole design above -
but a caller that reads a single verdict as "the reasoning icon is on screen"
is then reading evidence out of an empty room, and the TUI did exactly that:
resetting every tracker at the paste (``_open_reply_gate``) made tick one of
every message claim a generation, arm the auto-copy, and fire it at a chat the
model had not begun to answer. So ``BusyProbe.generating_now`` carries the raw
per-frame fact next to the de-bounced verdict, and it is deliberately NOT
symmetric:

* a **busy** tracker's positive match is strong evidence - the stop button, the
  shimmer, the thinking dots are drawn by a live generation and by nothing
  else, so one frame of it is proof;
* an **idle** tracker's same-frame miss is weak. "The send button is not
  visible" is also what a composer mid-layout on a fresh page looks like, and
  what a scrolled-away composer looks like. It counts only once this tracker
  has genuinely FOUND its template at least once since the last reset, which
  turns the observation into the transition it is meant to be - the idle
  appearance was there, and now it is gone.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.template import (
    DEFAULT_MAX_DIFF,
    DEFAULT_TOLERANCE,
    CandidateSource,
    RegionMatch,
    Template,
    find_in_region,
)

# Four ticks at the TUI's 0.5 s cadence: two seconds of agreement before a
# response is declared finished. Same default, and same reasoning, as the stale
# detector's DEFAULT_REQUIRED_TICKS - though not quite the same count: a tick
# here is one observation, while a stale tick is one frame-to-frame COMPARISON,
# so StaleTracker needs N + 1 frames to reach N of them and this tracker flips
# on the Nth.
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
        templates: Sequence[Template],
        *,
        found_is_busy: bool,
        required_ticks: int = DEFAULT_REQUIRED_TICKS,
        tolerance: int = DEFAULT_TOLERANCE,
        max_diff: float = DEFAULT_MAX_DIFF,
        matcher: CandidateSource | None = None,
    ) -> None:
        self._templates = tuple(templates)
        self._found_is_busy = found_is_busy
        self._required_ticks = max(1, required_ticks)
        self._tolerance = tolerance
        self._max_diff = max_diff
        # Which way of proposing candidate origins this service asked for
        # (screen.matchers). None is the built-in anchor search. It is held
        # rather than looked up per frame because resolving it can import cv2,
        # and this runs on a poll timer.
        self._matcher = matcher
        # Consecutive frames arguing for the *finished* verdict. The generating
        # verdict needs no counter: it is adopted on sight.
        self._streak = 0
        # Has this appearance actually been on screen since the last reset? Only
        # an idle tracker needs it, to tell "the send button went away" (a send)
        # from "the send button was never there" (a page still laying itself
        # out) - see the module docstring.
        self._found_since_reset = False
        # Where the appearance was on the last frame that was actually searched,
        # and WHICH of the kind's images found it - see ``last_sighting``.
        self._last_sighting: tuple[Template, RegionMatch] | None = None

    def _find(self, scene: RegionImage) -> tuple[Template, RegionMatch] | None:
        """The first of the kind's images that is on screen, with where it is.

        A plain OR over the variants (screen.profile): a service that greys its
        send button out mid-upload draws a second picture of the same control,
        and either one being there means the same thing. First rather than
        best-matching, because this runs on a poll timer and the question is
        presence - the diff only ever reaches the sidebar readout.

        The winning *template* travels with the match because a match is only
        half an answer: ``RegionMatch`` carries the top-left corner and the
        variant that matched carries the size, and it takes both to cut the
        matched pixels back out of the scene.
        """
        for template in self._templates:
            match = find_in_region(
                template,
                scene,
                tolerance=self._tolerance,
                max_diff=self._max_diff,
                matcher=self._matcher,
            )
            if match is not None:
                return (template, match)
        return None

    @property
    def last_sighting(self) -> tuple[Template, RegionMatch] | None:
        """Where the appearance was on the last SEARCHED frame, and what found it.

        The de-bounced verdict's raw counterpart in space, as
        ``generating_now`` is in time: ``observe`` folds a frame into a verdict
        about a *sequence*, and this is the one frame's own answer - the winning
        variant and its scene-local rectangle, or None when that frame did not
        contain the appearance.

        An ERROR frame (the caller's capture failed) is not a frame and leaves
        it alone, exactly as it leaves the streak alone: a dropped capture is
        not evidence that the button went away.

        Purely observational - nothing here reads it back - and it exists so the
        TUI can show the user the pixels that matched (``ElementsPanel``)
        instead of asking them to trust a diff percentage.
        """
        return self._last_sighting

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
        "finished" nor throw away the progress already made toward it. An ERROR
        is not evidence of anything, so ``generating_now`` is False on it too.

        ``generating_now`` is set only on the frames whose own search argues for
        "generating", and for an idle tracker only after the appearance has been
        genuinely seen since the reset (module docstring). Every other frame -
        including the whole grace period a reset opens, where the *verdict* says
        "generating" on no evidence at all - reports it False.
        """
        if scene is None:
            return BusyProbe(BusyState.ERROR, None)
        sighting = self._find(scene)
        self._last_sighting = sighting
        diff = sighting[1].diff if sighting is not None else None
        found = sighting is not None
        if found:
            self._found_since_reset = True
        if found == self._found_is_busy:
            self._streak = 0
            # A busy appearance that is on screen this very frame is proof; an
            # idle appearance that is missing this frame is only proof once we
            # have watched it go.
            return BusyProbe(
                self._generating, diff, self._found_is_busy or self._found_since_reset
            )
        self._streak += 1
        state = self._finished if self._streak >= self._required_ticks else self._generating
        return BusyProbe(state, diff)

    def reset(self) -> None:
        """Forget the streak - and the sighting - as if freshly built.

        No verdict survives this: the next frame decides on its own. For the
        TUI's flow suspension - the auto-copy flow clicks and scrolls the very
        window being watched, so the frames it produces say nothing about
        whether the model is generating - and for the paste and the send, where
        the frames behind the streak are AgentClip's own doing.

        The sighting goes with it because it is a claim about the frames since
        the reset: an idle appearance seen before a paste says nothing about
        whether the one that matters this turn was ever drawn. ``last_sighting``
        goes too, for the display's version of the same reason - a picture of
        where the button was before the reset is not a picture of now.
        """
        self._streak = 0
        self._found_since_reset = False
        self._last_sighting = None

    def fresh(self) -> PresenceTracker:
        """A tracker of this exact configuration that has seen nothing yet.

        The SWAP spelling of :meth:`reset`, for a caller on a different thread
        from the one polling. ``observe`` reads the streak, spends a template
        search, and writes the streak back - so a reset landing in the middle of
        that search is read-modify-written away a frame later, silently undoing
        itself. Replacing the *reference* cannot lose that race: the poll still
        in flight folds its frame into an object nobody will read again, and the
        next one starts from a tracker that genuinely remembers nothing.

        Deliberately this class and not ``type(self)``: what is copied is the
        calibration - the images, the polarity, the thresholds, the matcher -
        and a subclass is free to reset itself in place instead.
        """
        return PresenceTracker(
            self._templates,
            found_is_busy=self._found_is_busy,
            required_ticks=self._required_ticks,
            tolerance=self._tolerance,
            max_diff=self._max_diff,
            matcher=self._matcher,
        )
