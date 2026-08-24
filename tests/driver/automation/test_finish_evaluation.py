"""The finish decision, without a terminal: what fires the auto-copy flow.

The rules asserted here are the ones the shell suites assert through a real app
and a real sidebar. Those suites stay - they are the wiring check, and the only
proof that the screen is still plugged into this - but they pay a whole app boot
per scenario for rules that are pure state. This is the same scenarios against
the automation core directly, in microseconds, with
:class:`FakeAutomationView` in place of a screen.

**Phase 2 moved the subject of this file** (docs/design/ui-monitor.md §6.2).
``evaluate_finish`` is gone; what it decided is
:class:`~agentclip.driver.automation.recipes.reply.ReplyWatch`, folded by the
three recipes that have a reply to wait for (``WAIT_SEND``, ``MANUAL_INSERT``,
``WAIT_GENERATE``). So the shape of a scenario grew one line and lost another:

* say which detectors the monitor would be reporting (``active_detectors``,
  which is a READOUT now - the fold reads the probes a tick actually carries);
* put the loop where a delivery would have left it (``WAIT_SEND``) and open the
  reply gate, because nothing may arm or fire unless an outbound is actually
  waiting for an answer;
* **start the loop**, and then feed probes into it. ``feed_probe``
  (``tests/driver/automation/conftest.py``) waits until the loop is parked in an
  ``await observe()``, pushes the tick the monitor would have pushed, and gives
  the loop the turns it needs to fold it - which is why every test below is
  ``async``: the reading and its consequence are two different event-loop turns.

Everything the fold decided is read back through ``controller.reply``: the send
gate, the per-detector verdicts and the auto-copy arm are that object's fields,
and ``controller.reply is None`` is the new spelling of "no reply is
outstanding". The two streaks are the MONITOR's counts and are read off the tick
it stamped them into (``monitor.latest``).

Covered: the two ways the trigger arms (a busy/idle icon on the frame just
probed - proof; a sustained large delta - inference), the two-consecutive-ticks
rule that fires it, all four exits of the ready-to-send gate, the two refusals
that land on MANUAL_COPY instead of a click, the ghost filter, and the one-shot
fire.

**Two rules this file used to pin are gone with the push**, and their tests went
with them rather than being weakened:

* *the tick-closing invariant* ("only the LAST probe of a tick may decide").
  There is no half-reported tick any more: a :class:`~agentclip.driver.monitor.protocol.Tick`
  carries every probe the configuration ran, so one tick is one fold by
  construction. ``test_one_tick_is_folded_once`` is what replaced it.
* *the ghost filter's NAME check* ("a verdict from a detector that no longer
  exists is dropped"). The monitor stamps a tick with the probes it actually
  took, so a busy reading can only be on a tick that searched for busy; the
  generation stamp covers the other half (a run that has been retargeted away),
  and that half is still pinned below.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

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
from agentclip.driver.automation.recipes import loop as loop_mod
from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.stale import StaleProbe, StaleState

from .conftest import FakeAutomationView, feed_probe, fire_count, parked, settle

# The harvest is stubbed for every test in this file: a scenario about the
# DECISION must not then drive a mouse across an imaginary screen, and the recipe
# that would is the harvest's own (``conftest.stub_harvest``, which is the seam
# ``run_auto_copy_flow(flow=...)`` used to be).
pytestmark = pytest.mark.usefixtures("no_harvest")

# Every controller whose loop this module started, so a parked recipe cannot
# outlive the test that parked it.
_RUNNING: list[AutomationController] = []


@pytest.fixture(autouse=True)
def _stop_loops() -> Iterator[None]:
    yield
    while _RUNNING:
        _RUNNING.pop().stop_loop()


@dataclass
class Harness:
    """One controller, the machine under it, and its view."""

    controller: AutomationController
    monitor: FakeUIMonitor
    view: FakeAutomationView

    @property
    def fires(self) -> int:
        """How many times the harvest has been fired - the transitions INTO
        ``AUTO_COPY``, read off the harness log (``conftest.fires``). Entering
        that state IS the fire since phase 6.2; there is no callback to count."""
        return fire_count(self.controller)

    # -- the probes, in the shape a poll tick pushes them ---------------------

    async def busy(
        self, state: BusyState, *, evidence: bool | None = None, watched: bool = True
    ) -> None:
        """One busy-appearance probe.

        A probe carries two things and conflating them was a shipped bug:
        ``state`` is the DE-BOUNCED verdict, ``generating_now`` is whether that
        frame's own template search actually found the icon. They agree on every
        settled frame, which is why ``evidence`` defaults to the honest reading
        of ``state``; the tests about the disagreement pass it explicitly.
        """
        if evidence is None:
            evidence = state is BusyState.MATCH
        await feed_probe(
            self.monitor, "busy", BusyProbe(state, 0.2, evidence), watched=watched
        )

    async def idle(
        self, state: BusyState, *, evidence: bool | None = None, watched: bool = True
    ) -> None:
        """The same, inverted: for an idle appearance CHANGED is "generating"."""
        if evidence is None:
            evidence = state is BusyState.CHANGED
        await feed_probe(
            self.monitor, "idle", BusyProbe(state, 0.2, evidence), watched=watched
        )

    async def stale(
        self,
        state: StaleState,
        *,
        diff: float = 0.001,
        ticks: int = 0,
        watched: bool = True,
    ) -> None:
        await feed_probe(
            self.monitor, "stale", StaleProbe(state, diff, ticks), watched=watched
        )

    async def send_ready(self, found: bool | None, *, watched: bool = True) -> None:
        await feed_probe(self.monitor, "send_ready", found, watched=watched)

    # -- the sequences the rules are made of ----------------------------------

    async def stale_send(self) -> None:
        """The stale detector watching the user's message actually get sent:
        ``SEND_ARM_TICKS`` consecutive CHANGING probes each over
        ``SEND_ARM_MIN_DIFF``."""
        for _ in range(SEND_ARM_TICKS):
            await self.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)

    @property
    def armed(self) -> bool:
        """Has the trigger seen the model generating? False with no reply
        outstanding at all, which is the honest reading: the watch that would
        hold the arm does not exist."""
        return self.controller.reply is not None and self.controller.reply.copy_armed

    @property
    def gate(self) -> SendGate | None:
        return None if self.controller.reply is None else self.controller.reply.gate


async def build(
    view: FakeAutomationView,
    *,
    detectors: tuple[str, ...] = ("busy",),
    captures: tuple[TemplateKind, ...] = (TemplateKind.COPY,),
    armed: bool = True,
    open_gate: bool = True,
) -> Harness:
    """A controller wired the way a shell wires it, minus the screen, with its
    loop running.

    ``captures`` is what the LIVE window's service has an appearance of, which
    is the only question the fold hands back to the shell - and the one that
    decides whether there is a send gate at all (SEND_READY) and whether a
    finish has anything to click (COPY).

    ``open_gate`` is the session gate under everything: True puts the loop in
    ``WAIT_SEND``, the state a delivery leaves it in, whose recipe is what folds
    a tick. False leaves it in ``IDLE`` - where nothing observes at all, which is
    what "no reply is outstanding" now MEANS.
    """
    monitor = FakeUIMonitor()
    controller = AutomationController(
        view=view,
        monitor=monitor,
        has_appearance=lambda kind: kind in captures,
    )
    controller.active_detectors = detectors
    if not armed:
        controller.set_os_armed(False)
    if open_gate:
        controller.set_loop_state(LoopState.WAIT_SEND, "the payload was pasted into the chat box")
        controller.open_reply_gate()
    controller.start_loop()
    _RUNNING.append(controller)
    await settle(2)
    return Harness(controller, monitor, view)


# -- arming: the two kinds of evidence, and what each is worth ----------------


async def test_a_busy_icon_on_the_probed_frame_arms_the_trigger(
    view: FakeAutomationView,
) -> None:
    """The strongest evidence the monitor produces, and it counts on one frame:
    a reasoning icon on screen is something no still, unanswered chat can fake."""
    h = await build(view)

    await h.busy(BusyState.MATCH)

    assert h.armed is True
    assert h.controller.loop_state is LoopState.WAIT_GENERATE
    assert h.view.logged("auto-copy trigger armed: a busy/idle icon shows the model generating")
    # The send is demonstrably done, so the nag comes down with the arm.
    assert h.view.paste_flash_hides >= 1


async def test_a_settling_default_is_not_a_sighting(view: FakeAutomationView) -> None:
    """``generating_now`` and the verdict disagree for a whole settling window.

    Every paste resets every tracker, and a freshly reset tracker reports
    "generating" with no frame behind it. Arming on that made tick one of every
    single message claim a reasoning icon - which armed the auto-copy against a
    chat nobody had answered yet.
    """
    h = await build(view)

    await h.busy(BusyState.MATCH, evidence=False)

    assert h.armed is False
    assert h.controller.loop_state is not LoopState.WAIT_GENERATE

    await h.busy(BusyState.MATCH, evidence=True)
    assert h.armed is True


async def test_staleness_alone_arms_only_on_a_sustained_large_delta(
    view: FakeAutomationView,
) -> None:
    """A caret blinking in the composer is a CHANGING verdict too.

    So the stale detector's "generating" buys an arm only as a run of
    ``SEND_ARM_TICKS`` probes whose diff clears ``SEND_ARM_MIN_DIFF`` - long
    enough that no repaint fakes it and short enough that no answer outlasts it.
    """
    h = await build(view, detectors=("stale",))

    for _ in range(SEND_ARM_TICKS - 1):
        await h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
        assert h.armed is False  # not sustained yet

    await h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    assert h.armed is True
    assert h.view.logged("sustained large frame deltas in a row")


async def test_a_small_delta_breaks_the_arming_run(view: FakeAutomationView) -> None:
    """"Consecutive" means consecutive: one caret-sized frame restarts it.

    The count itself is the MONITOR's (ui-monitor.md §2.2) and rides in on the
    tick, so what is read back here is the tick it was stamped into.
    """
    h = await build(view, detectors=("stale",))

    for _ in range(SEND_ARM_TICKS - 1):
        await h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    await h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF / 10)
    assert h.monitor.latest is not None
    assert h.monitor.latest.stale_arm_streak == 0

    for _ in range(SEND_ARM_TICKS - 1):
        await h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    assert h.armed is False  # the interruption cost a full restart

    await h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)
    assert h.armed is True


# -- firing: two consecutive ticks, and only once -----------------------------


async def test_the_trigger_fires_on_two_consecutive_finished_ticks(
    view: FakeAutomationView,
) -> None:
    h = await build(view)

    await h.busy(BusyState.MATCH)  # generating -> armed
    await h.busy(BusyState.CHANGED)
    assert h.fires == 0  # one finished tick is not enough

    await h.busy(BusyState.CHANGED)
    assert h.fires == 1
    assert h.controller.loop_state is LoopState.AUTO_COPY
    # Firing IS the harvest: the reply that was outstanding is being collected
    # right now, so the watch that held the arm is gone with it.
    assert h.controller.reply is None


async def test_a_generating_tick_between_two_finished_ones_restarts_the_count(
    view: FakeAutomationView,
) -> None:
    h = await build(view)

    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.MATCH)  # still going after all
    await h.busy(BusyState.CHANGED)
    assert h.fires == 0

    await h.busy(BusyState.CHANGED)
    assert h.fires == 1


async def test_a_capture_error_breaks_the_streak_without_disarming(
    view: FakeAutomationView,
) -> None:
    """One bad frame must not silently cancel an in-flight finish."""
    h = await build(view)

    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.ERROR)
    assert h.fires == 0
    assert h.armed is True  # the arm survived the blind frame

    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.CHANGED)
    assert h.fires == 1


async def test_the_fire_happens_exactly_once_even_on_a_second_finished_tick(
    view: FakeAutomationView,
) -> None:
    """§4.1, and phase 2 makes it structural rather than a flag.

    The loop is ONE asyncio task. The moment it decides, it leaves the state that
    folds ticks and starts the harvest recipe - so from that instant nothing is
    parked in an ``observe()`` and the next tick is not offered to any fold at
    all. A second harvest cannot start until the first recipe returns, which is
    the guarantee ``flow_running`` had to make by hand when the poller pushed.

    The two ticks below are therefore fed at a screen nobody is reading
    (``watched=False``), which is exactly what a real poller does while the flow
    scrolls and clicks.
    """
    h = await build(view)

    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.CHANGED)
    assert h.fires == 1
    assert h.controller.flow_running is True
    # The structural half, and the whole claim: with the one task inside the
    # harvest recipe there is nothing parked in an ``observe`` at all, so the
    # ticks below are not "ignored" - they are not offered to anything.
    assert not h.monitor._waiters  # noqa: SLF001 - the double's own seam

    # Two more finished ticks land while the harvest is still running.
    await h.busy(BusyState.CHANGED, watched=False)
    await h.busy(BusyState.CHANGED, watched=False)
    assert h.fires == 1


async def test_a_tick_from_inside_the_harvest_cannot_fire_again(
    view: FakeAutomationView, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guarantee from the nastiest direction: the harvest itself pushing
    one more finished tick through the monitor - a recipe whose very first act
    scrolls a page the poller is watching - must not double-harvest.

    This is where the fire callback used to be injected. With ``on_fire`` gone
    the injection point is the recipe: the AUTO_COPY entry in the table is the
    fire, so a stub that feeds a finished tick and then parks IS "a tick arriving
    from inside the fire".
    """
    monitor = FakeUIMonitor()

    async def harvest_that_feeds(_ctx: RecipeContext) -> Outcome:
        # Straight into the monitor, from inside the harvest, exactly as a tick
        # that was already in flight would arrive.
        monitor.feed(
            monitor.make_tick(
                busy=BusyProbe(BusyState.CHANGED, 0.2, False), changed_streak=2
            )
        )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setitem(loop_mod.RECIPES, LoopState.AUTO_COPY, harvest_that_feeds)
    controller = AutomationController(
        view=view,
        monitor=monitor,
        has_appearance=lambda kind: kind is TemplateKind.COPY,
    )
    controller.active_detectors = ("busy",)
    controller.set_loop_state(LoopState.WAIT_SEND, "the payload was pasted into the chat box")
    controller.open_reply_gate()
    controller.start_loop()
    _RUNNING.append(controller)
    await settle(2)
    h = Harness(controller, monitor, view)

    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.CHANGED)
    await settle()

    assert h.fires == 1


