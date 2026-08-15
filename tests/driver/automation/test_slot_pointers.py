"""The two slot pointers, and the rule that they are two.

*calibrating* is the slot the configuration surface is pointed at; *live* is the
slot the automation is driving. Every bug this state has ever had was one of
them moving the other - a tab selection retargeting the poller, a delegation
dragging the sidebar along - so the assertions here are mostly about what did
NOT move.
"""

from __future__ import annotations

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot

from .conftest import FakeAutomationView

MASTER_BOX = ScreenRegion(0, 0, 400, 300)
SUB_BOX = ScreenRegion(900, 0, 400, 300)


def test_both_pointers_start_on_the_master(automation: AutomationController) -> None:
    assert automation.calibrating_slot is AgentSlot.MASTER
    assert automation.live_slot is AgentSlot.MASTER
    assert automation.calibrating is automation.live


def test_nothing_is_calibrated_to_begin_with(automation: AutomationController) -> None:
    for slot in AgentSlot:
        assert automation.calibration(slot).chat_region is None
        assert automation.calibration(slot).is_set is False


def test_selecting_a_tab_never_retargets_the_automation(
    automation: AutomationController,
) -> None:
    automation.select_calibrating_slot(AgentSlot.SUBAGENT)
    assert automation.calibrating_slot is AgentSlot.SUBAGENT
    assert automation.live_slot is AgentSlot.MASTER


def test_a_delegation_retargeting_never_moves_the_tab(automation: AutomationController) -> None:
    automation.select_live_slot(AgentSlot.SUBAGENT)
    assert automation.live_slot is AgentSlot.SUBAGENT
    assert automation.calibrating_slot is AgentSlot.MASTER
    automation.select_live_slot(AgentSlot.MASTER)
    assert automation.live_slot is AgentSlot.MASTER


def test_a_calibration_is_filed_under_the_slot_it_was_drawn_for(
    automation: AutomationController,
) -> None:
    """The slot is a parameter, not a read of the pointer: the picker blocks for
    as long as the user takes to drag, and the pointer moves meanwhile."""
    automation.set_calibration(AgentSlot.SUBAGENT, SUB_BOX)
    automation.select_calibrating_slot(AgentSlot.SUBAGENT)
    automation.set_calibration(AgentSlot.MASTER, MASTER_BOX)
    assert automation.calibration(AgentSlot.MASTER).chat_region == MASTER_BOX
    assert automation.calibration(AgentSlot.SUBAGENT).chat_region == SUB_BOX
    # ...and the accessors follow the pointers, not the drawing order.
    assert automation.calibrating.chat_region == SUB_BOX
    assert automation.live.chat_region == MASTER_BOX


def test_a_calibration_can_be_forgotten(automation: AutomationController) -> None:
    automation.set_calibration(AgentSlot.MASTER, MASTER_BOX)
    automation.set_calibration(AgentSlot.MASTER, None)
    assert automation.calibration(AgentSlot.MASTER).is_set is False


def test_services_are_per_window_and_opaque(automation: AutomationController) -> None:
    """Window ids are the shell's vocabulary; the controller only files keys
    under them, and answers "" for a window it has never been told about."""
    automation.set_service("m1", "claude")
    automation.set_service("m1-s1", "gemini")
    assert automation.service_of("m1") == "claude"
    assert automation.service_of("m1-s1") == "gemini"
    assert automation.service_of("m9") == ""


def test_the_services_snapshot_is_safe_to_write_through(
    automation: AutomationController,
) -> None:
    """The one iteration site (a config reload re-pointing dead services) writes
    while it reads, so the snapshot may not be the live mapping."""
    automation.set_service("m1", "gone")
    automation.set_service("m1-s1", "claude")
    for window, key in automation.services().items():
        if key == "gone":
            automation.set_service(window, "claude")
    assert automation.services() == {"m1": "claude", "m1-s1": "claude"}


def test_services_can_be_seeded_at_construction(view: FakeAutomationView) -> None:
    """How the shell hands its configured defaults down at startup."""
    controller = AutomationController(view=view, services={"m1": "claude"})
    assert controller.service_of("m1") == "claude"
