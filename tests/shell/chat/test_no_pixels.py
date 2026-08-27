"""The three reports of ui-monitor.md §11.0, staged against a monitor that can
see and a brain that holds nothing.

One bug, three symptoms, all on the same afternoon: with a green link and a
Monitor UI whose ELEMENTS column showed every button, `/new` answered "did not
land (not_calibrated)", the reply was never auto-copied, and the pasted prompt
was never submitted. Every one of those was decided by the Chat UI against a
service profile read off ITS OWN disk - which on any machine but the one the
appearances were captured on is empty.

So each test below stages exactly that machine: the fake monitor holds the
pictures (``captured`` per service, ``click_points`` per kind) and this window
holds none, which is not a fixture trick but the whole of §11.3 - there is no
profile store on this side any more to put one in.

The pixel policy itself is pinned one layer down (``tests/driver/automation``);
what is pinned here is that the wiring the real app runs reaches it: the view's
``captured_for`` is the monitor's answer, and the click lands on the pixel the
monitor aimed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

import agentclip.driver.automation.recipes.auto_copy as harvest_mod
from agentclip.driver.automation.flow import ELEMENT_CLICK_SETTLE_S
from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.monitor import beats
from agentclip.driver.monitor.protocol import Located
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.slot import AgentSlot
from tests.shell.chat.conftest import Harness

#: The box the MONITOR drew round the browser on its own desktop. It reaches
#: this window inside ``Watched`` and is adopted as the slot's calibration -
#: this side never drew it and cannot.
CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
#: Where the monitor "finds" whatever it was asked for.
FOUND = ScreenRegion(1100, 800, 240, 40)
PAYLOAD = "===CLIP:BEGIN=== the outbound ===CLIP:END==="


@pytest.fixture(autouse=True)
def quick_beats(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The real beats are seconds of a browser settling; the decisions under
    test are the same at zero."""
    for name in (
        "ACTIVATION_POLL_S",
        "FOCUS_CLICK_GAP_S",
        "PASTE_SETTLE_DELAY",
        "SNAP_BACK_SETTLE_S",
        "SUBMIT_SETTLE_S",
        "NEW_CHAT_SETTLE_S",
    ):
        monkeypatch.setattr(beats, name, 0.0)
    monkeypatch.setattr(harvest_mod, "SNAP_SETTLE_S", 0.0)
    yield


async def watching(harness: Harness, *, captured: tuple[TemplateKind, ...], **spec: object) -> str:
    """Point the monitor at a drawn window it holds ``captured`` for, and let
    this view adopt the answer - the one road a region or an appearance takes
    into the Chat UI since §10.5.

    Returns the service key the monitor settled on, which is a fact this side
    reads back rather than chooses.
    """
    target = replace(harness.monitor.specs_for[AgentSlot.MASTER], region=CHAT_REGION, **spec)
    harness.monitor.specs_for = dict.fromkeys(AgentSlot, target)
    harness.monitor.captured[target.service] = captured
    await harness.view._retarget_monitor()
    harness.monitor.calls.clear()
    return target.service


# == report 1: /new said "not_calibrated" over a browser with a new-chat button =


async def test_a_new_chat_the_monitor_has_captured_is_clicked(harness: Harness) -> None:
    """The monitor holds a new-chat button and this machine holds nothing at
    all: `/new` must reach ``click_element``.

    The refusal used to be made here, above the wire, out of a profile store
    this window no longer has - and this is what that deletion buys: the only
    "is it captured?" answer in the app is the one the machine with the pictures
    gave (``Watched.captured``).
    """
    await watching(harness, captured=(TemplateKind.NEW_CHAT,))
    harness.monitor.answers["click_element"] = ElementClick.CLICKED

    await harness.view._new_browser_chat(AgentSlot.MASTER)

    assert harness.view.captured_for(AgentSlot.MASTER) == (TemplateKind.NEW_CHAT,)
    assert ("click_element", (TemplateKind.NEW_CHAT, ELEMENT_CLICK_SETTLE_S)) in (
        harness.monitor.calls
    )
    assert any(
        "new browser chat opened" in event["message"] for event in harness.flush().of_type("toast")
    )


