"""Pilot tests for the new-chat button: captured per SERVICE, found in the
drawn chat region, clicked where it actually is.

Two buttons: "Capture new-chat button..." files what the browser's new-chat
control looks like into the active service's profile, and "New browser chat"
searches for it inside the calibrating slot's chat region and clicks the match.
Picker, capture, search, click and focus are monkeypatched at their use site
(agentclip.tui.screens.main).

The find step is the point, and it now buys two things at once: a browser that
re-laid itself out or moved gets clicked *where the button is* rather than
where it used to be, and a button that genuinely is not on screen gets no click
at all. The three failures stay three different stories - nothing captured,
not on screen, and the OS refusing the click.
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
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import load_profile
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import RegionMatch, Template
from agentclip.tui.app import AgentClipApp

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
NEWCHAT_BOX = ScreenRegion(120, 90, 180, 36)
# Where the button "is" inside the chat region, and the absolute rect that
# implies - the click has to land on the second one, never the first.
FOUND = RegionMatch(x=40, y=24, diff=0.02)
CLICK_TARGET = ScreenRegion(
    CHAT_REGION.left + FOUND.x, CHAT_REGION.top + FOUND.y, NEWCHAT_BOX.width, NEWCHAT_BOX.height
)
SIZE = (110, 100)


def _frame(region: ScreenRegion) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, profile_root: Path) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        profile_root=profile_root,
    )
    return app, fake


def _newchat_label(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-tpl-new-chat", Static).render())


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.focus()
    await pilot.pause()
    await pilot.press("enter")


def _patch_found(monkeypatch: pytest.MonkeyPatch, match: RegionMatch | None) -> list[RegionImage]:
    """Say whether the captured button is on screen, and record every search."""
    scenes: list[RegionImage] = []

    def fake_find(template: Template, scene: RegionImage, **kw: object) -> RegionMatch | None:
        scenes.append(scene)
        return match

    monkeypatch.setattr(main_mod, "find_in_region", fake_find)
    return scenes


async def _capture_newchat(
    app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch, *, region: bool = True
) -> None:
    """Draw the chat window (optional) and capture the new-chat appearance."""
    main = app.main_screen
    assert main is not None
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    if region:
        monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: NEWCHAT_BOX)
    await _press(app, pilot, "#capture-new-chat-btn")
    # The LABEL, not the in-memory profile: the profile is populated before the
    # save and the repaint, so waiting on it can outrun the sidebar.
    await _wait_for(
        pilot,
        lambda: "180×36 · captured" in _newchat_label(app),
        "new-chat button captured and painted",
    )


async def test_capturing_the_button_files_it_under_the_service(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not captured" in _newchat_label(app)
        key = main._selected_service()

        await _capture_newchat(app, pilot, monkeypatch, region=False)
        template = main._active_profile().get(TemplateKind.NEW_CHAT)
        assert template is not None
        assert (template.width, template.height) == (180, 36)
        assert "180×36 · captured" in _newchat_label(app)
        assert load_profile(profile_root, key).has(TemplateKind.NEW_CHAT)


async def test_capture_failure_keeps_it_unknown(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: NEWCHAT_BOX)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#capture-new-chat-btn")
        await pilot.pause(0.2)
        assert not main._active_profile().has(TemplateKind.NEW_CHAT)
        assert "not captured" in _newchat_label(app)


async def test_the_action_finds_it_clicks_it_and_hands_focus_back(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The click lands on the match's absolute rectangle, not on the box the
    user happened to drag - that is the whole difference."""
    events: list[str] = []
    clicks: list[tuple[ScreenRegion, float]] = []
    focus_calls: list[int] = []
    monkeypatch.setattr(main_mod, "foreground_window", lambda: 4242)
    monkeypatch.setattr(
        main_mod,
        "click_region",
        lambda region, *, settle_s=0.0: (
            bool(clicks.append((region, settle_s))) or bool(events.append("click")) or True
        ),
    )
    monkeypatch.setattr(
        main_mod,
        "focus_window",
        lambda handle: bool(focus_calls.append(handle)) or bool(events.append("focus")) or True,
    )

    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._own_window == 4242  # recorded at mount

        await _capture_newchat(app, pilot, monkeypatch)
        scenes = _patch_found(monkeypatch, FOUND)
        clicks.clear()
        events.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: focus_calls == [4242], "focus snapped back")

        assert "clicked" in _newchat_label(app)
        assert clicks == [(CLICK_TARGET, 0.05)]
        assert events == ["click", "focus"]  # find, then click, then snap back
        # The button was hunted in the chat region, not anywhere else.
        assert [(scene.width, scene.height) for scene in scenes] == [
            (CHAT_REGION.width, CHAT_REGION.height)
        ]


async def test_not_on_screen_warns_and_clicks_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page moved on: clicking blind could hit anything, so nothing is."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)

    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _capture_newchat(app, pilot, monkeypatch)
        _patch_found(monkeypatch, None)
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: "not on screen" in _newchat_label(app), "miss reported")
        assert clicks == []
        # The capture is kept, so the user can retry once the page settles.
        assert main._active_profile().has(TemplateKind.NEW_CHAT)


async def test_a_refused_click_is_reported_separately(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found fine but the OS swallowed the input (not Windows): a different
    story to tell than "this button is not on screen"."""
    focus_calls: list[int] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: False)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: focus_calls.append(handle) or True)

    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _capture_newchat(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: "did not land" in _newchat_label(app), "refusal reported")
        assert focus_calls == []  # leave the browser focused so the user can click


async def test_nothing_captured_means_nothing_is_even_searched_for(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)
    scenes = _patch_found(monkeypatch, FOUND)

    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert not main._active_profile().has(TemplateKind.NEW_CHAT)

        await _press(app, pilot, "#newchat-btn")
        await pilot.pause(0.3)
        assert clicks == []
        assert scenes == []
        assert "not captured" in _newchat_label(app)  # the toast is the only feedback


async def test_no_chat_region_means_nowhere_to_look(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The appearance is captured but the slot has no window drawn - the same
    "go calibrate" branch, because there is nowhere to search."""
    clicks: list[ScreenRegion] = []
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _capture_newchat(app, pilot, monkeypatch, region=False)
        scenes = _patch_found(monkeypatch, FOUND)
        monkeypatch.setattr(
            main_mod, "click_region", lambda region, **kw: clicks.append(region) or True
        )

        await _press(app, pilot, "#newchat-btn")
        await pilot.pause(0.3)
        assert clicks == []
        assert scenes == []


async def test_new_preserves_the_capture(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The appearance describes the service, not what the finished session
    said - /new must not make the user recapture it."""
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _capture_newchat(app, pilot, monkeypatch)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._active_profile().has(TemplateKind.NEW_CHAT)
        assert "180×36 · captured" in _newchat_label(app)