# -- one tick, one fold --------------------------------------------------------


async def test_one_tick_is_folded_once(view: FakeAutomationView) -> None:
    """What replaced the tick-closing invariant.

    The poller used to push a tick's probes one at a time, in a fixed order, and
    the fold had to wait for the last of them or one detector's word could arm
    while another's was still in flight. A :class:`Tick` carries every probe the
    configuration ran, so that whole class of half-reported tick is gone: the
    recipe folds what it is handed, once, and both detectors are in it.
    """
    h = await build(view, detectors=("busy", "stale"))
    tick = h.monitor.make_tick(
        busy=BusyProbe(BusyState.MATCH, 0.2, True),
        stale=StaleProbe(StaleState.CHANGING, 0.001, 0),
    )

    await parked(h.monitor)
    h.monitor.feed(tick)
    await settle()

    assert h.armed is True
    assert h.controller.reply is not None
    assert h.controller.reply.busy_seen is True
    assert h.controller.reply.stale_seen is True
    # Once, not once per probe: the arm is announced a single time.
    armings = [entry for entry in h.view.log_entries if entry.kind == "trigger"]
    assert len(armings) == 1


async def test_both_detectors_must_agree_before_it_fires(view: FakeAutomationView) -> None:
    """With two running, agreement is the whole point of having the second."""
    h = await build(view, detectors=("busy", "stale"))

    await h.busy(BusyState.MATCH)
    await h.stale(StaleState.CHANGING)
    assert h.armed is True

    for _ in range(3):
        await h.busy(BusyState.CHANGED)
        await h.stale(StaleState.CHANGING)  # stale still says generating
    assert h.fires == 0

    await h.busy(BusyState.CHANGED)
    await h.stale(StaleState.STALE)  # both agree: one tick of the run
    assert h.fires == 0

    await h.busy(BusyState.CHANGED)  # ...and two
    assert h.fires == 1


