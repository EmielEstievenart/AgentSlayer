"""Pilot tests for the dual chatbox detector (replaces the old click region).

A fresh chat centres its input box and an ongoing one docks it at the bottom,
so AgentClip calibrates BOTH ("Set initial chatbox..." / "Set ongoing
chatbox...") and, before every post-response click, asks which of them still
looks like its calibration snapshot. Picker, capture, element probe and click
are all monkeypatched at their use site (agentclip.tui.screens.main) - the real
ones spawn a tkinter overlay, read GDI pixels and move the OS cursor.

What we verify: button -> picker -> snapshot -> sidebar label + session state,
the resolution rule (ongoing asked first, then initial, then a fallback chain
ending at the chat window), and the calibrations surviving /new.
"""

from __future__ import annotations

import threading
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
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
INITIAL_BOX = ScreenRegion(1300, 520, 400, 90)
ONGOING_BOX = ScreenRegion(1300, 860, 400, 90)

# The sidebar is a tall stack of calibration rows now - every button has to be
# on screen for pilot.click to reach it.
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


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


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


def _patch_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "capture_region", _frame)


def _patch_matching(monkeypatch: pytest.MonkeyPatch, *matching: ScreenRegion) -> None:
    """Only the listed regions still look like their calibration snapshot."""
    monkeypatch.setattr(
        main_mod,
        "probe_element",
        lambda element: element.region in matching,
    )


# -- calibration ---------------------------------------------------------------


async def test_calibrating_a_chatbox_snapshots_it_and_updates_the_sidebar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: ONGOING_BOX)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not set" in _label(app, "#side-chatbox-ongoing")

        await _press(app, pilot, "#set-chatbox-ongoing-btn")
        await _wait_for(pilot, lambda: main._chatbox_ongoing is not None, "ongoing chatbox adopted")
        element = main._chatbox_ongoing
        assert element == CalibratedElement(ONGOING_BOX, _frame(ONGOING_BOX))
        assert "400×90 at (1300, 860)" in _label(app, "#side-chatbox-ongoing")
        assert "ongoing-chat input box" in _label(app, "#side-chatbox-ongoing")
        # The two calibrations are independent, and neither is the chat window.
        assert main._chatbox_initial is None
        assert main._chat_region is None


async def test_the_two_chatboxes_are_calibrated_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_capture(monkeypatch)
    picked = [INITIAL_BOX, ONGOING_BOX]
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: picked.pop(0))
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-chatbox-initial-btn")
        await _wait_for(pilot, lambda: main._chatbox_initial is not None, "initial adopted")
        await _press(app, pilot, "#set-chatbox-ongoing-btn")
        await _wait_for(pilot, lambda: main._chatbox_ongoing is not None, "ongoing adopted")

        assert main._chatbox_initial is not None
        assert main._chatbox_ongoing is not None
        assert main._chatbox_initial.region == INITIAL_BOX
        assert main._chatbox_ongoing.region == ONGOING_BOX
        assert "fresh-chat input box" in _label(app, "#side-chatbox-initial")


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-chatbox-ongoing-btn")
        await pilot.pause(0.2)
        assert main._chatbox_ongoing is None
        assert "not set" in _label(app, "#side-chatbox-ongoing")


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", boom)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-chatbox-initial-btn")
        await pilot.pause(0.2)
        assert main._chatbox_initial is None  # error notified; app carries on
        assert "not set" in _label(app, "#side-chatbox-initial")


async def test_capture_failure_keeps_the_chatbox_uncalibrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A region without a snapshot is useless - the whole point is recognising
    the box later, so a half-calibration is refused outright."""

    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: ONGOING_BOX)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-chatbox-ongoing-btn")
        await pilot.pause(0.2)
        assert main._chatbox_ongoing is None
        assert "not set" in _label(app, "#side-chatbox-ongoing")


async def test_a_second_picker_is_refused_while_an_overlay_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a worker cannot kill the child overlay process, so a press
    while a picker is up must be refused outright - never a second overlay."""
    overlay_open = threading.Event()
    finish_pick = threading.Event()
    picks = 0

    def slow_pick(prompt: str | None = None) -> ScreenRegion:
        nonlocal picks
        picks += 1
        overlay_open.set()
        assert finish_pick.wait(timeout=10.0), "test never released the overlay"
        return ONGOING_BOX

    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", slow_pick)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-chatbox-ongoing-btn")
        await _wait_for(pilot, lambda: overlay_open.is_set(), "first overlay up")

        # Every other picker button bounces off while the overlay is open.
        await _press(app, pilot, "#set-chatbox-initial-btn")
        await _press(app, pilot, "#set-region-btn")
        await _press(app, pilot, "#set-idle-btn")
        await _press(app, pilot, "#set-newchat-btn")
        await pilot.pause(0.2)
        assert picks == 1

        finish_pick.set()
        await _wait_for(pilot, lambda: main._chatbox_ongoing is not None, "ongoing adopted")
        assert main._chatbox_initial is None

        # The guard releases once the overlay resolves: picking works again.
        await _press(app, pilot, "#set-chatbox-initial-btn")
        await _wait_for(pilot, lambda: picks == 2, "second picker allowed after the first closed")


