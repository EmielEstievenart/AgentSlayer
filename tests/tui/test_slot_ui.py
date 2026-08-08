"""Pilot tests for the AGENT SLOT picker and per-slot calibration state.

Every calibration in the sidebar belongs to one slot: MASTER is the chat the
session runs in, SUBAGENT the second window a delegated sub-agent gets. The
picker under "AGENT SLOT" chooses which slot the buttons below it write into -
it does NOT change which window the automation drives (that is
``start_browser_chat``'s job, tested in test_subagent_slot_ui.py).

What we verify: the two slots are genuinely independent, switching the picker
repaints the whole column from the selected slot's stored state, the sub-agent's
picker prompts say which window to draw on, the picker is never locked by a
session (calibrating mid-session is the normal way to reach delegation), /new
keeps BOTH slots' calibrations and only sends the slot pointers home, and the
legacy single-window attributes still read and write the MASTER slot so the
older Pilot suites keep passing unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot
from agentclip.tui.app import AgentClipApp
from agentclip.tui.widgets.sidebar import SLOT_NOTE_MASTER, SLOT_NOTE_READY

MASTER_REGION = ScreenRegion(10, 20, 300, 400)
SUB_REGION = ScreenRegion(900, 20, 300, 400)
# Tall enough that every sidebar button is on screen: Pilot refuses to click a
# widget outside the visible region.
SIZE = (110, 100)


def _frame(region: ScreenRegion) -> RegionImage:
    """Pixels that differ per region, so a slot's snapshots are distinguishable."""
    fill = bytes([region.left % 251])
    return RegionImage(region.width, region.height, fill * (region.width * region.height * 4))


class _Picker:
    """Stand-in for the tkinter overlay: hands back whatever region is armed."""

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


def _patch_screen(monkeypatch: pytest.MonkeyPatch) -> _Picker:
    picker = _Picker()
    monkeypatch.setattr(main_mod, "pick_region", picker)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "probe_element", lambda element: True)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: True)
    return picker


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _select_slot(app: AgentClipApp, pilot: Pilot, slot: AgentSlot) -> None:
    main = app.main_screen
    assert main is not None
    main.sidebar.slot_select.value = str(slot)
    await _wait_for(pilot, lambda: main._calibrating is slot, f"{slot} selected")


async def _calibrate(
    app: AgentClipApp, pilot: Pilot, picker: _Picker, button_id: str, region: ScreenRegion
) -> None:
    picker.region = region
    before = len(picker.prompts)
    await _press(app, pilot, button_id)
    await _wait_for(pilot, lambda: len(picker.prompts) > before, f"{button_id} picker ran")
    await pilot.pause(0.1)


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.focus()
    await pilot.pause()
    await pilot.press("enter")


async def test_the_two_slots_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calibrating the sub-agent window must not disturb the master's, and the
    legacy attribute names keep meaning "the master slot"."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._calibrating is AgentSlot.MASTER
        assert main._live is AgentSlot.MASTER

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)

        assert main._slots[AgentSlot.MASTER].chat_region == MASTER_REGION
        assert main._slots[AgentSlot.SUBAGENT].chat_region == SUB_REGION
        assert main._chat_region == MASTER_REGION  # the compatibility proxy
        assert main._live is AgentSlot.MASTER  # calibrating never retargets


async def test_switching_the_slot_repaints_every_readout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        await _calibrate(app, pilot, picker, "#set-newchat-btn", MASTER_REGION)
        assert MASTER_REGION.describe() in _label(app, "#side-region")

        # The sub-agent slot is empty, so its column reads as uncalibrated...
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await pilot.pause()
        assert "not set" in _label(app, "#side-region")
        assert "not set" in _label(app, "#side-newchat")

        await _calibrate(app, pilot, picker, "#set-copy-btn", SUB_REGION)
        assert SUB_REGION.describe() in _label(app, "#side-copy")

        # ...and switching back restores the master's, from stored state.
        await _select_slot(app, pilot, AgentSlot.MASTER)
        await pilot.pause()
        assert MASTER_REGION.describe() in _label(app, "#side-region")
        assert MASTER_REGION.describe() in _label(app, "#side-newchat")
        assert "not set" in _label(app, "#side-copy")
        assert _label(app, "#side-slot-note") == SLOT_NOTE_MASTER


