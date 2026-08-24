"""The two counts a tick carries: the stale-arm run, and the agreement run.

Until phase 2 these were ``AutomationController`` fields, rolled inside
``evaluate_finish`` as it read each probe. docs/design/ui-monitor.md §2.2 puts
them on the tick, and §2.9 says why it has to be the monitor that counts: a
brain that kept its own running totals would lose them on every reconnect, and
would have to be told which ticks it had already counted.

Two layers, and they are deliberately separate files' worth of concern in one:

* the arithmetic (:func:`roll_arm_streak`, :func:`roll_changed_streak`) - pure,
  exhaustive, no screen and no thread. Every rule about what breaks a run is
  asserted here, because a rule asserted through a poll thread is a rule
  asserted once.
* the plumbing - that a real poll loop rolls them forward tick by tick, stamps
  each tick with the count as of ITSELF, and zeroes both on the two events that
  say "the screen these counts describe is gone": a retarget and a tracker
  reset.

What is NOT here, because it is not the monitor's: what a run of two is worth.
Whether an auto-copy may fire on it, whether the trigger is armed, whether a
send gate is holding - all policy, all the brain's (§2.3).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agentclip.driver.monitor.verdicts import roll_arm_streak, roll_changed_streak
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.stale import StaleProbe, StaleState

from .conftest import (
    REGION,
    TIMEOUT_S,
    ScriptedDetector,
    Wiring,
    await_until,
    snapshot,
    spec,
)

# The send-gate threshold every case below is measured against; ``conftest.spec``
# hands the monitor the same number.
MIN_DIFF = 0.02


def changing(diff: float) -> StaleProbe:
    """The response region moved by ``diff`` of its sampled pixels - the
    "generating" verdict, and the only one an arm run is built out of."""
    return StaleProbe(StaleState.CHANGING, diff, 0)


STALE = StaleProbe(StaleState.STALE, 0.0, 3)
STALE_ERROR = StaleProbe(StaleState.ERROR, None, 0)
BUSY_FINISHED = BusyProbe(BusyState.CHANGED, 0.4)  # calibrated while generating
BUSY_GENERATING = BusyProbe(BusyState.MATCH, 0.0, generating_now=True)
IDLE_FINISHED = BusyProbe(BusyState.MATCH, 0.0)  # calibrated while idle: inverted
IDLE_GENERATING = BusyProbe(BusyState.CHANGED, 0.4)
BUSY_ERROR = BusyProbe(BusyState.ERROR, None)


# == the arm run ===============================================================


def test_the_arm_run_climbs_while_the_change_stays_big() -> None:
    """Sustained is the whole requirement: one big frame is a page loading, three
    in a row is a model answering."""
    streak = 0
    for expected in (1, 2, 3):
        streak = roll_arm_streak(streak, changing(0.5), min_diff=MIN_DIFF)
        assert streak == expected


@pytest.mark.parametrize(
    ("what", "probe"),
    [
        ("a small change - a caret blinking, a hover tint", changing(MIN_DIFF / 2)),
        ("the region going still", STALE),
        ("a capture that failed", STALE_ERROR),
    ],
)
def test_anything_but_a_big_change_breaks_the_arm_run(what: str, probe: StaleProbe) -> None:
    """The bug these constants exist to close: arming on a blinking caret between
    AgentClip's paste and the user's Enter made the still, reply-less pre-Enter
    screen read as a finished response and fired the auto-copy at nothing."""
    assert roll_arm_streak(7, probe, min_diff=MIN_DIFF) == 0, what


def test_a_change_exactly_at_the_threshold_counts() -> None:
    """``>=``, not ``>``: the number is a floor the configuration names, and a
    reading that lands exactly on it has met it."""
    assert roll_arm_streak(0, changing(MIN_DIFF), min_diff=MIN_DIFF) == 1


def test_a_tick_with_no_stale_probe_leaves_the_run_where_it_is() -> None:
    """The one asymmetry worth spelling out, and the roll ``evaluate_finish``
    made under ``if self._stale_seen``: the run is a property of the STALE
    detector, so a configuration that does not run it has no run to break."""
    assert roll_arm_streak(3, None, min_diff=MIN_DIFF) == 3


def test_a_changing_probe_with_no_diff_at_all_is_not_evidence() -> None:
    """No number is not a big number. A probe can report CHANGING with nothing
    measured (the first frame after a reset), and that must not arm anything."""
    assert roll_arm_streak(2, StaleProbe(StaleState.CHANGING, None, 0), min_diff=MIN_DIFF) == 0


# == the agreement run =========================================================


def test_the_agreement_run_counts_only_when_every_active_detector_says_finished() -> None:
    """The agreement a second detector exists for. One dissenting verdict is the
    whole of the veto - and it does not matter which detector dissents."""
    both = {"active_detectors": ("busy", "stale")}
    assert roll_changed_streak(0, busy=BUSY_FINISHED, idle=None, stale=STALE, **both) == 1
    assert roll_changed_streak(1, busy=BUSY_FINISHED, idle=None, stale=STALE, **both) == 2
    assert roll_changed_streak(2, busy=BUSY_GENERATING, idle=None, stale=STALE, **both) == 0
    assert roll_changed_streak(2, busy=BUSY_FINISHED, idle=None, stale=changing(0.5), **both) == 0


def test_the_idle_detectors_polarity_is_the_inverse_of_the_busy_ones() -> None:
    """A MATCH means opposite things to the two of them, which is exactly why
    the verdicts are functions and not a comparison at the call site."""
    only_idle = {"busy": None, "stale": None, "active_detectors": ("idle",)}
    assert roll_changed_streak(0, idle=IDLE_FINISHED, **only_idle) == 1
    assert roll_changed_streak(4, idle=IDLE_GENERATING, **only_idle) == 0


def test_a_detector_this_configuration_does_not_run_has_no_vote() -> None:
    """Its probe is not on the tick to begin with, but the guard is the
    ACTIVE list: a service whose checklist ticks only staleness must not have a
    stray busy reading veto - or carry - its finish."""
    kept = roll_changed_streak(
        1, busy=BUSY_GENERATING, idle=None, stale=STALE, active_detectors=("stale",)
    )
    assert kept == 2


def test_a_detector_that_has_not_reported_yet_has_no_vote_either() -> None:
    """Configured but silent (the settling window after a reset) is not the same
    as voting "generating" - it is not voting."""
    silent_busy = roll_changed_streak(
        1, busy=None, idle=None, stale=STALE, active_detectors=("busy", "stale")
    )
    assert silent_busy == 2


def test_a_capture_error_breaks_the_run() -> None:
    """No verdict is not a finished verdict. Biasing away from "finished" on no
    evidence is the only thing such a tick may do."""
    assert (
        roll_changed_streak(3, busy=BUSY_ERROR, idle=None, stale=STALE, active_detectors=("busy",))
        == 0
    )


def test_a_tick_nobody_voted_on_breaks_the_run() -> None:
    """Nothing configured, nothing reported: vacuous agreement is not agreement,
    and counting it would let an unwatched screen accumulate a finish."""
    assert roll_changed_streak(5, busy=None, idle=None, stale=None, active_detectors=()) == 0


# == the plumbing ==============================================================


def _stale_run(probe: StaleProbe) -> ScriptedDetector:
    """A detector that reports the same stale probe on every tick, so the run of
    ticks IS the run under test."""
    return ScriptedDetector(snapshot(stale=probe), active_detectors=("stale",))


async def _quiet(wiring: Wiring) -> None:
    """Stop the run and join its thread, so what follows is the only thing
    touching the counts. A suspend, not a close: the spec and the trackers are
    the subject and have to survive."""
    poller = wiring.monitor.poller
    await wiring.monitor.suspend()
    if poller is not None:
        poller.thread.join(TIMEOUT_S)
        assert not poller.thread.is_alive()


async def test_every_tick_carries_the_counts_as_of_itself(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The plumbing claim, through a real poll thread: the counts roll forward
    tick by tick, and a tick reports the run INCLUDING itself.

    Asserted over the ordered list of ticks rather than over a live counter,
    because that ordering is deterministic no matter how the thread is
    scheduled - which is the only way to test a counter another thread is
    advancing.
    """
    wiring = wire()
    compose_with(_stale_run(changing(0.5)))
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: len(wiring.ticks) >= 3, "three ticks")
    await _quiet(wiring)

    assert [tick.stale_arm_streak for tick in wiring.ticks[:3]] == [1, 2, 3]
    # ...and the other count is not the same count: a CHANGING stale probe is
    # the "generating" verdict, so nothing has agreed on anything.
    assert [tick.changed_streak for tick in wiring.ticks[:3]] == [0, 0, 0]


