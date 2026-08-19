"""Pilot tests for the new-chat button: captured per SERVICE, found in the
drawn chat region, clicked where it actually is.

One button in this column now. "New browser chat" searches for the browser's
new-chat control inside the calibrating slot's chat region and clicks the
match. What that control *looks like* is filed under the active service by the
service editor (F2), so these tests seed it straight into the profile store -
the same files a real capture leaves behind - rather than re-driving a capture
flow that is covered once, in test_profile_capture_ui.py. Picker, search, click
and focus are monkeypatched at their use site (agentclip.shell.tui.screens.main).

The find step is the point, and it buys two things at once: a browser that
re-laid itself out or moved gets clicked *where the button is* rather than
where it used to be, and a button that genuinely is not on screen gets no click
at all. The three failures stay three different stories - nothing captured,
not on screen, and the OS refusing the click.

Every outcome is a toast and only a toast: the button is found on demand rather
than polled, so there is no verdict worth keeping on screen between presses and
the sidebar has no line for it.

The last two blocks are the two ways to ask for a fresh chat, which are one
implementation - ``_new_browser_chat(slot)`` - reached from both ends (§3.3a,
§1.3):

* the sidebar button: opening a fresh chat under a live master session ends that
  session too, because the conversation it is having no longer exists - and it
  does so mid-turn as well, aborting the turn in flight rather than refusing.
  The user must always be able to start a new chat; the only tab that still says
  no is the sub-agent's, whose chat belongs to a delegated run.
* ``/new``: the same flow, pinned to the master window and typed rather than
  clicked. The browser is touched at command time, and the reset is that flow's
  tail - which runs whether or not the click landed. The click is best-effort:
  the tool side is the half AgentClip can always deliver, and withholding it
  because the browser could not be reached left the user with neither half.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.shell.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.template import RegionMatch, Template
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.messages import ClipboardCaptured
from agentclip.shell.tui.screens.main import MASTER_WINDOW, SUBAGENT_WINDOW, MainScreen

from .conftest import send_composer

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

# Pinned so the mid-turn tests can address a reply to this session's chat.
CHAT_NAME = "amber-falcon"
# An edit, which gates by default: the cheapest way to park a real turn where
# the old busy guard used to refuse.
EDIT_REPLY = f"""On it.

