"""Pilot tests for the new-chat button: captured per SERVICE, found in the
drawn chat region, clicked where it actually is.

One button in this column now. "New browser chat" searches for the browser's
new-chat control inside the calibrating slot's chat region and clicks the
match. What that control *looks like* is filed under the active service by the
service editor (F2), so these tests seed it straight into the profile store -
the same files a real capture leaves behind - rather than re-driving a capture
flow that is covered once, in test_profile_capture_ui.py. Picker, search, click
and focus are monkeypatched at their use site (agentclip.tui.screens.main).

The find step is the point, and it buys two things at once: a browser that
re-laid itself out or moved gets clicked *where the button is* rather than
where it used to be, and a button that genuinely is not on screen gets no click
at all. The three failures stay three different stories - nothing captured,
not on screen, and the OS refusing the click.

Every outcome is a toast and only a toast: the button is found on demand rather
than polled, so there is no verdict worth keeping on screen between presses and
the sidebar has no line for it.
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
from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import RegionMatch, Template
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
NEWCHAT_BOX = ScreenRegion(120, 90, 180, 36)
# Where the button "is" inside the chat region, and the absolute rect that
# implies - the click has to land on the second one, never the first.
FOUND = RegionMatch(x=40, y=24, diff=0.02)
CLICK_TARGET = ScreenRegion(
    CHAT_REGION.left + FOUND.x, CHAT_REGION.top + FOUND.y, NEWCHAT_BOX.width, NEWCHAT_BOX.height
)
# The same button seen a couple of pixels over - one element, two matches.
JITTERED = RegionMatch(x=FOUND.x + 3, y=FOUND.y + 2, diff=0.04)
# A SECOND browser window of the same service inside the drawn region: far
# enough away to be a different button, which is the whole problem.
SECOND_WINDOW = RegionMatch(x=FOUND.x + 400, y=FOUND.y, diff=0.03)
SIZE = (110, 100)

# The four things the button can say. Nothing captured and no window drawn are
# one branch (there is nowhere to look, or nothing to look for), and it is the
# branch that sends the user to the editor.
NOT_CALIBRATED_TOAST = "capture the browser's new-chat button first"
MISMATCH_TOAST = "not on screen"
AMBIGUOUS_TOAST = "found several things that look like the new-chat button"
NOT_CLICKED_TOAST = "did not land"
CLICKED_TOAST = "new browser chat opened"


@pytest.fixture(autouse=True)
def _no_detector_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here is about the finish detectors, and a live poller rewrites a
    wrapping line in the sidebar on its own schedule - which reflows every
    button below it, so a probe landing between a click's mouse-down and
    mouse-up moves the button out from under the pointer and the press is
    silently lost. (The poller itself is test_stale_detector_ui's.)"""
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)


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
    """The service the sidebar starts on - the one an appearance has to be
    filed under for this app to load it."""
    config = app.app_config
    configured = config.general.service
    return configured if configured in config.services else next(iter(sorted(config.services)))


def _seed_newchat(app: AgentClipApp, seed_templates: Callable[..., None]) -> str:
    """Give the selected service a new-chat appearance, as an editor capture
    would: real PNGs in the profile store, which the app loads off disk.

    Call before ``run_test`` - the profile is read lazily on first use.
    """
    key = _service_key(app)
    seed_templates(key, TemplateKind.NEW_CHAT, size=(NEWCHAT_BOX.width, NEWCHAT_BOX.height))
    return key


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


def _toasts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every toast the screen raises, in order.

    The button paints no status line, so this is its whole output surface.
    """
    notes: list[str] = []
    monkeypatch.setattr(
        MainScreen, "notify", lambda self, message, *a, **kw: notes.append(str(message))
    )
    return notes


def _said(notes: list[str], fragment: str) -> bool:
    return any(fragment in note for note in notes)


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.focus()
    await pilot.pause()
    await pilot.press("enter")


def _patch_found(
    monkeypatch: pytest.MonkeyPatch, *matches: RegionMatch
) -> list[RegionImage]:
    """Say where the captured button is on screen, and record every search.

    Several matches means several of them really are in the region - the search
    itself is screen.template's job (tested there)."""
    scenes: list[RegionImage] = []

    def fake_find_all(template: Template, scene: RegionImage, **kw: object) -> list[RegionMatch]:
        scenes.append(scene)
        return list(matches)

    monkeypatch.setattr(main_mod, "find_all_in_region", fake_find_all)
    return scenes


