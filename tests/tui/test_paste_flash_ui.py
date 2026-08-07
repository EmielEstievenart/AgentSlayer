"""Pilot tests for the sidebar's blinking "PRESS CTRL+V" banner.

The banner turns on the moment an outbound payload lands on the clipboard
(``copy_outbound``) and keeps blinking until something proves the moment
passed: the busy region reports the model generating again (the paste landed),
a new clipboard capture arrives (the conversation moved on without it), or the
session is reset. The blink itself is a timer toggling a CSS class - display
on/off is the observable contract, so that is what these tests pin down.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Static

from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed, ClipboardCaptured
from agentclip.tui.screens.main import MainScreen


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


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


async def test_copy_outbound_turns_the_flash_on(tmp_path: Path) -> None:
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        assert _flash(app).display is False  # hidden until something is copied

        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True
        assert fake.read_text() == "the payload"  # the copy itself still happened


async def test_busy_match_turns_the_flash_off(tmp_path: Path) -> None:
    """The busy region matching its mid-generation baseline means the model is
    chewing again - the Ctrl+V landed, so the nag stops."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _flash(app).display is True

        main.post_message(BusyProbed(BusyProbe(BusyState.MATCH, 0.01)))
        await _wait_for(pilot, lambda: _flash(app).display is False, "flash hidden on MATCH")

        # CHANGED probes (idle screen) must NOT re-show or re-hide anything.
        main.post_message(BusyProbed(BusyProbe(BusyState.CHANGED, 0.4)))
        await pilot.pause()
        assert _flash(app).display is False


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