async def test_a_detector_that_never_reported_neither_vetoes_nor_fakes(
    view: FakeAutomationView,
) -> None:
    """``*_seen`` says which detectors have spoken. One that has produced
    nothing must not hold a finish up - and must not count toward one either."""
    h = await build(view, detectors=("busy",))

    await h.busy(BusyState.MATCH)
    assert h.controller.reply is not None
    assert h.controller.reply.idle_seen is False

    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.CHANGED)

    assert h.fires == 1


# -- the session gate under all of it ------------------------------------------


async def test_nothing_arms_while_no_reply_is_outstanding(view: FakeAutomationView) -> None:
    """Calibration is not consent: a configured but idle tab is watched, and its
    verdicts are a readout and nothing more.

    Which is what the loop's own shape says now - with no reply outstanding it is
    parked in ``IDLE``, where nothing is waiting for a tick, so a tick reaches no
    fold at all.
    """
    h = await build(view, open_gate=False)

    await h.busy(BusyState.MATCH, watched=False)
    await h.busy(BusyState.CHANGED, watched=False)
    await h.busy(BusyState.CHANGED, watched=False)

    assert h.controller.reply is None
    assert h.controller.loop_state is LoopState.IDLE
    assert h.fires == 0
    # ...but the readout ran the whole time.
    assert len(h.view.detection_lines) >= 3


