"""The finish decision, without a terminal: what fires the auto-copy flow.

The rules asserted here are the ones ``tests/shell/tui/test_finish_signal_ui.py`` and
``tests/shell/tui/test_stale_detector_ui.py`` assert through a real Textual app and a
real sidebar. Those suites stay - they are the wiring check, and the only proof
that the screen is still plugged into this object - but they pay a whole app
boot per scenario for rules that are pure state. This is the same scenarios
against :class:`~agentclip.driver.automation.controller.AutomationController` directly,
in microseconds, with :class:`FakeAutomationView` in place of a screen.

The shape of a scenario, three lines every time: say which detectors the poller
would be reporting (``active_detectors`` - the fixed busy -> idle -> stale build
order, whose LAST entry closes a tick), open the reply gate (nothing may arm or
fire unless an outbound is actually waiting for an answer), then feed probes.
``feed_probe`` is the seam the poll loop's own ``consume_*`` calls sit behind -
same call, same thread rules, stamped with the live run unless a test says
otherwise - so feeding a probe IS a tick completing.

Covered: the two ways the trigger arms (a busy/idle icon on the frame just
probed - proof; a sustained large delta - inference), the two-consecutive-ticks
rule that fires it, the tick-closing invariant that stops a half-reported tick
deciding anything, all four exits of the ready-to-send gate, the two refusals
that land on MANUAL_COPY instead of a click, the ghost filter, and the
re-entrancy guard that makes a fire one-shot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.finish import (
    SEND_ARM_MIN_DIFF,
    SEND_ARM_TICKS,
    SEND_GATE_SEEN_TIMEOUT_TICKS,
    SEND_GATE_TIMEOUT_TICKS,
    SEND_READY_ARMED,
    SEND_READY_HOLDING,
    SEND_READY_OVERRIDDEN,
    SEND_READY_RELEASED,
    SEND_READY_RESTING,
    SEND_READY_SEEN,
    SEND_READY_STUCK,
    SEND_READY_TIMEOUT,
    SendGate,
)
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.stale import StaleProbe, StaleState

from .conftest import FakeAutomationView


@dataclass
class Harness:
    """One controller, its view, and what it fired at."""

    controller: AutomationController
    view: FakeAutomationView
    fires: list[None] = field(default_factory=list)

    # -- the probes, in the shape a poll tick pushes them ---------------------

    def busy(self, state: BusyState, *, evidence: bool | None = None) -> None:
        """One busy-appearance probe.

        A probe carries two things and conflating them was a shipped bug:
        ``state`` is the DE-BOUNCED verdict, ``generating_now`` is whether that
        frame's own template search actually found the icon. They agree on every
        settled frame, which is why ``evidence`` defaults to the honest reading
        of ``state``; the tests about the disagreement pass it explicitly.
        """
        if evidence is None:
            evidence = state is BusyState.MATCH
        self.controller.feed_probe("busy", BusyProbe(state, 0.2, evidence))

    def idle(self, state: BusyState, *, evidence: bool | None = None) -> None:
        """The same, inverted: for an idle appearance CHANGED is "generating"."""
        if evidence is None:
            evidence = state is BusyState.CHANGED
        self.controller.feed_probe("idle", BusyProbe(state, 0.2, evidence))

    def stale(self, state: StaleState, *, diff: float = 0.001, ticks: int = 0) -> None:
        self.controller.feed_probe("stale", StaleProbe(state, diff, ticks))

    def send_ready(self, found: bool | None) -> None:
        self.controller.feed_probe("send_ready", found)

    # -- the sequences the rules are made of ----------------------------------

    def stale_send(self) -> None:
        """The stale detector watching the user's message actually get sent:
        ``SEND_ARM_TICKS`` consecutive CHANGING probes each over
        ``SEND_ARM_MIN_DIFF``."""
        for _ in range(SEND_ARM_TICKS):
            self.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)


def build(
    view: FakeAutomationView,
    *,
    detectors: tuple[str, ...] = ("busy",),
    captures: tuple[TemplateKind, ...] = (TemplateKind.COPY,),
    armed: bool = True,
    open_gate: bool = True,
) -> Harness:
    """A controller wired the way the screen wires it, minus the screen.

    ``captures`` is what the LIVE window's service has an appearance of, which
    is the only question the fold hands back to the shell - and the one that
    decides whether there is a send gate at all (SEND_READY) and whether a
    finish has anything to click (COPY).
    """
    fires: list[None] = []
    controller = AutomationController(
        view=view,
        has_appearance=lambda kind: kind in captures,
        on_fire=lambda: fires.append(None),
    )
    controller.active_detectors = detectors
    if not armed:
        controller.set_os_armed(False)
    if open_gate:
        controller.open_reply_gate()
    return Harness(controller, view, fires)


# -- arming: the two kinds of evidence, and what each is worth ----------------


def test_a_busy_icon_on_the_probed_frame_arms_the_trigger(view: FakeAutomationView) -> None:
    """The strongest evidence the poller produces, and it counts on one frame:
    a reasoning icon on screen is something no still, unanswered chat can fake."""
    h = build(view)

    h.busy(BusyState.MATCH)

    assert h.controller.copy_armed is True
    assert h.controller.loop_state is LoopState.WAIT_GENERATE
    assert h.view.logged("auto-copy trigger armed: a busy/idle icon shows the model generating")
    # The send is demonstrably done, so the nag comes down with the arm.
    assert h.view.paste_flash_hides >= 1


def test_a_settling_default_is_not_a_sighting(view: FakeAutomationView) -> None:
    """``generating_now`` and the verdict disagree for a whole settling window.

    Every paste resets every tracker, and a freshly reset tracker reports
    "generating" with no frame behind it. Arming on that made tick one of every
    single message claim a reasoning icon - which armed the auto-copy against a
    chat nobody had answered yet.
    """
    h = build(view)

    h.busy(BusyState.MATCH, evidence=False)

    assert h.controller.copy_armed is False
    assert h.controller.loop_state is not LoopState.WAIT_GENERATE

    h.busy(BusyState.MATCH, evidence=True)
    assert h.controller.copy_armed is True


def test_staleness_alone_arms_only_on_a_sustained_large_delta(view: FakeAutomationView) -> None:
    """A caret blinking in the composer is a CHANGING verdict too.

    So the stale detector's "generating" buys an arm only as a run of
    ``SEND_ARM_TICKS`` probes whose diff clears ``SEND_ARM_MIN_DIFF`` - long
    enough that no repaint fakes it and short enough that no answer outlasts it.
    """
    h = build(view, detectors=("stale",))

    for _ in range(SEND_ARM_TICKS - 1):
        h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
        assert h.controller.copy_armed is False  # not sustained yet

    h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    assert h.controller.copy_armed is True
    assert h.view.logged("sustained large frame deltas in a row")


def test_a_small_delta_breaks_the_arming_run(view: FakeAutomationView) -> None:
    """"Consecutive" means consecutive: one caret-sized frame restarts it."""
    h = build(view, detectors=("stale",))

    for _ in range(SEND_ARM_TICKS - 1):
        h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF / 10)
    assert h.controller.stale_arm_streak == 0

    for _ in range(SEND_ARM_TICKS - 1):
        h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    assert h.controller.copy_armed is False  # the interruption cost a full restart

    h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    assert h.controller.copy_armed is True


# -- firing: two consecutive ticks, and only once -----------------------------


def test_the_trigger_fires_on_two_consecutive_finished_ticks(view: FakeAutomationView) -> None:
    h = build(view)

    h.busy(BusyState.MATCH)  # generating -> armed
    h.busy(BusyState.CHANGED)
    assert h.fires == []  # one finished tick is not enough

    h.busy(BusyState.CHANGED)
    assert h.fires == [None]
    assert h.controller.loop_state is LoopState.AUTO_COPY
    # Firing IS the harvest: the arm is spent and no reply is outstanding.
    assert h.controller.copy_armed is False
    assert h.controller.awaiting_pasted_reply is False


def test_a_generating_tick_between_two_finished_ones_restarts_the_count(
    view: FakeAutomationView,
) -> None:
    h = build(view)

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.MATCH)  # still going after all
    h.busy(BusyState.CHANGED)
    assert h.fires == []

    h.busy(BusyState.CHANGED)
    assert h.fires == [None]


def test_a_capture_error_breaks_the_streak_without_disarming(view: FakeAutomationView) -> None:
    """One bad frame must not silently cancel an in-flight finish."""
    h = build(view)

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.ERROR)
    assert h.fires == []
    assert h.controller.copy_armed is True  # the arm survived the blind frame

    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)
    assert h.fires == [None]


def test_the_fire_happens_exactly_once_even_on_a_second_finished_tick(
    view: FakeAutomationView,
) -> None:
    """``flow_running`` is set SYNCHRONOUSLY, inside the decision.

    ``on_fire`` schedules work rather than doing it, so the next poll tick can
    arrive before the flow has done anything at all - and it arrives at a chat
    whose detectors have been saying "finished" for two ticks running. Without
    the flag being up by the time this method returns, that tick fires a second
    harvest: two clicks on one copy button, and the reply ingested twice.

    Slice 5b widened that window rather than closing it: the decision is taken
    on the poller thread now, so a shell's ``on_fire`` cannot even start its
    work - it hands the fire to its UI thread and returns, and the poller ticks
    on meanwhile. Which is exactly the callback this test hands in.
    """
    h = build(view)

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)
    assert h.fires == [None]
    assert h.controller.flow_running is True

    # Two more finished ticks land while the flow is still starting up.
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)
    assert h.fires == [None]


def test_a_re_entrant_tick_from_inside_the_fire_cannot_fire_again(
    view: FakeAutomationView,
) -> None:
    """The same guarantee from the nastiest direction: the callback itself
    evaluating again (a shell that repaints, pumps its loop and lets one more
    probe through before returning) must not double-harvest."""
    fires: list[None] = []
    controller = AutomationController(
        view=view,
        has_appearance=lambda kind: kind is TemplateKind.COPY,
        on_fire=lambda: (fires.append(None), controller.evaluate_finish())[0],
    )
    controller.active_detectors = ("busy",)
    controller.open_reply_gate()
    h = Harness(controller, view, fires)

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)

    assert fires == [None]


# -- the tick-closing invariant ------------------------------------------------


def test_a_partial_tick_decides_nothing(view: FakeAutomationView) -> None:
    """The fold runs once per tick, on the CLOSING probe.

    With busy and stale both running, a busy probe is the FIRST half of a tick:
    the stale reading of that same frame is still on its way. Evaluating there
    would let one detector's word arm or fire while the other's is in flight -
    which is the whole reason the poller pushes in a fixed order and the fold
    reads the build order's last entry.
    """
    h = build(view, detectors=("busy", "stale"))

    h.busy(BusyState.MATCH)
    assert h.controller.copy_armed is False  # busy alone closes nothing

    h.stale(StaleState.CHANGING)
    assert h.controller.copy_armed is True  # the tick closed, and busy's icon armed it


def test_both_detectors_must_agree_before_it_fires(view: FakeAutomationView) -> None:
    """With two running, agreement is the whole point of having the second."""
    h = build(view, detectors=("busy", "stale"))

    h.busy(BusyState.MATCH)
    h.stale(StaleState.CHANGING)
    assert h.controller.copy_armed is True

    for _ in range(3):
        h.busy(BusyState.CHANGED)
        h.stale(StaleState.CHANGING)  # stale still says generating
    assert h.fires == []

    h.busy(BusyState.CHANGED)
    h.stale(StaleState.STALE)
    h.busy(BusyState.CHANGED)
    h.stale(StaleState.STALE)
    assert h.fires == [None]


def test_a_detector_that_never_reported_neither_vetoes_nor_fakes(
    view: FakeAutomationView,
) -> None:
    """``active_detectors`` says which detectors EXIST; ``*_seen`` says which
    have spoken. An idle detector in the build order that has produced nothing
    yet must not hold a finish up - and must not count toward one either."""
    h = build(view, detectors=("busy",))

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)

    assert h.fires == [None]
    assert h.controller.idle_seen is False


# -- the session gate under all of it ------------------------------------------


def test_nothing_arms_while_no_reply_is_outstanding(view: FakeAutomationView) -> None:
    """Calibration is not consent: a configured but idle tab is watched, and
    its verdicts are a readout and nothing more."""
    h = build(view, open_gate=False)

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)

    assert h.controller.copy_armed is False
    assert h.fires == []
    # ...but the readout ran the whole time.
    assert len(h.view.detection_lines) >= 3


def test_the_flow_suspension_stops_the_flow_re_firing_itself(view: FakeAutomationView) -> None:
    """The flow scrolls and hover-scans the very window the detectors watch, so
    its own mouse work reads as a fresh generation. ``end_flow`` lifts the
    suspension and throws those frames away."""
    h = build(view)
    h.controller.flow_running = True

    h.busy(BusyState.MATCH)
    assert h.controller.copy_armed is False  # nothing was even armed

    h.controller.end_flow()
    assert h.controller.stale_arm_streak == 0
    h.busy(BusyState.MATCH)
    assert h.controller.copy_armed is True


# -- the two finishes that land on MANUAL_COPY instead of a click --------------


def test_a_finish_with_no_captured_copy_button_is_the_users_to_harvest(
    view: FakeAutomationView,
) -> None:
    h = build(view, captures=())

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)

    assert h.fires == []
    assert h.controller.loop_state is LoopState.MANUAL_COPY
    assert h.view.logged("no copy button is captured for this service")
    # Display only: the trigger stays exactly as armed as it was.
    assert h.controller.copy_armed is True
    assert h.controller.awaiting_pasted_reply is True


def test_a_finish_while_disarmed_is_reached_shown_and_never_clicked(
    view: FakeAutomationView,
) -> None:
    """Detection is not what disarming turns off: the whole decision runs
    against live probes and simply launches nothing."""
    h = build(view, armed=False)

    h.busy(BusyState.MATCH)
    h.busy(BusyState.CHANGED)
    h.busy(BusyState.CHANGED)

    assert h.fires == []
    assert h.controller.loop_state is LoopState.MANUAL_COPY
    assert any("disarmed" in message for message, _ in h.view.notifications)
    assert h.controller.copy_armed is True


# -- the ghost filter ----------------------------------------------------------


def test_a_probe_from_a_finished_poller_run_is_dropped(view: FakeAutomationView) -> None:
    """A cancelled loop still finishes the tick it was in.

    Its verdicts describe the window the automation was driving BEFORE the
    retarget, and letting one in armed the trigger against the new one.
    """
    h = build(view)
    stale_generation = h.controller.detector_generation
    h.controller.retarget_detectors()
    h.controller.active_detectors = ("busy",)

    h.controller.consume_busy_probe(BusyProbe(BusyState.MATCH, 0.2, True), stale_generation)

    assert h.controller.busy_seen is False
    assert h.controller.copy_armed is False


def test_a_verdict_from_a_detector_that_no_longer_exists_is_dropped(
    view: FakeAutomationView,
) -> None:
    """The older half of the filter: a smaller detector set leaves verdicts
    about a detector nothing runs any more, and a leaked "generating" one
    re-arms the trigger every tick, wedging auto-copy shut."""
    h = build(view, detectors=("stale",))

    h.busy(BusyState.MATCH)

    assert h.controller.busy_seen is False
    assert h.controller.copy_armed is False


# -- the ready-to-send gate ----------------------------------------------------


def test_no_captured_send_button_means_no_gate_at_all(view: FakeAutomationView) -> None:
    """A capture is a capability, not an instruction: for every service without
    one this is a no-op and the behaviour is what shipped before it existed."""
    h = build(view, captures=(TemplateKind.COPY,))

    assert h.controller.send_gate is None
    assert h.view.send_line() == SEND_READY_RESTING


def test_the_gate_holds_from_the_paste_and_says_so(view: FakeAutomationView) -> None:
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    assert h.controller.send_gate is SendGate.HOLD
    assert h.view.send_line() == SEND_READY_HOLDING
    assert h.view.logged("holding finish detection until the send is seen")


def test_a_held_gate_vetoes_every_arm_and_fire(view: FakeAutomationView) -> None:
    """Between the paste and the Enter the chat is STILL, which is exactly what
    the stale detector calls finished - about a message nobody has sent."""
    h = build(view, detectors=("stale",), captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    h.stale_send()
    h.stale(StaleState.STALE)
    h.stale(StaleState.STALE)

    assert h.fires == []
    assert h.controller.copy_armed is False
    assert h.controller.stale_arm_streak == 0  # the large-delta run does not advance either


def test_seen_then_gone_releases_the_gate_and_restarts_detection(
    view: FakeAutomationView,
) -> None:
    """The disappearance IS the user's Enter, and detection starts from THERE:
    the frames the gate held through show an unsent composer, so a streak built
    out of them describes the user typing rather than the model answering."""
    h = build(view, detectors=("stale",), captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    h.stale_send()  # a large-delta run built up under the hold

    h.send_ready(True)
    assert h.controller.send_gate is SendGate.SEEN
    assert h.view.send_line() == SEND_READY_SEEN

    h.send_ready(False)
    assert h.controller.send_gate is None
    assert h.view.send_line() == SEND_READY_RELEASED
    assert h.controller.loop_state is LoopState.WAIT_GENERATE
    assert h.controller.stale_arm_streak == 0
    assert h.view.logged("finish detection is released")


def test_an_icon_on_the_probed_frame_overrides_the_gate_outright(
    view: FakeAutomationView,
) -> None:
    """Nothing generates a reply to a message that was never sent.

    Deliberately NOT the ordinary release: the very frame doing the releasing is
    a genuine post-send reading of a generating chat, so the verdicts survive
    and the same tick goes straight on to arm the trigger.
    """
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    assert h.controller.send_gate is SendGate.HOLD

    h.busy(BusyState.MATCH)

    assert h.controller.send_gate is None
    assert h.view.send_line() == SEND_READY_OVERRIDDEN
    assert h.controller.copy_armed is True  # the very tick that released it also arms
    assert h.view.logged("gate overridden by better evidence")


def test_a_stale_generating_verdict_never_overrides_the_gate(view: FakeAutomationView) -> None:
    """It is only staleness the gate exists to distrust."""
    h = build(view, detectors=("stale",), captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    for _ in range(SEND_ARM_TICKS + 2):
        h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)

    assert h.controller.send_gate is SendGate.HOLD
    assert h.controller.copy_armed is False


def test_the_hold_phase_gives_up_on_a_button_that_never_appears(
    view: FakeAutomationView,
) -> None:
    """The gate may delay a session; it may never deadlock one."""
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    for _ in range(SEND_GATE_TIMEOUT_TICKS - 1):
        h.send_ready(False)
    assert h.controller.send_gate is SendGate.HOLD  # still waiting

    h.send_ready(False)
    assert h.controller.send_gate is None
    assert h.view.send_line() == SEND_READY_TIMEOUT
    assert any("never appeared" in message for message, _ in h.view.notifications)


def test_the_seen_phase_has_its_own_far_longer_clock(view: FakeAutomationView) -> None:
    """A button that matches happily and never stops matching would hold the
    session open forever - but the budget is generous, because what this phase
    waits for is a human reading what is about to go out.

    The clock also RESTARTS at the sighting, so a slow-appearing button does not
    eat the user's reading time.
    """
    assert SEND_GATE_SEEN_TIMEOUT_TICKS > SEND_GATE_TIMEOUT_TICKS * 10
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    for _ in range(SEND_GATE_TIMEOUT_TICKS - 1):
        h.send_ready(False)
    h.send_ready(True)
    assert h.controller.send_gate is SendGate.SEEN
    assert h.controller.send_gate_ticks == 0  # the clock restarted at the sighting

    for _ in range(SEND_GATE_SEEN_TIMEOUT_TICKS - 1):
        h.send_ready(True)
    assert h.controller.send_gate is SendGate.SEEN  # the user may take their time

    h.send_ready(True)
    assert h.controller.send_gate is None
    assert h.view.send_line() == SEND_READY_STUCK
    assert any("never went away" in message for message, _ in h.view.notifications)


def test_a_capture_failure_counts_against_the_gates_budget(view: FakeAutomationView) -> None:
    """A browser that cannot be captured at all must not hold a session open."""
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    for _ in range(SEND_GATE_TIMEOUT_TICKS):
        h.send_ready(None)

    assert h.controller.send_gate is None


def test_a_send_probe_from_a_dead_run_cannot_release_the_new_windows_gate(
    view: FakeAutomationView,
) -> None:
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    h.send_ready(True)
    dead = h.controller.detector_generation
    h.controller.retarget_detectors()

    h.controller.feed_probe("send_ready", False, dead)

    assert h.controller.send_gate is SendGate.SEEN


def test_closing_the_reply_gate_drops_the_send_gate_with_it(view: FakeAutomationView) -> None:
    """The send gate is a phase OF the reply gate - "out but not sent yet" - so
    it can outlive neither the reply it is holding for nor the window that
    reply belongs to."""
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    assert h.controller.send_gate is SendGate.HOLD

    h.controller.close_reply_gate()

    assert h.controller.send_gate is None
    assert h.controller.send_gate_ticks == 0
    assert h.controller.awaiting_pasted_reply is False
    # ...and the line falls back to "captured, waiting for the next paste".
    assert h.view.send_line() == SEND_READY_ARMED


def test_forgetting_the_verdicts_leaves_the_arm_and_the_reply_gate_alone(
    view: FakeAutomationView,
) -> None:
    """A rebuild (an appearance recaptured, a config adopted) invalidates what
    the detectors SAW, not that the model was generating - and it certainly does
    not un-paste the outbound the gate is holding for."""
    h = build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    h.busy(BusyState.MATCH)
    assert h.controller.copy_armed is True

    h.controller.forget_verdicts()

    assert h.controller.busy_seen is False
    assert h.controller.active_detectors == ()
    assert h.controller.copy_armed is True
    assert h.controller.awaiting_pasted_reply is True


def test_resetting_the_trigger_forgets_the_arm_but_not_the_outstanding_reply(
    view: FakeAutomationView,
) -> None:
    """The slot moving is the case: verdicts describe a window, so carrying them
    across a retarget could fire the auto-copy at the wrong chat."""
    h = build(view)
    h.busy(BusyState.MATCH)

    h.controller.reset_finish_trigger()

    assert h.controller.copy_armed is False
    assert h.controller.busy_finished is None
    assert h.controller.awaiting_pasted_reply is True


# -- the loop's narration ------------------------------------------------------


def test_the_loop_state_is_written_once_per_real_change(view: FakeAutomationView) -> None:
    """Repeated evidence must not fill the log with a repeated non-event."""
    h = build(view)

    h.busy(BusyState.MATCH)
    h.busy(BusyState.MATCH)
    h.busy(BusyState.MATCH)

    assert h.controller.loop_state is LoopState.WAIT_GENERATE
    assert h.view.loop_states.count(LoopState.WAIT_GENERATE) == 1
    transitions = [entry for entry in h.view.log_entries if entry.kind == "state"]
    assert len(transitions) == 1
    assert "WAIT_GENERATE" in transitions[0].text


def test_every_logged_entry_reaches_the_deque_and_the_view(view: FakeAutomationView) -> None:
    """The deque is the log; the view is a mirror of it, fed one entry at a
    time so an open pane shows a decision as it is taken."""
    h = build(view)
    h.busy(BusyState.MATCH)

    assert [entry.text for entry in h.controller.harness_log] == [
        entry.text for entry in h.view.log_entries
    ]
    assert h.controller.harness_log


# -- the readout ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (BusyState.MATCH, "● GENERATING"),
        (BusyState.CHANGED, "○ response ready"),
        (BusyState.ERROR, "✗ capture failed"),
    ],
)
def test_each_busy_probe_paints_its_own_line(
    view: FakeAutomationView, state: BusyState, expected: str
) -> None:
    h = build(view)

    h.busy(state)

    kind, text = h.view.detection_lines[-1]
    assert kind is TemplateKind.BUSY
    assert text.startswith(expected)


def test_the_stale_probe_paints_the_line_with_no_appearance_behind_it(
    view: FakeAutomationView,
) -> None:
    h = build(view, detectors=("stale",))

    h.stale(StaleState.STALE, ticks=4)

    assert h.view.stale_lines[-1] == "○ response ready · stale (still ×4)"


def test_recognised_crops_are_routed_through_but_never_read(view: FakeAutomationView) -> None:
    """The controller's whole share of the ELEMENTS column is the ghost check:
    a crop is sized for whatever renderer will draw it, so nothing here knows
    what one is."""
    h = build(view)
    crops: dict[TemplateKind, object] = {TemplateKind.COPY: object()}

    h.controller.feed_probe("elements", crops)
    assert h.view.element_paints == [crops]

    h.controller.feed_probe("elements", crops, h.controller.detector_generation - 1)
    assert len(h.view.element_paints) == 1  # a dead run's pictures are not this window's
