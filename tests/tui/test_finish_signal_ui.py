"""Pilot tests for the combined finish signal that fires the auto-copy flow.

Three detectors: the busy appearance is on screen only WHILE the model
generates (found = generating), the idle one only while the chat is idle
(found = finished), and the stale detector needs no appearance at all - the
chat region unchanged long enough (STALE) means finished, still moving
(CHANGING) means generating. MainScreen folds whichever are running into one
verdict - a busy/idle "generating" arms the trigger on the spot, a stale one
only as part of a sustained large delta (``SEND_ARM_MIN_DIFF`` for
``SEND_ARM_TICKS`` consecutive probes), and it fires only once EVERY live
detector says "finished" on two consecutive polls.

``AutomationController.feed_probe`` is the documented injectable path for the
poller; feeding a probe is equivalent to a poll completing, so these tests drive
the state machine without the real poller thread. A tick is *closed* by the LAST
entry in ``_active_detectors`` (the fixed busy -> idle -> stale build order), so
a multi-detector tick is several calls, in that order - and ``_detectors`` is how
each test says which subset the poller would have been built with. The
consumption itself is synchronous, but what it PAINTS crosses back as a message,
which is why every helper here pauses the pilot before anything is read off the
sidebar.

All of that sits UNDER a session gate: none of it may arm or fire unless an
outbound is actually waiting for a reply (``copy_outbound`` opens it, the
harvest shuts it), so every test that drives the trigger opens the gate first -
``_arm_with_template`` does it the way production does.

Covered: busy only, idle only (inverted), both (reinforced - one detector
alone can never fire it), stale only and stale vetoing the others, the
send-arming rule that keeps a caret blink from arming the trigger, the
flow suspension that stops the auto-copy flow's own scrolling from re-firing
the trigger, plus the session gate itself - a fully calibrated but idle tab
arming nothing, the outbound copy opening it, and firing / /new shutting it.

The last section covers the READY-TO-SEND gate that rides on top of that one
(``feed_probe("send_ready", ...)``, same shape): a service with that appearance
captured holds finish detection back from the paste until the send button is
seen and then seen to go, which is the user's Enter. Without the capture there
is no gate and nothing above it changed - which is what the rest of this file,
all of it written against such a service, keeps proving. The gate may delay a
session and may never deadlock one, so the same section pins all three of its
bounded exits: the button going (the Enter), a busy/idle detector reporting
that the model is GENERATING - which overrides the gate on the spot, because
nothing answers a message that was never sent - and, for each phase, a clock.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.stale import StaleProbe, StaleState
from agentclip.driver.screen.template import Template
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen
from agentclip.tui.widgets.sidebar import (
    SEND_READY_ARMED,
    SEND_READY_OVERRIDDEN,
    SEND_READY_RELEASED,
    SEND_READY_RESTING,
    SEND_READY_SEEN,
    SEND_READY_STUCK,
    SEND_READY_TIMEOUT,
    template_status_id,
)

SIZE = (110, 100)
# Somewhere for the picker to hand back, for the tests that need a tab whose
# calibration is finished (see ``_calibrated_but_idle``).
CHAT_REGION = ScreenRegion(0, 0, 400, 300)
# What the one test that runs the REAL poll thread captures: flat pixels, so no
# appearance is ever found in it.
BLANK_FRAME = RegionImage(200, 200, b"\x00" * (200 * 200 * 4))


def _noise(width: int, height: int, seed: int) -> RegionImage:
    rng = random.Random(seed)
    pixels = bytearray()
    for _ in range(width * height):
        pixels += bytes((rng.randrange(256), rng.randrange(256), rng.randrange(256), 0))
    return RegionImage(width, height, bytes(pixels))


def _with_icon(scene: RegionImage, patch: RegionImage, x: int, y: int) -> RegionImage:
    pixels = bytearray(scene.pixels)
    row = patch.width * 4
    for ty in range(patch.height):
        start = ((y + ty) * scene.width + x) * 4
        pixels[start : start + row] = patch.pixels[ty * row : (ty + 1) * row]
    return RegionImage(scene.width, scene.height, bytes(pixels))


# Two frames of a chat region for the one test that drives a REAL PresenceTracker
# rather than posting hand-made verdicts: the same picture with and without a
# reasoning icon in it.
ICON = _noise(20, 16, seed=2)
ICON_TEMPLATE = Template.build(ICON)
NO_ICON_FRAME = _noise(140, 90, seed=1)
ICON_FRAME = _with_icon(NO_ICON_FRAME, ICON, 60, 40)


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


def _patch_flow(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Stub the flow itself - these tests are about what fires it, not what it does."""
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    return calls


async def _arm_with_template(
    app: AgentClipApp, pilot: Pilot, seed: Callable[..., None]
) -> MainScreen:
    """The trigger refuses to fire without a copy-button appearance, so every
    test here gives the active service one first.

    Capturing is the service editor's job now, and what MainScreen sees of it is
    a profile on disk plus a dropped cache - which is what ``seed_templates``
    and ``update_config`` reproduce here, without an editor visit these tests
    are not about.

    It also opens the SESSION gate, because none of the rules below exist until
    a reply is outstanding: an outbound has to have gone into the chat before a
    verdict may arm or fire anything (``_open_reply_gate``, the state
    ``copy_outbound`` leaves behind - and the one test that goes through the
    real thing is ``test_the_gate_opens_on_the_outbound_copy`` below).
    """
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    seed(main._selected_service(), TemplateKind.COPY, size=(24, 24))
    main._profiles.clear()
    main.update_config(main._config)
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy button appearance known"
    )
    main._open_reply_gate()
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


