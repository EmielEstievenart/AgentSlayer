"""Pilot tests for the copy button: an appearance captured per SERVICE, found
lowest-first inside the drawn chat region.

Nothing remembers where the copy icon is. The user captures what it looks like
once ("Capture copy button..."), and when the finish detectors agree a response
is done the flow captures the live chat region and takes the BOTTOM-most match
in it - the icon appears once per response down the transcript, so lowest is
newest. There is no vertical search band any more, and therefore no way for the
search to fail because a band stopped fitting a template.

Picker, capture, matcher and focus calls are monkeypatched at their use site
(agentclip.tui.screens.main). ``BusyProbed`` is the documented injectable path
for the poller (tui/messages.py); posting it is equivalent to a poll
completing, so these tests drive the arm/fire trigger without the real thread.

The terminal has to be tall enough for every sidebar button to be on screen -
Pilot refuses to click a widget outside the visible region.
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
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import load_profile
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import RegionMatch
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed
from agentclip.tui.screens.main import MainScreen

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
COPY_ICON = ScreenRegion(1830, 612, 24, 24)
# Where the flow "finds" the icon: chat-region-local, so the click lands at
# CHAT_REGION's origin plus this.
MATCH = RegionMatch(x=120, y=300, diff=0.03)
CLICK_TARGET = ScreenRegion(CHAT_REGION.left + MATCH.x, CHAT_REGION.top + MATCH.y, 24, 24)

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


def _copy_label(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-copy", Static).render())


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


async def _post_probe(main: MainScreen, pilot: Pilot, state: BusyState, diff: float | None) -> None:
    """Inject one busy-poller verdict - the documented path (tui/messages.py)."""
    main.post_message(BusyProbed(BusyProbe(state, diff)))
    await pilot.pause()


def _patch_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: COPY_ICON)
    monkeypatch.setattr(main_mod, "capture_region", _frame)


async def _capture_copy(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """Wait for the composer, then capture the copy button's appearance."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    await _press(app, pilot, "#set-copy-btn")
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy button captured"
    )
    return main


async def _armed(app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> MainScreen:
    """Everything the flow needs: the drawn chat window and the captured icon."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: COPY_ICON)
    await _press(app, pilot, "#set-copy-btn")
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy button captured"
    )
    return main


# -- capture --------------------------------------------------------------------


async def test_capturing_the_copy_button_files_it_under_the_service(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not captured" in _copy_label(app)
        key = main._selected_service()

        main = await _capture_copy(app, pilot)
        template = main._active_profile().get(TemplateKind.COPY)
        assert template is not None
        assert (template.width, template.height) == (24, 24)
        assert "24×24 · captured" in _copy_label(app)
        assert load_profile(profile_root, key).has(TemplateKind.COPY)


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-copy-btn")
        await pilot.pause(0.2)
        assert not main._active_profile().has(TemplateKind.COPY)
        assert "not captured" in _copy_label(app)


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    monkeypatch.setattr(main_mod, "pick_region", boom)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-copy-btn")
        await pilot.pause(0.2)
        assert not main._active_profile().has(TemplateKind.COPY)
        assert "not captured" in _copy_label(app)


async def test_capture_failure_keeps_the_appearance_unknown(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: COPY_ICON)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-copy-btn")
        await pilot.pause(0.2)
        assert not main._active_profile().has(TemplateKind.COPY)
        assert "not captured" in _copy_label(app)


# -- the MATCH-then-two-CHANGED trigger ----------------------------------------


async def test_match_then_two_changed_fires_once_and_rearms(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _capture_copy(app, pilot)

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await pilot.pause(0.1)
        assert calls == []  # only one CHANGED so far - not enough to fire

        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fired once")

        # No refire on further CHANGED while disarmed.
        await _post_probe(main, pilot, BusyState.CHANGED, 0.32)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.33)
        await pilot.pause(0.1)
        assert len(calls) == 1

        # A fresh MATCH re-arms it; two more CHANGED fire again.
        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await _wait_for(pilot, lambda: len(calls) == 2, "flow fired again after re-arm")


async def test_error_probe_resets_streak_but_not_armed(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _capture_copy(app, pilot)

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.ERROR, None)
        await pilot.pause(0.1)
        assert calls == []  # streak was reset by the ERROR
        assert main._copy_armed is True  # but the arm survives it

        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await _wait_for(pilot, lambda: len(calls) == 1, "flow fires once the streak rebuilds")


async def test_no_fire_without_a_captured_copy_button(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert not main._active_profile().has(TemplateKind.COPY)

        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await pilot.pause(0.1)
        assert calls == []


async def test_no_fire_without_prior_match(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_picker(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _capture_copy(app, pilot)

        # CHANGED from the very start - never armed by a MATCH.
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await pilot.pause(0.1)
        assert calls == []


# -- the flow itself ------------------------------------------------------------


async def _fire(main: MainScreen, pilot: Pilot) -> None:
    for state in (BusyState.MATCH, BusyState.CHANGED, BusyState.CHANGED):
        await _post_probe(main, pilot, state, 0.2)


async def test_flow_searches_the_chat_region_and_clicks_the_lowest_match(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicks: list[ScreenRegion] = []
    scrolls: list[tuple[ScreenRegion, int]] = []
    searched: list[ScreenRegion] = []

    app, fake = _make_app(tmp_path, profile_root)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks.append(region)
        # A real click lands a copy; each one has to change the clipboard or
        # the verification step reads it as "the click did not take".
        fake.write_text(f"copied {len(clicks)}")
        return True

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(
        main_mod, "scroll_region", lambda region, n: scrolls.append((region, n)) or True
    )
    monkeypatch.setattr(main_mod, "find_lowest_in_region", lambda t, s, **kw: MATCH)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)

        def recording_capture(region: ScreenRegion) -> RegionImage:
            searched.append(region)
            return _frame(region)

        monkeypatch.setattr(main_mod, "capture_region", recording_capture)

        await _fire(main, pilot)
        await _wait_for(
            pilot, lambda: "clicked (diff 0.03)" in _copy_label(app), "copy button clicked"
        )

        # The chat region is what gets scrolled AND what gets searched.
        assert scrolls == [(CHAT_REGION, -40)]
        assert CHAT_REGION in searched
        assert len(clicks) == 2  # the focus poke, then the verified copy click
        assert clicks[0] == CHAT_REGION
        assert clicks[-1] == CLICK_TARGET


async def test_no_chat_region_means_the_flow_does_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat region is where the icon is looked for, so without one there is
    nowhere to look - and nothing is clicked or scrolled."""
    _patch_picker(monkeypatch)
    clicks: list[ScreenRegion] = []
    scrolls: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: scrolls.append(region) or True)
    monkeypatch.setattr(main_mod, "find_lowest_in_region", lambda t, s, **kw: MATCH)

    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _capture_copy(app, pilot)
        assert main._chat_region is None

        await _fire(main, pilot)
        await pilot.pause(0.3)
        assert clicks == []
        assert scrolls == []