async def test_a_running_harvest_is_never_re_armed_by_its_own_mouse_work(
    view: FakeAutomationView,
) -> None:
    """The flow scrolls and hover-scans the very window the detectors watch, so
    its own mouse work reads as a fresh generation.

    ``flow_running`` used to suspend the fold for exactly that reason. Phase 2
    makes it structural: the loop is in the harvest recipe, nothing is observing,
    and those frames reach no fold. ``end_flow`` still throws them away, so
    polling resumes from a clean post-flow baseline.
    """
    h = await build(view)
    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.CHANGED)
    assert h.fires == 1
    assert h.controller.flow_running is True
    decided = len(h.view.log_entries)

    await h.busy(BusyState.MATCH, watched=False)  # the flow's own scrolling
    await h.busy(BusyState.MATCH, watched=False)

    assert h.fires == 1
    assert len(h.view.log_entries) == decided  # nothing was decided by either

    resets = h.monitor.resets
    h.controller.end_flow()
    assert h.controller.flow_running is False
    assert h.monitor.resets == resets + 1


# -- the two finishes that land on MANUAL_COPY instead of a click --------------


async def test_a_finish_with_no_captured_copy_button_is_the_users_to_harvest(
    view: FakeAutomationView,
) -> None:
    h = await build(view, captures=())

    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.CHANGED)

    assert h.fires == 0
    assert h.controller.loop_state is LoopState.MANUAL_COPY
    assert h.view.logged("no copy button is captured for this service")
    # Display only: the trigger stays exactly as armed as it was, and the reply
    # is still outstanding - the user is the one collecting it.
    assert h.armed is True
    assert h.controller.reply is not None


