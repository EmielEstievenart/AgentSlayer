"""The ARMED switch's decision half, away from every consequence of it.

What the switch DOES to the machine (the clipboard watcher it stops, the four
chokepoints that consult it, the banner and the toast) is the shell's and is
covered by ``tests/tui/test_armed_ui.py``. What is asserted here is the part
that now lives below every shell: what the flag reads after a target, and that
the view is told - which is the only way a second frontend could ever draw it.
"""

from __future__ import annotations

from agentclip.automation.controller import AutomationController

from .conftest import FakeAutomationView


def test_armed_is_the_default(automation: AutomationController) -> None:
    """Every release before the switch existed behaved as armed, so it starts there."""
    assert automation.os_armed is True


def test_none_toggles(automation: AutomationController) -> None:
    """Bare `/armed` and F5 hand in None, which flips whatever is in force."""
    assert automation.set_os_armed(None) is False
    assert automation.os_armed is False
    assert automation.set_os_armed(None) is True
    assert automation.os_armed is True


def test_an_explicit_target_sets_rather_than_flips(automation: AutomationController) -> None:
    """`/armed off` means off, whether or not it already was."""
    assert automation.set_os_armed(False) is False
    assert automation.set_os_armed(False) is False
    assert automation.os_armed is False
    assert automation.set_os_armed(True) is True
    assert automation.os_armed is True


def test_the_view_is_painted_on_every_change(
    automation: AutomationController, view: FakeAutomationView
) -> None:
    automation.set_os_armed(False)
    automation.set_os_armed(True)
    automation.set_os_armed(None)
    assert view.armed_paints == [False, True, False]


def test_an_idempotent_set_repaints_anyway(
    automation: AutomationController, view: FakeAutomationView
) -> None:
    """Unconditional on purpose, and it is the behaviour MainScreen always had:
    an explicit `/armed off` typed twice has to confirm itself rather than look
    ignored, so the indicators repaint even when nothing moved."""
    automation.set_os_armed(False)
    automation.set_os_armed(False)
    assert view.armed_paints == [False, False]


def test_setting_the_state_it_is_already_in_repaints_too(
    automation: AutomationController, view: FakeAutomationView
) -> None:
    automation.set_os_armed(True)  # armed already
    assert view.armed_paints == [True]
    assert automation.os_armed is True
