"""Pilot tests for the chat input box: captured once per SERVICE, found in the
drawn chat region on the spot.

Nothing here remembers where a chat box is. The user draws one box - the chat
window - and captures what the two input-box layouts *look like* (a fresh chat
centres its box, an ongoing one docks it at the bottom); before every
post-response click the live chat region is captured once and both appearances
are hunted inside it. The pixels are the service's, so they persist to disk and
come back on the next run; the region is the window's, so it stays on the slot.

Picker, capture and click are monkeypatched at their use site
(agentclip.tui.screens.main); the in-region search is monkeypatched too, since
a synthetic frame of zero bytes has no icon in it. The autouse fixture in
conftest.py keeps every profile write inside tmp_path.

What we verify: capture -> profile + disk round trip, the resolution order
(ongoing found -> its rect; else initial; else the chat region itself), and the
calibrations surviving /new.
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
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import load_profile
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import RegionMatch, Template
from agentclip.tui.app import AgentClipApp

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
INITIAL_BOX = ScreenRegion(1300, 520, 400, 90)
ONGOING_BOX = ScreenRegion(1300, 860, 400, 90)

# The sidebar is a tall stack of calibration rows now - every button has to be
# on screen for pilot.click to reach it.
SIZE = (110, 100)


def _frame(region: ScreenRegion) -> RegionImage:
    """A capture of ``region``: flat pixels keyed to its left edge, so two
    different boxes never produce the same bytes."""
    fill = bytes([region.left % 251, region.top % 251, region.width % 251, 0])
    return RegionImage(region.width, region.height, fill * (region.width * region.height))


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, profile_root: Path) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        profile_root=profile_root,
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


def _patch_found(monkeypatch: pytest.MonkeyPatch, *rects: ScreenRegion) -> None:
    """Say which appearances are on screen, and where.

    The search itself is screen.template's job (tested there); here the frames
    are synthetic, so the stand-in recognises a template by the pixels ``_frame``
    gave it and answers with a chat-region-local offset. Anything not listed is
    simply not on screen.
    """
    wanted = {bytes(_frame(rect).pixels[:4]): rect for rect in rects}

    def fake_find(template: Template, scene: RegionImage, **kw: object) -> RegionMatch | None:
        rect = wanted.get(bytes(template.image.pixels[:4]))
        if rect is None:
            return None
        return RegionMatch(rect.left - CHAT_REGION.left, rect.top - CHAT_REGION.top, 0.01)

    monkeypatch.setattr(main_mod, "find_in_region", fake_find)


async def _draw_chat_region(app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    main = app.main_screen
    assert main is not None
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")


# -- capture -------------------------------------------------------------------


async def test_capturing_a_chat_box_files_it_under_the_service_and_saves_it(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: ONGOING_BOX)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not captured" in _label(app, "#side-tpl-chatbox-ongoing")
        key = main._selected_service()

        await _press(app, pilot, "#capture-chatbox-ongoing-btn")
        await _wait_for(
            pilot,
            lambda: main._active_profile().has(TemplateKind.CHATBOX_ONGOING),
            "ongoing chat box captured",
        )

        template = main._active_profile().get(TemplateKind.CHATBOX_ONGOING)
        assert template is not None
        assert (template.width, template.height) == (ONGOING_BOX.width, ONGOING_BOX.height)
        assert "400×90 · captured" in _label(app, "#side-tpl-chatbox-ongoing")
        # The other layout is a separate appearance, and no window was drawn.
        assert not main._active_profile().has(TemplateKind.CHATBOX_INITIAL)
        assert main._chat_region is None

        # ...and it is on disk, under the selected service.
        reloaded = load_profile(profile_root, key)
        assert reloaded.has(TemplateKind.CHATBOX_ONGOING)


async def test_the_captures_survive_a_restart(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason appearances left the slot: captured once, reused every
    run. A second app over the same profile root starts already calibrated."""
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: ONGOING_BOX)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _press(app, pilot, "#capture-chatbox-ongoing-btn")
        await _wait_for(
            pilot,
            lambda: main._active_profile().has(TemplateKind.CHATBOX_ONGOING),
            "ongoing chat box captured",
        )

    again, _ = _make_app(tmp_path, profile_root)
    async with again.run_test(size=SIZE) as pilot:
        main = again.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._active_profile().has(TemplateKind.CHATBOX_ONGOING)
        assert "captured" in _label(again, "#side-tpl-chatbox-ongoing")


async def test_the_two_layouts_are_captured_separately(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_capture(monkeypatch)
    picked = [INITIAL_BOX, ONGOING_BOX]
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: picked.pop(0))
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#capture-chatbox-initial-btn")
        await _wait_for(
            pilot,
            lambda: main._active_profile().has(TemplateKind.CHATBOX_INITIAL),
            "initial captured",
        )
        await _press(app, pilot, "#capture-chatbox-ongoing-btn")
        await _wait_for(
            pilot,
            lambda: main._active_profile().has(TemplateKind.CHATBOX_ONGOING),
            "ongoing captured",
        )

        profile = main._active_profile()
        initial = profile.get(TemplateKind.CHATBOX_INITIAL)
        ongoing = profile.get(TemplateKind.CHATBOX_ONGOING)
        assert initial is not None and ongoing is not None
        assert initial.image != ongoing.image
        assert "400×90 · captured" in _label(app, "#side-tpl-chatbox-initial")


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#capture-chatbox-ongoing-btn")
        await pilot.pause(0.2)
        assert not main._active_profile().has(TemplateKind.CHATBOX_ONGOING)
        assert "not captured" in _label(app, "#side-tpl-chatbox-ongoing")


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", boom)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#capture-chatbox-initial-btn")
        await pilot.pause(0.2)
        assert not main._active_profile().has(TemplateKind.CHATBOX_INITIAL)
        assert "not captured" in _label(app, "#side-tpl-chatbox-initial")