async def test_a_finish_while_disarmed_is_reached_shown_and_never_clicked(
    view: FakeAutomationView,
) -> None:
    """Detection is not what disarming turns off: the whole decision runs
    against live probes and simply launches nothing."""
    h = await build(view, armed=False)

    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.CHANGED)
    await h.busy(BusyState.CHANGED)

    assert h.fires == 0
    assert h.controller.loop_state is LoopState.MANUAL_COPY
    assert any("disarmed" in message for message, _ in h.view.notifications)
    assert h.armed is True


# -- the ghost filter ----------------------------------------------------------


async def test_a_probe_from_a_finished_poller_run_is_dropped(
    view: FakeAutomationView,
) -> None:
    """A cancelled run still finishes the tick it was in.

    Its verdicts describe the window the automation was driving BEFORE the
    retarget, and letting one in armed the trigger against the new one. The
    monitor drops it before any subscriber - and before any ``observe`` - sees
    it, so the recipe folding ticks never hears about it at all.
    """
    h = await build(view)
    stale_generation = h.controller.detector_generation
    h.monitor.retarget()

    await feed_probe(h.monitor, "busy", BusyProbe(BusyState.MATCH, 0.2, True), stale_generation)

    assert h.controller.reply is not None
    assert h.controller.reply.busy_seen is False
    assert h.armed is False


