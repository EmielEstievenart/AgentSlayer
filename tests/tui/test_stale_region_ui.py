"""Pilot tests for the THIRD finish detector's picker (sidebar "Set response
region..." / ``#set-stale-btn``).

Mirrors the picker half of test_busy_region_ui.py, with the one structural
difference that is the whole point of this detector: there is NO baseline
capture at calibration time. The user draws the response area and that region
is all that is stored - the poller's ``StaleTracker`` treats its first polled
frame as the baseline and compares every later frame to the one before it. So
"calibrated" here means ``stale_region is not None`` and nothing else.

As everywhere in this suite the real overlay, the real GDI capture and the real
poll cadence are monkeypatched at their use site (``main_mod.*``) - the autouse
OS gate in tests/conftest.py fails loudly if a picker escapes.

What we verify: button -> picker -> region on the *calibrating* slot -> sidebar
label -> poller (re)start, cancel and picker-failure leaving everything
untouched, the slot switch repainting the readout from stored state, and /new
keeping the region while restarting the poller. The verdict state machine the
stale probes feed lives in test_finish_signal_ui.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import RegionImage
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen
from agentclip.tui.widgets.sidebar import STALE_CALIBRATED, STALE_UNSET

REGION = ScreenRegion(1000, 200, 600, 500)
SUB_REGION = ScreenRegion(120, 60, 400, 300)
# A frame the fake capture always hands back: unchanging pixels are exactly
# what the tracker reads as stillness, so the poller is easy to observe.
FRAME = RegionImage(width=8, height=8, pixels=b"\x00" * (8 * 8 * 4))

# The sidebar is a tall stack of calibration rows now - every button has to be
# on screen for pilot.click to reach it.
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


def _make_app(tmp_path: Path) -> AgentClipApp:
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    app = AgentClipApp(
        config=config,
        provider=FakeClipboard(),
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
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


async def _click_set_stale(app: AgentClipApp, pilot: Pilot) -> None:
    await _press(app, pilot, "#set-stale-btn")


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
    milliseconds, so the static "calibrated" line and the slot-switch repaint
    can only be asserted deterministically with nothing polling.
    """
    starts: list[None] = []

    def fake_start(self: MainScreen) -> None:
        starts.append(None)

    monkeypatch.setattr(MainScreen, "_start_detector_worker", fake_start)
    return starts


def _record_notifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake_notify(self: MainScreen, message: str, *args: Any, **kwargs: Any) -> None:
        seen.append(message)

    monkeypatch.setattr(MainScreen, "notify", fake_notify)
    return seen


async def test_calibrating_the_response_region_starts_the_poller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picked region lands on the calibrating slot - with no baseline
    captured, which is what separates this detector from busy/idle - and the
    poller starts watching it."""
    picker = _patch_picker(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert STALE_UNSET in _stale_label(app)
        assert main._detector_worker is None

        await _click_set_stale(app, pilot)
        await _wait_for(pilot, lambda: main._stale_region == REGION, "response region adopted")

        assert main._slots[AgentSlot.MASTER].stale_region == REGION
        assert "RESPONSE area" in picker.prompts[-1]
        # No pixels stored: the region alone is the whole calibration, and it is
        # enough to make the slot finish-detectable.
        assert main._busy_baseline is None
        assert main._idle_baseline is None
        assert main.live.can_finish

        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        # ...and it really polls: the tracker's first frame reads as CHANGING.
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "a stale probe arrives")


async def test_the_readout_reads_calibrated_before_any_probe_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    starts = _freeze_detector(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_stale(app, pilot)
        await _wait_for(pilot, lambda: main._stale_region == REGION, "response region adopted")
        await pilot.pause()

        assert _stale_label(app) == STALE_CALIBRATED
        assert starts == [None]  # exactly one (re)start for one calibration


async def test_recalibrating_replaces_the_poller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second pick must not leave two loops polling different regions."""
    _patch_picker(monkeypatch)
    _record_notifications(monkeypatch)  # the first toast would cover the button
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_stale(app, pilot)
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        first = main._detector_worker
        assert first is not None

        await pilot.pause(_CLICK_CHAIN_S)
        await _click_set_stale(app, pilot)
        await _wait_for(pilot, lambda: first.is_cancelled, "the first poller was replaced")
        assert main._detector_worker is not None
        assert main._detector_worker is not first


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch, region=None)
    notes = _record_notifications(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_stale(app, pilot)
        await pilot.pause(0.2)

        assert main._stale_region is None
        assert main._slots[AgentSlot.MASTER].stale_region is None
        assert main._detector_worker is None
        assert STALE_UNSET in _stale_label(app)
        assert any("cancelled" in note for note in notes)


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    monkeypatch.setattr(main_mod, "pick_region", boom)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: FRAME)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)
    notes = _record_notifications(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_stale(app, pilot)
        await pilot.pause(0.2)

        assert main._stale_region is None
        assert main._detector_worker is None
        assert STALE_UNSET in _stale_label(app)
        assert any("no tkinter" in note for note in notes)
        # The refusal guard was released, so the button still works afterwards.
        assert main._picker_open is False


async def test_switching_slots_repaints_the_readout_from_stored_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``show_slot`` renders the column from ONE slot's stored calibration: the
    master's response region must not read as calibrated while the sub-agent
    slot (which has none) is selected, and must come back when it is."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_stale(app, pilot)
        await _wait_for(pilot, lambda: main._stale_region == REGION, "master region adopted")
        assert _stale_label(app) == STALE_CALIBRATED

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await pilot.pause()
        assert _stale_label(app) == STALE_UNSET
        assert main._slots[AgentSlot.SUBAGENT].stale_region is None

        await _select_slot(app, pilot, AgentSlot.MASTER)
        await pilot.pause()
        assert _stale_label(app) == STALE_CALIBRATED
        assert main._slots[AgentSlot.MASTER].stale_region == REGION


async def test_the_two_slots_keep_their_own_response_regions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker = _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_stale(app, pilot)
        await _wait_for(pilot, lambda: main._stale_region == REGION, "master region adopted")

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        picker.region = SUB_REGION
        await pilot.pause(_CLICK_CHAIN_S)
        await _click_set_stale(app, pilot)
        await _wait_for(
            pilot,
            lambda: main._slots[AgentSlot.SUBAGENT].stale_region == SUB_REGION,
            "sub-agent region adopted",
        )

        assert main._slots[AgentSlot.MASTER].stale_region == REGION
        assert main._stale_region == REGION  # the compatibility proxy is the master's
        assert main._live is AgentSlot.MASTER  # calibrating never retargets the automation
        assert "SUB-AGENT window" in picker.prompts[-1]


async def test_new_keeps_the_response_region_and_restarts_the_poller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/new is a session teardown, not a recalibration: the drawn response area
    survives it (with the stale detector as the ONLY calibrated one, which the
    poller's "nothing to watch" early-out must not mistake for uncalibrated),
    and a fresh poller takes over watching it."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _click_set_stale(app, pilot)
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        worker = main._detector_worker
        assert worker is not None
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "a stale probe arrives")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")

        assert main._stale_region == REGION
        assert main._slots[AgentSlot.MASTER].stale_region == REGION
        assert STALE_UNSET not in _stale_label(app)
        await _wait_for(pilot, lambda: worker.is_cancelled, "old poller cancelled")
        assert main._detector_worker is not None
        assert main._detector_worker is not worker
        # The fresh poller really is watching: a new verdict lands post-/new.
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "polling resumed")
