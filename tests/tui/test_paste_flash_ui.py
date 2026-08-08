"""Pilot tests for the sidebar's blinking "PRESS CTRL+V" / "PRESS ENTER" banner.

The banner turns on the moment an outbound payload lands on the clipboard
(``copy_outbound``) and keeps blinking until something proves the moment
passed: a detector ARMS the auto-copy trigger, i.e. the send is provably
detected (the busy region matching, or the chat region moving enough for long
enough - a caret blink does not count), a new clipboard capture arrives (the
conversation moved on without it), or the session is reset. The blink itself is a timer toggling a CSS class - display
on/off is the observable contract, so that is what these tests pin down.

``copy_outbound`` also auto-pastes (sends Ctrl+V) once the focus click lands,
so ``send_paste`` is monkeypatched in every test here even when it should
never be reached - reaching it for real would press Ctrl+V into whatever
window is focused on the machine running the suite.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.region import ScreenRegion
from agentclip.screen.stale import StaleProbe, StaleState
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed, ClipboardCaptured, StaleProbed
from agentclip.tui.screens.main import MainScreen
from agentclip.tui.widgets.sidebar import ENTER_FLASH_TEXT, PASTE_FLASH_TEXT


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
    )
    return app, fake


def _flash(app: AgentClipApp) -> Static:
    assert app.main_screen is not None
    return app.main_screen.query_one("#side-paste-flash", Static)


def _flash_text(app: AgentClipApp) -> str:
    return str(_flash(app).render())


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


@pytest.fixture(autouse=True)
def _no_real_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a real Ctrl+V escape into the test runner's window - every
    path through ``copy_outbound`` is monkeypatched here, even the ones that
    should never reach ``send_paste`` (no click region drawn)."""
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)


async def test_copy_outbound_turns_the_flash_on(tmp_path: Path) -> None:
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        assert _flash(app).display is False  # hidden until something is copied

        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True
        assert fake.read_text() == "the payload"  # the copy itself still happened


async def test_no_click_region_shows_ctrl_v_and_never_pastes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chat region drawn means no focus click, so the paste is never
    attempted - focus could be on any window, and blind-pasting is unsafe."""
    paste_calls: list[None] = []
    monkeypatch.setattr(main_mod, "send_paste", lambda: paste_calls.append(None) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        assert main._chat_region is None

        await main.copy_outbound("the payload")
        await pilot.pause()
        assert paste_calls == []
        assert PASTE_FLASH_TEXT.splitlines()[0] in _flash_text(app)


async def test_landed_click_pastes_and_shows_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known click target + a click that lands means AgentClip pastes the
    payload itself - the banner then only has to ask for Enter."""
    paste_calls: list[None] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region: True)
    monkeypatch.setattr(main_mod, "send_paste", lambda: paste_calls.append(None) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        main._chat_region = ScreenRegion(0, 0, 100, 20)

        await main.copy_outbound("the payload")
        await pilot.pause()
        assert paste_calls == [None]
        assert ENTER_FLASH_TEXT.splitlines()[0] in _flash_text(app)


async def test_busy_match_turns_the_flash_off(tmp_path: Path) -> None:
    """The busy region matching its mid-generation baseline means the model is
    chewing again - the Ctrl+V landed, so the nag stops."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True

        # Only a detector the poller was actually built with closes a tick, so
        # say the busy tracker is the live one before injecting its verdict.
        main._active_detectors = ("busy",)
        main.post_message(BusyProbed(BusyProbe(BusyState.MATCH, 0.01)))
        await _wait_for(pilot, lambda: _flash(app).display is False, "flash hidden on MATCH")

        # CHANGED probes (idle screen) must NOT re-show or re-hide anything.
        main.post_message(BusyProbed(BusyProbe(BusyState.CHANGED, 0.4)))
        await pilot.pause()
        assert _flash(app).display is False


async def test_a_caret_sized_stale_change_leaves_the_flash_up(tmp_path: Path) -> None:
    """The banner is asking for the Enter keystroke, so it may only come down
    once the send is provably detected. A blinking caret in the composer is a
    CHANGING stale probe with a tiny diff - if that took the nag down, the user
    would stop being told to press Enter before they had."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True

        main._active_detectors = ("stale",)
        for _ in range(main_mod.SEND_ARM_TICKS + 2):
            main.post_message(StaleProbed(StaleProbe(StaleState.CHANGING, 0.001, 0)))
            await pilot.pause()
        assert _flash(app).display is True
        assert main._copy_armed is False


async def test_a_sustained_stale_change_turns_the_flash_off(tmp_path: Path) -> None:
    """...and once the region really does start moving - the message sent, the
    answer streaming in - the nag has done its job and goes away with the arm."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True

        main._active_detectors = ("stale",)
        for _ in range(main_mod.SEND_ARM_TICKS):
            main.post_message(StaleProbed(StaleProbe(StaleState.CHANGING, 0.5, 0)))
            await pilot.pause()
        await _wait_for(pilot, lambda: _flash(app).display is False, "flash hidden on arm")
        assert main._copy_armed is True


async def test_clipboard_capture_turns_the_flash_off(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True

        main.post_message(ClipboardCaptured("a captured reply"))
        await _wait_for(pilot, lambda: _flash(app).display is False, "flash hidden on capture")


async def test_session_reset_turns_the_flash_off(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True

        await main.clear_transcript()  # the /new teardown hook
        await pilot.pause()
        assert _flash(app).display is False
