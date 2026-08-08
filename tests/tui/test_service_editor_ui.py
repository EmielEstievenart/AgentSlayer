"""Pilot tests for the service editor (F2 / the sidebar's "Edit services..."
button): opening it, editing a built-in preset's numbers, adding a new one,
validation blocking a bad save, and that it doesn't disturb a locked sidebar
Select when opened mid-session.

Complements tests/test_config.py (headless save/load coverage) - here the
concern is the Textual wiring: F2/the button open it, escape persists valid
edits and propagates the new Config to app_config/MainScreen/the sidebar, and
invalid input never gets saved.

The editor is also the whole per-service *profile* editor now: it captures the
six appearances (covered end to end in test_profile_capture_ui.py) and edits the
finish-signal checklist plus the hover-scan opt-in, both of which ride the same
close-and-persist path as the size fields.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, Input, Select, Static

from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import load_profile, profile_dir, save_template
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.service_editor import (
    SIGNAL_UNCAPTURED,
    TEMPLATES_NONE,
    ServiceEditorScreen,
    capture_button_id,
    signal_checkbox_id,
)
from agentclip.tui.widgets.sidebar import Sidebar


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


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


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


# -- the sidebar's side of the hand-off -------------------------------------


def _watch_service_events(
    monkeypatch: pytest.MonkeyPatch, sidebar: Sidebar
) -> list[Sidebar.ServiceChanged]:
    """Record every domain event this sidebar posts about the service picker."""
    seen: list[Sidebar.ServiceChanged] = []
    original = sidebar.post_message

    def spy(message: Message) -> bool:
        if isinstance(message, Sidebar.ServiceChanged):
            seen.append(message)
        return original(message)

    monkeypatch.setattr(sidebar, "post_message", spy)
    return seen


async def test_refreshing_the_picker_is_silent_when_the_selection_survives(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``set_options`` resets the value to the first option before the selection
    can be put back, so a plain rebuild announced two service switches for an
    edit that changed nothing about which service is selected - each one a
    profile reload, a readiness re-check and a detector restart."""
    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        sidebar = main.sidebar
        before = sidebar.service
        seen = _watch_service_events(monkeypatch, sidebar)

        sidebar.refresh_services(app.app_config)
        await pilot.pause()

        assert seen == []
        assert sidebar.service == before


async def test_refreshing_the_picker_reports_a_selection_it_had_to_drop(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case that IS a service switch: the selected preset was deleted,
    so the picker falls back - and everything downstream of "what does this
    service look like?" has to follow."""
    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        sidebar = main.sidebar
        gone = sidebar.service
        seen = _watch_service_events(monkeypatch, sidebar)

        services = {k: v for k, v in app.app_config.services.items() if k != gone}
        sidebar.refresh_services(replace(app.app_config, services=services))
        await pilot.pause()

        assert [message.key for message in seen] == [sidebar.service]
        assert sidebar.service != gone


# -- the appearance readout -------------------------------------------------


async def test_the_editor_reports_what_a_service_looks_like(
    tmp_path: Path, profile_root: Path
) -> None:
    """The editor is where a user reasons about a service, so "does this one
    know what its copy button looks like?" has to be answerable here - and it
    is the place the answer is changed, so the readout is re-derived from disk
    on every selection change."""
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
        # ...and the per-kind lines it captures from say the same thing.
        assert "not captured" in str(editor.query_one("#svc-tpl-copy", Static).render())


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


async def test_forgetting_an_appearance_reaches_the_main_screen(
    tmp_path: Path, profile_root: Path
) -> None:
    """The editor deletes the PNGs; the main screen is what caches them, paints
    them and hunts for them. Without the deletion reaching it, the sidebar kept
    saying "captured" and the poller kept looking for a template that was gone -
    and closing with the presets untouched used to report exactly nothing."""
    app, global_path = _make_app(tmp_path, profile_root)
    key = app.app_config.general.service
    save_template(profile_root, key, TemplateKind.COPY, _image())

    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._active_profile().has(TemplateKind.COPY)
        assert "1/6 captured" in _label(app, "#side-profile-note")

        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)
        editor.query_one("#svc-select", Select).value = key
        await pilot.pause()

        await pilot.click("#svc-forget-templates-btn")
        await _wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "confirm shown")
        await pilot.press("y")
        await _wait_for(pilot, lambda: app.screen is editor, "back on the editor")

        # Nothing about the presets changed - this close used to dismiss None.
        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        assert not main._active_profile().has(TemplateKind.COPY)  # the cache was invalidated
        assert "0/6 captured" in _label(app, "#side-profile-note")
        assert not global_path.exists()  # no preset edit to persist


async def test_deleting_a_service_takes_its_appearance_with_it(
    tmp_path: Path, profile_root: Path
) -> None:
    """A deleted key is in no picker, so its folder of PNGs is unreachable from
    anywhere in the app - leaving it behind is leaving litter no user can act on."""
    app, _global_path = _make_app(tmp_path, profile_root)

    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

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

        save_template(profile_root, "temp-svc", TemplateKind.COPY, _image())
        assert profile_dir(profile_root, "temp-svc").exists()

        await pilot.click("#svc-delete-btn")
        await pilot.pause()
        assert "temp-svc" not in editor._services
        assert not profile_dir(profile_root, "temp-svc").exists()

        # ...and a reset is NOT a delete: it restores numbers, never captures.
        save_template(profile_root, "claude", TemplateKind.COPY, _image())
        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()
        await pilot.click("#svc-reset-btn")
        await pilot.pause()
        assert load_profile(profile_root, "claude").has(TemplateKind.COPY)


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