async def _busy(
    main: MainScreen, pilot: Pilot, state: BusyState, *, evidence: bool | None = None
) -> None:
    """One busy-appearance probe, as ``PresenceTracker.observe`` would make it.

    A probe carries two things, and conflating them was a shipped bug (see
    ``test_the_pastes_tracker_reset_arms_nothing_on_its_own``): ``state`` is the
    DE-BOUNCED verdict, while ``generating_now`` is whether that frame's own
    template search actually found the icon. They agree on every frame the
    tracker is settled, which is why ``evidence`` defaults to the honest reading
    of ``state`` - MATCH is the busy appearance being on screen - and the tests
    that care about the disagreement pass it explicitly.
    """
    if evidence is None:
        evidence = state is BusyState.MATCH
    main._automation.feed_probe("busy", BusyProbe(state, 0.2, evidence))
    await pilot.pause()


async def _idle(
    main: MainScreen, pilot: Pilot, state: BusyState, *, evidence: bool | None = None
) -> None:
    """The same, inverted: for an idle appearance CHANGED is "generating", and
    the evidence behind it is the appearance having been watched to GO."""
    if evidence is None:
        evidence = state is BusyState.CHANGED
    main._automation.feed_probe("idle", BusyProbe(state, 0.2, evidence))
    await pilot.pause()


async def _stale(
    main: MainScreen, pilot: Pilot, state: StaleState, ticks: int = 0, diff: float = 0.001
) -> None:
    main._automation.feed_probe("stale", StaleProbe(state, diff, ticks))
    await pilot.pause()


async def _stale_send(main: MainScreen, pilot: Pilot) -> None:
    """The stale detector watching the user's message actually get sent.

    Staleness alone arms the trigger only on a SUSTAINED LARGE delta -
    ``SEND_ARM_TICKS`` consecutive CHANGING probes each over
    ``SEND_ARM_MIN_DIFF`` - because a caret blink or a mouse-over between
    AgentClip's paste and the user's Enter is a CHANGING probe too, and arming
    on one of those fired the auto-copy at a reply-less screen.
    """
    for _ in range(main_mod.SEND_ARM_TICKS):
        await _stale(main, pilot, StaleState.CHANGING, diff=main_mod.SEND_ARM_MIN_DIFF)


async def _tick(main: MainScreen, pilot: Pilot, busy: BusyState, idle: BusyState) -> None:
    """One dual-detector poll: busy first, idle closes the tick."""
    await _busy(main, pilot, busy)
    await _idle(main, pilot, idle)


# -- busy element only ---------------------------------------------------------


async def test_busy_only_arms_on_match_and_fires_on_two_changed(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)

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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)

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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inverted polarity: CHANGED is "generating" here, MATCH is "finished"."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle screen at startup matches the baseline forever - that must not
    look like a response finishing."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "idle")

        for _ in range(5):
            await _idle(main, pilot, BusyState.MATCH)
        await pilot.pause(0.1)
        assert calls == []


# -- both elements: the reinforced verdict ---------------------------------------