async def _draw_chat_region(
    app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Draw the calibrating slot's chat window - the box the new-chat button is
    hunted inside, and the other half of "calibrated"."""
    main = app.main_screen
    assert main is not None
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")


async def test_the_action_finds_it_clicks_it_and_hands_focus_back(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The click lands on the match's absolute rectangle, not on the box the
    user happened to drag - that is the whole difference."""
    events: list[str] = []
    clicks: list[tuple[ScreenRegion, float]] = []
    focus_calls: list[int] = []
    monkeypatch.setattr(main_mod, "foreground_window", lambda: 4242)
    monkeypatch.setattr(
        main_mod,
        "click_region",
        lambda region, *, settle_s=0.0: (
            bool(clicks.append((region, settle_s))) or bool(events.append("click")) or True
        ),
    )
    monkeypatch.setattr(
        main_mod,
        "focus_window",
        lambda handle: bool(focus_calls.append(handle)) or bool(events.append("focus")) or True,
    )

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._own_window == 4242  # recorded at mount

        await _draw_chat_region(app, pilot, monkeypatch)
        scenes = _patch_found(monkeypatch, FOUND)
        clicks.clear()
        events.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: focus_calls == [4242], "focus snapped back")

        assert _said(notes, CLICKED_TOAST)
        assert clicks == [(CLICK_TARGET, 0.05)]
        assert events == ["click", "focus"]  # find, then click, then snap back
        # The button was hunted in the chat region, not anywhere else.
        assert [(scene.width, scene.height) for scene in scenes] == [
            (CHAT_REGION.width, CHAT_REGION.height)
        ]


async def test_not_on_screen_warns_and_clicks_nothing(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The page moved on: clicking blind could hit anything, so nothing is."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch)
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: _said(notes, MISMATCH_TOAST), "miss reported")
        assert clicks == []
        # The capture is kept, so the user can retry once the page settles.
        assert main._active_profile().has(TemplateKind.NEW_CHAT)


async def test_two_of_them_in_the_region_warns_and_clicks_nothing(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The appearance is the SERVICE's, so a second window of the same service
    inside the drawn region carries an identical button. Picking one is a coin
    toss between two conversations - so neither is clicked, and the fix the user
    is told about is a redraw, not a recapture."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND, SECOND_WINDOW)
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: _said(notes, AMBIGUOUS_TOAST), "refusal shown")
        assert _said(notes, "redraw the window")
        assert clicks == []
        assert main._active_profile().has(TemplateKind.NEW_CHAT)  # nothing was lost


async def test_two_hits_on_the_same_button_are_still_one_button(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """A template matches its own element at several neighbouring origins - a
    pixel of drift is well inside the diff threshold - so counting raw matches
    would refuse every click on a perfectly ordinary screen."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND, JITTERED)
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: _said(notes, CLICKED_TOAST), "clicked once")
        assert clicks == [CLICK_TARGET]  # the first of the two, once


async def test_a_refused_click_is_reported_separately(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """Found fine but the OS swallowed the input (not Windows): a different
    story to tell than "this button is not on screen"."""
    focus_calls: list[int] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: False)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: focus_calls.append(handle) or True)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: _said(notes, NOT_CLICKED_TOAST), "refusal reported")
        assert focus_calls == []  # leave the browser focused so the user can click


async def test_nothing_captured_means_nothing_is_even_searched_for(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is drawn, so there IS somewhere to look - but this service has
    never been shown what it is looking for, and a search needs both."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)
    scenes = _patch_found(monkeypatch, FOUND)

    app, _ = _make_app(tmp_path, profile_root)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert not main._active_profile().has(TemplateKind.NEW_CHAT)

        await _draw_chat_region(app, pilot, monkeypatch)
        await _press(app, pilot, "#newchat-btn")
        await pilot.pause(0.3)
        assert clicks == []
        assert scenes == []
        # The toast is the only feedback, and it points at the editor.
        assert _said(notes, NOT_CALIBRATED_TOAST)


async def test_no_chat_region_means_nowhere_to_look(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The appearance is captured but the slot has no window drawn - the same
    "go calibrate" branch, because there is nowhere to search."""
    clicks: list[ScreenRegion] = []
    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._active_profile().has(TemplateKind.NEW_CHAT)

        scenes = _patch_found(monkeypatch, FOUND)
        monkeypatch.setattr(
            main_mod, "click_region", lambda region, **kw: clicks.append(region) or True
        )

        await _press(app, pilot, "#newchat-btn")
        await pilot.pause(0.3)
        assert clicks == []
        assert scenes == []
        assert _said(notes, NOT_CALIBRATED_TOAST)


async def test_new_preserves_the_capture(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The appearance describes the service, not what the finished session
    said - /new must not make the user recapture it."""
    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        assert "1/6 captured" in _label(app, "#side-profile-note")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._active_profile().has(TemplateKind.NEW_CHAT)
        assert "1/6 captured" in _label(app, "#side-profile-note")