async def test_a_new_chat_nobody_captured_is_refused_by_naming_the_monitor(
    harness: Harness,
) -> None:
    """The other half of the same answer, and the sentence matters: the button
    may be plainly on screen, so "not calibrated" was advice the user could not
    act on. The fix is a capture in the Monitor UI, and the toast says so - with
    the service the MONITOR named, not one this window picked."""
    service = await watching(harness, captured=())

    await harness.view._new_browser_chat(AgentSlot.MASTER)

    assert harness.view.captured_for(AgentSlot.MASTER) == ()
    assert not [call for call in harness.monitor.calls if call[0] == "click_element"]
    toast = harness.flush().last("toast")["message"]
    assert "the monitor has no new-chat button captured" in toast
    assert "capture one in the Monitor UI" in toast
    assert harness.view._watched[AgentSlot.MASTER].label in toast or service in toast


# == report 2: the reply finished and nothing was copied ======================


async def test_the_copy_click_lands_on_the_pixel_the_monitor_aimed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harvest clicks ``Located.target`` - the service's click point applied
    where the pictures are - and never a rectangle's middle it worked out for
    itself. A service that moved its click point is the proof: the aim is the
    monitor's arithmetic, so it survives the brain knowing nothing about it.
    """
    await watching(harness, captured=(TemplateKind.COPY,))
    harness.monitor.click_points[TemplateKind.COPY] = (0, 100)
    harness.monitor.answers["locate"] = Located(FOUND, False, None)
    clicked: list[ScreenRegion] = []

    async def record(target: ScreenRegion) -> bool:
        clicked.append(target)
        return True

    monkeypatch.setattr(harness.view, "verified_copy_click", record)

    await harness.view.automation.auto_copy_flow()

    assert clicked == [click_point_region(FOUND, 0, 100)]


async def test_a_copy_click_after_a_hover_scan_is_aimed_the_same_way(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hover walk is a second search, not a second rule: it answers a
    ``Located`` too, so the click after it goes through the same click point as
    the one after a static hit."""
    await watching(harness, captured=(TemplateKind.COPY,), hover_scan=True)
    harness.monitor.click_points[TemplateKind.COPY] = (0, 100)
    harness.monitor.answers["locate"] = Located(None, False, 0.3)
    harness.monitor.answers["hover_scan"] = Located(FOUND, False, None)
    clicked: list[ScreenRegion] = []

    async def record(target: ScreenRegion) -> bool:
        clicked.append(target)
        return True

    monkeypatch.setattr(harness.view, "verified_copy_click", record)

    await harness.view.automation.auto_copy_flow()

    assert ("hover_scan", (TemplateKind.COPY,)) in harness.monitor.calls
    assert clicked == [click_point_region(FOUND, 0, 100)]


# == report 3: the prompt was pasted and never submitted ======================


async def test_a_paste_that_lands_taps_enter_when_the_monitor_asked_for_it(
    harness: Harness,
) -> None:
    """``auto_submit`` is the monitor's setting (§10.5) and the brain drives the
    monitor from it: a paste that lands is followed by ``send_enter``.

    The chat box it pasted into was found by the monitor and aimed by the
    monitor - this window contributed no rectangle to any of it.
    """
    await watching(harness, captured=(TemplateKind.CHATBOX_ONGOING,), auto_submit=True)
    harness.monitor.answers["locate"] = Located(FOUND, False, None)
    harness.monitor.answers["click"] = True
    harness.monitor.answers["send_paste"] = True
    harness.monitor.answers["send_enter"] = True

    await harness.view.automation.copy_outbound(PAYLOAD)

    verbs = [verb for verb, _args in harness.monitor.calls]
    assert "send_paste" in verbs
    assert verbs.index("send_enter") > verbs.index("send_paste")
    assert ("click", (click_point_region(FOUND, 50, 50), None)) in harness.monitor.calls


async def test_a_paste_the_monitor_did_not_ask_to_submit_taps_nothing(
    harness: Harness,
) -> None:
    """The setting is read, not assumed: the same delivery with ``auto_submit``
    off leaves the Enter to the user."""
    await watching(harness, captured=(TemplateKind.CHATBOX_ONGOING,))
    harness.monitor.answers["locate"] = Located(FOUND, False, None)
    harness.monitor.answers["click"] = True
    harness.monitor.answers["send_paste"] = True

    await harness.view.automation.copy_outbound(PAYLOAD)

    verbs = [verb for verb, _args in harness.monitor.calls]
    assert "send_paste" in verbs
    assert "send_enter" not in verbs