# -- the ready-to-send gate ----------------------------------------------------


async def test_no_captured_send_button_means_no_gate_at_all(
    view: FakeAutomationView,
) -> None:
    """A capture is a capability, not an instruction: for every service without
    one this is a no-op and the behaviour is what shipped before it existed."""
    h = await build(view, captures=(TemplateKind.COPY,))

    assert h.gate is None
    assert h.view.send_line() == SEND_READY_RESTING


async def test_the_gate_holds_from_the_paste_and_says_so(view: FakeAutomationView) -> None:
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    assert h.gate is SendGate.HOLD
    assert h.view.send_line() == SEND_READY_HOLDING
    assert h.view.logged("holding finish detection until the send is seen")


async def test_a_held_gate_vetoes_every_arm_and_fire(view: FakeAutomationView) -> None:
    """Between the paste and the Enter the chat is STILL, which is exactly what
    the stale detector calls finished - about a message nobody has sent."""
    h = await build(
        view, detectors=("stale",), captures=(TemplateKind.COPY, TemplateKind.SEND_READY)
    )

    await h.stale_send()
    await h.stale(StaleState.STALE)
    await h.stale(StaleState.STALE)

    assert h.fires == 0
    assert h.armed is False
    assert h.controller.loop_state is LoopState.WAIT_SEND  # still waiting for the send


async def test_seen_then_gone_releases_the_gate_and_restarts_detection(
    view: FakeAutomationView,
) -> None:
    """The disappearance IS the user's Enter, and detection starts from THERE:
    the frames the gate held through show an unsent composer, so a streak built
    out of them describes the user typing rather than the model answering.

    Which is a TRACKER reset, on the far side of the seam - the monitor rebuilds
    the debounce and zeroes its own two counts (ui-monitor.md §4.3) - so what is
    asserted here is that the reset happened.
    """
    h = await build(
        view, detectors=("stale",), captures=(TemplateKind.COPY, TemplateKind.SEND_READY)
    )
    await h.stale_send()  # a large-delta run built up under the hold
    resets = h.monitor.resets

    await h.send_ready(True)
    assert h.gate is SendGate.SEEN
    assert h.view.send_line() == SEND_READY_SEEN

    await h.send_ready(False)
    assert h.gate is None
    assert h.view.send_line() == SEND_READY_RELEASED
    assert h.controller.loop_state is LoopState.WAIT_GENERATE
    assert h.monitor.resets == resets + 1
    assert h.view.logged("finish detection is released")


