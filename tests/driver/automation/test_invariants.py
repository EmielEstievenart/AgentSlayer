"""The eight invariants of docs/design/ui-monitor.md §4, each with a test that
fails if it regresses.

§4 opens with why this file exists: those rules "are load-bearing today and easy
to lose in the control-flow inversion of phase 2". The inversion has now
happened - the poller no longer pushes into a consumer, one asyncio task pulls
(``recipes/loop.py``) - so this is the file that says the rules survived it.

Deliberately one test per invariant and no more: the RULES are asserted in the
suites next door (``test_finish_evaluation``, ``test_delivery``,
``test_auto_copy_flow``, and the recorded narration in
``test_harness_log_scenarios``). What is here is the handful of structural facts
those suites all quietly depend on, several of which no ordinary scenario would
notice breaking - a lock reappearing, a recipe reaching past the seam for a
tracker, a paint leaving the loop's own thread.

Three of the eight are enforced by READING THE SOURCE rather than by running it
(§4.3, §4.4 and half of §4.7). That is on purpose: they are statements about what
the code may not CONTAIN, and a behavioural test for "nobody took a lock" passes
just as happily when somebody takes one and gets away with it.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import threading
from collections.abc import Iterator

import pytest

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.recipes import loop as loop_mod
from agentclip.driver.automation.recipes.transitions import TRANSITIONS
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.protocol import Located, UIMonitor
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot

from .conftest import FakeAutomationView, feed_probe, fire_count, settle

pytestmark = pytest.mark.usefixtures("no_harvest")

# The two packages this file reads. ``AUTOMATION`` is the brain: no threads, no
# locks, no pixels. ``RECIPES`` is the half of it that talks to the machine.
AUTOMATION = pathlib.Path(__file__).resolve().parents[3] / "src" / "agentclip" / "driver" / "automation"
RECIPES = AUTOMATION / "recipes"

CHAT_REGION = ScreenRegion(100, 100, 800, 600)
CHAT_BOX = ScreenRegion(200, 600, 400, 40)

# Everything a monitor can be asked to DO to the machine, as the verb names the
# double records. Reading is not doing: ``observe``, ``locate``, ``find_all`` and
# the clipboard READ answer questions about a screen the monitor is watching
# anyway, and a disarmed brain is still allowed to look. ``write_clipboard`` is
# not here for the same reason it happens above the armed check in the delivery:
# putting the payload where the user can paste it themselves is precisely what a
# disarmed AgentClip is for.
ACTION_VERBS = frozenset(
    {
        "click",
        "click_element",
        "move_cursor",
        "scroll",
        "scroll_key",
        "send_paste",
        "send_enter",
        "focus_window",
        "hover_scan",
        "snap_to_bottom",
    }
)


def _build(
    view: FakeAutomationView,
    monitor: FakeUIMonitor,
    *,
    armed: bool = True,
    captures: tuple[TemplateKind, ...] = (TemplateKind.COPY,),
) -> AutomationController:
    """A controller with a drawn chat window, wired the way a shell wires one."""
    controller = AutomationController(
        view=view, monitor=monitor, has_appearance=lambda kind: kind in captures
    )
    controller.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    if not armed:
        controller.set_os_armed(False)
    return controller


@pytest.fixture
def loops() -> Iterator[list[AutomationController]]:
    """Every controller whose loop a test started, stopped afterwards - a parked
    recipe may not outlive the test that parked it."""
    started: list[AutomationController] = []
    yield started
    for controller in started:
        controller.stop_loop()


def _run(controller: AutomationController, started: list[AutomationController]) -> None:
    controller.start_loop()
    started.append(controller)


# -- §4.1 the fire is one-shot -------------------------------------------------


async def test_the_fire_is_one_shot_however_many_finished_ticks_arrive(
    view: FakeAutomationView, monitor: FakeUIMonitor, loops: list[AutomationController]
) -> None:
    """§4.1. The old shape set ``_flow_running`` before calling ``on_fire`` inside
    the tick lock; the new one does not need a flag - or a callback - to be safe,
    because the loop is ONE task: the ``WAIT_GENERATE`` recipe returns exactly one
    outcome, and nothing else is running to return a second.

    The fire is counted off the harness log (``conftest.fire_count``), because
    entering ``AUTO_COPY`` is now the whole of what a fire IS.

    Driven the way a real over-eager screen would drive it: the model finishes,
    and then keeps looking finished. Every one of those later ticks lands on a
    loop that is inside the harvest, so it reaches no fold at all.
    """
    controller = AutomationController(
        view=view,
        monitor=monitor,
        has_appearance=lambda kind: kind is TemplateKind.COPY,
    )
    controller.set_loop_state(LoopState.WAIT_SEND, "the payload was pasted into the chat box")
    controller.open_reply_gate()
    _run(controller, loops)

    await feed_probe(monitor, "busy", BusyProbe(BusyState.MATCH, 0.2, True))
    await feed_probe(monitor, "busy", BusyProbe(BusyState.CHANGED, 0.2, False))
    await feed_probe(monitor, "busy", BusyProbe(BusyState.CHANGED, 0.2, False))
    assert controller.loop_state is LoopState.AUTO_COPY
    assert fire_count(controller) == 1

    # ...and now three more ticks that say exactly the same thing.
    for _ in range(3):
        monitor.feed(monitor.make_tick(busy=BusyProbe(BusyState.CHANGED, 0.2, False)))
        await settle()

    assert fire_count(controller) == 1


# -- §4.2 ghost ticks are dropped ---------------------------------------------


async def test_a_tick_from_a_dead_run_never_reaches_a_recipe(
    view: FakeAutomationView, monitor: FakeUIMonitor, loops: list[AutomationController]
) -> None:
    """§4.2. The drop itself is the monitor's (``tests/driver/monitor``); what is
    pinned here is that the brain relies on it and does not check a stamp of its
    own - so a ghost has to reach nobody, not merely be ignored by somebody.

    A retarget is what makes a tick a ghost, and it is exactly the moment the
    automation changed windows: an in-flight "the model is generating" from the
    window we have just left must not arm a trigger against the window we have
    just arrived at.
    """
    controller = _build(view, monitor)
    controller.set_loop_state(LoopState.WAIT_SEND, "the payload was pasted into the chat box")
    controller.open_reply_gate()
    _run(controller, loops)

    dead = controller.detector_generation
    monitor.retarget()
    await feed_probe(monitor, "busy", BusyProbe(BusyState.MATCH, 0.2, True), dead)

    assert monitor.ghosts, "the double should have refused the stale stamp"
    assert controller.reply is not None
    assert controller.reply.copy_armed is False
    assert controller.loop_state is LoopState.WAIT_SEND


# -- §4.3 trackers are swapped, not mutated ------------------------------------


def _monitor_attrs(path: pathlib.Path) -> set[str]:
    """Every ``ctx.monitor.<name>`` the module reaches for."""
    used: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "monitor"
        ):
            used.add(node.attr)
    return used


def test_a_recipe_reaches_the_monitor_only_through_the_wire_able_contract() -> None:
    """§4.3, pinned where it can actually be lost.

    The swap discipline itself lives in the monitor and is tested there: a
    tracker being reset is being POLLED at the same time, so an in-place clear
    landing inside a search is undone by the write that follows it. What the
    brain has to promise is that it never touches one - and the way to promise
    that is to reach the machine only through the contract that has no trackers
    on it. A recipe that could name ``busy_tracker`` could clear it.

    So: every attribute any recipe reads off ``ctx.monitor`` must be a member of
    the ``UIMonitor`` Protocol - the half that will cross a socket in phase 5 -
    and none of them may be from the local-only tier.
    """
    # ``spec`` is the one addition to the Protocol's own listing: it is the
    # configuration read back, and it is answered on THIS side of the wire -
    # ``SwitchableMonitor`` remembers what it last configured, precisely so the
    # send gate's tick budgets stay readable when the machine is a socket away.
    contract = {name for name in dir(UIMonitor) if not name.startswith("_")} | {"spec"}
    local_only = {
        "ops",
        "detector",
        "capture",
        "on_frame",
        "busy_tracker",
        "idle_tracker",
        "stale_tracker",
        "reset_trackers",
        "self_writes",
        "poller",
        "stamp",
        "feed",
    }
    for path in sorted(RECIPES.glob("*.py")):
        used = _monitor_attrs(path)
        assert used <= contract, f"{path.name} reaches past the UIMonitor contract: {used - contract}"
        assert not (used & local_only), f"{path.name} touches the local-only tier"


# -- §4.4 / §4.7 no lock, and no thread to need one ---------------------------


def test_the_brain_holds_no_lock_and_owns_no_tick_thread() -> None:
    """§4.4, and the half of §4.7 that is a fact about the code.

    The whole point of the inversion: the loop pulls, so every writer in this
    layer is on the event-loop thread and there is nothing left to serialize.
    ``_tick_lock`` is gone, and with it the last reason ``driver/automation``
    imported ``threading`` for anything the tick path touches.

    One carve-out, and it is unrelated to a tick: ``alerts.py`` runs the audible
    "your move" alarm on a thread of its own, because a repeating tone is a sleep
    loop and the event loop may not sleep. It holds nothing the loop reads and
    takes no lock, which is the distinction this test draws.
    """
    for path in sorted(AUTOMATION.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if path.name == "alerts.py":
            # ...and the carve-out is only a carve-out while the alarm stays a
            # LEAF: it may import nothing from this package, so nothing on the
            # tick path can reach the thread or the lock behind it.
            assert "driver.automation" not in source
            continue
        assert "import threading" not in source, f"{path.name} imports threading"
        assert "Lock(" not in source, f"{path.name} takes a lock"


# -- §4.5 os_armed gates every action -----------------------------------------


async def test_a_disarmed_brain_never_asks_the_machine_to_do_anything(
    view: FakeAutomationView, loops: list[AutomationController]
) -> None:
    """§4.5. Disarmed stops the ACTING, and it stops it HERE - the refusal is
    made before the verb, not by a monitor that was asked and said no.

    The delivery is the test because it is the sequence with the most ways to
    touch a machine: a focus click, an activation poll, a synthetic Ctrl+V, an
    Enter tap, a snap back. Disarmed, the payload still reaches the clipboard -
    that is what a disarmed AgentClip is FOR - and nothing else happens at all.
    """
    monitor = FakeUIMonitor()
    monitor.answers["locate"] = Located(CHAT_BOX, False, None)
    controller = _build(view, monitor, armed=False)
    _run(controller, loops)

    await controller.copy_outbound("===CLIP:BEGIN=== hello ===CLIP:END===")
    await settle()

    asked = {verb for verb, _args in monitor.calls}
    assert not (asked & ACTION_VERBS), f"a disarmed brain asked for {asked & ACTION_VERBS}"
    assert monitor.written == ["===CLIP:BEGIN=== hello ===CLIP:END==="]
    assert controller.loop_state is LoopState.MANUAL_INSERT


# -- §4.6 no blind paste -------------------------------------------------------


async def test_nothing_is_pasted_into_a_chat_box_nobody_verified(
    view: FakeAutomationView, loops: list[AutomationController]
) -> None:
    """§4.6. A payload goes into a box a capture actually matched, or it goes
    nowhere - and "nowhere" means no click either, because the middle of the
    user's drawn window is the TRANSCRIPT: a click there selects a word of an old
    response or follows a link, and the synthetic Ctrl+V lands wherever that left
    the caret.

    The screen here is the ordinary way this happens: the chat box is simply not
    on screen (a page mid-transition, a dialog over it, a drifted capture), so
    ``locate`` answers with no region and the whole OS half is refused.
    """
    monitor = FakeUIMonitor()  # ``locate`` answers "not on screen" by default
    controller = _build(view, monitor)
    _run(controller, loops)

    await controller.copy_outbound("===CLIP:BEGIN=== hello ===CLIP:END===")
    await settle()

    asked = {verb for verb, _args in monitor.calls}
    assert "click" not in asked
    assert "send_paste" not in asked
    assert controller.loop_state is LoopState.MANUAL_INSERT
    assert any("not found on screen" in message for message, _ in view.notifications)


# -- §4.7 the paint contract ---------------------------------------------------


class ThreadRecordingView(FakeAutomationView):
    """A view that also remembers WHO painted: which thread, and which task."""

    def __init__(self) -> None:
        super().__init__()
        self.painters: list[tuple[int, object]] = []

    def paint_loop_state(self, state: LoopState) -> None:
        super().paint_loop_state(state)
        self.painters.append((threading.get_ident(), asyncio.current_task()))


async def test_every_paint_a_tick_causes_comes_from_the_loop_task(
    monitor: FakeUIMonitor, loops: list[AutomationController]
) -> None:
    """§4.7. The port's contract (may be called off the event loop, must not
    block) is unchanged and still honoured - but after phase 2 the paints that
    matter are made by the LOOP, on the event-loop thread, which is strictly
    safer than what the poller thread used to do.

    Pinned as "which task painted", not merely "which thread": a paint made from
    a task the controller does not own would be a second writer of the rail, and
    that is the thing the one-task rule exists to forbid.
    """
    view = ThreadRecordingView()
    controller = _build(view, monitor)
    controller.set_loop_state(LoopState.WAIT_SEND, "the payload was pasted into the chat box")
    controller.open_reply_gate()
    _run(controller, loops)
    # The shell's own move, made before the loop had anything to say.
    shell_paints = len(view.painters)

    await feed_probe(monitor, "busy", BusyProbe(BusyState.MATCH, 0.2, True))

    caused = view.painters[shell_paints:]
    assert caused, "the arm should have moved the rail"
    for ident, task in caused:
        assert ident == threading.get_ident()
        assert task is controller._loop_task  # noqa: SLF001 - the point of the test


# -- §4.8 harness parity -------------------------------------------------------


def test_the_recorded_scenarios_are_still_pinned_line_by_line() -> None:
    """§4.8. ``test_harness_log_scenarios.py`` is the diff-check: six recorded
    scenarios, 39 literal harness lines, generated before any of the split landed
    and unchanged by it.

    This test guards the guard. The pins are only worth anything while they are
    LITERALS - a refactor that quietly relaxed one into a substring probe, or
    dropped a scenario, would leave a file that still passes and no longer checks
    the narration. So the shape is asserted: six scenarios, and 39 strings pinned
    by equality between them.
    """
    path = pathlib.Path(__file__).with_name("test_harness_log_scenarios.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scenarios = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_")
    ]
    pinned = [
        element
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.ops[0], ast.Eq)
        and isinstance(node.comparators[0], ast.List)
        for element in node.comparators[0].elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    ]
    assert len(scenarios) == 6
    assert len(pinned) == 39


# -- the table itself ----------------------------------------------------------


def test_every_outcome_a_recipe_can_return_has_a_row() -> None:
    """Not one of the eight, but the thing they all stand on: the loop looks a
    transition up and would raise if it were missing.

    Totality is checked from the OTHER side - every row must be reachable from
    the state it starts in - because a table with a row nobody can produce is a
    table that has drifted from the recipes it describes.
    """
    for (state, _outcome), after in TRANSITIONS.items():
        assert state in loop_mod.RECIPES, f"{state} has a row but no recipe"
        assert after in loop_mod.RECIPES, f"{after} is a destination with no recipe"
    assert set(loop_mod.RECIPES) == set(LoopState)
