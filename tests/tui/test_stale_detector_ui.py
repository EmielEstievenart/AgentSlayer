"""Pilot tests for the staleness detector, which has no button any more.

It is the one finish detector that needs no captured cue at all: a chat region
that has stopped changing frame to frame is a finished response, whatever a
particular service's pixel cues do. So drawing the chat window IS its whole
calibration - and, because that window is drawn anyway for everything else, it
means every slot has a working finish detector from the first drag.

The overlay, the GDI capture and the poll cadence are monkeypatched at their
use site (``main_mod.*``) - the autouse OS gate in tests/conftest.py fails
loudly if a picker escapes. The verdict state machine the stale probes feed
lives in test_finish_signal_ui.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, can_finish
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen
from agentclip.tui.widgets.sidebar import STALE_CALIBRATED, STALE_UNSET

REGION = ScreenRegion(1000, 200, 600, 500)
SUB_REGION = ScreenRegion(120, 60, 400, 300)
# A frame the fake capture always hands back: unchanging pixels are exactly
# what the tracker reads as stillness, so the poller is easy to observe.
FRAME = RegionImage(width=8, height=8, pixels=b"\x00" * (8 * 8 * 4))

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


def _make_app(tmp_path: Path, profile_root: Path) -> AgentClipApp:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    app = AgentClipApp(
        config=config,
        provider=FakeClipboard(),
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        profile_root=profile_root,
    )
    return app


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


def _stale_label(app: AgentClipApp) -> str:
    return _label(app, "#side-stale")


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


# Textual folds two clicks on the same spot inside its chain window into one
# double-click, which the Button never reports as a second press - so a test
# that pushes the same button twice has to wait the window out.
_CLICK_CHAIN_S = 0.6


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """Type into the composer and send - refocusing it first, since clicking the
    sidebar button leaves focus on the button."""
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.focus()
    await pilot.pause()
    await pilot.press("enter")


async def _select_slot(app: AgentClipApp, pilot: Pilot, slot: AgentSlot) -> None:
    main = app.main_screen
    assert main is not None
    main.sidebar.slot_select.value = str(slot)
    await _wait_for(pilot, lambda: main._calibrating is slot, f"{slot} selected")


class _Picker:
    """Stand-in for the tkinter overlay: hands back whatever region is armed."""

    def __init__(self, region: ScreenRegion | None) -> None:
        self.region = region
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> ScreenRegion | None:
        self.prompts.append(prompt)
        return self.region


def _patch_picker(
    monkeypatch: pytest.MonkeyPatch,
    region: ScreenRegion | None = REGION,
    *,
    poll_s: float = 0.02,
) -> _Picker:
    """Stub the overlay, the GDI capture the tracker polls with, and the cadence."""
    picker = _Picker(region)
    monkeypatch.setattr(main_mod, "pick_region", picker)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: FRAME)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", poll_s)
    return picker


def _freeze_detector(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Stub the poller out and count its starts.

    The live poller repaints ``#side-stale`` with a probe readout within
    milliseconds, so the static "watching" line and the slot-switch repaint
    can only be asserted deterministically with nothing polling.
    """
    starts: list[None] = []

    def fake_start(self: MainScreen) -> None:
        starts.append(None)

    monkeypatch.setattr(MainScreen, "_start_detector_worker", fake_start)
    return starts


def _record_stale_ticks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the ``required_ticks`` every StaleTracker the poller builds gets.

    The stillness window is a preset field converted to ticks of the poll
    cadence ONCE, when the poller starts - so this list is both "how many times
    was the poller rebuilt" and "what did each rebuild believe", which is
    exactly what an edited ``stable_seconds`` has to change.
    """
    ticks: list[int] = []
    real = main_mod.StaleTracker

    def spy(region: ScreenRegion, **kwargs: Any) -> Any:
        ticks.append(int(kwargs["required_ticks"]))
        return real(region, **kwargs)

    monkeypatch.setattr(main_mod, "StaleTracker", spy)
    return ticks


def _record_notifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake_notify(self: MainScreen, message: str, *args: Any, **kwargs: Any) -> None:
        seen.append(message)

    monkeypatch.setattr(MainScreen, "notify", fake_notify)
    return seen


async def test_drawing_the_chat_window_is_the_whole_calibration(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pixels are stored and no separate box is asked for: the drawn window
    makes the slot finish-detectable on its own, and the poller starts."""
    picker = _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert STALE_UNSET in _stale_label(app)
        assert main._detector_worker is None

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == REGION, "chat region adopted")

        assert main._slots[AgentSlot.MASTER].chat_region == REGION
        assert "WHOLE browser window" in picker.prompts[-1] or "box" in picker.prompts[-1]
        assert main._active_profile().captured == ()  # nothing captured at all
        assert can_finish(main.live, main._active_profile())

        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        # ...and it really polls: the tracker's first frame reads as CHANGING.
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "a stale probe arrives")


