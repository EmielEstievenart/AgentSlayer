"""Unit tests for the per-slot calibration record (screen/slot.py).

Pure data, no Textual and no screen: the readiness rules are what gate a
delegation, so they are worth pinning down on their own. ``can_delegate`` is
deliberately strict - every one of the four pieces has to be drawn - because a
half-calibrated sub-agent slot must read as unavailable rather than strand a
sub-run halfway through.
"""

from __future__ import annotations

from agentclip.screen.capture import RegionImage
from agentclip.screen.element import CalibratedElement
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import (
    MISSING_CHATBOX,
    MISSING_COPY,
    MISSING_FINISH,
    MISSING_NEWCHAT,
    AgentSlot,
    SlotCalibration,
    new_slots,
)

REGION = ScreenRegion(10, 20, 40, 30)


def _image(region: ScreenRegion = REGION) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


def _element(region: ScreenRegion = REGION) -> CalibratedElement:
    return CalibratedElement(region, _image(region))


def _ready(slot: AgentSlot = AgentSlot.SUBAGENT) -> SlotCalibration:
    """Every piece ``can_delegate`` insists on, and nothing more."""
    return SlotCalibration(
        slot,
        chatbox_ongoing=_element(),
        busy_region=REGION,
        busy_baseline=_image(),
        copy_region=REGION,
        copy_template=_image(),
        new_chat=_element(),
    )


def test_a_fresh_slot_can_do_nothing() -> None:
    cal = SlotCalibration(AgentSlot.MASTER)
    assert not cal.can_paste
    assert not cal.can_finish
    assert not cal.can_copy
    assert not cal.can_delegate
    assert cal.missing() == (MISSING_CHATBOX, MISSING_FINISH, MISSING_COPY, MISSING_NEWCHAT)


def test_new_slots_gives_one_empty_calibration_per_slot() -> None:
    slots = new_slots()
    assert set(slots) == {AgentSlot.MASTER, AgentSlot.SUBAGENT}
    assert [cal.slot for cal in slots.values()] == [AgentSlot.MASTER, AgentSlot.SUBAGENT]
    assert all(not cal.can_delegate for cal in slots.values())
    # Independent records: writing one must not show up in the other.
    slots[AgentSlot.MASTER].chat_region = REGION
    assert slots[AgentSlot.SUBAGENT].chat_region is None


def test_any_of_the_three_click_targets_makes_a_paste_possible() -> None:
    """The whole chat window is the documented last resort, so it counts."""
    assert SlotCalibration(AgentSlot.MASTER, chat_region=REGION).can_paste
    assert SlotCalibration(AgentSlot.MASTER, chatbox_initial=_element()).can_paste
    assert SlotCalibration(AgentSlot.MASTER, chatbox_ongoing=_element()).can_paste


def test_either_detector_is_enough_to_know_the_model_stopped() -> None:
    busy = SlotCalibration(AgentSlot.MASTER, busy_region=REGION, busy_baseline=_image())
    idle = SlotCalibration(AgentSlot.MASTER, idle_region=REGION, idle_baseline=_image())
    assert busy.can_finish
    assert idle.can_finish
    # The baseline is what the poller needs - a region without one is nothing.
    assert not SlotCalibration(AgentSlot.MASTER, busy_region=REGION).can_finish


def test_a_stale_region_alone_is_also_a_finish_detector() -> None:
    """The stability detector stores NO baseline - its tracker's first polled
    frame is the baseline - so unlike busy/idle a bare region is enough."""
    assert SlotCalibration(AgentSlot.MASTER, stale_region=REGION).can_finish


def test_can_delegate_needs_all_four_pieces() -> None:
    assert _ready().can_delegate
    assert _ready().missing() == ()


def test_each_missing_piece_is_named_and_blocks_delegation() -> None:
    cases = {
        MISSING_CHATBOX: {"chatbox_ongoing": None},
        MISSING_FINISH: {"busy_baseline": None},
        MISSING_COPY: {"copy_template": None},
        MISSING_NEWCHAT: {"new_chat": None},
    }
    for name, patch in cases.items():
        cal = _ready()
        for field, value in patch.items():
            setattr(cal, field, value)
        assert not cal.can_delegate, name
        assert cal.missing() == (name,)


def test_clear_empties_everything_but_keeps_the_slot_identity() -> None:
    cal = _ready(AgentSlot.SUBAGENT)
    cal.chat_region = REGION
    cal.chatbox_initial = _element()
    cal.idle_region = REGION
    cal.idle_baseline = _image()
    cal.stale_region = REGION
    cal.clear()
    assert cal.slot is AgentSlot.SUBAGENT
    assert not cal.can_delegate
    assert cal.missing() == (MISSING_CHATBOX, MISSING_FINISH, MISSING_COPY, MISSING_NEWCHAT)
    assert cal == SlotCalibration(AgentSlot.SUBAGENT)


def test_the_slot_enum_is_a_string_with_a_display_label() -> None:
    """It is stored in a Textual Select (str values) and shown in the sidebar."""
    assert str(AgentSlot.MASTER) == "master"
    assert AgentSlot("subagent") is AgentSlot.SUBAGENT
    assert AgentSlot.MASTER.label == "MASTER"
    assert AgentSlot.SUBAGENT.label == "SUB-AGENT"
