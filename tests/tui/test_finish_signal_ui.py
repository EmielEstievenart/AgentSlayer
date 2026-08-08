"""Pilot tests for the combined finish signal that fires the auto-copy flow.

Three detectors: the busy appearance is on screen only WHILE the model
generates (found = generating), the idle one only while the chat is idle
(found = finished), and the stale detector needs no appearance at all - the
chat region unchanged long enough (STALE) means finished, still moving
(CHANGING) means generating. MainScreen folds whichever are running into one
verdict - anything saying "generating" arms the trigger, and it fires only
once EVERY live detector says "finished" on two consecutive polls.

``BusyProbed`` / ``IdleProbed`` / ``StaleProbed`` are the documented
injectable path for the poller (tui/messages.py); posting them is equivalent
to a poll completing, so these tests drive the state machine without the real
poller thread. A tick is *closed* by the LAST entry in ``_active_detectors``
(the fixed busy -> idle -> stale build order), so a multi-detector tick is
several posts, in that order - and ``_detectors`` is how each test says which
subset the poller would have been built with.

Covered: busy only, idle only (inverted), both (reinforced - one detector
alone can never fire it), stale only and stale vetoing the others, plus the
flow suspension that stops the auto-copy flow's own scrolling from re-firing
the trigger.
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
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.stale import StaleProbe, StaleState
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed, IdleProbed, StaleProbed
from agentclip.tui.screens.main import MainScreen

COPY_REGION = ScreenRegion(1830, 612, 24, 24)
TEMPLATE = RegionImage(width=24, height=24, pixels=b"\x00" * (24 * 24 * 4))
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
    """The trigger refuses to fire without a copy-button appearance, so every
    test here captures one first (into the active service's profile)."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    await _press(app, pilot, "#capture-copy-btn")
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy button captured"
    )
    _detectors(main, "busy")  # the default for these tests; each overrides it
    return main


def _detectors(main: MainScreen, *names: str) -> None:
    """Declare which detectors the (stubbed-out) poller would be posting.

    ``_active_detectors`` is the seam the tick-closing rule reads: the poller
    builds it once from the drawn window plus the service's appearances, in the
    fixed busy -> idle -> stale order, and the LAST entry is what closes a
    tick. Setting it directly is how these tests drive any subset without a
    real poller thread racing the sequence they inject.
    """
    main._active_detectors = names


async def _busy(main: MainScreen, pilot: Pilot, state: BusyState) -> None:
    main.post_message(BusyProbed(BusyProbe(state, 0.2)))
    await pilot.pause()


async def _idle(main: MainScreen, pilot: Pilot, state: BusyState) -> None:
    main.post_message(IdleProbed(BusyProbe(state, 0.2)))
    await pilot.pause()


async def _stale(main: MainScreen, pilot: Pilot, state: StaleState, ticks: int = 0) -> None:
    main.post_message(StaleProbed(StaleProbe(state, 0.01, ticks)))
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
        _detectors(main, "idle")

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
        _detectors(main, "idle")

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
        _detectors(main, "busy", "idle")

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
        _detectors(main, "busy", "idle")

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
        _detectors(main, "busy", "idle")

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
        _detectors(main, "busy", "idle")

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
        _detectors(main, "busy", "idle")

        for _ in range(4):
            await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is False  # never evaluated
        assert calls == []


# -- the stale detector -----------------------------------------------------------


async def test_stale_only_arms_on_changing_and_fires_on_two_stale_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third detector works alone: CHANGING (the response region moving)
    is "generating", STALE is "finished" - the same arm/streak rules."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _detectors(main, "stale")

        await _stale(main, pilot, StaleState.CHANGING)  # generating -> armed
        assert main._copy_armed is True
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await pilot.pause(0.1)
        assert calls == []  # one finished tick is not enough

        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")

        await _stale(main, pilot, StaleState.STALE, ticks=6)
        await _stale(main, pilot, StaleState.STALE, ticks=7)
        await pilot.pause(0.1)
        assert len(calls) == 1  # disarmed until the region moves again