async def test_both_fire_only_when_they_agree_for_two_ticks(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of calibrating two: the busy element going quiet while
    the idle element still reads "generating" is not a finish."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idle element alone spotting a new generation re-arms it - the busy
    element may well never have caught the stop button in a poll."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "busy", "idle")

        # Only the idle element notices; the busy element says "finished".
        await _tick(main, pilot, BusyState.CHANGED, BusyState.CHANGED)
        assert main._copy_armed is True

        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _tick(main, pilot, BusyState.CHANGED, BusyState.MATCH)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired")


async def test_both_an_error_on_either_side_breaks_the_streak(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With both calibrated the verdict is evaluated once per tick, on the
    closing (idle) message - so half a tick can neither arm nor fire."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "busy", "idle")

        for _ in range(4):
            await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is False  # never evaluated
        assert calls == []


# -- the stale detector -----------------------------------------------------------


async def test_stale_only_arms_on_a_sustained_change_and_fires_on_two_stale_ticks(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third detector works alone: a sustained large CHANGING run (the
    response region really moving) is "generating", STALE is "finished" - the
    same streak rules from there on."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "stale")

        await _stale_send(main, pilot)  # generating -> armed
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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A screen that is stale from the start (nothing ever generated) must not
    read as a response finishing - same rule as the idle element."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "stale")

        for ticks in range(5):
            await _stale(main, pilot, StaleState.STALE, ticks=ticks + 2)
        await pilot.pause(0.1)
        assert calls == []


async def test_stale_saying_changing_vetoes_the_other_detectors(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With busy + stale running, the busy indicator going away while the chat
    region is still moving is not a finish - and with stale running,
    the STALE probe (not the busy one) closes the tick."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
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
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale is last in the fixed busy -> idle -> stale order, so once it is
    running nothing earlier in the order may fold the verdict."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "busy", "stale")

        for _ in range(4):
            await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is False  # never evaluated
        assert calls == []


async def test_the_ticks_that_land_during_the_fires_thread_hop_cannot_refire(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window slice 5b opened, and the guard that was already closing it.

    The fire is taken on the poller thread now and reaches this screen as a
    message, so ``run_worker`` does not even start until the pump gets round to
    it - a whole extra hop during which the poller goes on ticking at a chat the
    first harvest has not touched yet, and every one of those ticks is a
    finished streak. ``evaluate_finish`` sets ``flow_running`` SYNCHRONOUSLY
    before asking, which is what makes them all no-ops. Here the probes are fed
    with no pause at all between them, so they all land before the handler runs.
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "busy")

        # No ``pilot.pause`` anywhere in here: the whole burst is consumed while
        # the ``AutoCopyRequested`` from the third probe is still in the queue.
        for state in (BusyState.MATCH, BusyState.CHANGED, BusyState.CHANGED):
            main._automation.feed_probe("busy", BusyProbe(state, 0.2, state is BusyState.MATCH))
        assert main._flow_running is True  # up before the fire ever left the fold
        assert calls == []  # ...and the worker has not started yet
        for _ in range(4):
            main._automation.feed_probe("busy", BusyProbe(BusyState.CHANGED, 0.2, False))

        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired")
        await pilot.pause(0.1)
        assert len(calls) == 1


async def test_evaluation_is_suspended_while_the_flow_runs(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-copy flow scrolls the very region the stale detector watches,
    so while it runs no probe may arm or fire anything - and once its finally
    lifts the suspension, a genuine new generation re-arms as usual."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "stale")

        main._flow_running = True  # as if _evaluate_finish just fired the flow
        await _stale_send(main, pilot)
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is False  # nothing was even armed

        main._flow_running = False  # the flow's finally
        await _stale_send(main, pilot)
        assert main._copy_armed is True


async def test_the_flow_wrapper_lifts_the_suspension_so_it_can_refire(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second response must be able to fire the flow again: the wrapper's
    finally clears ``_flow_running`` even with the flow body stubbed out."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "stale")

        await _stale_send(main, pilot)
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")
        await _wait_for(pilot, lambda: not main._flow_running, "suspension lifted")

        # Firing harvested the reply, so the gate shut with it: the next turn's
        # outbound copy is what re-opens it (``_open_reply_gate``).
        main._open_reply_gate()
        await _stale_send(main, pilot)  # next generation
        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 2, "flow fired again")


# -- send-arming: what it takes for staleness alone to arm the trigger ------------


async def test_small_stale_deltas_never_arm_and_a_still_screen_never_fires(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE bug this rule closes: between AgentClip's paste and the user's Enter
    the composer's caret blinks and the mouse drifts, which the stale detector
    reports as CHANGING with a tiny diff. Arming on that made the still,
    reply-less pre-Enter screen read as a finished response - auto-copy fired
    and harvested nothing."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "stale")

        for _ in range(6):  # well past SEND_ARM_TICKS
            await _stale(main, pilot, StaleState.CHANGING, diff=0.001)
        assert main._copy_armed is False

        for ticks in range(2, 8):  # ...and the screen going still fires nothing
            await _stale(main, pilot, StaleState.STALE, ticks=ticks)
        await pilot.pause(0.1)
        assert calls == []


async def test_a_sustained_large_delta_takes_exactly_send_arm_ticks(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "stale")

        for _ in range(main_mod.SEND_ARM_TICKS - 1):
            await _stale(main, pilot, StaleState.CHANGING, diff=0.5)
            assert main._copy_armed is False  # not sustained yet

        await _stale(main, pilot, StaleState.CHANGING, diff=0.5)
        assert main._copy_armed is True


async def test_one_small_delta_restarts_the_large_delta_run(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Consecutive" is literal: a quiet frame in the middle means what came
    before it was not one sustained change, and the count starts over."""
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "stale")

        for _ in range(main_mod.SEND_ARM_TICKS - 1):
            await _stale(main, pilot, StaleState.CHANGING, diff=0.5)
        await _stale(main, pilot, StaleState.CHANGING, diff=0.001)  # a caret blink
        assert main._stale_arm_streak == 0

        for _ in range(main_mod.SEND_ARM_TICKS - 1):
            await _stale(main, pilot, StaleState.CHANGING, diff=0.5)
        assert main._copy_armed is False  # the interruption cost a full restart

        await _stale(main, pilot, StaleState.CHANGING, diff=0.5)
        assert main._copy_armed is True


async def test_a_busy_verdict_still_arms_on_a_single_tick(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Icon evidence is not de-bounced: the reasoning indicator being on screen
    is something only a real generation produces, so one frame is enough - and
    the sustained-delta rule must not slow that down."""
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "busy", "stale")

        # A tick where only the busy icon says "generating": the stale diff is
        # caret-sized, so it could never arm on its own.
        await _busy(main, pilot, BusyState.MATCH)
        await _stale(main, pilot, StaleState.CHANGING, diff=0.001)
        assert main._copy_armed is True
        assert main._stale_arm_streak == 0


# -- verdicts from a detector that no longer runs ---------------------------------


async def test_a_ghost_verdict_from_a_dropped_detector_cannot_wedge_the_trigger(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the poller only raises a flag: the loop it interrupts still
    finishes its tick and posts it, AFTER the restart cleared the verdicts. When
    the new detector set is smaller (a forgotten busy appearance, a service
    switch) that leftover "still generating" used to re-arm the trigger on every
    later tick, and the auto-copy could never fire again."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        _detectors(main, "busy", "stale")

        await _busy(main, pilot, BusyState.MATCH)  # the model is generating
        await _stale(main, pilot, StaleState.CHANGING)
        assert main._copy_armed is True

        # The restart, as _start_detector_worker performs it: the busy detector
        # is gone, and every verdict it produced went with it.
        main._busy_seen = False
        main._busy_finished = None
        _detectors(main, "stale")

        # ...and now the cancelled loop's last busy probe lands anyway.
        await _busy(main, pilot, BusyState.MATCH)
        assert main._busy_seen is False  # the ghost recorded nothing

        await _stale(main, pilot, StaleState.STALE, ticks=4)
        await _stale(main, pilot, StaleState.STALE, ticks=5)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires on the stale detector alone")


async def test_a_late_probe_from_the_previous_live_window_arms_nothing(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-window ghost, which the detector NAME alone cannot catch.

    Both windows run a stale detector, so a verdict taken from the sub-agent's
    window passed a name-only filter unchanged after ``end_browser_chat`` had
    already handed the automation back to the master. Proven scenario: /abort
    during a generating sub-run; the cancelled loop's in-flight "still
    generating" arrived a moment later, armed the trigger, and two quiet ticks
    fired the copy flow at the MASTER's chat - clicking a copy button under a
    conversation nobody had sent anything to. Every probe carries the generation
    of the run that produced it instead.
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _arm_with_template(app, pilot, seed_templates)
        # No real poll threads: these are injected verdicts, and a live loop
        # would race the sequence with readings of its own.
        monkeypatch.setattr(MainScreen, "_spawn_detector_worker", lambda self, loop: None)
        main._slots[AgentSlot.MASTER].chat_region = ScreenRegion(0, 0, 400, 300)
        main._slots[AgentSlot.SUBAGENT].chat_region = ScreenRegion(900, 0, 400, 300)

        main._live = AgentSlot.SUBAGENT  # as start_browser_chat leaves it
        main._start_detector_worker()
        sub_generation = main._detector_generation
        assert main._active_detectors == ("stale",)

        main.end_browser_chat()  # /abort: the master gets the automation back
        assert main._live is AgentSlot.MASTER
        assert main._detector_generation != sub_generation
        # The retarget shuts the session gate too; re-open it so the generation
        # stamp is the only thing standing between the ghost and the trigger -
        # which is what this test is about. In the real flow the master's next
        # outbound copy does exactly this a moment later.
        main._open_reply_gate()

        # ...and now the sub window's last tick lands: a sustained large delta,
        # which on the live window would arm the trigger outright.
        for _ in range(main_mod.SEND_ARM_TICKS + 1):
            main._automation.feed_probe(
                "stale", StaleProbe(StaleState.CHANGING, 0.5, 0), sub_generation
            )
            await pilot.pause()
        assert main._copy_armed is False
        assert main._stale_arm_streak == 0

        # The master's own chat is sitting still, as it has been all along.
        for ticks in (4, 5):
            await _stale(main, pilot, StaleState.STALE, ticks=ticks)
        await pilot.pause(0.1)
        assert calls == []


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


# -- the session gate: is a reply outstanding at all? ------------------------------


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _calibrated_but_idle(
    app: AgentClipApp,
    pilot: Pilot,
    seed: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    *,
    send_ready: bool = False,
) -> MainScreen:
    """Exactly what a finished calibration leaves behind, and nothing more.

    The copy-button appearance, the chat window drawn through the real picker
    button, a poller composed around it - and no session, no outbound, no reply
    due. The poll THREAD is stubbed out (only the spawn: the composition is
    part of what is being set up) so its own verdicts cannot race the ones each
    test injects.

    ``send_ready`` adds the ready-to-send appearance, which is the ONLY switch
    the send gate has (§3.4b): a capture is a capability, so a service that has
    one gets the gate and a service that has not behaves exactly as it always
    did.
    """
    monkeypatch.setattr(MainScreen, "_spawn_detector_worker", lambda self, loop: None)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    kinds = (TemplateKind.COPY, TemplateKind.SEND_READY) if send_ready else (TemplateKind.COPY,)
    seed(main._selected_service(), *kinds, size=(24, 24))
    main._profiles.clear()
    main.update_config(main._config)
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy button appearance known"
    )
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
    return main


async def test_setting_the_chat_region_arms_nothing_and_fires_nothing(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE bug the gate closes: drawing the box was enough to start clicking.

    With the appearances captured and the window drawn, the poller starts - and
    it used to be free to act on whatever it saw. A resting chat gives it plenty:
    an un-debounced busy/idle "generating" needs one frame, and the picker
    overlay closing is a sustained large delta all by itself. Two quiet ticks
    later the auto-copy flow scrolled and clicked its way through a conversation
    nobody had sent anything to. Setting a region is configuration; it is not a
    turn.
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch)
        assert main._awaiting_pasted_reply is False

        # The icon path: one "generating" frame used to be the whole arming.
        _detectors(main, "busy")
        await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is False
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)

        # ...and the staleness path: the overlay coming down, then the settled
        # screen it left behind.
        _detectors(main, "stale")
        await _stale_send(main, pilot)
        assert main._copy_armed is False
        for ticks in (4, 5, 6):
            await _stale(main, pilot, StaleState.STALE, ticks=ticks)

        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is False


async def test_the_outbound_copy_opens_the_gate_and_the_trigger_works_again(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: once AgentClip has actually put a payload in the chat,
    the arm-then-fire machinery is exactly what it always was. ``copy_outbound``
    is the real opener - the same call the controller makes for a bootstrap, a
    results payload, an answer or a re-copy."""
    calls = _patch_flow(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch)

        await main.copy_outbound("the payload")
        await pilot.pause()
        assert fake.read_text() == "the payload"
        assert main._awaiting_pasted_reply is True

        _detectors(main, "busy")
        await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is True
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired for a real outbound")


async def test_firing_shuts_the_gate_until_the_next_outbound(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firing IS the harvest, so the reply stops being outstanding with it - and
    a second response cannot come out of nowhere: it takes a second outbound."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch)
        await main.copy_outbound("the payload")
        await pilot.pause()

        _detectors(main, "busy")
        await _busy(main, pilot, BusyState.MATCH)
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")
        await _wait_for(pilot, lambda: not main._flow_running, "suspension lifted")
        assert main._awaiting_pasted_reply is False

        # The same sequence again, with nothing pasted in between: nothing.
        await _busy(main, pilot, BusyState.MATCH)
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert calls == [None]
        assert main._copy_armed is False

        # The next turn's outbound copy is what makes it possible again.
        await main.copy_outbound("the next payload")
        await pilot.pause()
        await _busy(main, pilot, BusyState.MATCH)
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await _wait_for(pilot, lambda: len(calls) == 2, "flow fired for the second outbound")


async def test_new_shuts_the_gate(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """/new tears the session down while the drawn window and the appearances
    survive it (they describe where the chat is, not what it said) - so the tab
    is calibrated and idle again, which is precisely the state that may not
    click anything."""
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert main._awaiting_pasted_reply is True

        await main.clear_transcript()  # the /new teardown hook
        await pilot.pause()
        assert main._awaiting_pasted_reply is False
        assert main._chat_region == CHAT_REGION  # the calibration outlives it

        _detectors(main, "busy")
        await _busy(main, pilot, BusyState.MATCH)
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is False


async def test_the_gate_survives_a_modal_that_only_suspends_the_detectors(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn interrupted by an F2 visit is still a turn awaiting its reply.

    ``suspend_detectors`` forgets what the detectors SAW, which is the right
    answer for an overlay drawn over the browser - but the outbound is still
    sitting in the chat, so the gate is not trigger state and does not go with
    it."""
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch)
        await main.copy_outbound("the payload")
        await pilot.pause()

        main.suspend_detectors()
        main.resume_detectors()
        assert main._awaiting_pasted_reply is True

        _detectors(main, "busy")
        await _busy(main, pilot, BusyState.MATCH)
        assert main._copy_armed is True


# -- the ready-to-send gate: has the user actually pressed Enter yet? --------------


def _record_notifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake_notify(self: MainScreen, message: str, *args: Any, **kwargs: Any) -> None:
        seen.append(message)

    monkeypatch.setattr(MainScreen, "notify", fake_notify)
    return seen


async def _send_ready(main: MainScreen, pilot: Pilot, found: bool | None) -> None:
    """One look for the ready-to-send button (None = the tick's capture failed)."""
    main._automation.feed_probe("send_ready", found)
    await pilot.pause()


def _send_line(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    widget_id = f"#{template_status_id(TemplateKind.SEND_READY)}"
    return str(app.main_screen.query_one(widget_id, Static).render())


def _banner_up(main: MainScreen) -> bool:
    return bool(main.query_one("#side-paste-flash", Static).display)


async def _finishes(main: MainScreen, pilot: Pilot) -> None:
    """The busy sequence that arms and then fires: generating, then two quiet ticks."""
    await _busy(main, pilot, BusyState.MATCH)
    await _busy(main, pilot, BusyState.CHANGED)
    await _busy(main, pilot, BusyState.CHANGED)


async def _stale_finishes(main: MainScreen, pilot: Pilot) -> None:
    """The same round trip, seen by the STALE detector alone: the send landing
    in the transcript as a sustained large delta, then two still ticks.

    The gate tests below want the sequence that arms and fires WITHOUT any icon
    evidence in it, because a busy/idle "generating" verdict now overrides the
    gate outright - deliberately, since nothing generates a reply to a message
    that was never sent. Staleness is the evidence the gate exists to distrust,
    so staleness is what has to stay vetoed while it holds.
    """
    await _stale_send(main, pilot)
    await _stale(main, pilot, StaleState.STALE)
    await _stale(main, pilot, StaleState.STALE)


async def test_without_a_send_capture_the_paste_gates_nothing_and_still_needs_an_icon(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture is a capability: no ready-to-send appearance, no gate at all.

    That is the whole of the compatibility promise - and it is a promise about
    the GATE, not a licence to arm on nothing. What the paste hands finish
    detection is a clean slate (every tracker reset), so the ticks that follow
    it are settling ticks: "generating" verdicts with no icon behind them. They
    may not arm. The first frame that genuinely sees the reasoning appearance
    may, immediately, and from there the sequence is what it always was.
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch)
        assert not main._live_profile().has(TemplateKind.SEND_READY)

        await main.copy_outbound("the payload")
        await pilot.pause()
        assert main._send_gate is None
        assert SEND_READY_RESTING in _send_line(app)

        _detectors(main, "busy")
        await _busy(main, pilot, BusyState.MATCH, evidence=False)
        assert main._copy_armed is False  # a settling tick is not a sighting
        assert main._loop_state is not LoopState.WAIT_GENERATE

        await _busy(main, pilot, BusyState.MATCH)  # the icon, actually on screen
        assert main._copy_armed is True
        assert main._loop_state is LoopState.WAIT_GENERATE
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired with no gate in the way")


async def test_the_pastes_tracker_reset_arms_nothing_on_its_own(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE bug: every message reached MANUAL_COPY a couple of seconds after the
    paste, "like it fails to detect the reasoning and continues too fast".

    ``_open_reply_gate`` resets every tracker at the paste, and a freshly reset
    ``PresenceTracker`` reports the de-bounced "generating" default for its
    first ``required_ticks - 1`` frames - the right bias for a finish decision,
    and evidence of precisely nothing. Reading that as "the reasoning icon is on
    screen" armed the auto-copy on tick one; the same un-evidenced ticks then
    completed their streak, read "finished" twice over, and fired the flow at a
    chat with no response in it - which found no copy button and reported
    MANUAL_COPY. Nothing on this screen changed the whole time.
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch)
        await main.copy_outbound("the payload")
        await pilot.pause()
        _detectors(main, "busy")

        # The settling window: the verdict says "generating", no frame saw it.
        for _ in range(3):
            await _busy(main, pilot, BusyState.MATCH, evidence=False)
        assert main._copy_armed is False

        # ...and now the debounce completes on that same unchanged screen.
        for _ in range(3):
            await _busy(main, pilot, BusyState.CHANGED)
        await pilot.pause(0.1)
        assert calls == []
        assert main._loop_state is not LoopState.MANUAL_COPY  # the user's symptom
        assert _banner_up(main)  # still waiting for the Enter it asked for


async def test_a_real_tracker_across_the_paste_holds_the_gate_until_the_icon_shows(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same bug with nothing hand-made in the loop.

    Every other test here posts pre-fabricated verdicts, and that is exactly how
    the regression shipped: the helpers could not express "the verdict says
    generating and no frame saw anything", because only ``PresenceTracker``
    produces that state - and only across the reset ``_open_reply_gate``
    performs at the paste. So this one feeds real frames of a real chat region
    to a real tracker and posts what it actually says.

    The screen is IDENTICAL throughout the hold: the same pixels, no icon in
    them, exactly what a browser shows while a pasted payload waits for its
    Enter. Nothing may arm, and the send gate must still be holding at the end
    of it. Then the model starts and the icon appears - one frame, and the gate
    is overridden and the trigger armed, which is the deadlock fix intact.
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        tracker = PresenceTracker((ICON_TEMPLATE,), found_is_busy=True)
        main._busy_tracker = tracker

        async def tick(scene: RegionImage) -> None:
            """One poller tick: one capture, through the tracker, onto the screen."""
            main._automation.feed_probe("busy", tracker.observe(scene))
            await pilot.pause()

        # The chat before the paste: settled, no icon, tracker long since sure.
        _detectors(main, "busy")
        for _ in range(6):
            await tick(NO_ICON_FRAME)
        assert main._busy_finished is True

        # The paste. This is the boundary: the gate opens and the tracker is
        # reset in the same call, so the next few verdicts are the settling
        # default all over again.
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert main._send_gate is main_mod.SendGate.HOLD

        for _ in range(8):  # well past the tracker's required_ticks
            await tick(NO_ICON_FRAME)
        await pilot.pause(0.1)
        assert main._copy_armed is False
        assert main._send_gate is main_mod.SendGate.HOLD  # no Enter, no evidence
        assert calls == []
        assert _banner_up(main)

        # The user presses Enter and the model starts: the icon is on screen.
        await tick(ICON_FRAME)
        assert main._send_gate is None
        assert SEND_READY_OVERRIDDEN in _send_line(app)
        assert main._copy_armed is True
        assert main._loop_state is LoopState.WAIT_GENERATE
        assert not _banner_up(main)

        # ...and the answer finishing harvests it, icon gone for the whole streak.
        for _ in range(6):
            await tick(NO_ICON_FRAME)
        await _wait_for(pilot, lambda: len(calls) == 1, "the harvest fired on real frames")


async def test_the_gate_holds_finish_detection_until_the_button_comes_and_goes(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feature, end to end.

    While the send button is on screen the payload is sitting in the composer
    UNSENT - so the stillness of that screen is about a message nobody has
    asked anything with, and the sequence that would normally arm and fire the
    auto-copy must do neither. The button vanishing IS the user's Enter, and
    from that instant everything behaves as it did before the gate existed.

    On the stale detector throughout, because that is the evidence the gate
    exists to distrust: a busy/idle icon saying the model is *generating* is
    evidence of a send rather than of a still screen, and now overrides the
    gate rather than being vetoed by it (see the override test below).
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert main._send_gate is main_mod.SendGate.HOLD
        assert _banner_up(main)

        # The full arm-then-fire sequence, twice over, vetoed throughout.
        _detectors(main, "stale")
        await _stale_finishes(main, pilot)
        await _stale_finishes(main, pilot)
        await pilot.pause(0.1)
        assert calls == []
        assert main._copy_armed is False
        assert main._stale_arm_streak == 0  # the large-delta run does not advance either
        assert _banner_up(main)  # the user has not sent anything yet

        # Seen: still holding, and the banner still nags.
        await _send_ready(main, pilot, True)
        assert main._send_gate is main_mod.SendGate.SEEN
        assert SEND_READY_SEEN in _send_line(app)
        await _stale_finishes(main, pilot)
        await pilot.pause(0.1)
        assert calls == []
        assert _banner_up(main)

        # Gone: that was the Enter keystroke.
        await _send_ready(main, pilot, False)
        assert main._send_gate is None
        assert SEND_READY_RELEASED in _send_line(app)
        assert not _banner_up(main)
        assert main._copy_armed is False  # detection starts from the send, not the paste

        await _stale_finishes(main, pilot)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once the send was seen")


async def test_a_button_that_never_shows_times_the_gate_out_and_says_so(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delaying a session is allowed; deadlocking one is not.

    A capture that stopped matching (a theme switch, a redesign) must cost a
    few seconds and a toast, not the turn."""
    calls = _patch_flow(monkeypatch)
    notes = _record_notifications(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()

        for _ in range(main_mod.SEND_GATE_TIMEOUT_TICKS - 1):
            await _send_ready(main, pilot, False)
        assert main._send_gate is main_mod.SendGate.HOLD  # still waiting
        assert not any("never appeared" in note for note in notes)

        await _send_ready(main, pilot, False)
        assert main._send_gate is None
        assert SEND_READY_TIMEOUT in _send_line(app)
        assert any("never appeared" in note for note in notes)
        # ...and the fallback is today's behaviour, banner included.
        assert _banner_up(main)

        _detectors(main, "busy")
        await _finishes(main, pilot)
        await _wait_for(pilot, lambda: len(calls) == 1, "finish detection took over")


async def test_a_blind_poller_runs_the_gates_clock_down_too(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way the gate could hang for ever: captures that keep failing.

    A None probe says nothing about the button, so it may not release the gate -
    but it must still count, or a browser that cannot be captured at all would
    hold the reply back until ``/new``."""
    _patch_flow(monkeypatch)
    _record_notifications(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()

        for _ in range(main_mod.SEND_GATE_TIMEOUT_TICKS):
            await _send_ready(main, pilot, None)
        assert main._send_gate is None


async def test_a_generating_icon_overrides_a_gate_whose_button_will_not_go(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadlock the gate used to be able to reach, and the way out of it.

    Its ordinary release is one non-debounced template match going away, and a
    fresh chat's FIRST message is exactly where that does not happen: the
    composer is centred and animating rather than docked where the capture was
    taken, so the button gets seen once and then never yields a clean
    not-found frame. The user pressed Enter regardless, the model generated,
    and nothing was left that could ever let go - ``>>> PRESS ENTER <<<``
    flashed for ever and the reply was never harvested.

    A reasoning icon on screen answers the gate's own question - has the user
    pressed Enter? - better than the button ever could, because nothing
    generates a reply to a message that was never sent. So it releases on the
    spot, on that same tick, keeping the verdict that did it.
    """
    calls = _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()
        _detectors(main, "busy")

        await _send_ready(main, pilot, True)
        assert main._send_gate is main_mod.SendGate.SEEN
        assert _banner_up(main)

        # The button is stuck on screen; the model starts generating anyway.
        await _send_ready(main, pilot, True)
        await _busy(main, pilot, BusyState.MATCH)
        assert main._send_gate is None
        assert SEND_READY_OVERRIDDEN in _send_line(app)
        assert main._copy_armed is True  # the very tick that released it also arms
        assert main._loop_state is LoopState.WAIT_GENERATE
        assert not _banner_up(main)  # the nag is over: the send is proven

        # ...and the turn then finishes like any other.
        await _busy(main, pilot, BusyState.CHANGED)
        await _busy(main, pilot, BusyState.CHANGED)
        await _wait_for(pilot, lambda: len(calls) == 1, "the harvest fired after the override")


async def test_a_stale_verdict_alone_never_overrides_the_gate(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override is icon evidence only, and that limit is the gate's point.

    A caret blinking in a composer full of unsent text is a CHANGING probe, and
    a sustained one at that while the paste itself lands; letting that claim
    "the model is generating" would hand the gate straight back to the noise it
    was built to ignore."""
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()
        _detectors(main, "stale")

        await _send_ready(main, pilot, True)
        await _stale_send(main, pilot)
        await pilot.pause(0.1)
        assert main._send_gate is main_mod.SendGate.SEEN
        assert SEND_READY_SEEN in _send_line(app)


async def test_a_button_that_never_goes_away_times_the_gate_out_too(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate may delay a session; it may never deadlock one - and that has to
    hold for the phase AFTER the sighting as well as the one before it.

    The override above rescues every session whose model can be *seen* to
    generate; this is the backstop for the rest - a service running no icon
    detector at all, whose only evidence is a button that goes on matching long
    after the message it belonged to was sent. The budget is deliberately a
    generous one (minutes, not the five seconds a never-appearing button costs)
    because what the SEEN phase is waiting for is a human reading what is about
    to go out, and it may not expire on one who paused to think.

    The clock also RESTARTS at the sighting, so a slow-appearing button does not
    eat the user's reading time.
    """
    calls = _patch_flow(monkeypatch)
    notes = _record_notifications(monkeypatch)
    monkeypatch.setattr(main_mod, "SEND_GATE_SEEN_TIMEOUT_TICKS", 6)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()
        _detectors(main, "stale")

        # A sighting that only just beats the HOLD clock; the SEEN phase still
        # gets its whole budget afterwards.
        for _ in range(main_mod.SEND_GATE_TIMEOUT_TICKS - 1):
            await _send_ready(main, pilot, False)
        await _send_ready(main, pilot, True)
        assert main._send_gate is main_mod.SendGate.SEEN

        for _ in range(5):
            await _send_ready(main, pilot, True)
        assert main._send_gate is main_mod.SendGate.SEEN  # the user may take their time
        assert not any("never went away" in note for note in notes)

        await _send_ready(main, pilot, True)
        assert main._send_gate is None
        assert SEND_READY_STUCK in _send_line(app)
        assert any("never went away" in note for note in notes)
        # ...and the fallback is today's behaviour, banner included: nothing is
        # reset and nothing is hidden, because no send was ever proven.
        assert _banner_up(main)

        await _stale_finishes(main, pilot)
        await _wait_for(pilot, lambda: len(calls) == 1, "finish detection took over")


def test_the_seen_phase_waits_far_longer_than_the_sighting_does() -> None:
    """The two budgets are not interchangeable and must not drift together.

    Five seconds is the right cost for a capture that never matches; it is the
    wrong one for a human deciding whether to send what AgentClip just wrote, so
    the phase that waits on a person is an order of magnitude more patient."""
    assert main_mod.SEND_GATE_SEEN_TIMEOUT_TICKS > main_mod.SEND_GATE_TIMEOUT_TICKS * 10


async def test_new_during_the_hold_clears_the_send_gate(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The send gate is a phase OF the reply gate - "out but not sent yet" - so
    every way an outstanding reply stops being one takes it along."""
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert main._send_gate is main_mod.SendGate.HOLD

        await main.clear_transcript()  # the /new teardown hook
        await pilot.pause()
        assert main._send_gate is None
        assert main._awaiting_pasted_reply is False
        assert SEND_READY_ARMED in _send_line(app)  # captured, waiting for the next paste


async def test_the_send_gate_survives_a_modal_that_only_suspends_the_detectors(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same reasoning as the reply gate it rides on: an F2 visit forgets what
    the detectors SAW, and un-pastes nothing. The payload is still sitting
    unsent in the chat box afterwards, so the hold has to still be on."""
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()
        await _send_ready(main, pilot, True)

        main.suspend_detectors()
        main.resume_detectors()
        await pilot.pause()
        assert main._send_gate is main_mod.SendGate.SEEN
        assert SEND_READY_SEEN in _send_line(app)


async def test_the_poller_thread_really_looks_for_the_button(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test above injects probes; this one lets the real loop produce them.

    The wiring it proves is that the send probe rides the tick's ONE shared
    capture and is posted from the same thread as the three finish detectors -
    with a blank frame the button is never found, so ten real ticks are exactly
    the timeout path.
    """
    _patch_flow(monkeypatch)
    _record_notifications(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: BLANK_FRAME)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.01)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        seed_templates(
            main._selected_service(),
            TemplateKind.COPY,
            TemplateKind.SEND_READY,
            size=(24, 24),
        )
        main._profiles.clear()
        main.update_config(main._config)
        await _wait_for(
            pilot,
            lambda: main._active_profile().has(TemplateKind.SEND_READY),
            "send button appearance known",
        )
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")

        main._open_reply_gate()
        assert main._send_gate is main_mod.SendGate.HOLD
        await _wait_for(pilot, lambda: main._send_gate is None, "the gate timed out on real probes")


async def test_the_button_is_looked_for_whether_or_not_anyone_is_waiting(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detector searches on calibration alone (screen/detector.py), so the
    probes arrive on every tick and the gate is a READER of them.

    Two halves. The stream exists with no gate open - which is what keeps the
    ELEMENTS column's send row honest on a resting chat - and a probe that lands
    while nothing is holding changes nothing at all: the gate is opened by the
    paste, never by a sighting.
    """
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        assert main._send_gate is None
        assert main._detector is not None
        assert main._detector.searches(TemplateKind.SEND_READY)

        # The button is on screen (the composer holds something the user typed
        # themselves) and then goes. Outside a gate that is not a send, and not
        # anybody's business.
        await _send_ready(main, pilot, True)
        await _send_ready(main, pilot, False)
        assert main._send_gate is None
        assert main._send_gate_ticks == 0
        assert main._awaiting_pasted_reply is False

        # ...and the gate the paste opens still works exactly as it did.
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert main._send_gate is main_mod.SendGate.HOLD
        await _send_ready(main, pilot, True)
        assert main._send_gate is main_mod.SendGate.SEEN
        await _send_ready(main, pilot, False)
        assert main._send_gate is None


async def test_a_probe_from_a_dead_poller_run_cannot_release_the_gate(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict is a reading of one window. Cancelling a poll thread only
    raises a flag, so an in-flight look at the window AgentClip has stopped
    driving lands after the retarget - and "the send button is gone" over there
    says nothing about the payload sitting in the chat over here."""
    _patch_flow(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrated_but_idle(app, pilot, seed_templates, monkeypatch, send_ready=True)
        await main.copy_outbound("the payload")
        await pilot.pause()
        await _send_ready(main, pilot, True)

        main._automation.feed_probe("send_ready", False, main._detector_generation - 1)
        await pilot.pause()
        assert main._send_gate is main_mod.SendGate.SEEN
