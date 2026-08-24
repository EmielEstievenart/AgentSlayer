"""A headless AutomationView and a headless machine, so the automation core can
be driven in microseconds.

The sibling of ``tests/shell/app/conftest.py`` and the same bargain:
:class:`~agentclip.driver.automation.controller.AutomationController` talks to its UI
through exactly one narrow port (:class:`agentclip.driver.automation.view.AutomationView`),
so everything it decides is testable without a terminal, a browser window or a
mouse. The Pilot suites in ``tests/shell/tui/`` stay as the wiring check - that the
real screen is still plugged into this - but the *rules* are asserted here.

:class:`FakeAutomationView` records; it scripts nothing, because nothing on this
port asks a question yet. Everything is a LIST rather than a last-value, because
several of these paints are re-issued unconditionally and "how many times, in
what order" is as much of the contract as "with what".

The other half is :class:`~agentclip.driver.monitor.fake.FakeUIMonitor`, the machine
the controller acts on since docs/design/ui-monitor.md phase 6.1 - and
:func:`feed_probe` below is the ``tick_feed`` seam that replaced the
controller's own ``feed_probe``. Same vocabulary, one more argument: a reading
is now a whole :class:`~agentclip.driver.monitor.protocol.Tick` the monitor pushes,
so feeding one IS a tick completing, and a stamp the monitor has moved past
never reaches the controller at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.harness_log import HarnessEntry
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.view import Severity
from agentclip.driver.monitor.fake import FakeUIMonitor
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


def feed_probe(
    monitor: FakeUIMonitor,
    detector: str,
    probe: object = None,
    generation: int | None = None,
) -> None:
    """One reading, delivered as the tick that carries it.

    The suites' one door onto the consumer, and the same vocabulary the
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
    monitor's local-only frame hook (ui-monitor.md §2.2).
    """
    stamp = monitor.generation if generation is None else generation
    if detector == "elements":
        crops = cast("Mapping[TemplateKind, Sighting | None]", probe)
        monitor.push_frame(crops, generation=stamp)
        return
    if detector == "busy":
        tick = monitor.make_tick(generation=stamp, busy=cast("BusyProbe", probe))
    elif detector == "idle":
        tick = monitor.make_tick(generation=stamp, idle=cast("BusyProbe", probe))
    elif detector == "stale":
        tick = monitor.make_tick(generation=stamp, stale=cast("StaleProbe", probe))
    elif detector == "send_ready":
        if probe is None:
            tick = monitor.make_tick(generation=stamp, captured=False)
        else:
            tick = monitor.make_tick(
                generation=stamp,
                sightings={TemplateKind.SEND_READY: SEND_READY_AT if probe else None},
            )
    else:
        raise ValueError(f"no such detector: {detector!r}")
    monitor.feed(tick)


@pytest.fixture
def monitor() -> FakeUIMonitor:
    return FakeUIMonitor()


@pytest.fixture
def automation(view: FakeAutomationView, monitor: FakeUIMonitor) -> AutomationController:
    return AutomationController(view=view, monitor=monitor)
