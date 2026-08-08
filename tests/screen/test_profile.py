"""The service appearance profile: kinds, their calibration copy, and the
in-memory collection (screen/profile.py)."""

from __future__ import annotations

import pytest

from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import ServiceProfile, TemplateKind


def patch(width: int = 20, height: int = 16, shade: int = 0) -> RegionImage:
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 11 + shade) % 256, (y * 23) % 256, (x * y) % 256, 0))
    return RegionImage(width, height, bytes(pixels))


def test_every_kind_has_a_label_and_a_prompt() -> None:
    for kind in TemplateKind:
        assert kind.label.strip()
        assert kind.prompt.strip()
        # The overlay copy has one shape everywhere the user meets it.
        assert kind.prompt.endswith("· Esc cancels")
        assert kind.prompt.startswith("Drag a")


def test_the_busy_prompt_warns_about_animated_spinners() -> None:
    """A spinner is a different picture every frame, so it can never match."""
    assert "spinner" in TemplateKind.BUSY.prompt.lower()
    assert "generating" in TemplateKind.BUSY.prompt.lower()


def test_the_chat_box_prompts_ask_for_an_empty_box() -> None:
    for kind in (TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING):
        assert "EMPTY" in kind.prompt


def test_thresholds_are_strict_for_buttons_and_loose_for_chat_boxes() -> None:
    assert TemplateKind.BUSY.max_diff == 0.08
    assert TemplateKind.IDLE.max_diff == 0.08
    assert TemplateKind.COPY.max_diff == 0.08
    assert TemplateKind.NEW_CHAT.max_diff == 0.10
    assert TemplateKind.CHATBOX_INITIAL.max_diff == 0.20
    assert TemplateKind.CHATBOX_ONGOING.max_diff == 0.20


def test_kinds_are_their_own_wire_names() -> None:
    """The value IS the filename and the manifest key, so it must stay stable."""
    assert TemplateKind.CHATBOX_INITIAL == "chatbox-initial"
    assert TemplateKind("new-chat") is TemplateKind.NEW_CHAT


def test_a_fresh_profile_holds_nothing() -> None:
    profile = ServiceProfile("chatgpt")
    assert profile.captured == ()
    assert profile.describe() == "0/6 captured"
    assert not profile.has(TemplateKind.BUSY)
    assert profile.get(TemplateKind.BUSY) is None


def test_put_builds_a_searchable_template() -> None:
    profile = ServiceProfile("chatgpt")
    profile.put(TemplateKind.BUSY, patch())
    template = profile.get(TemplateKind.BUSY)
    assert template is not None
    assert (template.width, template.height) == (20, 16)
    assert template.anchors
    assert profile.has(TemplateKind.BUSY)


def test_put_replaces_a_previous_capture() -> None:
    profile = ServiceProfile("chatgpt")
    profile.put(TemplateKind.BUSY, patch())
    profile.put(TemplateKind.BUSY, patch(30, 8))
    template = profile.get(TemplateKind.BUSY)
    assert template is not None and (template.width, template.height) == (30, 8)
    assert profile.describe() == "1/6 captured"


def test_captured_is_in_declaration_order_not_capture_order() -> None:
    profile = ServiceProfile("chatgpt")
    for kind in (TemplateKind.COPY, TemplateKind.BUSY, TemplateKind.CHATBOX_ONGOING):
        profile.put(kind, patch())
    assert profile.captured == (
        TemplateKind.BUSY,
        TemplateKind.CHATBOX_ONGOING,
        TemplateKind.COPY,
    )
    assert profile.describe() == "3/6 captured"


def test_drop_and_clear() -> None:
    profile = ServiceProfile("chatgpt")
    profile.put(TemplateKind.BUSY, patch())
    profile.put(TemplateKind.IDLE, patch())
    profile.drop(TemplateKind.BUSY)
    assert profile.captured == (TemplateKind.IDLE,)
    profile.drop(TemplateKind.BUSY)  # dropping what isn't there is not an error
    profile.clear()
    assert profile.captured == ()


def test_put_rejects_a_capture_that_cannot_be_searched_for() -> None:
    """A one-pixel drag has nothing to anchor on; the error must reach the UI."""
    profile = ServiceProfile("chatgpt")
    with pytest.raises(ValueError):
        profile.put(TemplateKind.BUSY, RegionImage(0, 0, b""))
    with pytest.raises(ValueError):
        profile.put(TemplateKind.BUSY, RegionImage(4, 4, bytes(4 * 4 * 4)))
    assert profile.captured == ()