async def test_the_agreement_run_rides_the_tick_too(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    wiring = wire()
    compose_with(_stale_run(STALE))
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: len(wiring.ticks) >= 3, "three ticks")
    await _quiet(wiring)

    assert [tick.changed_streak for tick in wiring.ticks[:3]] == [1, 2, 3]
    assert [tick.stale_arm_streak for tick in wiring.ticks[:3]] == [0, 0, 0]


async def test_a_retarget_restarts_both_counts(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """A streak is a claim about one screen. Retargeting says the screen is a
    different screen, so both claims expire with the generation that made them -
    the same rule that makes a leftover tick a ghost (§4.2)."""
    wiring = wire()
    compose_with(_stale_run(changing(0.5)))
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: wiring.monitor.stale_arm_streak >= 2, "a run building")

    await wiring.monitor.configure(spec(region=None))
    await _quiet(wiring)

    assert wiring.monitor.stale_arm_streak == 0
    assert wiring.monitor.changed_streak == 0


async def test_the_first_tick_of_a_new_run_starts_the_count_over(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The same fact from the tick's side: nothing the previous window's ticks
    counted may show up on the new window's first one."""
    wiring = wire()
    compose_with(_stale_run(changing(0.5)))
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: len(wiring.ticks) >= 2, "a run building")

    generation = await wiring.monitor.configure(spec(region=REGION))
    await await_until(
        lambda: any(tick.generation == generation for tick in wiring.ticks), "the new run's ticks"
    )
    await _quiet(wiring)

    fresh = [tick for tick in wiring.ticks if tick.generation == generation]
    assert fresh[0].stale_arm_streak == 1


