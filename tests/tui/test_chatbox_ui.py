"""Pilot tests for the chat input box: captured once per SERVICE, found in the
drawn chat region on the spot.

Nothing here remembers where a chat box is. The user draws one box - the chat
window - and the service is told what the two input-box layouts *look like* (a
fresh chat centres its box, an ongoing one docks it at the bottom); before every
post-response click the live chat region is captured once and both appearances
are hunted inside it. The pixels are the service's, so they persist to disk and
come back on the next run; the region is the window's, so it stays on the slot.

The appearances are seeded straight into the profile store - the same PNGs a
capture in the service editor (F2) leaves behind - because the capture flow is
the editor's subject, covered once in test_profile_capture_ui.py. What is
tested here is what the drawn window and those pixels are then used FOR.

Picker, capture and click are monkeypatched at their use site
(agentclip.tui.screens.main); the in-region search is monkeypatched too, since
a synthetic frame has no icon in it - the stand-in recognises a seeded template
by the very bytes ``_frame`` stored for it. The autouse fixture in conftest.py
keeps every profile write inside tmp_path.

What we verify: the appearances loading off disk, the resolution order (ongoing
found -> its rect; else initial; else the chat region itself), and the
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
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import save_template
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import RegionMatch, Template
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
INITIAL_BOX = ScreenRegion(1300, 520, 400, 90)
ONGOING_BOX = ScreenRegion(1300, 860, 400, 90)

# Every button has to be on screen for pilot.click to reach it.
SIZE = (110, 100)


@pytest.fixture(autouse=True)
def _no_detector_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here is about the finish detectors, and a live poller is actively
    hostile to it: it rewrites the sidebar's probe lines on its own schedule,
    and it could fire the auto-copy flow into the middle of a bootstrap."""
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)


