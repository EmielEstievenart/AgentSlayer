"""Pilot tests for the service editor (F2 / the sidebar's "Edit services..."
button): opening it, editing a built-in preset's numbers, adding a new one,
validation blocking a bad save, and that it doesn't disturb a locked sidebar
Select when opened mid-session.

Complements tests/test_config.py (headless save/load coverage) - here the
concern is the Textual wiring: F2/the button open it, escape persists valid
edits and propagates the new Config to app_config/MainScreen/the sidebar, and
invalid input never gets saved.

The editor also *reports* on a service's captured appearances and can forget
them, but it never captures one: drawing a box needs the browser on screen,
which is a main-screen job.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Button, Input, Select, Static

from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import load_profile, save_template
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.service_editor import TEMPLATES_NONE, ServiceEditorScreen


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, profile_root: Path) -> tuple[AgentClipApp, Path]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    global_path = tmp_path / "config.toml"
    config = load_config(project, global_config_path=global_path)
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        global_config_path=global_path,
        profile_root=profile_root,
    )
    return app, global_path


def _image(size: int = 32) -> RegionImage:
    """A capture big enough for Template.build to anchor."""
    return RegionImage(size, size, bytes(range(256)) * (size * size * 4 // 256))


async def _open_editor_via_f2(app: AgentClipApp, pilot: Pilot) -> None:
    await pilot.press("f2")
    await _wait_for(pilot, lambda: isinstance(app.screen, ServiceEditorScreen), "editor opened")


async def test_f2_opens_editor_edits_persist_and_sidebar_updates(
    tmp_path: Path, profile_root: Path
) -> None:
    app, global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        # Editor is reachable via F2 even while just waiting for a task (no session yet).
        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

        select = editor.query_one("#svc-select", Select)
        select.value = "claude"
        await pilot.pause()

        max_input = editor.query_one("#svc-max", Input)
        total_input = editor.query_one("#svc-total", Input)
        max_input.value = "30000"
        await pilot.pause()
        total_input.value = "999000"
        await pilot.pause()

        assert editor._current_error is None

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        # app_config was replaced with the edited services table.
        assert app.app_config.services["claude"].max_paste_chars == 30_000
        assert app.app_config.services["claude"].total_context_chars == 999_000

        # The sidebar picked up the new numbers (refresh_services was called).
        select_widget = main.sidebar.service_select
        claude_row = next(text for text, value in select_widget._options if value == "claude")  # type: ignore[attr-defined]
        assert "30k" in claude_row

        # And it was persisted to the global config.toml.
        raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
        assert raw["services"]["claude"]["max_paste_chars"] == 30_000
        assert raw["services"]["claude"]["total_context_chars"] == 999_000


async def test_add_new_service_appears_in_sidebar(
    tmp_path: Path, profile_root: Path
) -> None:
    app, global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

        select = editor.query_one("#svc-select", Select)
        select.value = "+add-new+"
        await pilot.pause()

        editor.query_one("#svc-key", Input).value = "my-llm"
        await pilot.pause()
        editor.query_one("#svc-label", Input).value = "My LLM"
        await pilot.pause()
        editor.query_one("#svc-max", Input).value = "8000"
        await pilot.pause()
        editor.query_one("#svc-total", Input).value = "300000"
        await pilot.pause()

        add_btn = editor.query_one("#svc-add-btn", Button)
        assert not add_btn.disabled
        await pilot.click("#svc-add-btn")
        await pilot.pause()

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        assert "my-llm" in app.app_config.services
        assert app.app_config.services["my-llm"].label == "My LLM"

        options = main.sidebar.service_select._options  # type: ignore[attr-defined]
        assert any(value == "my-llm" for _text, value in options)

        raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
        assert raw["services"]["my-llm"]["label"] == "My LLM"


async def test_max_exceeding_total_blocks_the_save(
    tmp_path: Path, profile_root: Path
) -> None:
    app, global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        original_claude = app.app_config.services["claude"]

        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

        select = editor.query_one("#svc-select", Select)
        select.value = "claude"
        await pilot.pause()

        # total below max: an invalid cross-field combination.
        editor.query_one("#svc-total", Input).value = "1000"
        await pilot.pause()
        editor.query_one("#svc-max", Input).value = "50000"
        await pilot.pause()

        assert editor._current_error is not None and "exceed" in editor._current_error

        # Escaping with an invalid pending edit asks for confirmation via ConfirmScreen.
        await pilot.press("escape")
        await _wait_for(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), "discard confirm shown"
        )
        # Deny: stays on the editor, nothing was saved.
        await pilot.press("n")
        await _wait_for(pilot, lambda: app.screen is editor, "back on the editor")

        # Confirm discard this time.
        await pilot.press("escape")
        await _wait_for(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), "discard confirm shown again"
        )
        await pilot.press("y")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        # The invalid value was never applied - claude is untouched, nothing persisted.
        assert app.app_config.services["claude"] == original_claude
        assert not global_path.exists()


async def test_stale_seconds_field_accepts_a_float_and_refuses_out_of_bounds(
    tmp_path: Path, profile_root: Path
) -> None:
    """The stale detector's stillness window is edited here, bounded by exactly
    what config.py enforces on load - so a value the editor accepts is never
    silently replaced on the next start."""
    app, global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()
        stable_input = editor.query_one("#svc-stable", Input)
        assert stable_input.value == "2.0"  # the built-in default, pre-filled

        stable_input.value = "6.5"
        await pilot.pause()
        assert editor._current_error is None
        assert editor._services["claude"].stable_seconds == 6.5

        # Below the 0.5 floor: rejected, and never applied to the working copy.
        stable_input.value = "0.2"
        await pilot.pause()
        assert editor._current_error is not None
        assert "between 0.5 and 60" in editor._current_error
        assert editor._services["claude"].stable_seconds == 6.5

        stable_input.value = "6.5"
        await pilot.pause()
        assert editor._current_error is None

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        assert app.app_config.services["claude"].stable_seconds == 6.5
        raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
        assert raw["services"]["claude"]["stable_seconds"] == 6.5


async def test_builtin_cannot_be_deleted_but_custom_can(
    tmp_path: Path, profile_root: Path
) -> None:
    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()
        assert not editor.query_one("#svc-delete-btn", Button).display
        assert editor.query_one("#svc-reset-btn", Button).display

        # Add a throwaway custom service, then delete it again.
        editor.query_one("#svc-select", Select).value = "+add-new+"
        await pilot.pause()
        editor.query_one("#svc-key", Input).value = "temp-svc"
        await pilot.pause()
        editor.query_one("#svc-label", Input).value = "Temp"
        await pilot.pause()
        editor.query_one("#svc-max", Input).value = "1000"
        await pilot.pause()
        editor.query_one("#svc-total", Input).value = "5000"
        await pilot.pause()
        await pilot.click("#svc-add-btn")
        await pilot.pause()
        assert "temp-svc" in editor._services

        delete_btn = editor.query_one("#svc-delete-btn", Button)
        assert delete_btn.display
        await pilot.click("#svc-delete-btn")
        await pilot.pause()
        assert "temp-svc" not in editor._services

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")
        assert "temp-svc" not in app.app_config.services


async def test_editing_services_mid_session_does_not_unlock_the_sidebar(
    tmp_path: Path, profile_root: Path
) -> None:
    """The Select is locked for the life of a session; opening/using the editor
    mid-session must leave that lock exactly as it was (tui.md section 1.3)."""
    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        main.composer.load_text("Do something.")
        await pilot.press("enter")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        assert main.sidebar.service_select.disabled  # locked while a session runs

        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)
        editor.query_one("#svc-select", Select).value = "gemini"
        await pilot.pause()
        editor.query_one("#svc-max", Input).value = "20000"
        await pilot.pause()

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        assert app.app_config.services["gemini"].max_paste_chars == 20_000
        # Still locked: the edit didn't touch the active session's picker state.
        assert main.sidebar.service_select.disabled


# -- the appearance readout -------------------------------------------------


async def test_the_editor_reports_what_a_service_looks_like(
    tmp_path: Path, profile_root: Path
) -> None:
    """The editor is where a user reasons about a service, so "does this one
    know what its copy button looks like?" has to be answerable here - even
    though the capture itself lives on the main screen."""
    save_template(profile_root, "claude", TemplateKind.COPY, _image())
    save_template(profile_root, "claude", TemplateKind.BUSY, _image())

    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()
        line = str(editor.query_one("#svc-templates", Static).render())
        assert "2/6 captured" in line
        assert "busy indicator" in line
        assert "copy button" in line
        assert editor.query_one("#svc-forget-templates-btn", Button).display

        # A service with nothing captured says so, and offers nothing to forget.
        editor.query_one("#svc-select", Select).value = "gemini"
        await pilot.pause()
        assert TEMPLATES_NONE in str(editor.query_one("#svc-templates", Static).render())
        assert not editor.query_one("#svc-forget-templates-btn", Button).display


async def test_forgetting_an_appearance_leaves_the_preset_alone(
    tmp_path: Path, profile_root: Path
) -> None:
    """Deliberately separate from "Delete": a user whose browser theme changed
    wants to recapture, not to lose their size settings."""
    save_template(profile_root, "claude", TemplateKind.COPY, _image())

    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)
        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()
        before = editor._services["claude"]

        await pilot.click("#svc-forget-templates-btn")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "confirm shown")
        await pilot.press("y")
        await _wait_for(pilot, lambda: app.screen is editor, "back on the editor")

        assert not load_profile(profile_root, "claude").captured
        assert TEMPLATES_NONE in str(editor.query_one("#svc-templates", Static).render())
        assert editor._services["claude"] == before  # the preset is untouched


async def test_declining_keeps_the_appearance(tmp_path: Path, profile_root: Path) -> None:
    save_template(profile_root, "claude", TemplateKind.COPY, _image())

    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)
        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()

        await pilot.click("#svc-forget-templates-btn")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "confirm shown")
        await pilot.press("n")
        await _wait_for(pilot, lambda: app.screen is editor, "back on the editor")

        assert load_profile(profile_root, "claude").has(TemplateKind.COPY)
