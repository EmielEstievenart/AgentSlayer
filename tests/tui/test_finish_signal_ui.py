"""Pilot tests for the combined finish signal that fires the auto-copy flow.

Two detectors, opposite polarities: the busy element was calibrated WHILE the
model generated (MATCH = generating), the idle element while the chat was idle
(MATCH = finished). MainScreen folds whichever are live into one verdict -
anything saying "generating" arms the trigger, and it fires only once EVERY
live detector says "finished" on two consecutive polls.

``BusyProbed`` / ``IdleProbed`` are the documented injectable path for the
poller (tui/messages.py); posting them is equivalent to a poll completing, so
these tests drive the state machine without the real poller thread. With both
detectors calibrated a tick is *closed* by ``IdleProbed``, so a dual-detector
tick is two posts, busy first.

Covered: busy only (today's behaviour), idle only (inverted), and both
(reinforced - one detector alone can never fire it).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed, IdleProbed
from agentclip.tui.screens.main import MainScreen

COPY_REGION = ScreenRegion(1830, 612, 24, 24)
TEMPLATE = RegionImage(width=24, height=24, pixels=b"\x00" * (24 * 24 * 4))
IDLE_REGION = ScreenRegion(900, 980, 40, 40)
IDLE_BASELINE = RegionImage(width=40, height=40, pixels=b"\x00" * (40 * 40 * 4))

SIZE = (110, 100)


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


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


def _patch_copy_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: COPY_REGION)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: TEMPLATE)


def _patch_flow(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Stub the flow itself - these tests are about what fires it, not what it does."""
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    return calls


async def _arm_with_template(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """The trigger refuses to fire without a copy-button template, so every
    test here calibrates one first."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    await _press(app, pilot, "#set-copy-btn")
    await _wait_for(pilot, lambda: main._copy_template is not None, "template captured")
    return main


def _calibrate_idle_detector(main: MainScreen) -> None:
    """Mark the idle detector calibrated without going through the picker.

    Pressing the button would also start the real poller, whose probes would
    race the sequence each test injects. What the tick-closing rule actually
    depends on is the calibrated baseline, so that is what is set.
    """
    main._idle_region = IDLE_REGION
    main._idle_baseline = IDLE_BASELINE


async def _busy(main: MainScreen, pilot: Pilot, state: BusyState) -> None:
    main.post_message(BusyProbed(BusyProbe(state, 0.2)))
    await pilot.pause()


async def _idle(main: MainScreen, pilot: Pilot, state: BusyState) -> None:
    main.post_message(IdleProbed(BusyProbe(state, 0.2)))
    await pilot.pause()


async def _tick(main: MainScreen, pilot: Pilot, busy: BusyState, idle: BusyState) -> None:
    """One dual-detector poll: busy first, idle closes the tick."""
    await _busy(main, pilot, busy)
    await _idle(main, pilot, idle)


# -- busy element only ---------------------------------------------------------


async def test_busy_only_arms_on_match_and_fires_on_two_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)

        await _busy(main, pilot, BusyState.MATCH)  # generating -> armed
        await _busy(main, pilot, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert calls == []  # one finished poll is not enough

        await _busy(main, pilot, BusyState.CHANGED)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")

        # Disarmed: no refire until the model demonstrably generates again.
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert len(calls) == 1


async def test_busy_only_error_breaks_the_streak_but_keeps_the_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)

        await _busy(main, pilot, BusyState.MATCH)
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.ERROR)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is True  # one bad frame cannot cancel a finish

        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires once the streak rebuilds")


# -- idle element only ----------------------------------------------------------


async def test_idle_only_arms_on_changed_and_fires_on_two_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inverted polarity: CHANGED is "generating" here, MATCH is "finished"."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)

        await _idle(main, pilot, BusyState.CHANGED)  # generating -> armed
        assert main._copy_armed is True
        await _idle(main, pilot, BusyState.MATCH)
        await pilot.pause(0.1)
        assert calls == []

        await _idle(main, pilot, BusyState.MATCH)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")

        await _idle(main, pilot, BusyState.MATCH)
        await _idle(main, pilot, BusyState.MATCH)
        await pilot.pause(0.1)
        assert len(calls) == 1  # disarmed until the chat generates again


async def test_idle_only_never_fires_without_a_generation_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle screen at startup matches the baseline forever - that must not
    look like a response finishing."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)

        for _ in range(5):
            await _idle(main, pilot, BusyState.MATCH)
        await pilot.pause(0.1)
        assert calls == []


# -- both elements: the reinforced verdict ---------------------------------------


async def test_both_fire_only_when_they_agree_for_two_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _calibrate_idle_detector(main)

        # Generating: busy matches its mid-generation baseline, idle does not.
        await _tick(main, pilot, BusyState.MATCH, BusyState.CHANGED)
        assert main._copy_armed is True

        # First agreeing tick is not enough.
        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await pilot.pause(0.1)
        assert calls == []

        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once both agreed twice")

        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await pilot.pause(0.1)
        assert len(calls) == 1  # disarmed


async def test_both_one_detector_alone_can_never_fire_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of calibrating two: the busy element going quiet while
    the idle element still reads "generating" is not a finish."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _calibrate_idle_detector(main)

        await _tick(main, pilot, BusyState.MATCH, BusyState.CHANGED)
        for _ in range(4):
            await _tick(main, pilot, BusyState.CHANGED, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is True  # still waiting for the second opinion

        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires once they agree")


async def test_both_either_detector_can_arm_the_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idle element alone spotting a new generation re-arms it - the busy
    element may well never have caught the stop button in a poll."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _calibrate_idle_detector(main)

        # Only the idle element notices; the busy element says "finished".
        await _tick(main, pilot, BusyState.CHANGED, BusyState.CHANGED)
        assert main._copy_armed is True

        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired")


async def test_both_an_error_on_either_side_breaks_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _calibrate_idle_detector(main)

        await _tick(main, pilot, BusyState.MATCH, BusyState.CHANGED)
        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _tick(main, pilot, BusyState.ERROR, BusyState.MATCH)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is True

        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires once the streak rebuilds")


async def test_a_busy_probe_alone_never_closes_a_dual_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With both calibrated the verdict is evaluated once per tick, on the
    closing (idle) message - so half a tick can neither arm nor fire."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _calibrate_idle_detector(main)

        for _ in range(4):
            await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is False  # never evaluated
        assert calls == []


# -- no template ------------------------------------------------------------------


async def test_nothing_fires_without_a_copy_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._copy_template is None

        await _busy(main, pilot, BusyState.MATCH)
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert calls == []
