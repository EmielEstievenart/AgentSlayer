"""A headless AutomationView so the automation core can be driven in microseconds.

The sibling of ``tests/app/conftest.py`` and the same bargain:
:class:`~agentclip.driver.automation.controller.AutomationController` talks to its UI
through exactly one narrow port (:class:`agentclip.driver.automation.view.AutomationView`),
so everything it decides is testable without a terminal, a browser window or a
mouse. The Pilot suites in ``tests/tui/`` stay as the wiring check - that the
real screen is still plugged into this - but the *rules* are asserted here.

:class:`FakeAutomationView` records; it scripts nothing, because nothing on this
port asks a question yet. Everything is a LIST rather than a last-value, because
several of these paints are re-issued unconditionally and "how many times, in
what order" is as much of the contract as "with what".
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.harness_log import HarnessEntry
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.view import Severity
from agentclip.driver.screen.profile import TemplateKind


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


@pytest.fixture
def automation(view: FakeAutomationView) -> AutomationController:
    return AutomationController(view=view)