async def test_stale_never_fires_without_a_change_observed_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A screen that is stale from the start (nothing ever generated) must not
    read as a response finishing - same rule as the idle element."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _detectors(main, "stale")

        for ticks in range(5):
            await _stale(main, pilot, StaleState.STALE, ticks=ticks + 2)
        await pilot.pause(0.1)
        assert calls == []


async def test_stale_saying_changing_vetoes_the_other_detectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With busy + stale running, the busy indicator going away while the chat
    region is still moving is not a finish - and with stale running,
    ``StaleProbed`` (not ``BusyProbed``) closes the tick."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _detectors(main, "busy", "stale")

        await _busy(main, pilot, BusyState.MATCH)
        await _stale(main, pilot, StaleState.CHANGING)
        assert main._copy_armed is True

        # The busy element reads finished, but text is still streaming in.
        for _ in range(4):
            await _busy(main, pilot, BusyState.CHANGED)
            await _stale(main, pilot, StaleState.CHANGING)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is True  # still waiting for stillness

        await _busy(main, pilot, BusyState.CHANGED)
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _busy(main, pilot, BusyState.CHANGED)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires once they agree")


async def test_a_busy_probe_alone_never_closes_a_tick_with_stale_calibrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale is last in the fixed busy -> idle -> stale order, so once it is
    running nothing earlier in the order may fold the verdict."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _detectors(main, "busy", "stale")

        for _ in range(4):
            await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is False  # never evaluated
        assert calls == []


async def test_evaluation_is_suspended_while_the_flow_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-copy flow scrolls the very region the stale detector watches,
    so while it runs no probe may arm or fire anything - and once its finally
    lifts the suspension, a genuine new generation re-arms as usual."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _detectors(main, "stale")

        main._flow_running = True  # as if _evaluate_finish just fired the flow
        await _stale(main, pilot, StaleState.CHANGING)
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is False  # nothing was even armed

        main._flow_running = False  # the flow's finally
        await _stale(main, pilot, StaleState.CHANGING)
        assert main._copy_armed is True


async def test_the_flow_wrapper_lifts_the_suspension_so_it_can_refire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second response must be able to fire the flow again: the wrapper's
    finally clears ``_flow_running`` even with the flow body stubbed out."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _detectors(main, "stale")

        await _stale(main, pilot, StaleState.CHANGING)
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")
        await _wait_for(pilot, lambda: not main._flow_running, "suspension lifted")

        await _stale(main, pilot, StaleState.CHANGING)  # next generation
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 2, "flow fired again")


# -- verdicts from a detector that no longer runs ---------------------------------


async def test_a_ghost_verdict_from_a_dropped_detector_cannot_wedge_the_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the poller only raises a flag: the loop it interrupts still
    finishes its tick and posts it, AFTER the restart cleared the verdicts. When
    the new detector set is smaller (a forgotten busy appearance, a service
    switch) that leftover "still generating" used to re-arm the trigger on every
    later tick, and the auto-copy could never fire again."""
    _patch_copy_picker(monkeypatch)
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot)
        _detectors(main, "busy", "stale")

        await _busy(main, pilot, BusyState.MATCH)  # the model is generating
        await _stale(main, pilot, StaleState.CHANGING)
        assert main._copy_armed is True

        # The restart, as _start_detector_worker performs it: the busy detector
        # is gone, and every verdict it produced went with it.
        main._busy_seen = False
        main._busy_finished = None
        _detectors(main, "stale")

        # ...and now the cancelled loop's last BusyProbed lands anyway.
        await _busy(main, pilot, BusyState.MATCH)
        assert main._busy_seen is False  # the ghost recorded nothing

        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires on the stale detector alone")


# -- no template ------------------------------------------------------------------


async def test_nothing_fires_without_a_captured_copy_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert not main._active_profile().has(TemplateKind.COPY)

        await _busy(main, pilot, BusyState.MATCH)
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert calls == []