def _frame(region: ScreenRegion) -> RegionImage:
    """A capture of ``region``: a cycling byte pattern keyed to its origin, so
    two different boxes never produce the same bytes.

    Varied rather than flat because a seeded frame is also a template: a block
    of one colour has no anchors to search for, and ``ServiceProfile.put``
    refuses it - which would show up only as an appearance that silently failed
    to load. The undefined X byte is zeroed because the profile store's PNGs
    come back that way, so a stored frame and a fresh one compare equal.
    """
    start = (region.left + region.top) % 251
    unit = bytes(0 if i % 4 == 3 else (start + i) % 256 for i in range(256))
    size = region.width * region.height * 4
    return RegionImage(region.width, region.height, (unit * (size // 256 + 1))[:size])


# Which drawn box each layout was captured from - the pixels a seeded template
# holds, and the ones the fake scene below is asked about.
BOX_FOR = {
    TemplateKind.CHATBOX_INITIAL: INITIAL_BOX,
    TemplateKind.CHATBOX_ONGOING: ONGOING_BOX,
}


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


def _service_key(app: AgentClipApp) -> str:
    """The service the sidebar starts on - the one every appearance is filed
    under (mirrors ``Sidebar._default_service``)."""
    config = app.app_config
    if config.general.service in config.services:
        return config.general.service
    return sorted(config.services)[0]


def _seed_boxes(profile_root: Path, app: AgentClipApp, *kinds: TemplateKind) -> None:
    """File the listed input-box layouts under the app's service, exactly as a
    capture in the service editor would.

    Straight into the store rather than through ``seed_templates``' generic
    pixels: these appearances have to be recognisable in the fake scene, so what
    is stored is the very frame ``_frame`` will hand the stand-in search.
    """
    for kind in kinds:
        save_template(profile_root, _service_key(app), kind, _frame(BOX_FOR[kind]))


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


def _local(rect: ScreenRegion) -> RegionMatch:
    return RegionMatch(rect.left - CHAT_REGION.left, rect.top - CHAT_REGION.top, 0.01)


def _patch_found(monkeypatch: pytest.MonkeyPatch, *rects: ScreenRegion) -> None:
    """Say which appearances are on screen, and where.

    The search itself is screen.template's job (tested there); here the frames
    are synthetic, so the stand-in recognises a template by the pixels ``_frame``
    gave it and answers with a chat-region-local offset. Anything not listed is
    simply not on screen.
    """
    wanted = {bytes(_frame(rect).pixels[:4]): rect for rect in rects}

    def fake_find_all(template: Template, scene: RegionImage, **kw: object) -> list[RegionMatch]:
        rect = wanted.get(bytes(template.image.pixels[:4]))
        return [] if rect is None else [_local(rect)]

    monkeypatch.setattr(main_mod, "find_all_in_region", fake_find_all)


async def _draw_chat_region(app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    main = app.main_screen
    assert main is not None
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")


# -- what the service already looks like -----------------------------------------


async def test_the_captures_survive_a_restart(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason appearances left the slot: captured once, reused every
    run. An app over a profile root an earlier run captured into starts already
    calibrated - the right pixels, filed under the right service."""
    _patch_capture(monkeypatch)
    app, _ = _make_app(tmp_path, profile_root)
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        profile = main._active_profile()
        assert profile.key == _service_key(app)
        template = profile.get(TemplateKind.CHATBOX_ONGOING)
        assert template is not None
        assert (template.width, template.height) == (ONGOING_BOX.width, ONGOING_BOX.height)
        # The other layout is a separate appearance, and no window was drawn.
        assert not profile.has(TemplateKind.CHATBOX_INITIAL)
        assert main._chat_region is None
        assert "1/6 captured" in _label(app, "#side-profile-note")


async def test_the_two_layouts_are_kept_apart(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two appearances, not one: the same service holds both layouts, and the
    pixels of one are never the pixels of the other."""
    _patch_capture(monkeypatch)
    app, _ = _make_app(tmp_path, profile_root)
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        profile = main._active_profile()
        initial = profile.get(TemplateKind.CHATBOX_INITIAL)
        ongoing = profile.get(TemplateKind.CHATBOX_ONGOING)
        assert initial is not None and ongoing is not None
        assert initial.image != ongoing.image
        assert "2/6 captured" in _label(app, "#side-profile-note")


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
        return CHAT_REGION

    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "pick_region", slow_pick)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: overlay_open.is_set(), "first overlay up")

        # Any further picker press bounces off while the overlay is open.
        await _press(app, pilot, "#set-region-btn")
        await pilot.pause(0.2)
        assert picks == 1
        assert main._chat_region is None  # the overlay has not answered yet

        finish_pick.set()
        await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")

        # The guard releases once the overlay resolves: picking works again.
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: picks == 2, "second picker allowed after the first closed")


# -- click resolution ------------------------------------------------------------


async def test_the_ongoing_box_found_in_the_region_wins(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-session the docked box is on screen, so that is what gets clicked -
    and it is hunted first, so the fresh-chat layout is never even asked for."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, fake = _make_app(tmp_path, profile_root)
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _draw_chat_region(app, pilot, monkeypatch)
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
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _draw_chat_region(app, pilot, monkeypatch)
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
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch)  # nothing is on screen

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")
        assert clicks == [CHAT_REGION]


async def test_two_boxes_of_one_layout_fall_back_to_the_drawn_window(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two windows of the same service under one drawn box show two identical
    input boxes. This click is what focuses the window a whole turn is about to
    be pasted into, so poking the wrong one puts the payload in somebody else's
    conversation - the drawn region's centre is the user's own answer to "where
    is this chat", and it is what a service with no box captured gets anyway."""
    clicks: list[ScreenRegion] = []
    _patch_capture(monkeypatch)
    monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(region) or True)
    app, _ = _make_app(tmp_path, profile_root)
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _draw_chat_region(app, pilot, monkeypatch)

        second = ScreenRegion(
            ONGOING_BOX.left + 400, ONGOING_BOX.top, ONGOING_BOX.width, ONGOING_BOX.height
        )

        def fake_find_all(
            template: Template, scene: RegionImage, **kw: object
        ) -> list[RegionMatch]:
            if bytes(template.image.pixels[:4]) != bytes(_frame(ONGOING_BOX).pixels[:4]):
                return []
            return [_local(ONGOING_BOX), _local(second)]

        monkeypatch.setattr(main_mod, "find_all_in_region", fake_find_all)
        assert await main._chatbox_region() == CHAT_REGION

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
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _draw_chat_region(app, pilot, monkeypatch)

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
    _seed_boxes(profile_root, app, TemplateKind.CHATBOX_INITIAL, TemplateKind.CHATBOX_ONGOING)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _draw_chat_region(app, pilot, monkeypatch)
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