async def test_a_tracker_reset_restarts_both_counts(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """The counts go with the debounce, by the same argument (§4.3): they are
    counts of what those trackers said about frames the CALLER has just produced
    itself - a paste, a send, the auto-copy flow's own scrolling. A count that
    survived would be the flow's own mouse work, still being counted as the
    model's."""
    wiring = wire()
    compose_with(_stale_run(changing(0.5)))
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: wiring.monitor.stale_arm_streak >= 2, "a run building")
    await _quiet(wiring)  # ...so the reset is the last thing that touches them

    wiring.monitor.reset_trackers()

    assert wiring.monitor.stale_arm_streak == 0
    assert wiring.monitor.changed_streak == 0


async def test_a_reset_with_nothing_configured_still_clears_the_counts(
    wire: Callable[..., Wiring],
) -> None:
    """The flows call ``reset_trackers`` unconditionally, and a monitor with no
    trackers at all is an ordinary state - so the clear may not hang off finding
    one to swap."""
    wiring = wire()
    wiring.monitor.reset_trackers()

    assert wiring.monitor.stale_arm_streak == 0
    assert wiring.monitor.changed_streak == 0


async def test_a_ghost_run_never_advances_the_live_counts(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """A poll thread outlives the configure that ended it by up to one tick, and
    that tick is a ghost. Letting it roll a count on its way out would put a dead
    screen's evidence on the live screen's tally - which is the ONE thing a ghost
    could still do after ``_deliver`` drops it."""
    wiring = wire()
    detector = ScriptedDetector(snapshot(stale=changing(0.5)), active_detectors=("stale",))
    compose_with(detector)
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: wiring.monitor.stale_arm_streak >= 1, "a run starting")
    old_poller = wiring.monitor.poller
    assert old_poller is not None

    # Retarget onto a window with nothing to watch: the count is zeroed, and the
    # old thread is still running with the old stamp in its hand.
    await wiring.monitor.configure(spec(region=None))
    old_poller.thread.join(TIMEOUT_S)

    assert wiring.monitor.stale_arm_streak == 0
    assert wiring.monitor.changed_streak == 0


async def test_the_counts_survive_a_suspend(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """A suspend is not a retarget: the capture overlay is up and NOTHING has
    moved, so the run the interrupted loop was building is still an honest
    reading of the same window - deliberately no bump, deliberately no clear."""
    wiring = wire()
    compose_with(_stale_run(changing(0.5)))
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: wiring.monitor.stale_arm_streak >= 2, "a run building")
    await _quiet(wiring)

    assert wiring.monitor.stale_arm_streak >= 2


async def test_a_detector_with_no_screen_never_starts_a_count(
    wire: Callable[..., Wiring], compose_with: Callable[[object], None]
) -> None:
    """A capture that fails reaches every detector as the same ERROR, and an
    error is not evidence of anything - neither a big change nor a finish."""
    wiring = wire()
    compose_with(
        ScriptedDetector(snapshot(captured=False, stale=STALE_ERROR), active_detectors=("stale",))
    )
    await wiring.monitor.configure(spec(region=REGION))
    await await_until(lambda: len(wiring.ticks) >= 3, "three ticks")
    await _quiet(wiring)

    assert [tick.stale_arm_streak for tick in wiring.ticks[:3]] == [0, 0, 0]
    assert [tick.changed_streak for tick in wiring.ticks[:3]] == [0, 0, 0]


def test_a_hand_written_tick_still_costs_one_line(
    wire: Callable[..., Wiring],
) -> None:
    """The default that keeps every existing scenario buildable: a tick nobody
    said anything about carries no run at all."""
    wiring = wire()
    tick = wiring.monitor.stamp()

    assert tick.stale_arm_streak == 0
    assert tick.changed_streak == 0
    assert wiring.monitor.stamp(stale_arm_streak=4, changed_streak=2).changed_streak == 2
