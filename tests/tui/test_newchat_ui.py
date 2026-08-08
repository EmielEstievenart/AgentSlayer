"""Pilot tests for the new-chat calibration + action (sidebar "NEW CHAT").

Two buttons: "Set new-chat button..." snapshots the browser's new-chat control
into a CalibratedElement, and "New browser chat" verifies that snapshot still
matches before clicking it and handing focus back to AgentClip. Picker,
capture, element probe, click and focus are monkeypatched at their use site
(agentclip.tui.screens.main).

The verify step is the point: a browser that re-laid itself out would otherwise
get a click wherever those pixels used to be, so a mismatch warns and clicks
nothing at all.
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
from agentclip.screen.element import CalibratedElement
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp

NEWCHAT_REGION = ScreenRegion(120, 90, 180, 36)
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


def _newchat_label(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-newchat", Static).render())


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


def _patch_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: NEWCHAT_REGION)
    monkeypatch.setattr(main_mod, "capture_region", _frame)


async def test_calibration_snapshots_the_button_and_updates_the_sidebar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not set" in _newchat_label(app)

        await _press(app, pilot, "#set-newchat-btn")
        await _wait_for(pilot, lambda: main._newchat is not None, "new-chat button calibrated")
        assert main._newchat == CalibratedElement(NEWCHAT_REGION, _frame(NEWCHAT_REGION))
        assert "180×36 at (120, 90)" in _newchat_label(app)


async def test_capture_failure_keeps_it_uncalibrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: NEWCHAT_REGION)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-newchat-btn")
        await pilot.pause(0.2)
        assert main._newchat is None
        assert "not set" in _newchat_label(app)


async def test_the_action_verifies_clicks_and_hands_focus_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    events: list[str] = []
    clicks: list[tuple[ScreenRegion, float]] = []
    focus_calls: list[int] = []
    monkeypatch.setattr(main_mod, "foreground_window", lambda: 4242)
    monkeypatch.setattr(main_mod, "probe_element", lambda element: events.append("probe") or True)
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

    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._own_window == 4242  # recorded at mount

        await _press(app, pilot, "#set-newchat-btn")
        await _wait_for(pilot, lambda: main._newchat is not None, "new-chat button calibrated")

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: focus_calls == [4242], "focus snapped back")

        assert "clicked" in _newchat_label(app)
        assert clicks == [(NEWCHAT_REGION, 0.05)]
        assert events[-3:] == ["probe", "click", "focus"]  # verify, then click, then snap back


async def test_a_mismatch_warns_and_clicks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page moved: clicking blind could hit anything, so nothing is clicked."""
    _patch_picker(monkeypatch)
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "probe_element", lambda element: False)
    monkeypatch.setattr(
        main_mod, "click_region", lambda region, **kw: clicks.append(region) or True
    )
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-newchat-btn")
        await _wait_for(pilot, lambda: main._newchat is not None, "new-chat button calibrated")

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: "mismatch" in _newchat_label(app), "mismatch reported")
        assert clicks == []
        assert main._newchat is not None  # kept, so the user can redraw or retry


async def test_a_refused_click_is_reported_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified fine but the OS swallowed the input (not Windows): a different
    story to tell than "this is no longer the new-chat button"."""
    _patch_picker(monkeypatch)
    focus_calls: list[int] = []
    monkeypatch.setattr(main_mod, "probe_element", lambda element: True)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: False)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: focus_calls.append(handle) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-newchat-btn")
        await _wait_for(pilot, lambda: main._newchat is not None, "new-chat button calibrated")

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: "did not land" in _newchat_label(app), "refusal reported")
        assert focus_calls == []  # leave the browser focused so the user can click


async def test_the_action_without_a_calibration_clicks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicks: list[ScreenRegion] = []
    probes: list[object] = []
    monkeypatch.setattr(main_mod, "probe_element", lambda element: probes.append(element) or True)
    monkeypatch.setattr(
        main_mod, "click_region", lambda region, **kw: clicks.append(region) or True
    )
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._newchat is None

        await _press(app, pilot, "#newchat-btn")
        await pilot.pause(0.3)
        assert clicks == []
        assert probes == []
        assert "not set" in _newchat_label(app)  # the toast is the only feedback


async def test_new_preserves_the_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot describes where the browser's new-chat button is, not what
    the finished session said - /new must not make the user re-draw it (and the
    verify-before-click step already guards against a window that moved)."""
    _patch_picker(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-newchat-btn")
        await _wait_for(pilot, lambda: main._newchat is not None, "new-chat button calibrated")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._newchat is not None
        assert main._newchat.region == NEWCHAT_REGION
        assert "180×36 at (120, 90)" in _newchat_label(app)
