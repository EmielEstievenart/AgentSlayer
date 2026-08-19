"""Pilot tests for SettingsScreen (F4 - F3 is MainScreen's sidebar toggle):
opening it, live theme preview, cancel reverting the preview, and Save
persisting the choice to config.toml.

Complements tests/test_config.py (headless save_theme/load_config coverage) -
here the concern is the Textual wiring: F4 opens the screen, selecting a
RadioButton applies app.theme immediately, escape/Cancel restores whatever
was active on open, and Save writes through to disk via the injectable
global_config_path (same pattern test_service_editor_ui.py uses).

The tail of the file covers the OTHER door onto the same setting - the
``ChatView`` seam `/theme` drives (``MainScreen.theme_choices`` /
``current_theme`` / ``apply_theme``) - and the fact that there is no third one:
Textual's ctrl+p palette is disabled.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, RadioSet

import agentclip.shell.tui.app as app_module
from agentclip.cli import make_engine_factory
from agentclip.config import VALID_THEMES, load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.settings import THEME_CHOICES, SettingsScreen


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> tuple[AgentClipApp, Path]:
    project = tmp_path / "project"
    project.mkdir()
    global_path = tmp_path / "config.toml"
    config = load_config(project, global_config_path=global_path)
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        global_config_path=global_path,
    )
    return app, global_path


async def _open_settings_via_f4(app: AgentClipApp, pilot: Pilot) -> None:
    await pilot.press("f4")
    await _wait_for(pilot, lambda: isinstance(app.screen, SettingsScreen), "settings opened")


async def test_both_claude_themes_are_registered_on_mount(tmp_path: Path) -> None:
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "claude-warm" in app.available_themes
        assert "claude-dark" in app.available_themes
        assert "textual-light" in app.available_themes
        assert "textual-dark" in app.available_themes


async def test_f4_opens_settings_and_default_theme_is_textual_dark(tmp_path: Path) -> None:
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert app.theme == "textual-dark"

        await _open_settings_via_f4(app, pilot)
        assert isinstance(app.screen, SettingsScreen)


async def test_selecting_claude_dark_applies_it_live(tmp_path: Path) -> None:
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_settings_via_f4(app, pilot)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)

        await pilot.click("#theme-claude-dark")
        await pilot.pause()

        assert app.theme == "claude-dark"
        # still on the settings screen - selecting is a preview, not a close.
        assert app.screen is screen


async def test_cancel_reverts_the_live_preview(tmp_path: Path) -> None:
    app, global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert app.theme == "textual-dark"

        await _open_settings_via_f4(app, pilot)
        await pilot.click("#theme-claude-warm")
        await pilot.pause()
        assert app.theme == "claude-warm"  # previewed

        await pilot.click("#settings-cancel-btn")
        await _wait_for(pilot, lambda: app.screen is main, "settings closed back to the chat")

        assert app.theme == "textual-dark"  # reverted
        assert app.app_config.general.theme == "textual-dark"  # never persisted
        assert not global_path.exists()


async def test_escape_also_reverts_the_live_preview(tmp_path: Path) -> None:
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_settings_via_f4(app, pilot)
        await pilot.click("#theme-claude-dark")
        await pilot.pause()
        assert app.theme == "claude-dark"

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "settings closed back to the chat")
        assert app.theme == "textual-dark"


async def test_save_persists_the_theme_and_it_sticks(tmp_path: Path) -> None:
    app, global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_settings_via_f4(app, pilot)
        await pilot.click("#theme-claude-dark")
        await pilot.pause()

        await pilot.click("#settings-save-btn")
        await _wait_for(pilot, lambda: app.screen is main, "settings closed back to the chat")

        # sticks live and in app_config
        assert app.theme == "claude-dark"
        assert app.app_config.general.theme == "claude-dark"

        # and it was persisted to the global config.toml
        raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
        assert raw["general"]["theme"] == "claude-dark"

        # reloading config.toml from disk reproduces the choice
        reloaded = load_config(app.project_root, global_config_path=global_path)
        assert reloaded.general.theme == "claude-dark"


async def test_opening_settings_twice_does_not_stack_screens(tmp_path: Path) -> None:
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_settings_via_f4(app, pilot)
        screen = app.screen
        await pilot.press("f4")  # already open - action_preferences should no-op
        await pilot.pause()
        assert app.screen is screen

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "settings closed back to the chat")


async def test_radio_set_pressed_button_matches_selected_theme(tmp_path: Path) -> None:
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_settings_via_f4(app, pilot)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        radio_set = screen.query_one("#theme-radio-set", RadioSet)
        pressed = radio_set.pressed_button
        assert pressed is not None
        assert pressed.id == "theme-textual-dark"

        await pilot.click("#theme-claude-warm")
        await pilot.pause()
        pressed = radio_set.pressed_button
        assert pressed is not None
        assert pressed.id == "theme-claude-warm"


# == the other door: /theme and no command palette =============================
# F4's picker and the chat command are two ways to one setting, so they share
# the list (THEME_CHOICES) and the save path (AgentClipApp.remember_theme).
# There is no third way in: Textual's palette is switched off, because the page
# has none and a TUI-only command surface listing a different set of things is
# how a feature comes to exist in half the app.


def test_the_command_palette_is_disabled() -> None:
    """Commands are typed into the composer with a leading slash, in both
    shells. A palette would be a second, TUI-only roster."""
    assert AgentClipApp.ENABLE_COMMAND_PALETTE is False


async def test_the_theme_seam_offers_exactly_what_the_picker_offers(
    tmp_path: Path,
) -> None:
    """One list, in the order the settings screen offers them - a `/theme` that
    could set something F4 will not show would be a second source of truth."""
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        assert main.theme_choices() == tuple(name for name, _label in THEME_CHOICES)
        assert set(main.theme_choices()) == set(VALID_THEMES)
        assert main.current_theme() == "textual-dark"


async def test_the_theme_seam_applies_and_persists_like_save_does(tmp_path: Path) -> None:
    """`/theme`'s whole mechanism: worn live (the app-wide reactive) and written
    to the same `[general] theme` key Save writes, so the next launch reads it."""
    app, global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        main.apply_theme("claude-warm")
        await pilot.pause()

        assert app.theme == "claude-warm"
        assert main.current_theme() == "claude-warm"
        assert app.app_config.general.theme == "claude-warm"
        raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
        assert raw["general"]["theme"] == "claude-warm"
        reloaded = load_config(app.project_root, global_config_path=global_path)
        assert reloaded.general.theme == "claude-warm"


async def test_an_unsavable_theme_still_gets_worn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trade ``_persist_services`` makes: remembering a preference is a
    convenience, never the point of the press - and only the failure to remember
    is the view's to report, since the command's own toast is the controller's."""
    app, _global_path = _make_app(tmp_path)

    def boom(theme: str, path: Path | None = None) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(app_module, "save_theme", boom)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        assert app.remember_theme("claude-dark") is False
        main.apply_theme("claude-dark")
        await pilot.pause()

        assert app.theme == "claude-dark"  # worn anyway
        assert app.app_config.general.theme == "claude-dark"


async def test_button_pressed_events_are_stopped(tmp_path: Path) -> None:
    """Sanity check: pressing Save/Cancel doesn't also toggle a Button
    handler outside the screen (event.stop() is called in both handlers)."""
    app, _global_path = _make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_settings_via_f4(app, pilot)
        save_btn = app.screen.query_one("#settings-save-btn", Button)
        assert save_btn.variant == "primary"
        cancel_btn = app.screen.query_one("#settings-cancel-btn", Button)
        assert cancel_btn.variant == "default"
