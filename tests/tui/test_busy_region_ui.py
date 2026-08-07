"""Pilot tests for the busy-region detector (sidebar "Set busy region..." button).

Mirrors test_chat_region_ui.py: the real picker spawns a tkinter overlay in a
child process, the real capture reads GDI pixels, and the real prober polls the
screen - none of that belongs in a test run, so all three are monkeypatched at
their use site (agentclip.tui.screens.main), along with the poll interval so the
tests do not sit around for half a second per probe. What we verify is the
wiring: button -> picker -> baseline capture -> sidebar label, the poller
bridging probes into an unmistakable readout, and the session-scoped reset on
/new.
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
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp

REGION = ScreenRegion(200, 150, 64, 64)
BASELINE = RegionImage(width=64, height=64, pixels=b"\x00" * (64 * 64 * 4))


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


def _busy_label(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    label = app.main_screen.query_one("#side-busy", Static)
    return str(label.render())


async def _click_set_busy(app: AgentClipApp, pilot: Pilot) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one("#set-busy-btn", Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click("#set-busy-btn")


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """Type into the composer and send - refocusing it first, since clicking the
    sidebar button leaves focus on the button."""
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.focus()
    await pilot.pause()
    await pilot.press("enter")


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, poll_s: float = 0.02) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: REGION)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: BASELINE)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", poll_s)


async def test_calibrate_then_match_probe_shows_generating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        main_mod, "probe_busy", lambda baseline, region: BusyProbe(BusyState.MATCH, 0.012)
    )
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not calibrated" in _busy_label(app)

        await _click_set_busy(app, pilot)
        await _wait_for(pilot, lambda: main._busy_region == REGION, "busy region adopted")
        assert main._busy_baseline == BASELINE

        await _wait_for(pilot, lambda: "GENERATING" in _busy_label(app), "match probe arrives")
        assert "1.2%" in _busy_label(app)


async def test_probe_flips_to_changed_updates_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_common(monkeypatch)
    cell: dict[str, BusyProbe] = {"probe": BusyProbe(BusyState.MATCH, 0.01)}
    monkeypatch.setattr(main_mod, "probe_busy", lambda baseline, region: cell["probe"])
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_busy(app, pilot)
        await _wait_for(pilot, lambda: "GENERATING" in _busy_label(app), "match probe arrives")

        cell["probe"] = BusyProbe(BusyState.CHANGED, 0.34)
        await _wait_for(
            pilot, lambda: "response ready" in _busy_label(app), "changed probe arrives"
        )
        assert "34.0%" in _busy_label(app)


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: BASELINE)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_busy(app, pilot)
        await pilot.pause(0.2)
        assert main._busy_region is None
        assert main._busy_baseline is None
        assert "not calibrated" in _busy_label(app)


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    monkeypatch.setattr(main_mod, "pick_region", boom)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: BASELINE)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_busy(app, pilot)
        await pilot.pause(0.2)
        assert main._busy_region is None
        assert "not calibrated" in _busy_label(app)


async def test_capture_failure_at_calibration_is_reported_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: REGION)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_busy(app, pilot)
        await pilot.pause(0.2)
        assert main._busy_region is None
        assert main._busy_baseline is None
        assert main._busy_worker is None
        assert "not calibrated" in _busy_label(app)


async def test_new_stops_polling_and_resets_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        main_mod, "probe_busy", lambda baseline, region: BusyProbe(BusyState.MATCH, 0.01)
    )
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_busy(app, pilot)
        await _wait_for(pilot, lambda: "GENERATING" in _busy_label(app), "match probe arrives")
        worker = main._busy_worker
        assert worker is not None

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._busy_region is None
        assert main._busy_baseline is None
        assert "not calibrated" in _busy_label(app)
        await _wait_for(pilot, lambda: worker.is_cancelled, "poller cancelled")
