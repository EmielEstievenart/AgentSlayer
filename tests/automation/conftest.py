"""A headless AutomationView so the automation core can be driven in microseconds.

The sibling of ``tests/app/conftest.py`` and the same bargain:
:class:`~agentclip.automation.controller.AutomationController` talks to its UI
through exactly one narrow port (:class:`agentclip.automation.view.AutomationView`),
so everything it decides is testable without a terminal, a browser window or a
mouse. The Pilot suites in ``tests/tui/`` stay as the wiring check - that the
real screen is still plugged into this - but the *rules* are asserted here.

:class:`FakeAutomationView` records; it scripts nothing, because nothing on this
port asks a question yet.
"""

from __future__ import annotations

import pytest

from agentclip.automation.controller import AutomationController
from agentclip.automation.view import Severity


class FakeAutomationView:
    """Structural ``AutomationView`` that records. No Textual, no screen, no OS."""

    def __init__(self) -> None:
        # Every paint_armed argument, in order - the list, not a flag, because
        # the port is repainted unconditionally and "how many times" is as much
        # of the contract as "with what".
        self.armed_paints: list[bool] = []
        self.notifications: list[tuple[str, Severity]] = []

    def paint_armed(self, armed: bool) -> None:
        self.armed_paints.append(armed)

    def notify(
        self,
        message: str,
        *,
        severity: Severity = "information",
        timeout: float | None = None,
    ) -> None:
        self.notifications.append((message, severity))


@pytest.fixture
def view() -> FakeAutomationView:
    return FakeAutomationView()


@pytest.fixture
def automation(view: FakeAutomationView) -> AutomationController:
    return AutomationController(view=view)
