"""Pilot tests for the click-region flow (sidebar "Set click region..." button).

Mirrors test_chat_region_ui.py. The click region is where AgentClip pokes once a
model response is fully handled; it wins over the chat region, which is only the
fallback for users who drew the window and nothing else. Picker and click are
monkeypatched at their use site (agentclip.tui.screens.main) - the real ones
spawn a tkinter overlay and move the OS cursor. What we verify is the wiring:
button -> picker -> sidebar label + session state, the click resolution rule
(click region first, chat region second, no region = no click), and the
session-scoped reset on /new.
"""

from __future__ import annotations

import threading
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

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
CLICK_REGION = ScreenRegion(1200, 800, 400, 90)


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


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


def _click_label(app: AgentClipApp) -> str:
    return _label(app, "#side-click")


def _region_label(app: AgentClipApp) -> str:
    return _label(app, "#side-region")


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """Type into the composer and send - refocusing it first, since clicking the
    sidebar button leaves focus on the button."""
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.focus()
    await pilot.pause()
    await pilot.press("enter")


async def test_pick_click_region_updates_sidebar_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CLICK_REGION)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not set" in _click_label(app)

        await _press(app, pilot, "#set-click-btn")
        await _wait_for(pilot, lambda: main._click_region == CLICK_REGION, "click region adopted")
        assert "400×90 at (1200, 800)" in _click_label(app)
        assert main._chat_region is None  # the two regions are independent


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-click-btn")
        await pilot.pause(0.2)
        assert main._click_region is None
        assert "not set" in _click_label(app)


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

        await _press(app, pilot, "#set-click-btn")
        await pilot.pause(0.2)
        assert main._click_region is None  # error notified; app carries on
        assert "not set" in _click_label(app)


async def test_click_region_wins_over_chat_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both drawn: the outbound copy clicks the click region, never the window."""
    clicks: list[ScreenRegion] = []
    picked: list[ScreenRegion] = [CHAT_REGION, CLICK_REGION]
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: picked.pop(0))
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
        await _press(app, pilot, "#set-click-btn")
        await _wait_for(pilot, lambda: main._click_region == CLICK_REGION, "click region adopted")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert fake.read_text() is not None  # the bootstrap really was copied
        assert clicks == [CLICK_REGION]

        # A follow-up is another outbound copy - another click, same region.
        await _send(app, pilot, "Also say goodbye.")
        await _wait_for(pilot, lambda: len(clicks) == 2, "follow-up copy clicked")
        await _wait_for(pilot, lambda: not main.busy, "follow-up flow settled")
        assert clicks == [CLICK_REGION, CLICK_REGION]

        # /new tears the session down: both regions are session-scoped.
        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._click_region is None
        assert main._chat_region is None
        assert "not set" in _click_label(app)
        assert "not set" in _region_label(app)

        # With neither region drawn, the next bootstrap copies without clicking.
        await _send(app, pilot, "Fresh session task.")
        await _wait_for(pilot, lambda: main.session_active, "second session armed")
        await _wait_for(pilot, lambda: not main.busy, "second session flow settled")
        assert len(clicks) == 2


async def test_chat_region_is_the_fallback_when_no_click_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the window drawn: today's behaviour is preserved - it gets clicked."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
        assert main._click_region is None

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [CHAT_REGION]


async def test_click_region_alone_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chat region at all: the click region still drives the focus click."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CLICK_REGION)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-click-btn")
        await _wait_for(pilot, lambda: main._click_region == CLICK_REGION, "click region adopted")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [CLICK_REGION]


async def test_second_button_is_refused_while_an_overlay_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a worker cannot kill the child overlay process, so a press
    while a picker is up must be refused outright - never a second overlay."""
    overlay_open = threading.Event()
    finish_pick = threading.Event()
    picks = 0

    def slow_pick(prompt: str | None = None) -> ScreenRegion:
        nonlocal picks
        picks += 1
        overlay_open.set()
        assert finish_pick.wait(timeout=10.0), "test never released the overlay"
        return CHAT_REGION

    monkeypatch.setattr(main_mod, "pick_region", slow_pick)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: overlay_open.is_set(), "first overlay up")

        # Both other picker buttons bounce off while the overlay is open.
        await _press(app, pilot, "#set-click-btn")
        await _press(app, pilot, "#set-busy-btn")
        await pilot.pause(0.2)
        assert picks == 1

        finish_pick.set()
        await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
        assert main._click_region is None

        # The guard releases once the overlay resolves: picking works again.
        finish_pick.set()  # stub reused; let it return immediately this time
        await _press(app, pilot, "#set-click-btn")
        await _wait_for(pilot, lambda: picks == 2, "second picker allowed after the first closed")
