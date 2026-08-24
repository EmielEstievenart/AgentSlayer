"""A headless AutomationView, a headless machine, and the one way to drive them.

The sibling of ``tests/shell/app/conftest.py`` and the same bargain:
:class:`~agentclip.driver.automation.controller.AutomationController` talks to its UI
through exactly one narrow port (:class:`agentclip.driver.automation.view.AutomationView`),
so everything it decides is testable without a terminal, a browser window or a
mouse. The Pilot suites in ``tests/shell/`` stay as the wiring check - that the
real screen is still plugged into this - but the *rules* are asserted here.

:class:`FakeAutomationView` records; it scripts nothing, because nothing on this
port asks a question yet. Everything is a LIST rather than a last-value, because
several of these paints are re-issued unconditionally and "how many times, in
what order" is as much of the contract as "with what".

The other half is :class:`~agentclip.driver.monitor.fake.FakeUIMonitor`, the machine
the controller acts on.

**Since phase 6.2 the tick is PULLED, so a suite has to run the loop.**
``feed_probe`` was a push into the controller's own consumer; there is no
consumer any more. What there is instead is one asyncio task
(``recipes/loop.py``) parked on ``await monitor.observe()``, so feeding a reading
now means three things in order: wait until the loop is actually parked in an
``observe`` (``parked``), push the tick the monitor would have pushed, and give
the loop the turns it needs to fold it and take whatever transition it earns
(``settle``). That is what the coroutine below does, and it is why every suite
that drives readings is now ``async`` - the reading and its consequence are two
different event-loop turns, and asserting between them would assert on a decision
nobody has taken yet.

The helper also carries the two STREAKS. They are ``Tick`` fields the monitor
counts (ui-monitor.md 2.2), not brain state, so a scenario that drives with
probes has to stamp them the way a real poll tick would - which is what
:class:`_Streaks` below does, by calling the monitor's own
:func:`~agentclip.driver.monitor.verdicts.roll_arm_streak` /
:func:`~agentclip.driver.monitor.verdicts.roll_changed_streak` rather than a
second copy of the arithmetic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import cast

import pytest

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.finish import SEND_ARM_MIN_DIFF
from agentclip.driver.automation.harness_log import KIND_STATE, HarnessEntry
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.recipes import loop as loop_mod
from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.automation.view import Severity
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.verdicts import roll_arm_streak, roll_changed_streak
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.detector import Sighting
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.stale import StaleProbe

# Where a "found" send button is said to be. Nothing under test reads the
# rectangle - the gate is about presence - so one constant does for every tick.
SEND_READY_AT = ScreenRegion(1400, 900, 40, 40)


class FakeAutomationView:
    """Structural ``AutomationView`` that records. No Textual, no screen, no OS."""

    def __init__(self) -> None:
        # Every paint_armed argument, in order - the list, not a flag, because
        # the port is repainted unconditionally and "how many times" is as much
        # of the contract as "with what".
        self.armed_paints: list[bool] = []
        self.notifications: list[tuple[str, Severity]] = []
        self.loop_states: list[LoopState] = []
        self.log_entries: list[HarnessEntry] = []
        # (kind, text) per DETECTION line written, and the stale line's own
        # stream: they are two calls on the port because the stale detector has
        # no appearance behind it.
        self.detection_lines: list[tuple[TemplateKind, str]] = []
        self.stale_lines: list[str] = []
        self.element_paints: list[Mapping[TemplateKind, object]] = []
        self.paste_flashes: list[tuple[str, bool]] = []
        self.paste_flash_hides = 0

    def paint_armed(self, armed: bool) -> None:
        self.armed_paints.append(armed)

    def paint_loop_state(self, state: LoopState) -> None:
        self.loop_states.append(state)

    def paint_harness_entry(self, entry: HarnessEntry) -> None:
        self.log_entries.append(entry)

    def paint_detection(self, kind: TemplateKind, text: str) -> None:
        self.detection_lines.append((kind, text))

    def paint_stale(self, text: str) -> None:
        self.stale_lines.append(text)

    def paint_elements(self, crops: Mapping[TemplateKind, object]) -> None:
        self.element_paints.append(crops)

    def show_paste_flash(self, text: str, *, retry: bool = False) -> None:
        self.paste_flashes.append((text, retry))

    def hide_paste_flash(self) -> None:
        self.paste_flash_hides += 1

    def notify(
        self,
        message: str,
        *,
        severity: Severity = "information",
        timeout: float | None = None,
    ) -> None:
        self.notifications.append((message, severity))

    # -- readers the assertions phrase themselves in --------------------------

    def send_line(self) -> str:
        """The last thing written to the ready-to-send line."""
        for kind, text in reversed(self.detection_lines):
            if kind is TemplateKind.SEND_READY:
                return text
        return ""

    def logged(self, needle: str) -> bool:
        """Did any harness entry say this?"""
        return any(needle in entry.text for entry in self.log_entries)


@pytest.fixture
def view() -> FakeAutomationView:
    return FakeAutomationView()


# == driving the loop =========================================================


async def parked(monitor: FakeUIMonitor, *, turns: int = 500) -> None:
    """Wait until the loop is parked in an ``await monitor.observe()``.

    The one synchronisation a pulling loop needs: a tick fed while no recipe is
    waiting for one is a tick nobody was listening for, and the suite would
    assert on a decision that was never offered. The double keeps its pending
    ``observe`` futures in ``_waiters``; there is no public reader for them
    because nothing in ``src`` needs one.
    """
    for _ in range(turns):
        if monitor._waiters:  # noqa: SLF001 - the double's own seam
            return
        await asyncio.sleep(0)
    raise AssertionError("the loop never reached `await observe()`")


async def settle(turns: int = 20) -> None:
    """Give the loop the turns it needs to fold a tick and move.

    Deliberately a fixed number of yields rather than a sleep: everything under
    test is synchronous once the tick lands (a fold, a log line, a transition, the
    next recipe starting), so what this buys is event-loop turns and never wall
    clock.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