async def test_the_stale_detector_is_the_only_one_running_by_default(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing captured, staleness is the whole finish signal - and it is
    therefore the message that closes every tick."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._active_detectors == ()

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        assert main._active_detectors == ("stale",)
        assert main._finish_tick_closed_by("stale")
        assert not main._finish_tick_closed_by("busy")


async def test_the_readout_reads_watching_before_any_probe_lands(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    starts = _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == REGION, "chat region adopted")
        await pilot.pause()

        assert _stale_label(app) == STALE_CALIBRATED
        assert starts == [None]  # exactly one (re)start for one drawn region


async def test_redrawing_replaces_the_poller(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second draw must not leave two loops watching two windows."""
    _patch_picker(monkeypatch)
    _record_notifications(monkeypatch)  # the first toast would cover the button
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        first = main._detector_worker
        assert first is not None

        await pilot.pause(_CLICK_CHAIN_S)
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: first.is_cancelled, "the first poller was replaced")
        assert main._detector_worker is not None
        assert main._detector_worker is not first


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch, region=None)
    notes = _record_notifications(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await pilot.pause(0.2)

        assert main._chat_region is None
        assert main._detector_worker is None
        assert STALE_UNSET in _stale_label(app)
        assert any("cancelled" in note for note in notes)


async def test_a_shared_capture_failure_reaches_the_detector_as_an_error(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One capture per tick is handed to every detector, so a failed one has to
    read as ERROR everywhere rather than letting some detectors see a frame."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        def boom(region: ScreenRegion) -> RegionImage:
            raise CaptureError("no display")

        monkeypatch.setattr(main_mod, "capture_region", boom)
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: "capture failed" in _stale_label(app), "error reported")


async def test_switching_slots_repaints_the_readout_from_stored_state(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``show_slot`` renders the column from ONE slot's drawn window: the
    master's must not read as watching while the sub-agent slot (which has no
    window) is selected, and must come back when it is."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == REGION, "master region adopted")
        assert _stale_label(app) == STALE_CALIBRATED

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await pilot.pause()
        assert _stale_label(app) == STALE_UNSET
        assert main._slots[AgentSlot.SUBAGENT].chat_region is None

        await _select_slot(app, pilot, AgentSlot.MASTER)
        await pilot.pause()
        assert _stale_label(app) == STALE_CALIBRATED


async def test_the_two_slots_keep_their_own_windows(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker = _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == REGION, "master region adopted")

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        picker.region = SUB_REGION
        await pilot.pause(_CLICK_CHAIN_S)
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(
            pilot,
            lambda: main._slots[AgentSlot.SUBAGENT].chat_region == SUB_REGION,
            "sub-agent region adopted",
        )

        assert main._slots[AgentSlot.MASTER].chat_region == REGION
        assert main._chat_region == REGION  # the compatibility proxy is the master's
        assert main._live is AgentSlot.MASTER  # calibrating never retargets the automation
        assert "SUB-AGENT window" in picker.prompts[-1]


async def test_new_keeps_the_window_and_restarts_the_poller(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/new is a session teardown, not a recalibration: the drawn window
    survives it and a fresh poller takes over watching it."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        worker = main._detector_worker
        assert worker is not None
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "a stale probe arrives")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")

        assert main._chat_region == REGION
        assert STALE_UNSET not in _stale_label(app)
        await _wait_for(pilot, lambda: worker.is_cancelled, "old poller cancelled")
        assert main._detector_worker is not None
        assert main._detector_worker is not worker
        # The fresh poller really is watching: a new verdict lands post-/new.
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "polling resumed")


async def test_editing_the_stillness_window_rebuilds_the_poller(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stable_seconds`` is baked into the tracker's tick count at poller
    start, so adopting an edited Config has to restart it - otherwise the new
    value sat unused until some unrelated recalibration happened to rebuild it."""
    _patch_picker(monkeypatch, poll_s=0.02)
    ticks = _record_stale_ticks(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: len(ticks) == 1, "poller built for the drawn window")
        assert ticks == [100]  # the 2.0 s default at a 0.02 s cadence

        key = main._selected_service()
        services = dict(main._config.services)
        services[key] = replace(services[key], stable_seconds=6.0)
        main.update_config(replace(main._config, services=services))

        await _wait_for(pilot, lambda: len(ticks) == 2, "poller rebuilt for the edited preset")
        assert ticks[-1] == 300


async def test_calibrating_the_subagent_slot_spares_the_masters_poller(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The master chat can be mid-generation while the user draws the sub-agent
    window, and a restart would throw its streaks (and its trackers' previous
    frames) away. Only what the poller actually hunts may rebuild it: the live
    slot's window, and the busy/idle appearances."""
    picker = _patch_picker(monkeypatch)
    starts = _freeze_detector(monkeypatch)
    _record_notifications(monkeypatch)  # toasts would cover the buttons
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == REGION, "master region adopted")
        assert starts == [None]  # the live slot's own window: rebuilt

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        picker.region = SUB_REGION
        await pilot.pause(_CLICK_CHAIN_S)
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(
            pilot,
            lambda: main._slots[AgentSlot.SUBAGENT].chat_region == SUB_REGION,
            "sub-agent region adopted",
        )
        await _press(app, pilot, "#capture-copy-btn")
        await _wait_for(
            pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy captured"
        )
        await pilot.pause(0.2)  # a restart would have landed by now
        assert starts == [None]  # neither touched the window the poller watches
        assert main._live is AgentSlot.MASTER

        # The busy appearance IS one of the things the poller hunts, and it is
        # the service's - so capturing it rebuilds whichever slot is live.
        await _press(app, pilot, "#capture-busy-btn")
        await _wait_for(
            pilot, lambda: starts == [None, None], "poller rebuilt for the new busy appearance"
        )
        assert main._active_profile().has(TemplateKind.BUSY)


async def test_no_window_means_no_poller_at_all(
    tmp_path: Path, profile_root: Path
) -> None:
    """Nothing to watch: /new on an undrawn app must not spin up a loop."""
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._detector_worker is None

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._detector_worker is None
        assert main._active_detectors == ()
