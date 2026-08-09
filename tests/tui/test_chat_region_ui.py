"""Pilot tests for the chat-region flow (sidebar "Set chat region..." button).

The chat region is the window that hosts the chatbot; it is also the fallback
target of the post-response click when no click region is drawn (that one has
its own suite: test_click_region_ui.py).

The real picker spawns a tkinter overlay in a child process and the real click
moves the OS cursor - neither belongs in a test run, so both are monkeypatched
at their use site (agentclip.tui.screens.main). What we verify is the wiring:
button -> picker -> sidebar label + session state, click fired after every
outbound copy, and the calibration surviving /new.
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
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp

from .conftest import send_composer

REGION = ScreenRegion(1050, 340, 812, 540)


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


def _region_label(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    label = app.main_screen.query_one("#side-region", Static)
    return str(label.render())


async def _click_set_region(app: AgentClipApp, pilot: Pilot) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one("#set-region-btn", Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click("#set-region-btn")


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """Send a composer line - see ``send_composer`` for why /new takes two Enters."""
    await send_composer(app, pilot, text)


async def test_pick_region_updates_sidebar_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: REGION)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not set" in _region_label(app)

        await _click_set_region(app, pilot)
        await _wait_for(pilot, lambda: main._chat_region == REGION, "region adopted")
        assert "812×540 at (1050, 340)" in _region_label(app)
        assert "chatbot window" in _region_label(app)


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_region(app, pilot)
        await pilot.pause(0.2)
        assert main._chat_region is None
        assert "not set" in _region_label(app)


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    monkeypatch.setattr(main_mod, "pick_region", boom)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_region(app, pilot)
        await pilot.pause(0.2)
        assert main._chat_region is None  # error notified; app carries on


async def test_outbound_copy_clicks_the_region_and_it_survives_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: with only the chat region drawn, every outbound copy
    (here: the bootstrap, then a follow-up) fires a focus click at it - it is the
    fallback click target. /new starts a fresh session, but the region describes
    where the window is, not what the session said, so it survives."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: REGION)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        # Draw the box before the session starts - the bootstrap copy must click.
        await _click_set_region(app, pilot)
        await _wait_for(pilot, lambda: main._chat_region == REGION, "region adopted")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert fake.read_text() is not None  # the bootstrap really was copied
        assert clicks == [REGION]

        # A follow-up is another outbound copy - another click.
        await _send(app, pilot, "Also say goodbye.")
        await _wait_for(pilot, lambda: len(clicks) == 2, "follow-up copy clicked")
        await _wait_for(pilot, lambda: not main.busy, "follow-up flow settled")

        # /new tears the session down, but the calibration outlives it.
        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._chat_region == REGION
        assert REGION.describe() in _region_label(app)

        # The surviving region means the next bootstrap clicks it again.
        await _send(app, pilot, "Fresh session task.")
        await _wait_for(pilot, lambda: main.session_active, "second session armed")
        await _wait_for(pilot, lambda: not main.busy, "second session flow settled")
        assert clicks == [REGION, REGION, REGION]