===CLIP:CALL id=1 tool=edit_file===
path: notes.txt
find <<EOT
one
EOT
replace <<EOT
two
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat={CHAT_NAME}===
"""

# The four things the button can say. Nothing captured and no window drawn are
# one branch (there is nowhere to look, or nothing to look for), and it is the
# branch that sends the user to the editor.
NOT_CALIBRATED_TOAST = "capture the browser's new-chat button first"
MISMATCH_TOAST = "not on screen"
AMBIGUOUS_TOAST = "found several things that look like the new-chat button"
NOT_CLICKED_TOAST = "did not land"
CLICKED_TOAST = "new browser chat opened"

# The one refusal the button has left: the SUB-AGENT tab, mid-run. On the master
# tab a press mid-turn aborts the turn instead (§1.3), and says so.
SUB_MID_RUN_TOAST = "the sub-agent window's chat belongs to the run in flight"
ABORT_TOAST = "aborting the current step - starting a fresh session"
# Every failed click ends by handing the browser half back to the user, and says
# whether the tool half was renewed - the pair that stops a "fresh session"
# claim on a tab that never had one.
DISARMED_TOAST = "no new chat was opened"
RESTARTED_TAIL = "fresh session anyway"
YOURSELF_TAIL = "open a new browser chat yourself"


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
        engine_factory=make_engine_factory(lambda: app.app_config, project, CHAT_NAME),
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
    """Send a composer line - see ``send_composer`` for why /new takes two Enters."""
    await send_composer(app, pilot, text)


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
        "focus_window_verified",
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


async def test_the_button_learns_our_window_before_it_hands_focus_away(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """Pressing a button in the sidebar is proof AgentClip has the OS focus at
    that instant, so the handle is read there too - not only at mount and at
    every composer send. Without it, a run whose mount reading came back empty
    (mid focus switch) and whose composer was never used has nothing to snap
    back to, and the press leaves the user staring at the browser."""
    readings: list[int | None] = [None]  # mount catches a focus switch: no handle
    monkeypatch.setattr(main_mod, "foreground_window", lambda: readings[-1])
    focus_calls: list[int] = []
    monkeypatch.setattr(
        main_mod, "focus_window_verified", lambda handle: focus_calls.append(handle) or True
    )
    _record_clicks(monkeypatch)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._own_window is None

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)
        readings.append(4242)  # the press itself: our terminal is in front

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: focus_calls == [4242], "focus snapped back")


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
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: focus_calls.append(handle) or True)

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
        # The toast is the only feedback, and it points at the editor. No
        # session is running, so there is no tool side to renew either - and the
        # toast may not claim one.
        assert _said(notes, NOT_CALIBRATED_TOAST)
        assert _said(notes, YOURSELF_TAIL)
        assert not _said(notes, RESTARTED_TAIL)
        assert main.awaiting_new_session and not main.session_active


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
        assert _said(notes, YOURSELF_TAIL)
        assert not _said(notes, RESTARTED_TAIL)  # nothing running to renew
        assert main.awaiting_new_session and not main.session_active


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
        assert "1/7 captured" in _label(app, "#side-profile-note")

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        # /new spends the capture on its way past (§3.3a) - which is the point:
        # the very command that uses it must not consume it.
        _record_clicks(monkeypatch)
        _patch_found(monkeypatch, FOUND)
        _no_real_paste(monkeypatch)
        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._active_profile().has(TemplateKind.NEW_CHAT)
        assert "1/7 captured" in _label(app, "#side-profile-note")


# == /new opens the browser's new chat at once (§3.3a) =========================
#
# The command is the button typed out: it runs the very same flow, pinned to the
# MASTER window, and the session reset is that flow's own tail. The two halves
# of a fresh chat are not equally available, though - the tool side always is,
# the browser side needs a calibrated window and an armed switch - so the tail
# runs either way and the toast says which halves the user got. Making the reset
# wait for the click meant /new did nothing at all in exactly the state it was
# reached for: a window whose new-chat button can no longer be found.
#
# No chat box is captured in this suite, so the paste's box click falls back to
# the drawn chat region: the new-chat click and the box click are trivially told
# apart by where they land.


@pytest.fixture
def _fast_new_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fresh chat's render beat, shrunk. Real value is 0.4 s per paste."""
    monkeypatch.setattr(main_mod, "_NEW_CHAT_SETTLE_S", 0.01)


def _record_clicks(monkeypatch: pytest.MonkeyPatch) -> list[ScreenRegion]:
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(
        main_mod, "click_region", lambda region, **kw: clicks.append(region) or True
    )
    return clicks


def _no_real_paste(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Ctrl+V must never escape into the runner's window; record it instead."""
    pastes: list[None] = []
    monkeypatch.setattr(main_mod, "send_paste", lambda: pastes.append(None) or True)
    return pastes


async def _start_session(app: AgentClipApp, pilot: Pilot, main: MainScreen) -> None:
    """Type a task, wait for the bootstrap copy to finish - i.e. reach the
    ordinary idle-mid-session state both features are reached from."""
    await _send(app, pilot, "Say hello.")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")


async def test_new_opens_the_fresh_chat_at_command_time(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    _fast_new_chat: None,
) -> None:
    """/new means "start over", and the chat on screen is the old conversation -
    so the command opens a fresh one there and then, exactly as the sidebar
    button does, and the session reset rides on that click landing."""
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)
        await _start_session(app, pilot, main)
        clicks.clear()

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        # The button where it was found, and nothing else: no paste is waiting
        # on it, because the browser side is already finished.
        assert clicks == [CLICK_TARGET]
        assert _said(notes, CLICKED_TOAST)
        assert not main.has_transcript_events()  # the transcript went with it
        clicks.clear()

        # ...and the session that follows pastes straight into that fresh chat.
        await main.copy_outbound("the payload")
        assert clicks == [CHAT_REGION]  # the chat box only, never a second one


