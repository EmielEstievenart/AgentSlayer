"""Pilot tests for the auto-submit tap (``ServicePreset.auto_submit``).

``copy_outbound`` normally ends a successful paste in WAIT_SEND and waits for
the user's Enter; a service that opted in gets a synthetic Enter tapped right
after the paste instead. What is at stake here: the tap happens ONLY after a
successful paste, only for a service that asked for it, the loop still comes
out in WAIT_SEND (the send gate's evidence - not the tap - is what moves it
on), and the sidebar flash says whose Enter it now is.

Everything that touches the OS is monkeypatched at the *use site*
(``main_mod.click_region`` / ``main_mod.send_paste`` / ``main_mod.send_enter``,
which main.py from-imports) - a real ``send_enter`` here would SEND whatever
sits in the window running the suite.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.region import ScreenRegion
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.main import MainScreen
from agentclip.shell.tui.widgets.sidebar import AUTO_SEND_FLASH_TEXT, ENTER_FLASH_TEXT
from tests.shell.tui.conftest import patch_os


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, *, auto_submit: bool) -> tuple[AgentClipApp, FakeClipboard]:
    """An app whose default service does (or does not) tap Enter itself."""
    project = tmp_path / "project"
    project.mkdir()
    global_path = tmp_path / "config.toml"
    global_path.write_text(
        f"[services.chatgpt-attach]\nauto_submit = {str(auto_submit).lower()}\n",
        encoding="utf-8",
    )
    config = load_config(project, global_config_path=global_path)
    assert config.preset().auto_submit is auto_submit
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
    )
    return app, fake


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main._chat_region = ScreenRegion(0, 0, 100, 20)  # something to click, so the paste runs
    return main


def _flash_text(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-paste-flash", Static).render())


@pytest.fixture(autouse=True)
def _no_real_input(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_os(monkeypatch, "click_region", lambda region: True)
    patch_os(monkeypatch, "send_paste", lambda: True)


async def test_a_successful_paste_taps_enter_for_an_opted_in_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taps: list[None] = []
    patch_os(monkeypatch, "send_enter", lambda: taps.append(None) or True)
    app, _fake = _make_app(tmp_path, auto_submit=True)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()

        assert taps == [None]
        # Still WAIT_SEND: the tap is an attempt, and only the send gate's own
        # evidence (button vanishing, busy icon) says the send actually landed.
        assert main._loop_state is LoopState.WAIT_SEND
        assert AUTO_SEND_FLASH_TEXT.splitlines()[0] in _flash_text(app)


async def test_no_tap_without_the_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default service behaves exactly as before this existed: pasted,
    waiting on the user's Enter, and asked for it."""
    taps: list[None] = []
    patch_os(monkeypatch, "send_enter", lambda: taps.append(None) or True)
    app, _fake = _make_app(tmp_path, auto_submit=False)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()

        assert taps == []
        assert main._loop_state is LoopState.WAIT_SEND
        assert ENTER_FLASH_TEXT.splitlines()[0] in _flash_text(app)


async def test_no_tap_when_the_paste_never_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Enter into a chat box that holds nothing (or into an unknown window,
    when the click was refused) is exactly the accident the pasted-first order
    exists to prevent."""
    taps: list[None] = []
    patch_os(monkeypatch, "send_paste", lambda: False)
    patch_os(monkeypatch, "send_enter", lambda: taps.append(None) or True)
    app, _fake = _make_app(tmp_path, auto_submit=True)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()

        assert taps == []
        assert main._loop_state is LoopState.MANUAL_INSERT


async def test_a_refused_tap_falls_back_to_asking_for_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``send_enter`` returning False means nothing was typed - so the flash
    must keep asking the user for their own Enter, not claim it was sent."""
    patch_os(monkeypatch, "send_enter", lambda: False)
    app, _fake = _make_app(tmp_path, auto_submit=True)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()

        assert main._loop_state is LoopState.WAIT_SEND
        assert ENTER_FLASH_TEXT.splitlines()[0] in _flash_text(app)