async def test_an_icon_on_the_probed_frame_overrides_the_gate_outright(
    view: FakeAutomationView,
) -> None:
    """Nothing generates a reply to a message that was never sent.

    Deliberately NOT the ordinary release: the very frame doing the releasing is
    a genuine post-send reading of a generating chat, so the verdicts survive
    and the same tick goes straight on to arm the trigger.
    """
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    assert h.gate is SendGate.HOLD

    await h.busy(BusyState.MATCH)

    assert h.gate is None
    assert h.view.send_line() == SEND_READY_OVERRIDDEN
    assert h.armed is True  # the very tick that released it also arms
    assert h.view.logged("gate overridden by better evidence")


async def test_a_stale_generating_verdict_never_overrides_the_gate(
    view: FakeAutomationView,
) -> None:
    """It is only staleness the gate exists to distrust."""
    h = await build(
        view, detectors=("stale",), captures=(TemplateKind.COPY, TemplateKind.SEND_READY)
    )

    for _ in range(SEND_ARM_TICKS + 2):
        await h.stale(StaleState.CHANGING, diff=SEND_ARM_MIN_DIFF)

    assert h.gate is SendGate.HOLD
    assert h.armed is False


async def test_the_hold_phase_gives_up_on_a_button_that_never_appears(
    view: FakeAutomationView,
) -> None:
    """The gate may delay a session; it may never deadlock one."""
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    for _ in range(SEND_GATE_TIMEOUT_TICKS - 1):
        await h.send_ready(False)
    assert h.gate is SendGate.HOLD  # still waiting

    await h.send_ready(False)
    assert h.gate is None
    assert h.view.send_line() == SEND_READY_TIMEOUT
    assert any("never appeared" in message for message, _ in h.view.notifications)


async def test_the_seen_phase_has_its_own_far_longer_clock(view: FakeAutomationView) -> None:
    """A button that matches happily and never stops matching would hold the
    session open forever - but the budget is generous, because what this phase
    waits for is a human reading what is about to go out.

    The clock also RESTARTS at the sighting, so a slow-appearing button does not
    eat the user's reading time.
    """
    assert SEND_GATE_SEEN_TIMEOUT_TICKS > SEND_GATE_TIMEOUT_TICKS * 10
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    for _ in range(SEND_GATE_TIMEOUT_TICKS - 1):
        await h.send_ready(False)
    await h.send_ready(True)
    assert h.gate is SendGate.SEEN
    assert h.controller.reply is not None
    assert h.controller.reply.gate_ticks == 0  # the clock restarted at the sighting

    for _ in range(SEND_GATE_SEEN_TIMEOUT_TICKS - 1):
        await h.send_ready(True)
    assert h.gate is SendGate.SEEN  # the user may take their time

    await h.send_ready(True)
    assert h.gate is None
    assert h.view.send_line() == SEND_READY_STUCK
    assert any("never went away" in message for message, _ in h.view.notifications)


async def test_a_capture_failure_counts_against_the_gates_budget(
    view: FakeAutomationView,
) -> None:
    """A browser that cannot be captured at all must not hold a session open."""
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))

    for _ in range(SEND_GATE_TIMEOUT_TICKS):
        await h.send_ready(None)

    assert h.gate is None


async def test_a_send_probe_from_a_dead_run_cannot_release_the_new_windows_gate(
    view: FakeAutomationView,
) -> None:
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    await h.send_ready(True)
    dead = h.controller.detector_generation
    h.monitor.retarget()

    await feed_probe(h.monitor, "send_ready", False, dead)

    assert h.gate is SendGate.SEEN


