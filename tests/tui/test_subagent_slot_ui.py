"""Pilot tests for the sub-agent transport primitives on MainScreen.

Three methods, and the safety story is the whole reason they exist:

* ``delegation_available()`` - the controller's gate before it builds a
  sub-agent engine at all.
* ``start_browser_chat(slot)`` - verify-then-click that slot's new-chat button
  and, ONLY on success, retarget every automation at it. A False return has to
  mean nothing happened: no click, no retarget, no trigger reset. Pasting a
  sub-agent bootstrap into the master chat would corrupt that conversation
  irrecoverably, so the failure paths are tested harder than the happy one.
* ``end_browser_chat()`` - unconditional return to the master window.

The OS calls (picker, capture, element probe, click, busy probe) are
monkeypatched at their use site (agentclip.tui.screens.main).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen

MASTER_BUSY = ScreenRegion(10, 10, 40, 20)
MASTER_BOX = ScreenRegion(10, 400, 300, 40)
MASTER_NEWCHAT = ScreenRegion(10, 60, 80, 24)
SUB_BUSY = ScreenRegion(900, 10, 40, 20)
SUB_BOX = ScreenRegion(900, 400, 300, 40)
SUB_NEWCHAT = ScreenRegion(900, 60, 80, 24)
SUB_COPY = ScreenRegion(900, 300, 24, 24)
SIZE = (110, 100)

# Where each slot's new-chat button "is" on screen. The appearance is captured
# once for the SERVICE, but it is searched for inside each slot's own drawn
# window, so the two slots resolve it to two different rectangles.
NEWCHAT_AT = {AgentSlot.MASTER: MASTER_NEWCHAT, AgentSlot.SUBAGENT: SUB_NEWCHAT}


def _frame(region: ScreenRegion) -> RegionImage:
    fill = bytes([region.left % 251])
    return RegionImage(region.width, region.height, fill * (region.width * region.height * 4))


class _Picker:
    def __init__(self) -> None:
        self.region: ScreenRegion | None = None
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> ScreenRegion | None:
        self.prompts.append(prompt)
        return self.region


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> AgentClipApp:
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    app = AgentClipApp(
        config=config,
        provider=FakeClipboard(),
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
    )
    return app


def _patch_screen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe_ok: bool = True,
    click_ok: bool = True,
) -> tuple[_Picker, list[ScreenRegion], list[ScreenRegion]]:
    picker = _Picker()
    clicks: list[ScreenRegion] = []
    probed: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "pick_region", picker)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "_NEW_CHAT_SETTLE_S", 0.01)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)
    async def fake_find(
        self: MainScreen,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> ScreenRegion | None:
        """Stand-in for the in-region appearance search (screen.template's job,
        tested there): records the attempt and answers where each slot's copy
        of ``kind`` sits."""
        cal = self._slots[slot] if slot is not None else self.live
        if cal.chat_region is None or not self._active_profile().has(kind):
            return None
        rect = NEWCHAT_AT[cal.slot] if kind is TemplateKind.NEW_CHAT else cal.chat_region
        probed.append(rect)
        return rect if probe_ok else None

    monkeypatch.setattr(MainScreen, "_find", fake_find)
    monkeypatch.setattr(
        main_mod,
        "click_region",
        lambda region, **kw: bool(clicks.append(region)) or click_ok,
    )
    return picker, clicks, probed


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _calibrate(
    app: AgentClipApp, pilot: Pilot, picker: _Picker, button_id: str, region: ScreenRegion
) -> None:
    picker.region = region
    before = len(picker.prompts)
    await _press(app, pilot, button_id)
    await _wait_for(pilot, lambda: len(picker.prompts) > before, f"{button_id} picker ran")
    await pilot.pause(0.1)


async def _select_slot(app: AgentClipApp, pilot: Pilot, slot: AgentSlot) -> None:
    main = app.main_screen
    assert main is not None
    main.sidebar.slot_select.value = str(slot)
    await _wait_for(pilot, lambda: main._calibrating is slot, f"{slot} selected")


async def _calibrate_master(app: AgentClipApp, pilot: Pilot, picker: _Picker) -> None:
    """The master slot's own calibrations: its window and its busy element.

    Deliberately no appearance captures - those belong to the SERVICE and are
    shared by both slots, so capturing one here would pre-satisfy the sub-agent
    slot and hide what the sequence below is measuring.
    """
    await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_BOX)
    await _calibrate(app, pilot, picker, "#capture-busy-btn", MASTER_BUSY)


async def _calibrate_subagent(app: AgentClipApp, pilot: Pilot, picker: _Picker) -> None:
    await _select_slot(app, pilot, AgentSlot.SUBAGENT)
    await _calibrate(app, pilot, picker, "#set-region-btn", SUB_BOX)
    await _calibrate(app, pilot, picker, "#capture-busy-btn", SUB_BUSY)
    await _calibrate(app, pilot, picker, "#capture-copy-btn", SUB_COPY)
    await _calibrate(app, pilot, picker, "#capture-new-chat-btn", SUB_NEWCHAT)
    await _select_slot(app, pilot, AgentSlot.MASTER)


async def test_delegation_is_unavailable_until_every_piece_is_calibrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker, _, _ = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert not main.delegation_available()

        # A calibrated MASTER window does nothing for delegation.
        await _calibrate_master(app, pilot, picker)
        assert not main.delegation_available()

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_BOX)
        assert not main.delegation_available()
        await _calibrate(app, pilot, picker, "#capture-busy-btn", SUB_BUSY)
        assert not main.delegation_available()
        await _calibrate(app, pilot, picker, "#capture-copy-btn", SUB_COPY)
        assert not main.delegation_available()  # still no way to open a fresh chat
        await _calibrate(app, pilot, picker, "#capture-new-chat-btn", SUB_NEWCHAT)
        assert main.delegation_available()


async def test_start_browser_chat_clicks_the_slot_and_retargets_the_automation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker, clicks, probed = _patch_screen(monkeypatch)
    # One capture per tick, of the live slot's chat region: which rectangle the
    # poller is capturing IS which window it is watching.
    captures: list[ScreenRegion] = []

    def record_capture(region: ScreenRegion) -> RegionImage:
        captures.append(region)
        return _frame(region)

    monkeypatch.setattr(main_mod, "capture_region", record_capture)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_master(app, pilot, picker)
        await _calibrate_subagent(app, pilot, picker)
        await _wait_for(pilot, lambda: MASTER_BOX in captures, "the master poller is running")
        clicks.clear()
        probed.clear()

        assert await main.start_browser_chat(AgentSlot.SUBAGENT) is True

        assert probed == [SUB_NEWCHAT]  # verified before clicking
        assert clicks == [SUB_NEWCHAT]
        assert main._live is AgentSlot.SUBAGENT
        assert main._calibrating is AgentSlot.MASTER  # the sidebar stays where it was
        # The poller followed the live slot onto the sub-agent's window.
        await _wait_for(pilot, lambda: SUB_BOX in captures, "the poller retargeted")
        # ...and the paste click now goes into the sub-agent's chat box.
        assert await main._chatbox_region() == SUB_BOX

        main.end_browser_chat()
        assert main._live is AgentSlot.MASTER
        assert await main._chatbox_region() == MASTER_BOX
        marker = len(captures)
        await _wait_for(
            pilot,
            lambda: MASTER_BOX in captures[marker:],
            "the poller returned to the master",
        )


async def test_start_browser_chat_resets_the_finish_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verdicts describe a window: carrying the master's armed trigger into the
    sub-agent's chat could fire the auto-copy against a page that never ran."""
    picker, _, _ = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_subagent(app, pilot, picker)

        main._copy_armed = True
        main._copy_changed_streak = 1
        main._busy_seen = True
        main._busy_finished = False

        assert await main.start_browser_chat(AgentSlot.SUBAGENT) is True
        assert main._copy_armed is False
        assert main._copy_changed_streak == 0
        assert main._busy_seen is False
        assert main._busy_finished is None