async def test_the_note_reports_what_delegation_is_still_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await pilot.pause()
        note = _label(app, "#side-slot-note")
        assert "new-chat button" in note
        assert "copy button" in note

        await _calibrate(app, pilot, picker, "#set-newchat-btn", SUB_REGION)
        assert "new-chat button" not in _label(app, "#side-slot-note")

        await _calibrate(app, pilot, picker, "#set-chatbox-ongoing-btn", SUB_REGION)
        await _calibrate(app, pilot, picker, "#set-busy-btn", SUB_REGION)
        await _calibrate(app, pilot, picker, "#set-copy-btn", SUB_REGION)
        await _wait_for(
            pilot,
            lambda: _label(app, "#side-slot-note") == SLOT_NOTE_READY,
            "delegation reported ready",
        )
        assert main.delegation_available()


async def test_subagent_prompts_name_the_window_being_drawn_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both slots share the picker code, so the prompt is the only thing that
    tells the user which browser window to point at."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        assert "SUB-AGENT window" not in picker.prompts[-1]

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        for button in (
            "#set-region-btn",
            "#set-chatbox-initial-btn",
            "#set-chatbox-ongoing-btn",
            "#set-busy-btn",
            "#set-idle-btn",
            "#set-copy-btn",
            "#set-newchat-btn",
        ):
            await _calibrate(app, pilot, picker, button, SUB_REGION)
            assert "SUB-AGENT window" in picker.prompts[-1], button


async def test_the_slot_picker_is_never_locked_by_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the service picker: a session that wants to delegate has to be
    able to calibrate the sub-agent window after it started."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        assert main.sidebar.service_select.disabled
        assert not main.sidebar.slot_select.disabled

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-newchat-btn", SUB_REGION)
        assert main._slots[AgentSlot.SUBAGENT].new_chat is not None


async def test_new_keeps_both_slots_and_sends_the_pointers_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/new is a session teardown, not a recalibration: the service's windows
    have not moved, so both slots' calibrations survive and only the slot
    pointers (calibrating + live) go home to MASTER."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)
        await _calibrate(app, pilot, picker, "#set-newchat-btn", SUB_REGION)
        main._live = AgentSlot.SUBAGENT  # as a delegation would leave it

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")

        assert main._slots[AgentSlot.MASTER].chat_region == MASTER_REGION
        assert main._slots[AgentSlot.SUBAGENT].chat_region == SUB_REGION
        assert main._slots[AgentSlot.SUBAGENT].new_chat is not None
        assert main._calibrating is AgentSlot.MASTER
        assert main._live is AgentSlot.MASTER
        assert main.sidebar.slot_select.value == str(AgentSlot.MASTER)
        assert MASTER_REGION.describe() in _label(app, "#side-region")
        assert _label(app, "#side-slot-note") == SLOT_NOTE_MASTER


async def test_new_rederives_delegation_readiness_from_the_surviving_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-agent slot calibrated to readiness stays ready across /new:
    ``_delegation_ready`` is re-derived from the surviving slot instead of being
    zeroed, so the next session gets the delegate tool and the one-shot "slot
    ready" toast has no False->True edge to re-fire on."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-newchat-btn", SUB_REGION)
        await _calibrate(app, pilot, picker, "#set-chatbox-ongoing-btn", SUB_REGION)
        await _calibrate(app, pilot, picker, "#set-busy-btn", SUB_REGION)
        await _calibrate(app, pilot, picker, "#set-copy-btn", SUB_REGION)
        await _wait_for(pilot, lambda: main.delegation_available(), "sub-agent slot ready")

        await main.clear_transcript()  # the /new teardown hook
        await pilot.pause()
        assert main.delegation_available()
        assert main._delegation_ready is True
        assert main._calibrating is AgentSlot.MASTER


async def test_the_new_browser_chat_button_targets_the_calibrating_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button is how the user *tests* a calibration, so it follows the
    sidebar - and it never moves the live slot."""
    picker = _patch_screen(monkeypatch)
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(
        main_mod, "click_region", lambda region, **kw: bool(clicks.append(region)) or True
    )
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-newchat-btn", MASTER_REGION)
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-newchat-btn", SUB_REGION)

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: clicks == [SUB_REGION], "the sub-agent's button clicked")
        assert main._live is AgentSlot.MASTER
