"""Unit tests for slot readiness (screen/slot.py).

Pure data, no Textual and no screen. The interesting thing here is the shape of
the answer rather than any one rule: readiness is a function of a *pair* - the
box drawn for this window, and what the service it is pointed at looks like -
so the table below is the whole contract. ``can_delegate`` is deliberately
strict, because a half-calibrated sub-agent slot must read as unavailable
rather than strand a sub-run halfway through.
"""

from __future__ import annotations

import pytest

from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import (
    MISSING_CHAT_REGION,
    MISSING_COPY,
    MISSING_NEWCHAT,
    AgentSlot,
    SlotCalibration,
    can_copy,
    can_delegate,
    can_finish,
    can_paste,
    missing,
    new_slots,
)

REGION = ScreenRegion(10, 20, 40, 30)


def _image() -> RegionImage:
    return RegionImage(REGION.width, REGION.height, b"\x00" * (REGION.width * REGION.height * 4))


def _profile(*kinds: TemplateKind) -> ServiceProfile:
    """A service profile holding exactly ``kinds``."""
    profile = ServiceProfile("chatgpt")
    for kind in kinds:
        profile.put(kind, _image())
    return profile


def _slot(*, drawn: bool, slot: AgentSlot = AgentSlot.SUBAGENT) -> SlotCalibration:
    return SlotCalibration(slot, chat_region=REGION if drawn else None)


BOTH = (TemplateKind.COPY, TemplateKind.NEW_CHAT)


# (drawn window, captured appearances) -> (paste, finish, copy, delegate)
TABLE = [
    (False, (), (False, False, False, False)),
    # Appearances with nowhere to be searched for are not yet usable pieces.
    (False, BOTH, (False, False, False, False)),
    # One drag is the whole minimum setup: it can paste AND it can tell when
    # the model stopped, because the window IS the staleness detector.
    (True, (), (True, True, False, False)),
    (True, (TemplateKind.COPY,), (True, True, True, False)),
    (True, (TemplateKind.NEW_CHAT,), (True, True, False, False)),
    (True, BOTH, (True, True, True, True)),
    # Appearances nothing here asks about change no answer.
    (True, (TemplateKind.BUSY, TemplateKind.IDLE), (True, True, False, False)),
]


@pytest.mark.parametrize(("drawn", "kinds", "expected"), TABLE)
def test_readiness_is_composed_from_the_slot_and_the_profile(
    drawn: bool, kinds: tuple[TemplateKind, ...], expected: tuple[bool, bool, bool, bool]
) -> None:
    cal, profile = _slot(drawn=drawn), _profile(*kinds)
    actual = (
        can_paste(cal, profile),
        can_finish(cal, profile),
        can_copy(cal, profile),
        can_delegate(cal, profile),
    )
    assert actual == expected


def test_the_drawn_window_alone_is_a_finish_detector() -> None:
    """The staleness detector needs no captured cue at all - only a rectangle
    to watch stop changing - so every slot can finish from its first drag."""
    empty = ServiceProfile("chatgpt")
    assert can_finish(_slot(drawn=True), empty)
    assert not can_finish(_slot(drawn=False), empty)


def test_a_second_slot_inherits_the_service_appearances() -> None:
    """The point of the whole model: one profile, two windows. A sub-agent slot
    costs exactly one drag once the master's service has been captured."""
    profile = _profile(*BOTH)
    master = _slot(drawn=True, slot=AgentSlot.MASTER)
    subagent = SlotCalibration(AgentSlot.SUBAGENT)
    assert can_delegate(master, profile)
    assert not can_delegate(subagent, profile)

    subagent.chat_region = ScreenRegion(900, 20, 300, 400)
    assert can_delegate(subagent, profile)


def test_missing_names_every_gap_in_calibration_order() -> None:
    empty = ServiceProfile("chatgpt")
    assert missing(_slot(drawn=False), empty) == (
        MISSING_CHAT_REGION,
        MISSING_COPY,
        MISSING_NEWCHAT,
    )
    assert missing(_slot(drawn=True), empty) == (MISSING_COPY, MISSING_NEWCHAT)
    assert missing(_slot(drawn=True), _profile(TemplateKind.COPY)) == (MISSING_NEWCHAT,)
    assert missing(_slot(drawn=True), _profile(*BOTH)) == ()


def test_losing_the_window_takes_the_buttons_with_it() -> None:
    """Honest rather than noisy: with nowhere to search, neither button can be
    found - and all three gaps close again on one drag."""
    profile = _profile(*BOTH)
    cal = _slot(drawn=True)
    assert missing(cal, profile) == ()
    cal.clear()
    assert missing(cal, profile) == (MISSING_CHAT_REGION, MISSING_COPY, MISSING_NEWCHAT)


def test_missing_is_empty_exactly_when_delegation_is_available() -> None:
    for drawn, kinds, expected in TABLE:
        cal, profile = _slot(drawn=drawn), _profile(*kinds)
        assert (missing(cal, profile) == ()) is expected[3]


def test_new_slots_gives_one_empty_slot_per_window() -> None:
    slots = new_slots()
    assert set(slots) == {AgentSlot.MASTER, AgentSlot.SUBAGENT}
    assert [cal.slot for cal in slots.values()] == [AgentSlot.MASTER, AgentSlot.SUBAGENT]
    assert all(not cal.is_set for cal in slots.values())
    # Independent records: writing one must not show up in the other.
    slots[AgentSlot.MASTER].chat_region = REGION
    assert slots[AgentSlot.SUBAGENT].chat_region is None


def test_clear_empties_the_window_but_keeps_the_slot_identity() -> None:
    cal = _slot(drawn=True, slot=AgentSlot.SUBAGENT)
    cal.clear()
    assert cal.slot is AgentSlot.SUBAGENT
    assert not cal.is_set
    assert cal == SlotCalibration(AgentSlot.SUBAGENT)


def test_the_slot_enum_is_a_string_with_a_display_label() -> None:
    """It is stored in a Textual Select (str values) and shown in the sidebar."""
    assert str(AgentSlot.MASTER) == "master"
    assert AgentSlot("subagent") is AgentSlot.SUBAGENT
    assert AgentSlot.MASTER.label == "MASTER"
    assert AgentSlot.SUBAGENT.label == "SUB-AGENT"
