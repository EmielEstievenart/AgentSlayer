"""Unit tests for the per-slot calibration record (screen/slot.py).

Pure data, no Textual and no screen: the readiness rules are what gate a
delegation, so they are worth pinning down on their own. ``can_delegate`` is
deliberately strict - every one of the four pieces has to be drawn - because a
half-calibrated sub-agent slot must read as unavailable rather than strand a
sub-run halfway through.
"""

from __future__ import annotations

from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import (
    MISSING_CHATBOX,
    MISSING_COPY,
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


# The appearances a ready slot needs on top of its drawn window.
CAPTURED = (TemplateKind.COPY, TemplateKind.NEW_CHAT)


def _image(region: ScreenRegion = REGION) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


def _ready(slot: AgentSlot = AgentSlot.SUBAGENT) -> SlotCalibration:
    """Every piece of ``can_delegate`` the SLOT owns, and nothing more - the
    copy button is the profile's half (``_profile(*CAPTURED)``)."""
    return SlotCalibration(slot, chat_region=REGION)


def test_a_fresh_slot_can_do_nothing() -> None:
    cal = SlotCalibration(AgentSlot.MASTER)
    empty = ServiceProfile("chatgpt")
    assert not cal.can_paste
    assert not cal.can_finish
    assert not can_delegate(cal, empty)
    assert missing(cal, empty) == (
        MISSING_CHATBOX,
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


def test_the_drawn_window_is_a_finish_detector_by_itself() -> None:
    """The stability detector needs no captured cue at all - a rectangle that
    stops changing is a finished response - so the drawn window IS it."""
    assert SlotCalibration(AgentSlot.MASTER, chat_region=REGION).can_finish
    assert not SlotCalibration(AgentSlot.MASTER).can_finish


def test_can_delegate_needs_all_four_pieces() -> None:
    """Three of them are the slot's; the copy button is the SERVICE's, which is
    exactly why readiness is a function of the pair."""
    assert can_delegate(_ready(), _profile(*CAPTURED))
    assert missing(_ready(), _profile(*CAPTURED)) == ()
    assert not can_delegate(_ready(), ServiceProfile("chatgpt"))


def test_each_missing_piece_is_named_and_blocks_delegation() -> None:
    cases: dict[str, tuple[dict[str, object], tuple[str, ...]]] = {
        # Losing the window loses the copy button with it: the icon is hunted
        # INSIDE the drawn region, so there is nowhere to look without one.
        "chat window": (
            {"chat_region": None},
            (MISSING_CHATBOX, MISSING_COPY, MISSING_NEWCHAT),
        ),

    }
    for label, (patch, expected) in cases.items():
        cal = _ready()
        for field, value in patch.items():
            setattr(cal, field, value)
        assert not can_delegate(cal, _profile(*CAPTURED)), label
        assert missing(cal, _profile(*CAPTURED)) == expected, label

    # The copy and new-chat buttons are the profile's half of the same question.
    assert missing(_ready(), ServiceProfile("chatgpt")) == (MISSING_COPY, MISSING_NEWCHAT)
    assert missing(_ready(), _profile(TemplateKind.COPY)) == (MISSING_NEWCHAT,)


def test_clear_empties_everything_but_keeps_the_slot_identity() -> None:
    cal = _ready(AgentSlot.SUBAGENT)
    cal.clear()
    assert cal.slot is AgentSlot.SUBAGENT
    assert not can_delegate(cal, _profile(*CAPTURED))
    assert missing(cal, _profile(*CAPTURED)) == (
        MISSING_CHATBOX,
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