async def test_the_launch_paste_opens_no_new_chat(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    _fast_new_chat: None,
) -> None:
    """Only /new opens one. The first session of the run has no stale
    conversation behind it - clicking new-chat at launch would throw away
    whatever the user had already set up in that browser tab."""
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)

        await _start_session(app, pilot, main)
        # The bootstrap was pasted into the chat that was already there.
        assert CLICK_TARGET not in clicks
        assert clicks == [CHAT_REGION]


async def test_new_with_the_button_gone_still_starts_the_fresh_session(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    _fast_new_chat: None,
) -> None:
    """The button being unfindable is a fact about the browser, not about what
    the user asked for. AgentClip still cannot click blind - so it clicks
    nothing - but the half it CAN do is done, and the toast hands the other half
    over: the tool side is new, the browser is showing the old chat, go open one.
    Refusing both halves made /new useless in the one state it was reached for."""
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch)  # the page moved on: nothing to click
        await _start_session(app, pilot, main)
        clicks.clear()

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert clicks == []  # the browser was not touched
        assert _said(notes, MISMATCH_TOAST)  # which of the reasons it was...
        assert _said(notes, RESTARTED_TAIL)  # ...and that the tool side went ahead
        assert _said(notes, YOURSELF_TAIL)
        assert not main.has_transcript_events()  # the transcript went with it


async def test_new_while_disarmed_still_starts_the_fresh_session(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    _fast_new_chat: None,
) -> None:
    """Disarmed, nothing on screen may be touched - that is the whole promise of
    the switch, and it is kept here: no capture, no search, no click. It says
    nothing about the tool's own session, though, so /new still starts one and
    the toast names the switch as the reason the browser was left alone."""
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)
    scenes = _patch_found(monkeypatch, FOUND)  # findable, if anyone were looking

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        await _start_session(app, pilot, main)
        main.set_os_armed(False)
        clicks.clear()
        scenes.clear()

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert clicks == []
        assert scenes == []  # the switch is answered before anything is searched
        assert _said(notes, DISARMED_TOAST)
        assert _said(notes, RESTARTED_TAIL)
        assert _said(notes, YOURSELF_TAIL)
        assert not main.has_transcript_events()


# == the button ends the session too (§1.3) ===================================
#
# The other end of the same flow. A fresh chat in the MASTER window means the
# conversation this session is having no longer exists, so the session goes with
# it; in the sub-agent's window it means nothing to the master at all.


async def test_the_button_on_an_idle_session_resets_the_tool_side_too(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    _fast_new_chat: None,
) -> None:
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)
        await _start_session(app, pilot, main)
        assert main.session_active
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "the session was reset")
        assert _said(notes, CLICKED_TOAST)
        assert clicks == [CLICK_TARGET]
        assert not main.has_transcript_events()  # the transcript went with it

        # This click WAS the fresh chat: the paste that follows goes into it,
        # and opens nothing.
        clicks.clear()
        await main.copy_outbound("the payload")
        assert clicks == [CHAT_REGION]  # the chat box, and nothing else