async def test_capture_failure_keeps_the_appearance_unknown(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A region without pixels is useless here - the pixels ARE the calibration."""

    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: ONGOING_BOX)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#capture-chatbox-ongoing-btn")
        await pilot.pause(0.2)
        assert not main._active_profile().has(TemplateKind.CHATBOX_ONGOING)
        assert "not captured" in _label(app, "#side-tpl-chatbox-ongoing")


async def test_a_second_picker_is_refused_while_an_overlay_is_open(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
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
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#capture-chatbox-ongoing-btn")
        await _wait_for(pilot, lambda: overlay_open.is_set(), "first overlay up")

        # Every other picker button bounces off while the overlay is open.
        await _press(app, pilot, "#capture-chatbox-initial-btn")
        await _press(app, pilot, "#set-region-btn")
        await _press(app, pilot, "#capture-idle-btn")
        await _press(app, pilot, "#capture-new-chat-btn")
        await pilot.pause(0.2)
        assert picks == 1

        finish_pick.set()
        await _wait_for(
            pilot,
            lambda: main._active_profile().has(TemplateKind.CHATBOX_ONGOING),
            "ongoing captured",
        )
        assert not main._active_profile().has(TemplateKind.CHATBOX_INITIAL)

        # The guard releases once the overlay resolves: picking works again.
        await _press(app, pilot, "#capture-chatbox-initial-btn")
        await _wait_for(pilot, lambda: picks == 2, "second picker allowed after the first closed")


# -- click resolution ------------------------------------------------------------


async def _capture_both(app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    """Draw the chat window, then capture both input-box layouts into the profile."""
    main = app.main_screen
    assert main is not None
    await _draw_chat_region(app, pilot, monkeypatch)
    picked = [INITIAL_BOX, ONGOING_BOX]
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: picked.pop(0))
    await _press(app, pilot, "#capture-chatbox-initial-btn")
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.CHATBOX_INITIAL), "initial captured"
    )
    await _press(app, pilot, "#capture-chatbox-ongoing-btn")
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.CHATBOX_ONGOING), "ongoing captured"
    )


async def test_the_ongoing_box_found_in_the_region_wins(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-session the docked box is on screen, so that is what gets clicked -
    and it is hunted first, so the fresh-chat layout is never even asked for."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, fake = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _capture_both(app, pilot, monkeypatch)
        # Both layouts happen to be findable; ongoing is asked first and wins.
        _patch_found(monkeypatch, ONGOING_BOX, INITIAL_BOX)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert fake.read_text() is not None  # the bootstrap really was copied
        assert clicks == [ONGOING_BOX]


async def test_the_initial_box_wins_when_only_it_is_on_screen(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh chat: the docked box is not on screen, the centred one is."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _capture_both(app, pilot, monkeypatch)
        _patch_found(monkeypatch, INITIAL_BOX)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [INITIAL_BOX]


async def test_neither_on_screen_falls_back_to_the_chat_window(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-transition (or behind a dialog) neither appearance is found. Clicking
    the window is recoverable; not clicking means the paste never lands."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _capture_both(app, pilot, monkeypatch)
        _patch_found(monkeypatch)  # nothing is on screen

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [CHAT_REGION]


async def test_the_chat_region_alone_is_enough_to_click(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No appearance captured at all: the drawn chatbot window gets the click,
    exactly as before - one drawn box is the whole minimum setup."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _draw_chat_region(app, pilot, monkeypatch)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [CHAT_REGION]


async def test_a_failed_capture_of_the_region_still_clicks_the_window(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A screen we cannot read is not a reason to skip the focus click: the
    window is where the box is, whatever the pixels say."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _capture_both(app, pilot, monkeypatch)

        def boom(region: ScreenRegion) -> RegionImage:
            raise CaptureError("no display")

        monkeypatch.setattr(main_mod, "capture_region", boom)
        monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [CHAT_REGION]


async def test_nothing_drawn_means_no_click(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == []


# -- session teardown ------------------------------------------------------------


async def test_new_preserves_the_region_and_the_appearances(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window has not moved and the service still looks like itself - /new
    must not make the user redraw or recapture anything."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _capture_both(app, pilot, monkeypatch)
        _patch_found(monkeypatch, ONGOING_BOX)
        monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [ONGOING_BOX]

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._chat_region == CHAT_REGION
        assert main._active_profile().has(TemplateKind.CHATBOX_INITIAL)
        assert main._active_profile().has(TemplateKind.CHATBOX_ONGOING)
        assert CHAT_REGION.describe() in _label(app, "#side-region")

        # The survivors mean the next bootstrap clicks the same box again.
        await _send(app, pilot, "Fresh session task.")
        await _wait_for(pilot, lambda: len(clicks) == 2, "second bootstrap clicked")
        await _wait_for(pilot, lambda: not main.busy, "second session flow settled")
        assert clicks == [ONGOING_BOX, ONGOING_BOX]