# -- the detection checklist ------------------------------------------------


def _tick(editor: ServiceEditorScreen, signal: str, on: bool) -> None:
    editor.query_one(f"#{signal_checkbox_id(signal)}", Checkbox).value = on


async def test_the_checklist_and_hover_scan_round_trip_into_the_saved_services(
    tmp_path: Path, profile_root: Path
) -> None:
    """``finish_signals``/``hover_scan`` are per-service policy, so they ride the
    same working-copy-then-persist path as the size fields - no separate save,
    no separate propagation."""
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
        # The shipped default: stale only, no hover scan.
        assert not editor.query_one(f"#{signal_checkbox_id('busy')}", Checkbox).value
        assert editor.query_one(f"#{signal_checkbox_id('stale')}", Checkbox).value
        assert not editor.query_one("#svc-hover-scan", Checkbox).value

        _tick(editor, "busy", True)
        await pilot.pause()
        _tick(editor, "stale", False)
        await pilot.pause()
        editor.query_one("#svc-hover-scan", Checkbox).value = True
        await pilot.pause()

        # Canonical order, whatever order the boxes were ticked in.
        assert editor._services["claude"].finish_signals == ("busy",)
        assert editor._services["claude"].hover_scan is True

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        assert app.app_config.services["claude"].finish_signals == ("busy",)
        assert app.app_config.services["claude"].hover_scan is True
        raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
        assert raw["services"]["claude"]["finish_signals"] == ["busy"]
        assert raw["services"]["claude"]["hover_scan"] is True


async def test_the_checkboxes_follow_the_selected_service(
    tmp_path: Path, profile_root: Path
) -> None:
    """Four toggles, one per service: switching the picker must reload them, and
    the echo Textual fires while they are being written must not leak the old
    service's answers into the new one."""
    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)
        before_gemini = editor._services["gemini"]

        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()
        _tick(editor, "idle", True)
        await pilot.pause()
        assert editor._services["claude"].finish_signals == ("idle", "stale")

        editor.query_one("#svc-select", Select).value = "gemini"
        await pilot.pause()
        assert not editor.query_one(f"#{signal_checkbox_id('idle')}", Checkbox).value
        assert editor._services["gemini"] == before_gemini


async def test_a_ticked_signal_with_nothing_captured_warns_until_it_is_captured(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checklist and the profile are ANDed, so a ticked busy signal with no
    busy appearance runs nothing at all - invisible until an auto-copy never
    fires, hence the inline warning next to the tick that caused it."""
    import agentclip.tui.screens.service_editor as editor_mod
    from agentclip.screen.region import ScreenRegion

    box = ScreenRegion(10, 10, 32, 32)
    monkeypatch.setattr(editor_mod, "pick_region", lambda prompt=None: box)
    monkeypatch.setattr(editor_mod, "capture_region", lambda region: _image(32))

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
        warning = editor.query_one("#svc-signal-warning", Static)
        assert str(warning.render()) == ""  # stale is ticked, and stale needs nothing

        _tick(editor, "busy", True)
        await pilot.pause()
        assert SIGNAL_UNCAPTURED in str(warning.render())
        assert "busy indicator" in str(warning.render())

        await pilot.click(f"#{capture_button_id(TemplateKind.BUSY)}")
        await _wait_for(
            pilot,
            lambda: load_profile(profile_root, "claude").has(TemplateKind.BUSY),
            "busy captured",
        )
        await _wait_for(pilot, lambda: str(warning.render()) == "", "the warning cleared")

        # Unticking is the other way out of it.
        _tick(editor, "idle", True)
        await pilot.pause()
        assert "idle indicator" in str(warning.render())
        _tick(editor, "idle", False)
        await pilot.pause()
        assert str(warning.render()) == ""


async def test_captures_and_checklist_are_inert_until_the_service_exists(
    tmp_path: Path, profile_root: Path
) -> None:
    """There is no key to file a PNG or a checklist under until "Add service"
    has run, so the controls are disabled rather than hidden - the column must
    not reflow while the form is being filled in."""
    app, _global_path = _make_app(tmp_path, profile_root)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _open_editor_via_f2(app, pilot)
        editor = app.screen
        assert isinstance(editor, ServiceEditorScreen)

        editor.query_one("#svc-select", Select).value = "+add-new+"
        await pilot.pause()
        assert all(
            button.disabled
            for button in editor.query(f"#{capture_button_id(TemplateKind.BUSY)}").results(Button)
        )
        assert all(box.disabled for box in editor.query(Checkbox))
        assert TEMPLATES_NONE in str(editor.query_one("#svc-templates", Static).render())

        editor.query_one("#svc-select", Select).value = "claude"
        await pilot.pause()
        assert not editor.query_one(f"#{capture_button_id(TemplateKind.BUSY)}", Button).disabled
        assert not any(box.disabled for box in editor.query(Checkbox))