async def test_closing_the_reply_gate_drops_the_send_gate_with_it(
    view: FakeAutomationView,
) -> None:
    """The send gate is a phase OF the reply gate - "out but not sent yet" - so
    it can outlive neither the reply it is holding for nor the window that
    reply belongs to."""
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    assert h.gate is SendGate.HOLD

    h.controller.close_reply_gate()

    assert h.controller.reply is None
    assert h.gate is None
    # ...and the line falls back to "captured, waiting for the next paste".
    assert h.view.send_line() == SEND_READY_ARMED


async def test_forgetting_the_verdicts_leaves_the_arm_and_the_reply_gate_alone(
    view: FakeAutomationView,
) -> None:
    """A rebuild (an appearance recaptured, a config adopted) invalidates what
    the detectors SAW, not that the model was generating - and it certainly does
    not un-paste the outbound the gate is holding for."""
    h = await build(view, captures=(TemplateKind.COPY, TemplateKind.SEND_READY))
    await h.busy(BusyState.MATCH)
    assert h.armed is True

    h.controller.forget_verdicts()

    assert h.controller.reply is not None
    assert h.controller.reply.busy_seen is False
    assert h.controller.active_detectors == ()
    assert h.armed is True


async def test_resetting_the_trigger_forgets_the_arm_but_not_the_outstanding_reply(
    view: FakeAutomationView,
) -> None:
    """The slot moving is the case: verdicts describe a window, so carrying them
    across a retarget could fire the auto-copy at the wrong chat."""
    h = await build(view)
    await h.busy(BusyState.MATCH)

    h.controller.reset_finish_trigger()

    assert h.controller.reply is not None
    assert h.armed is False
    assert h.controller.reply.busy_finished is None


# -- the loop's narration ------------------------------------------------------


async def test_the_loop_state_is_written_once_per_real_change(
    view: FakeAutomationView,
) -> None:
    """Repeated evidence must not fill the log with a repeated non-event."""
    h = await build(view)

    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.MATCH)
    await h.busy(BusyState.MATCH)

    assert h.controller.loop_state is LoopState.WAIT_GENERATE
    assert h.view.loop_states.count(LoopState.WAIT_GENERATE) == 1
    transitions = [
        entry
        for entry in h.view.log_entries
        if entry.kind == "state" and "WAIT_GENERATE" in entry.text
    ]
    assert len(transitions) == 1


async def test_every_logged_entry_reaches_the_deque_and_the_view(
    view: FakeAutomationView,
) -> None:
    """The deque is the log; the view is a mirror of it, fed one entry at a
    time so an open pane shows a decision as it is taken."""
    h = await build(view)
    await h.busy(BusyState.MATCH)

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
async def test_each_busy_probe_paints_its_own_line(
    view: FakeAutomationView, state: BusyState, expected: str
) -> None:
    h = await build(view)

    await h.busy(state)

    kind, text = h.view.detection_lines[-1]
    assert kind is TemplateKind.BUSY
    assert text.startswith(expected)


async def test_the_stale_probe_paints_the_line_with_no_appearance_behind_it(
    view: FakeAutomationView,
) -> None:
    h = await build(view, detectors=("stale",))

    await h.stale(StaleState.STALE, ticks=4)

    assert h.view.stale_lines[-1] == "○ response ready · stale (still ×4)"


async def test_recognised_crops_are_routed_through_but_never_read(
    view: FakeAutomationView,
) -> None:
    """The controller's whole share of the ELEMENTS column is the ghost check:
    a crop is sized for whatever renderer will draw it, so nothing here knows
    what one is."""
    h = await build(view)
    crops: dict[TemplateKind, object] = {TemplateKind.COPY: object()}

    await feed_probe(h.monitor, "elements", crops)
    assert h.view.element_paints == [crops]

    await feed_probe(h.monitor, "elements", crops, h.controller.detector_generation - 1)
    assert len(h.view.element_paints) == 1  # a dead run's pictures are not this window's
