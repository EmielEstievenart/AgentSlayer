"""Pilot tests for the staleness detector, which has no button any more.

It is the one finish detector that needs no captured cue at all: a chat region
that has stopped changing frame to frame is a finished response, whatever a
particular service's pixel cues do. So drawing the chat window IS its whole
calibration - and, because that window is drawn anyway for everything else, and
because it is what every service's ``finish_signals`` checklist ships ticked, it
means every slot has a working finish detector from the first drag. Ticked, not
unconditional: the last section here covers a service that opts out of it (or of
finish detection altogether).

The overlay, the GDI capture and the poll cadence are monkeypatched at their
use site (``main_mod.*``) - the autouse OS gate in tests/conftest.py fails
loudly if a picker escapes. The verdict state machine the stale probes feed
lives in test_finish_signal_ui.py.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.screen.detector as detector_mod
import agentclip.tui.screens.main as main_mod
from agentclip.app.types import SessionRef
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, can_finish
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed
from agentclip.tui.screens.main import MASTER_WINDOW, SUBAGENT_WINDOW, MainScreen
from agentclip.tui.widgets.sidebar import (
    PROBE_RESTING,
    PROBE_UNCAPTURED,
    STALE_CALIBRATED,
    STALE_OFF,
    STALE_UNSET,
    STALE_UNTICKED,
)
from agentclip.tui.widgets.window_tabs import WindowTabs

from .conftest import send_composer

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


def _detection_title(app: AgentClipApp) -> str:
    """The DETECTION heading, which names the LIVE window the block is about."""
    return _label(app, "#side-detection-title")


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
    """Send a composer line - see ``send_composer`` for why /new takes two Enters."""
    await send_composer(app, pilot, text)


# Which window tab each slot lives on. Selecting the tab is what points the
# sidebar at a slot now; the mapping is MainScreen's seam for an N-window bar.
WINDOW_OF = {AgentSlot.MASTER: MASTER_WINDOW, AgentSlot.SUBAGENT: SUBAGENT_WINDOW}


async def _select_slot(app: AgentClipApp, pilot: Pilot, slot: AgentSlot) -> None:
    """Select that window's tab - which is what points the sidebar at a slot now
    (the tab bar itself is test_tabs_ui's)."""
    main = app.main_screen
    assert main is not None
    main._select_window(WINDOW_OF[slot])
    await _wait_for(pilot, lambda: main._calibrating is slot, f"{slot} selected")
    await pilot.pause()


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


class _FrozenWorker:
    """A poll worker that never polls - cancellable, and above all *present*.

    ``resume_detectors`` restarts only when nothing is running, so a stub that
    left ``_detector_worker`` at None would turn every no-op resume into a
    second rebuild the real app never performs, and the restart counts below
    would be counting the stub.
    """

    def __init__(self) -> None:
        self.is_cancelled = False

    def cancel(self) -> None:
        self.is_cancelled = True


def _freeze_detector(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Stop the poll THREAD and count how often one would have been started.

    Only the spawn is stubbed, not the whole of ``_start_detector_worker``: the
    composition (which detectors, and the DETECTION block that reports them) is
    what most of these tests are about, and a live loop repaints ``#side-stale``
    with a probe readout within milliseconds of it.
    """
    starts: list[None] = []

    def fake_spawn(self: MainScreen, loop: object) -> None:
        starts.append(None)
        self._detector_worker = cast(Any, _FrozenWorker())

    monkeypatch.setattr(MainScreen, "_spawn_detector_worker", fake_spawn)
    return starts


def _record_stale_ticks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the ``required_ticks`` every StaleTracker the poller builds gets.

    The stillness window is a preset field converted to ticks of the poll
    cadence ONCE, when the poller starts - so this list is both "how many times
    was the poller rebuilt" and "what did each rebuild believe", which is
    exactly what an edited ``stable_seconds`` has to change.

    Spied where the tracker is now BUILT: composing a detector out of one
    window's calibration is ``screen.detector.build_detector``'s job, and
    MainScreen only asks it for one.
    """
    ticks: list[int] = []
    real = detector_mod.StaleTracker

    def spy(region: ScreenRegion, **kwargs: Any) -> Any:
        ticks.append(int(kwargs["required_ticks"]))
        return real(region, **kwargs)

    monkeypatch.setattr(detector_mod, "StaleTracker", spy)
    return ticks


def _capture_busy(main: MainScreen, seed: Callable[..., None]) -> None:
    """Give the ACTIVE service a busy appearance, as a capture leaves it.

    Capturing moved into the service editor (F2), and all this screen ever sees
    of a visit there is a PNG in the profile store plus an ``update_config``
    telling it to drop its cached profile - which is also the documented path
    that rebuilds the poller around what it can now hunt for.
    """
    seed(main._selected_service(), TemplateKind.BUSY)
    main._profiles.clear()
    main.update_config(main._config)


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


async def test_switching_tabs_leaves_the_detection_block_alone(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DETECTION block reports on the LIVE window, not the selected tab.

    It used to be repainted from the selected slot's stored region, which made
    every tab click (and every F6) claim "watching the chat region" for whatever
    tab happened to be up - clobbering a real readout of the window the poller
    is actually on, and clobbering "finish detection off" with a claim that was
    false for the entire session. Only the detector machinery writes here now,
    and the block's heading names the window it means.
    """
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert _detection_title(app) == "DETECTION · MASTER"

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == REGION, "master region adopted")
        assert _stale_label(app) == STALE_CALIBRATED

        # The sub-agent tab has no window of its own, but nothing is watching it
        # either: the automation is still on the master's.
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await pilot.pause()
        assert _stale_label(app) == STALE_CALIBRATED
        assert _detection_title(app) == "DETECTION · MASTER"
        assert main._slots[AgentSlot.SUBAGENT].chat_region is None
        assert "not set" in _label(app, "#side-region")  # the CHAT WINDOW block DID follow

        await _select_slot(app, pilot, AgentSlot.MASTER)
        await pilot.pause()
        assert _stale_label(app) == STALE_CALIBRATED


async def test_finish_detection_off_survives_tab_clicks_and_f6(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression in its plainest form: a service that detects nothing says
    so, and browsing the tabs must not talk over it. "watching the chat region"
    there is a promise of an auto-copy that will never come."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        _with_signals(main)  # nothing ticked
        await pilot.pause()
        assert _stale_label(app) == STALE_OFF

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await pilot.pause()
        assert _stale_label(app) == STALE_OFF

        await pilot.press("f6")
        await pilot.pause()
        assert _stale_label(app) == STALE_OFF

        # ...including the tab bar's re-announcement of the tab already selected.
        main._on_window_selected(WindowTabs.WindowSelected(main._selected_window))
        await pilot.pause()
        assert _stale_label(app) == STALE_OFF


async def test_reselecting_the_same_tab_never_wipes_a_live_verdict(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clicking the tab you are already on is a no-op. The bar re-posts
    ``WindowSelected`` for it, and the repaint that followed reset every probe
    line to "no verdict yet" - throwing away exactly the readout the user
    clicked over to look at."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    _record_notifications(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == REGION, "master region adopted")
        seed_templates(main._selected_service(), TemplateKind.BUSY)
        main._profiles.clear()
        _with_signals(main, "busy", "stale")
        await pilot.pause()

        main.post_message(
            BusyProbed(BusyProbe(BusyState.MATCH, 0.42), main._detector_generation)
        )
        await _wait_for(pilot, lambda: "GENERATING" in _label(app, "#side-tpl-busy"), "a verdict")

        main._on_window_selected(WindowTabs.WindowSelected(main._selected_window))
        await pilot.pause()
        assert "GENERATING" in _label(app, "#side-tpl-busy")


async def test_a_ticked_but_uncaptured_signal_says_so_on_its_own_line(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checklist entry with no appearance behind it runs nothing at all, and
    "no verdict yet" for the rest of the run is indistinguishable from a
    detector that simply never finds anything."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")

        _with_signals(main, "busy", "stale")  # busy ticked, nothing captured
        await pilot.pause()
        assert main._active_detectors == ("stale",)
        assert PROBE_UNCAPTURED in _label(app, "#side-tpl-busy")
        assert PROBE_RESTING in _label(app, "#side-tpl-idle")  # not ticked: nothing to say


async def test_an_unticked_stale_signal_reads_as_unwatched_not_silent(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the icon detectors running and stale unticked, the stale line has no
    verdict coming - so it says why rather than sitting on whatever it last
    said."""
    _patch_picker(monkeypatch)
    _record_notifications(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "a stale probe arrives")
        _capture_busy(main, seed_templates)
        await _wait_for(
            pilot, lambda: main._active_profile().has(TemplateKind.BUSY), "busy appearance known"
        )

        _with_signals(main, "busy")
        await _wait_for(pilot, lambda: main._active_detectors == ("busy",), "busy alone")
        assert _stale_label(app) == STALE_UNTICKED


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


class _BlockingPicker:
    """An overlay that stays up until the test says otherwise.

    The real one blocks for as long as the user takes to drag a box, which is
    exactly the window in which the rest of the app keeps moving - the point of
    the test below. It runs in a worker thread (``asyncio.to_thread``), so the
    handshake is two events rather than an await.
    """

    def __init__(self, region: ScreenRegion | None) -> None:
        self.region = region
        self.prompts: list[str] = []
        self.opened = threading.Event()
        self.finish = threading.Event()

    def __call__(self, prompt: str = "") -> ScreenRegion | None:
        self.prompts.append(prompt)
        self.opened.set()
        self.finish.wait(10)
        return self.region


async def test_the_box_lands_in_the_tab_that_opened_the_picker(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selected tab is not fixed while the overlay is up, and reading it
    afterwards filed the box under whichever tab had moved in underneath.

    ``_calibrating`` moves on its own now: the controller focusing a delegated
    run's transcript (``open_session_view`` -> ``focus_session_view`` ->
    ``_select_window``) selects the sub-agent tab. A delegation starting while
    the user was mid-drag therefore stored the box they drew around the MASTER's
    window as the SUB-AGENT's, and skipped the poller restart the master needed.
    """
    picker = _BlockingPicker(REGION)
    monkeypatch.setattr(main_mod, "pick_region", picker)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: FRAME)
    starts = _freeze_detector(monkeypatch)
    _record_notifications(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._calibrating is AgentSlot.MASTER

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: picker.opened.is_set(), "the overlay opened")
        assert "SUB-AGENT window" not in picker.prompts[-1]  # asked about the MASTER's

        # Mid-drag, a delegation opens its transcript and selects the sub tab.
        await main.open_session_view(
            SessionRef(id="sub-1", role="subagent", title="read the docs", chat_name="jade-otter")
        )
        await pilot.pause()
        assert main._calibrating is AgentSlot.SUBAGENT

        picker.finish.set()  # ...and only now does the user let go
        await _wait_for(
            pilot, lambda: main._slots[AgentSlot.MASTER].chat_region == REGION, "box filed"
        )

        assert main._slots[AgentSlot.SUBAGENT].chat_region is None
        # The master's window is the one the automation drives, so its poller
        # was rebuilt around the box that just changed.
        assert main._live is AgentSlot.MASTER
        assert starts == [None]
        # ...and the sidebar, which is showing the SUB-AGENT tab, was not given
        # the master's rectangle to display.
        assert "not set" in _label(app, "#side-region")


async def test_new_keeps_the_window_and_restarts_the_poller(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_chat_click_lands: None,
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


async def test_calibrating_the_subagent_slot_never_retargets_the_poller(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drawing the sub-agent's window must not re-aim the automation.

    The poller keeps watching the MASTER's window - only the live slot's own
    rectangle (and the busy/idle appearances, which are the service's) compose
    it. What the sub-agent draw does cost is the suspension every fullscreen
    overlay gets (§3.4e): the picker covers the whole virtual desktop, so the
    master's window is behind it whichever tab opened it, and the frames its
    trackers would carry across describe the overlay rather than the chat.
    Sparing them there is how an overlay coming down armed the trigger."""
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
        await pilot.pause(0.2)
        # One restart, and it is the overlay suspension's - not a retarget: the
        # automation and the poller are both still on the master's window.
        assert starts == [None, None]
        assert main._live is AgentSlot.MASTER
        assert main._chat_region == REGION
        assert "MASTER" in _detection_title(app)

        # The busy appearance IS one of the things the poller hunts, and it is
        # the service's - so gaining one rebuilds whichever slot is live.
        _capture_busy(main, seed_templates)
        await _wait_for(
            pilot,
            lambda: starts == [None, None, None],
            "poller rebuilt for the new busy appearance",
        )
        assert main._active_profile().has(TemplateKind.BUSY)


# -- the per-service finish-signal checklist ---------------------------------------


def _with_signals(main: MainScreen, *signals: str, hover_scan: bool = False) -> None:
    """Rewrite the ACTIVE service's checklist and adopt the edited config.

    ``finish_signals`` is a per-service preset field, so this is exactly what
    the (not-yet-built) editor checkbox will do - and ``update_config`` is the
    documented path that restarts the poller against it."""
    key = main._selected_service()
    services = dict(main._config.services)
    services[key] = replace(services[key], finish_signals=signals, hover_scan=hover_scan)
    main.update_config(replace(main._config, services=services))


async def test_an_empty_checklist_runs_nothing_and_says_so(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service whose checklist asks for no detector gets no poller at all -
    and the readout has to say why, because "auto-copy never fires" is
    otherwise indistinguishable from a detector that never finds anything."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")

        _with_signals(main)  # nothing ticked
        await pilot.pause()
        assert main._active_detectors == ()
        assert main._detector_worker is None
        assert _stale_label(app) == STALE_OFF


async def test_stale_is_opt_out_not_unconditional(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staleness is what every service ships with, but it is a checklist entry
    like the others: unticked with nothing else captured, nothing runs."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._active_preset().finish_signals == ("stale",)

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._active_detectors == ("stale",), "stale running")

        _with_signals(main, "busy")  # ticked, but nothing captured to match on
        await pilot.pause()
        assert main._active_detectors == ()
        assert _stale_label(app) == STALE_OFF


async def test_a_ticked_detector_needs_its_appearance_captured(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checklist says what the service is allowed to use; the profile says
    what it can actually see. Both, or the detector does not run."""
    _patch_picker(monkeypatch)
    _record_notifications(monkeypatch)  # toasts would cover the buttons
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")
        _capture_busy(main, seed_templates)
        await _wait_for(
            pilot, lambda: main._active_profile().has(TemplateKind.BUSY), "busy appearance known"
        )
        # Captured but not ticked: the default checklist is stale-only.
        assert main._active_detectors == ("stale",)

        _with_signals(main, "busy", "stale")
        await _wait_for(
            pilot, lambda: main._active_detectors == ("busy", "stale"), "busy joined the poll"
        )
        assert main._finish_tick_closed_by("stale")  # still the canonical last

        _with_signals(main, "busy")
        await _wait_for(pilot, lambda: main._active_detectors == ("busy",), "stale dropped")
        assert main._finish_tick_closed_by("busy")


async def test_an_unticked_stale_detector_posts_no_stale_verdicts(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tick's message sequence follows the built set, so an unticked
    detector must be silent rather than merely ignored."""
    _patch_picker(monkeypatch)
    _record_notifications(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: "GENERATING" in _stale_label(app), "a stale probe arrives")
        _capture_busy(main, seed_templates)
        await _wait_for(
            pilot, lambda: main._active_profile().has(TemplateKind.BUSY), "busy appearance known"
        )

        _with_signals(main, "busy")
        await _wait_for(pilot, lambda: main._active_detectors == ("busy",), "busy alone")
        assert main._stale_tracker is None
        main._stale_seen = False
        await pilot.pause(0.2)  # several ticks at the 0.02 s cadence
        assert main._stale_seen is False


async def test_no_window_means_no_poller_at_all(
    tmp_path: Path, profile_root: Path, new_chat_click_lands: None
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