# -- click resolution ------------------------------------------------------------


async def _calibrate_both(app: AgentClipApp, pilot: Pilot) -> None:
    await _press(app, pilot, "#set-chatbox-initial-btn")
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main._chatbox_initial is not None, "initial adopted")
    await _press(app, pilot, "#set-chatbox-ongoing-btn")
    await _wait_for(pilot, lambda: main._chatbox_ongoing is not None, "ongoing adopted")


def _patch_two_picks(monkeypatch: pytest.MonkeyPatch) -> None:
    picked = [INITIAL_BOX, ONGOING_BOX]
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: picked.pop(0))


async def test_the_matching_ongoing_chatbox_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-session the docked box is on screen, so that is what gets clicked -
    and it is asked first, so the initial box is never even probed."""
    clicks: list[ScreenRegion] = []
    probed: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    _patch_two_picks(monkeypatch)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    monkeypatch.setattr(
        main_mod,
        "probe_element",
        lambda element: bool(probed.append(element.region)) or element.region == ONGOING_BOX,
    )
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_both(app, pilot)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert fake.read_text() is not None  # the bootstrap really was copied
        assert clicks == [ONGOING_BOX]
        assert probed == [ONGOING_BOX]  # the initial box was never asked


async def test_the_initial_chatbox_wins_when_only_it_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh chat: the docked box is not on screen, the centred one is."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    _patch_two_picks(monkeypatch)
    _patch_matching(monkeypatch, INITIAL_BOX)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_both(app, pilot)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [INITIAL_BOX]


async def test_neither_matching_falls_back_to_the_ongoing_chatbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-transition (or behind a dialog) both probes fail. Clicking a
    stale-looking box is recoverable; not clicking means the paste never lands,
    so the ongoing box is still poked."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    _patch_two_picks(monkeypatch)
    _patch_matching(monkeypatch)  # nothing matches
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_both(app, pilot)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [ONGOING_BOX]


async def test_only_the_initial_calibrated_and_not_matching_still_clicks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: INITIAL_BOX)
    _patch_matching(monkeypatch)  # nothing matches
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-chatbox-initial-btn")
        await _wait_for(pilot, lambda: main._chatbox_initial is not None, "initial adopted")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [INITIAL_BOX]


async def test_the_chat_region_is_the_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chatbox calibrated at all: today's behaviour is preserved - the drawn
    chatbot window gets the focus click."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [CHAT_REGION]


async def test_nothing_calibrated_means_no_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == []


# -- session teardown ------------------------------------------------------------


async def test_new_preserves_both_chatboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chatboxes describe where the service's input boxes are, not what the
    finished session said - /new must not make the user re-draw them."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    _patch_two_picks(monkeypatch)
    _patch_matching(monkeypatch, ONGOING_BOX)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _calibrate_both(app, pilot)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [ONGOING_BOX]

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._chatbox_initial is not None
        assert main._chatbox_initial.region == INITIAL_BOX
        assert main._chatbox_ongoing is not None
        assert main._chatbox_ongoing.region == ONGOING_BOX
        assert INITIAL_BOX.describe() in _label(app, "#side-chatbox-initial")
        assert ONGOING_BOX.describe() in _label(app, "#side-chatbox-ongoing")

        # The surviving calibrations mean the next bootstrap clicks again.
        await _send(app, pilot, "Fresh session task.")
        await _wait_for(pilot, lambda: len(clicks) == 2, "second bootstrap clicked")
        await _wait_for(pilot, lambda: not main.busy, "second session flow settled")
        assert clicks == [ONGOING_BOX, ONGOING_BOX]