@dataclass
class _Streaks:
    """One monitor's running counts, as the real one keeps them.

    The suites feed ONE probe per tick, which no real poll tick ever is: the
    poller captures a frame and hands every active detector's reading over
    together. The counts are folded over the tick as a WHOLE, so this keeps the
    last reading each detector gave and rolls against that - which reconstructs
    the tick the monitor would have pushed without making every scenario spell
    out three probes to say one thing.

    Both resets are mirrored too, because both are what a scenario is really
    saying when it calls them: a ``configure`` (the generation moving) and a
    ``reset_trackers`` are declarations that the screen behind those counts is a
    different screen, so the run restarts at zero
    (``LocalUIMonitor.reset_trackers``, ui-monitor.md 4.3).
    """

    arm: int = 0
    changed: int = 0
    generation: int = 0
    resets: int = 0
    # The last probe each detector reported, and therefore which detectors have
    # reported at all - the ``active_detectors`` a fold over this tick sees.
    busy: BusyProbe | None = None
    idle: BusyProbe | None = None
    stale: StaleProbe | None = None

    def roll(self, monitor: FakeUIMonitor, detector: str, probe: object) -> tuple[int, int]:
        """Fold one reading in and hand back the two counts to stamp."""
        if monitor.generation != self.generation or monitor.resets != self.resets:
            self.arm = self.changed = 0
            self.generation, self.resets = monitor.generation, monitor.resets
        if detector == "busy":
            self.busy = cast("BusyProbe", probe)
        elif detector == "idle":
            self.idle = cast("BusyProbe", probe)
        elif detector == "stale":
            self.stale = cast("StaleProbe", probe)
        active = tuple(
            name
            for name, seen in (
                ("busy", self.busy),
                ("idle", self.idle),
                ("stale", self.stale),
            )
            if seen is not None
        )
        min_diff = SEND_ARM_MIN_DIFF if monitor.spec is None else monitor.spec.send_arm_min_diff
        self.arm = roll_arm_streak(self.arm, self.stale, min_diff=min_diff)
        self.changed = roll_changed_streak(
            self.changed,
            busy=self.busy,
            idle=self.idle,
            stale=self.stale,
            active_detectors=active,
        )
        return self.arm, self.changed


# One per monitor, keyed by identity: a suite builds several controllers in one
# test and each drives its own machine, so the counts may not be shared.
_STREAKS: dict[int, _Streaks] = {}