async def test_not_found_notifies_and_does_not_click(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_in_region", lambda t, s, **kw: None)
    monkeypatch.setattr(main_mod, "move_cursor", lambda x, y: False)  # no hover scan either
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)

    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "not found" in _copy_label(app), "not-found reported")
        assert clicks == [CHAT_REGION]  # only the focus poke, never the icon


async def test_a_failed_capture_of_the_chat_region_is_reported(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)

    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)

        def boom(region: ScreenRegion) -> RegionImage:
            raise CaptureError("no display")

        monkeypatch.setattr(main_mod, "capture_region", boom)

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "capture failed" in _copy_label(app), "capture failure shown")


async def test_flow_snaps_focus_back_to_the_tool(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After clicking the browser's copy button the flow hands focus back to
    the window recorded at mount - click first, snap-back strictly after."""
    events: list[str] = []
    monkeypatch.setattr(main_mod, "foreground_window", lambda: 4242)

    app, fake = _make_app(tmp_path, profile_root)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        events.append("click")
        # Verification needs each click to actually change the clipboard.
        fake.write_text(f"copied {len(events)}")
        return True

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_in_region", lambda t, s, **kw: MATCH)
    focus_calls: list[int] = []

    def fake_focus(handle: int) -> bool:
        focus_calls.append(handle)
        events.append("focus")
        return True

    monkeypatch.setattr(main_mod, "focus_window", fake_focus)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)
        assert main._own_window == 4242  # recorded at mount

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: focus_calls == [4242], "focus snapped back")
        assert events[-2:] == ["click", "focus"]


# -- the verified, retried copy click --------------------------------------------


async def test_verified_click_retries_at_an_offset_on_no_change(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sometimes the click lands on the right spot but nothing gets copied -
    the flow must retry at a slightly offset point still inside the icon
    before giving up."""
    clicks: list[tuple[ScreenRegion, float]] = []

    app, fake = _make_app(tmp_path, profile_root)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks.append((region, settle_s))
        if len(clicks) == 3:  # the SECOND copy attempt is the one that "takes"
            fake.write_text("copied!")
        return True

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_in_region", lambda t, s, **kw: MATCH)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)

        await _fire(main, pilot)
        await _wait_for(
            pilot, lambda: "clicked (diff 0.03)" in _copy_label(app), "success status shown"
        )
        assert len(clicks) == 3  # focus poke + two copy attempts, not a third

        assert clicks[1] == (CLICK_TARGET, 0.05)
        offset = ScreenRegion(
            CLICK_TARGET.left - 3, CLICK_TARGET.top - 3, CLICK_TARGET.width, CLICK_TARGET.height
        )
        assert clicks[2] == (offset, 0.05)


async def test_verified_click_exhausts_retries_and_leaves_focus(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the clipboard never changes across all three attempts, the flow
    reports the failure and deliberately does NOT snap focus back - the
    browser stays focused so the user can click the copy button themselves."""
    clicks: list[tuple[ScreenRegion, float]] = []
    focus_calls: list[int] = []

    app, _fake = _make_app(tmp_path, profile_root)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks.append((region, settle_s))
        return True  # the clipboard never actually changes

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_in_region", lambda t, s, **kw: MATCH)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: focus_calls.append(handle) or True)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)

        await _fire(main, pilot)
        await _wait_for(
            pilot, lambda: "click did not take" in _copy_label(app), "exhausted status shown"
        )

        expected = [
            ScreenRegion(
                CLICK_TARGET.left + dx,
                CLICK_TARGET.top + dy,
                CLICK_TARGET.width,
                CLICK_TARGET.height,
            )
            for dx, dy in [(0, 0), (-3, -3), (3, 3)]
        ]
        assert [region for region, _settle in clicks[1:]] == expected
        assert all(settle == 0.05 for _region, settle in clicks[1:])
        assert focus_calls == []  # no snap-back on failure


# -- session teardown -----------------------------------------------------------


async def test_new_keeps_the_capture_but_disarms_the_trigger(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy button's appearance describes the service, so it survives /new
    (and the app itself) - but the arm/streak belong to the dead session's
    verdicts, so they reset with it."""
    _patch_picker(monkeypatch)
    app, _ = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _capture_copy(app, pilot)
        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        assert main._copy_armed is True

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")
        assert main._active_profile().has(TemplateKind.COPY)
        assert main._copy_armed is False
        assert main._copy_changed_streak == 0
        assert "24×24 · captured" in _copy_label(app)
