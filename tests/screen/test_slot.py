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
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import (
    MISSING_CHATBOX,
    MISSING_COPY,
    MISSING_FINISH,
    MISSING_NEWCHAT,
    AgentSlot,
    SlotCalibration,
    can_delegate,
    missing,
    new_slots,
)

REGION = ScreenRegion(10, 20, 40, 30)


def _profile(*kinds: TemplateKind) -> ServiceProfile:
    """A service profile holding exactly ``kinds``."""
    profile = ServiceProfile("chatgpt")
    for kind in kinds:
        profile.put(kind, _image())
    return profile


CAPTURED = TemplateKind.COPY


def _image(region: ScreenRegion = REGION) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


def _element(region: ScreenRegion = REGION) -> CalibratedElement:
    return CalibratedElement(region, _image(region))


def _ready(slot: AgentSlot = AgentSlot.SUBAGENT) -> SlotCalibration:
    """Every piece of ``can_delegate`` the SLOT owns, and nothing more - the
    copy button is the profile's half (``_profile(CAPTURED)``)."""
    return SlotCalibration(
        slot,
        chat_region=REGION,
        busy_region=REGION,
        busy_baseline=_image(),
        new_chat=_element(),
    )


def test_a_fresh_slot_can_do_nothing() -> None:
    cal = SlotCalibration(AgentSlot.MASTER)
    empty = ServiceProfile("chatgpt")
    assert not cal.can_paste
    assert not cal.can_finish
    assert not can_delegate(cal, empty)
    assert missing(cal, empty) == (
        MISSING_CHATBOX,
        MISSING_FINISH,
        MISSING_COPY,
        MISSING_NEWCHAT,
    )


def test_new_slots_gives_one_empty_calibration_per_slot() -> None:
    slots = new_slots()
    assert set(slots) == {AgentSlot.MASTER, AgentSlot.SUBAGENT}
    assert [cal.slot for cal in slots.values()] == [AgentSlot.MASTER, AgentSlot.SUBAGENT]
    empty = ServiceProfile("chatgpt")
    assert all(not can_delegate(cal, empty) for cal in slots.values())
    # Independent records: writing one must not show up in the other.
    slots[AgentSlot.MASTER].chat_region = REGION
    assert slots[AgentSlot.SUBAGENT].chat_region is None


def test_the_drawn_chat_window_is_what_makes_a_paste_possible() -> None:
    """The input box is found inside it by appearance, so the window is the
    only thing that has to be drawn."""
    assert SlotCalibration(AgentSlot.MASTER, chat_region=REGION).can_paste
    assert not SlotCalibration(AgentSlot.MASTER).can_paste


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
    """Three of them are the slot's; the copy button is the SERVICE's, which is
    exactly why readiness is a function of the pair."""
    assert can_delegate(_ready(), _profile(CAPTURED))
    assert missing(_ready(), _profile(CAPTURED)) == ()
    assert not can_delegate(_ready(), ServiceProfile("chatgpt"))


def test_each_missing_piece_is_named_and_blocks_delegation() -> None:
    cases: dict[str, tuple[dict[str, object], tuple[str, ...]]] = {
        # Losing the window loses the copy button with it: the icon is hunted
        # INSIDE the drawn region, so there is nowhere to look without one.
        "chat window": ({"chat_region": None}, (MISSING_CHATBOX, MISSING_COPY)),
        "finish detector": ({"busy_baseline": None}, (MISSING_FINISH,)),
        "new-chat button": ({"new_chat": None}, (MISSING_NEWCHAT,)),
    }
    for label, (patch, expected) in cases.items():
        cal = _ready()
        for field, value in patch.items():
            setattr(cal, field, value)
        assert not can_delegate(cal, _profile(CAPTURED)), label
        assert missing(cal, _profile(CAPTURED)) == expected, label

    # The copy button is the profile's half of the same question.
    assert missing(_ready(), ServiceProfile("chatgpt")) == (MISSING_COPY,)


def test_clear_empties_everything_but_keeps_the_slot_identity() -> None:
    cal = _ready(AgentSlot.SUBAGENT)
    cal.idle_region = REGION
    cal.idle_baseline = _image()
    cal.stale_region = REGION
    cal.clear()
    assert cal.slot is AgentSlot.SUBAGENT
    assert not can_delegate(cal, _profile(CAPTURED))
    assert missing(cal, ServiceProfile("chatgpt")) == (
        MISSING_CHATBOX,
        MISSING_FINISH,
        MISSING_COPY,
        MISSING_NEWCHAT,
    )
    assert cal == SlotCalibration(AgentSlot.SUBAGENT)


def test_the_slot_enum_is_a_string_with_a_display_label() -> None:
    """It is stored in a Textual Select (str values) and shown in the sidebar."""
    assert str(AgentSlot.MASTER) == "master"
    assert AgentSlot("subagent") is AgentSlot.SUBAGENT
    assert AgentSlot.MASTER.label == "MASTER"
    assert AgentSlot.SUBAGENT.label == "SUB-AGENT"