async def feed_probe(
    monitor: FakeUIMonitor,
    detector: str,
    probe: object = None,
    generation: int | None = None,
    *,
    watched: bool = True,
) -> None:
    """One reading, delivered as the tick that carries it, into a running loop.

    The suites' one door onto the recipes, and the same vocabulary the
    controller's own ``feed_probe`` took: the three finish detectors by name,
    plus the two readings that are not finish detectors at all. The stamp
    defaults to the LIVE one, because "speak as the current run" is what nearly
    every caller wants; pass it explicitly to speak as a run that has been
    retargeted away, and the monitor drops it as a ghost exactly as it would
    drop a real one.

    ``send_ready`` is three-valued and each value is a different SHAPE of tick:
    found is a sighting with a rectangle, not-found is a sighting mapped to
    ``None`` (searched, not on screen), and "no answer" is a tick that captured
    nothing at all - which is what a failed capture is, and the only way a tick
    can say it (``Tick.sightings`` is empty when there was no frame to search).

    ``elements`` is not on the tick at all: crops are pixels, so they ride the
    monitor's local-only frame hook (ui-monitor.md 2.2) and need no loop.

    Every tick carries the two STREAKS as well, rolled by :class:`_Streaks` from
    this reading and the ones before it. A GHOST rolls nothing: the real monitor
    refuses to advance a count from a run it has already retargeted away from,
    because letting a dead screen's evidence onto the live screen's tally is the
    bug the generation stamp exists for.

    ``watched=False`` says the loop is deliberately NOT waiting for a tick - it is
    parked in ``IDLE``, or in one of the two states where the next move is the
    user's - so the tick is pushed at a screen nobody is reading. That is a
    scenario in its own right (nothing may arm or fire while no reply is
    outstanding) and the only way to write it without hanging on an ``observe``
    that is never going to happen.
    """
    stamp = monitor.generation if generation is None else generation
    if detector == "elements":
        crops = cast("Mapping[TemplateKind, Sighting | None]", probe)
        monitor.push_frame(crops, generation=stamp)
        await settle(2)
        return
    # A ``send_ready`` reading folds into NEITHER count: it is not a finish
    # detector, and on a real tick it rides beside the probes rather than
    # arriving as a tick of its own - so the counts simply carry over.
    streaks = _STREAKS.setdefault(id(monitor), _Streaks(generation=monitor.generation))
    rolls = stamp == monitor.generation and detector != "send_ready"
    arm, changed = (
        streaks.roll(monitor, detector, probe) if rolls else (streaks.arm, streaks.changed)
    )
    counts = {"stale_arm_streak": arm, "changed_streak": changed}
    if detector == "busy":
        tick = monitor.make_tick(generation=stamp, busy=cast("BusyProbe", probe), **counts)
    elif detector == "idle":
        tick = monitor.make_tick(generation=stamp, idle=cast("BusyProbe", probe), **counts)
    elif detector == "stale":
        tick = monitor.make_tick(generation=stamp, stale=cast("StaleProbe", probe), **counts)
    elif detector == "send_ready":
        if probe is None:
            tick = monitor.make_tick(generation=stamp, captured=False, **counts)
        else:
            tick = monitor.make_tick(
                generation=stamp,
                sightings={TemplateKind.SEND_READY: SEND_READY_AT if probe else None},
                **counts,
            )
    else:
        raise ValueError(f"no such detector: {detector!r}")
    if watched:
        await parked(monitor)
    monitor.feed(tick)
    await settle()


async def stub_harvest(_ctx: RecipeContext) -> Outcome:
    """The AUTO_COPY recipe as a suite about the DECISION wants it: nothing at all.

    The seam ``run_auto_copy_flow(flow=...)`` used to be, one delegation above the
    real body: a scenario that is pinning how the loop REACHES the harvest must
    not then drive a mouse across an imaginary screen, and the recipe that does
    that is the harvest's own. Parks rather than returning an outcome, so the loop
    stays in ``AUTO_COPY`` and the story ends where the scenario ends.
    """
    await asyncio.Event().wait()
    raise AssertionError("unreachable")  # pragma: no cover


def fire_count(controller: AutomationController) -> int:
    """How many harvests this loop has started, counted off the harness log.

    There is no fire CALLBACK any more (ui-monitor.md §6.2): entering
    ``AUTO_COPY`` IS the fire, and a transition cannot reach the rail without
    reaching the log (``harness_log``), so the log is the honest register of how
    often it happened. Reading it back rather than recording it at a seam is also
    what keeps the count true for a fire nobody wired anything to.
    """
    move = f"→ {LoopState.AUTO_COPY.name} — "
    return sum(1 for e in controller.harness_log if e.kind == KIND_STATE and move in e.text)


@pytest.fixture
def no_harvest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put :func:`stub_harvest` in the recipe table for the length of one test."""
    monkeypatch.setitem(loop_mod.RECIPES, LoopState.AUTO_COPY, stub_harvest)


@pytest.fixture
def monitor() -> FakeUIMonitor:
    return FakeUIMonitor()


@pytest.fixture
def automation(view: FakeAutomationView, monitor: FakeUIMonitor) -> AutomationController:
    return AutomationController(view=view, monitor=monitor)


@pytest.fixture
def running(automation: AutomationController) -> Iterator[AutomationController]:
    """The same controller with its loop task running - and stopped afterwards,
    so a parked recipe cannot outlive the test that parked it."""
    automation.start_loop()
    yield automation
    automation.stop_loop()