async def test_a_mismatched_new_chat_button_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page moved. Returning False *and having done nothing* is what lets
    the caller abort before any paste."""
    picker, clicks, _ = _patch_screen(monkeypatch, probe_ok=False)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_subagent(app, pilot, picker)
        main._copy_armed = True
        clicks.clear()

        assert await main.start_browser_chat(AgentSlot.SUBAGENT) is False

        assert clicks == []
        assert main._live is AgentSlot.MASTER
        assert main._copy_armed is True  # untouched, like everything else


async def test_a_click_the_os_refused_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified fine, but the input never landed (not Windows): the chat may or
    may not be fresh, so the live slot must not move either."""
    picker, clicks, _ = _patch_screen(monkeypatch, click_ok=False)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_subagent(app, pilot, picker)
        clicks.clear()

        assert await main.start_browser_chat(AgentSlot.SUBAGENT) is False
        assert clicks == [SUB_NEWCHAT]  # attempted, but the OS swallowed it
        assert main._live is AgentSlot.MASTER


async def test_an_uncalibrated_slot_is_refused_without_touching_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clicks, probed = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        assert await main.start_browser_chat(AgentSlot.SUBAGENT) is False
        assert probed == []
        assert clicks == []
        assert main._live is AgentSlot.MASTER


async def test_a_drawn_window_without_a_new_chat_capture_is_refused_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOT_CALIBRATED has two causes - no window drawn, or no appearance
    captured - and the second must refuse just as absolutely: a drawn window
    with nothing to look for in it is still nowhere to click."""
    picker, clicks, probed = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_BOX)
        await _calibrate(app, pilot, picker, "#capture-copy-btn", SUB_COPY)
        clicks.clear()
        probed.clear()

        assert await main.start_browser_chat(AgentSlot.SUBAGENT) is False
        assert probed == []  # refused before any search, let alone a click
        assert clicks == []
        assert main._live is AgentSlot.MASTER


async def test_end_browser_chat_always_returns_to_the_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs in the controller's ``finally``, so it must work even when
    nothing was ever started."""
    _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        main.end_browser_chat()
        assert main._live is AgentSlot.MASTER

        main._live = AgentSlot.SUBAGENT
        main._copy_armed = True
        main.end_browser_chat()
        assert main._live is AgentSlot.MASTER
        assert main._copy_armed is False
