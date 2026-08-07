"""Pilot tests for the copy-button region + auto-copy-click flow (sidebar's
"Set copy button..." button).

Mirrors test_busy_region_ui.py and test_click_region_ui.py: the real picker
spawns a tkinter overlay, the real capture reads GDI pixels, and the real
matcher/focus calls touch the OS - all monkeypatched at their use site
(agentclip.tui.screens.main). ``BusyProbed`` is the documented injectable path
for the busy-region poller (tui/messages.py); posting it directly is
equivalent to a poll completing, so these tests drive the arm/fire trigger
without spinning up the real poller thread.

What we verify: the picker captures a template alongside the region, the
MATCH-then-two-CHANGED trigger fires the auto-copy flow exactly once and
re-arms only after another MATCH, the flow clicks the matched coordinates
(band-local offset translated back to screen space), the not-found path
notifies without clicking, and /new resets all of it.
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
from agentclip.screen.template import TemplateMatch
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed
from agentclip.tui.screens.main import MainScreen

COPY_REGION = ScreenRegion(1830, 612, 24, 24)
TEMPLATE = RegionImage(width=24, height=24, pixels=b"\x00" * (24 * 24 * 4))


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


def _copy_label(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-copy", Static).render())


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


async def _post_probe(main: MainScreen, pilot: Pilot, state: BusyState, diff: float | None) -> None:
    """Inject one busy-poller verdict - the documented path (tui/messages.py)."""
    main.post_message(BusyProbed(BusyProbe(state, diff)))
    await pilot.pause()


def _patch_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: COPY_REGION)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: TEMPLATE)


async def _arm_with_template(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """Common setup for the trigger tests: wait for the composer, draw the
    copy button, and confirm the template landed."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    await _press(app, pilot, "#set-copy-btn")
    await _wait_for(pilot, lambda: main._copy_template is not None, "template captured")
    return main


# -- picker flow --------------------------------------------------------------


async def test_pick_copy_region_captures_template_and_updates_sidebar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not set" in _copy_label(app)

        await _press(app, pilot, "#set-copy-btn")
        await _wait_for(pilot, lambda: main._copy_region == COPY_REGION, "copy region adopted")
        assert main._copy_template == TEMPLATE
        assert "24×24 at (1830, 612)" in _copy_label(app)


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: TEMPLATE)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-copy-btn")
        await pilot.pause(0.2)
        assert main._copy_region is None
        assert main._copy_template is None
        assert "not set" in _copy_label(app)


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    monkeypatch.setattr(main_mod, "pick_region", boom)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: TEMPLATE)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-copy-btn")
        await pilot.pause(0.2)
        assert main._copy_region is None
        assert main._copy_template is None
        assert "not set" in _copy_label(app)


async def test_capture_failure_at_calibration_keeps_neither_region_nor_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: COPY_REGION)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-copy-btn")
        await pilot.pause(0.2)
        assert main._copy_region is None
        assert main._copy_template is None
        assert "not set" in _copy_label(app)


# -- the MATCH-then-two-CHANGED trigger ----------------------------------------


async def test_match_then_two_changed_fires_once_and_rearms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _arm_with_template(app, pilot)

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await pilot.pause(0.1)
        assert calls == []  # only one CHANGED so far - not enough to fire

        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")

        # No refire on further CHANGED while disarmed.
        await _post_probe(main, pilot, BusyState.CHANGED, 0.32)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.33)
        await pilot.pause(0.1)
        assert len(calls) == 1

        # A fresh MATCH re-arms it; two more CHANGED fire again.
        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await _wait_for(pilot, lambda: len(calls) == 2, "flow fired again after re-arm")


async def test_error_probe_resets_streak_but_not_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _arm_with_template(app, pilot)

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.ERROR, None)
        await pilot.pause(0.1)
        assert calls == []  # streak was reset by the ERROR
        assert main._copy_armed is True  # but the arm survives it

        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires once the streak rebuilds")


async def test_no_fire_without_a_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._copy_template is None

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await pilot.pause(0.1)
        assert calls == []


async def test_no_fire_without_prior_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_picker(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _arm_with_template(app, pilot)

        # CHANGED from the very start - never armed by a MATCH.
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await pilot.pause(0.1)
        assert calls == []


# -- the flow itself ------------------------------------------------------------


async def test_flow_clicks_the_lowest_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    clicks: list[ScreenRegion] = []
    scrolls: list[tuple[ScreenRegion, int]] = []
    bands: list[ScreenRegion] = []
    match = TemplateMatch(y_offset=4, diff=0.03)

    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    monkeypatch.setattr(
        main_mod, "scroll_region", lambda region, n: scrolls.append((region, n)) or True
    )
    monkeypatch.setattr(main_mod, "find_lowest_match", lambda template, band: match)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)

    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _arm_with_template(app, pilot)

        def fake_band_capture(region: ScreenRegion) -> RegionImage:
            bands.append(region)
            return RegionImage(
                width=region.width,
                height=region.height,
                pixels=b"\x00" * (region.width * region.height * 4),
            )

        monkeypatch.setattr(main_mod, "capture_region", fake_band_capture)

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)

        await _wait_for(pilot, lambda: len(clicks) == 1, "copy button clicked")
        assert scrolls, "the transcript was scrolled before the click"
        band = bands[0]
        assert band == COPY_REGION  # no chat region drawn: band is the copy region alone
        expected = ScreenRegion(
            COPY_REGION.left, band.top + match.y_offset, COPY_REGION.width, TEMPLATE.height
        )
        assert clicks[-1] == expected
        assert "clicked (diff 0.03)" in _copy_label(app)


async def test_not_found_notifies_and_does_not_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_match", lambda template, band: None)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)

    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _arm_with_template(app, pilot)

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)

        await _wait_for(pilot, lambda: "not found" in _copy_label(app), "not-found reported")
        assert clicks == []


async def test_flow_snaps_focus_back_to_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After clicking the browser's copy button the flow hands focus back to
    the window recorded at mount - click first, snap-back strictly after."""
    _patch_picker(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(main_mod, "foreground_window", lambda: 4242)
    monkeypatch.setattr(main_mod, "click_region", lambda region: events.append("click") or True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(
        main_mod, "find_lowest_match", lambda template, band: TemplateMatch(y_offset=4, diff=0.02)
    )
    focus_calls: list[int] = []

    def fake_focus(handle: int) -> bool:
        focus_calls.append(handle)
        events.append("focus")
        return True

    monkeypatch.setattr(main_mod, "focus_window", fake_focus)

    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _arm_with_template(app, pilot)
        assert main._own_window == 4242  # recorded at mount

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)

        await _wait_for(pilot, lambda: focus_calls == [4242], "focus snapped back")
        assert events == ["click", "focus"]


# -- session teardown -----------------------------------------------------------


async def test_new_resets_copy_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_picker(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _arm_with_template(app, pilot)
        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        assert main._copy_armed is True

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._copy_region is None
        assert main._copy_template is None
        assert main._copy_armed is False
        assert main._copy_changed_streak == 0
        assert "not set" in _copy_label(app)