async def test_the_button_mid_turn_aborts_the_turn_and_starts_over(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    _fast_new_chat: None,
) -> None:
    """The press used to be refused here, which put the only way out of a
    conversation behind the very turn the user wanted out of. It now does what
    it says: the turn in flight is aborted (this one is parked on an approval
    gate, so the gate's future is poisoned), and then the ordinary click-and-
    reset runs. The pending edit is never applied - the decision it was waiting
    for never came."""
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)

    app, _ = _make_app(tmp_path, profile_root)
    target = tmp_path / "project" / "notes.txt"
    target.write_text("one\n", encoding="utf-8", newline="")
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)
        await _start_session(app, pilot, main)
        main.post_message(ClipboardCaptured(EDIT_REPLY))
        await _wait_for(pilot, lambda: main.pending_approval, "the edit to reach the gate")
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "the session was reset")

        assert _said(notes, ABORT_TOAST)
        assert clicks == [CLICK_TARGET]  # the browser WAS touched, unlike before
        assert _said(notes, CLICKED_TOAST)
        assert not main.pending_approval  # the drawer came down with the turn
        assert not main.has_transcript_events()
        assert target.read_text() == "one\n"  # the ungated edit never ran


async def test_the_button_on_the_sub_tab_is_refused_mid_turn(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The one refusal left, and it is not the master's to give.

    The sub-agent window hosts delegated runs, and a run keeps the master busy
    for its whole length - so a press here while a turn is in flight would empty
    the chat a sub-agent is still talking to, destroying its conversation
    without ending the run. Nothing is reset either way on this tab, so there is
    no fresh session to offer in exchange: the toast points at `/abort` and at
    the master tab instead."""
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)

    app, _ = _make_app(tmp_path, profile_root)
    (tmp_path / "project" / "notes.txt").write_text("one\n", encoding="utf-8", newline="")
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)
        await _start_session(app, pilot, main)
        main._select_window(SUBAGENT_WINDOW)
        await _wait_for(pilot, lambda: main._calibrating is AgentSlot.SUBAGENT, "sub tab selected")
        await _draw_chat_region(app, pilot, monkeypatch)
        main.post_message(ClipboardCaptured(EDIT_REPLY))
        await _wait_for(pilot, lambda: main.pending_approval, "a turn in flight")
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await pilot.pause(0.3)
        assert clicks == []  # the browser was not touched
        assert _said(notes, SUB_MID_RUN_TOAST)
        assert main.pending_approval  # and the turn is exactly where it was
        assert main.session_active and not main.awaiting_new_session


async def test_the_button_on_the_sub_tab_only_clicks(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    _fast_new_chat: None,
) -> None:
    """The sub-agent window hosts delegated runs, which the controller starts
    and ends. Emptying it says nothing about the master's conversation, so the
    session it is having stays exactly where it was."""
    clicks = _record_clicks(monkeypatch)
    _no_real_paste(monkeypatch)
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)

    app, _ = _make_app(tmp_path, profile_root)
    _seed_newchat(app, seed_templates)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _draw_chat_region(app, pilot, monkeypatch)
        _patch_found(monkeypatch, FOUND)
        await _start_session(app, pilot, main)

        # Point the sidebar at the sub-agent window and draw ITS chat region.
        main._select_window(SUBAGENT_WINDOW)
        await _wait_for(pilot, lambda: main._calibrating is AgentSlot.SUBAGENT, "sub tab selected")
        await _draw_chat_region(app, pilot, monkeypatch)
        clicks.clear()

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: _said(notes, CLICKED_TOAST), "the sub chat was opened")
        assert clicks == [CLICK_TARGET]
        # The master's session is untouched: still armed, still not re-prompting.
        assert main.session_active and not main.awaiting_new_session
        assert main._live is AgentSlot.MASTER  # the button never retargets, either

        # Selecting the master tab while the press is still finishing (its focus
        # beat is an await) must not hand the master a chat opened in the
        # sub-agent's window - the slot is read before the click, not after it.
        main._select_window(MASTER_WINDOW)
        await pilot.pause(0.4)
        assert main.session_active and not main.awaiting_new_session

        # ...and /new still opens the MASTER's chat while the sidebar points
        # somewhere else: the command is typed into the master's session, so the
        # tab the user last clicked on cannot redirect it (nor the reset).
        main._select_window(SUBAGENT_WINDOW)
        await _wait_for(pilot, lambda: main._calibrating is AgentSlot.SUBAGENT, "sub tab selected")
        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._live is AgentSlot.MASTER
