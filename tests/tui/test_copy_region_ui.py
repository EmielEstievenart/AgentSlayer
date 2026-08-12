"""Pilot tests for the copy button: an appearance captured per SERVICE, found
lowest-first inside the drawn chat region.

Nothing remembers where the copy icon is. The service is told what it looks
like once (in the service editor, F2), and when the finish detectors agree a
response is done the flow captures the live chat region and takes the
BOTTOM-most match in it - the icon appears once per response down the
transcript, so lowest is newest. There is no vertical search band any more, and
therefore no way for the search to fail because a band stopped fitting a
template.

The appearance itself is seeded straight into the profile store
(``seed_templates``) rather than captured through the UI: these tests are about
everything DOWNSTREAM of the capture, and the capture flow is the service
editor's subject, covered once in test_profile_capture_ui.py. Seeding writes
the same PNGs a real capture leaves behind, so the app simply loads them.

Capture, matcher and focus calls are monkeypatched at their use site
(agentclip.tui.screens.main). ``BusyProbed`` is the documented injectable path
for the poller (tui/messages.py); posting it is equivalent to a poll
completing, so these tests drive the arm/fire trigger without the real thread.

The terminal has to be tall enough for the sidebar's buttons to be on screen -
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
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import RegionMatch
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import BusyProbed
from agentclip.tui.screens.main import MainScreen

from .conftest import send_composer

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


def _service_key(app: AgentClipApp) -> str:
    """The service the sidebar starts on - the one every appearance is filed
    under (mirrors ``Sidebar._default_service``)."""
    config = app.app_config
    if config.general.service in config.services:
        return config.general.service
    return sorted(config.services)[0]


def _app_with_copy(
    tmp_path: Path, profile_root: Path, seed: Callable[..., None]
) -> tuple[AgentClipApp, FakeClipboard]:
    """An app whose service already knows what its copy button looks like.

    The state a capture leaves behind - a real PNG in the real store, under the
    selected service - so the app picks it up off disk at startup exactly as it
    would on the run after the user calibrated it.
    """
    app, fake = _make_app(tmp_path, profile_root)
    seed(_service_key(app), TemplateKind.COPY, size=(COPY_ICON.width, COPY_ICON.height))
    return app, fake


def _copy_label(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-tpl-copy", Static).render())


def _profile_note(app: AgentClipApp) -> str:
    """The sidebar's read-only "appearance: n/7 captured" summary."""
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-profile-note", Static).render())


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """Send a composer line - see ``send_composer`` for why /new takes two Enters."""
    await send_composer(app, pilot, text)


async def _post_probe(main: MainScreen, pilot: Pilot, state: BusyState, diff: float | None) -> None:
    """Inject one busy-poller verdict - the documented path (tui/messages.py).

    A MATCH here is a frame that genuinely found the busy appearance
    (``BusyProbe.generating_now``), which is what arms the trigger; the settling
    default that shares the state carries no evidence and is
    test_finish_signal_ui.py's subject.
    """
    main.post_message(
        BusyProbed(BusyProbe(state, diff, state is BusyState.MATCH), main._detector_generation)
    )
    await pilot.pause()


def _patch_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "capture_region", _frame)


def _freeze_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the poller out and let each test declare what it would be posting.

    The real poller would watch the drawn chat region and interleave its own
    stale verdicts with the busy sequence these tests inject, which is a race,
    not a test. ``_active_detectors`` is the seam that says which message
    closes a tick, so setting it directly is the whole substitution.
    """
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """Wait for the composer, with the copy button's appearance already loaded."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    assert main._active_profile().has(TemplateKind.COPY)
    main._active_detectors = ("busy",)
    main._open_reply_gate()  # see _armed
    return main


async def _armed(app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> MainScreen:
    """Everything the flow needs: the drawn chat window and the captured icon.

    Plus the session gate open. Calibration alone never fires the flow - a
    payload has to be sitting in the chat waiting for a reply first - and
    ``_open_reply_gate`` is the state ``copy_outbound`` leaves behind
    (test_finish_signal_ui owns the gate's own rules).
    """
    _freeze_detector(monkeypatch)
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
    assert main._active_profile().has(TemplateKind.COPY)
    main._active_detectors = ("busy",)
    main._open_reply_gate()
    return main


# -- the MATCH-then-two-CHANGED trigger ----------------------------------------


async def test_match_then_two_changed_fires_once_and_rearms(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capture(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _app_with_copy(tmp_path, profile_root, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)

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

        # A fresh MATCH re-arms it; two more CHANGED fire again. The firing
        # above harvested the reply and shut the session gate with it, so the
        # next turn's outbound copy has to re-open it first.
        main._open_reply_gate()
        await _post_probe(main, pilot, BusyState.MATCH, 0.01)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.3)
        await _post_probe(main, pilot, BusyState.CHANGED, 0.31)
        await _wait_for(pilot, lambda: len(calls) == 2, "flow fired again after re-arm")


async def test_error_probe_resets_streak_but_not_armed(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capture(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _app_with_copy(tmp_path, profile_root, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)

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
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capture(monkeypatch)
    calls: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        calls.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _app_with_copy(tmp_path, profile_root, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)

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
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clicks: list[ScreenRegion] = []
    scrolls: list[tuple[ScreenRegion, int]] = []
    searched: list[ScreenRegion] = []

    app, fake = _app_with_copy(tmp_path, profile_root, seed_templates)

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
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (MATCH, None))
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)

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

        # The chat region is what gets scrolled AND what gets searched. One
        # scroll, because this round finds the icon: the flow's extra snap
        # rounds are a retry budget for a miss, not a schedule
        # (test_scroll_action_ui owns that rule).
        assert scrolls == [(CHAT_REGION, main_mod._SNAP_WHEEL_DETENTS)]
        assert CHAT_REGION in searched
        assert len(clicks) == 2  # the focus poke, then the verified copy click
        assert clicks[0] == CHAT_REGION
        assert clicks[-1] == CLICK_TARGET


async def test_the_lowest_match_across_every_captured_image_wins(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kind holds a stack, so "lowest is newest" is asked across the whole
    stack - and the rectangle clicked is the size of the image that matched,
    not of whichever one happens to be first."""
    clicks: list[ScreenRegion] = []
    app, fake = _make_app(tmp_path, profile_root)
    key = _service_key(app)
    seed_templates(key, TemplateKind.COPY, size=(24, 24))
    seed_templates(key, TemplateKind.COPY, size=(30, 18))
    higher = RegionMatch(x=10, y=100, diff=0.05)
    lower = RegionMatch(x=40, y=400, diff=0.07)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks.append(region)
        fake.write_text(f"copied {len(clicks)}")
        return True

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)
    monkeypatch.setattr(
        main_mod,
        "find_lowest_with_best_miss",
        lambda template, scene, **kw: (higher if template.width == 24 else lower, None),
    )

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)
        await _fire(main, pilot)
        await _wait_for(
            pilot, lambda: "clicked (diff 0.07)" in _copy_label(app), "copy button clicked"
        )
        assert clicks[-1] == ScreenRegion(
            CHAT_REGION.left + lower.x, CHAT_REGION.top + lower.y, 30, 18
        )
        # ...and the readout says how many pictures of it are being searched for.
        assert "24×24 +1 · " in _copy_label(app)


async def test_no_chat_region_means_the_flow_does_nothing(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat region is where the icon is looked for, so without one there is
    nowhere to look - and nothing is clicked or scrolled."""
    _patch_capture(monkeypatch)
    clicks: list[ScreenRegion] = []
    scrolls: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: scrolls.append(region) or True)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (MATCH, None))

    app, _ = _app_with_copy(tmp_path, profile_root, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        assert main._chat_region is None

        await _fire(main, pilot)
        await pilot.pause(0.3)
        assert clicks == []
        assert scrolls == []


async def test_not_found_notifies_and_does_not_click(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: clicks.append(region) or True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(
        main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (None, 0.21)
    )
    monkeypatch.setattr(main_mod, "move_cursor", lambda x, y: False)  # no hover scan either
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)

    app, _ = _app_with_copy(tmp_path, profile_root, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "not found" in _copy_label(app), "not-found reported")
        assert clicks == [CHAT_REGION]  # only the focus poke, never the icon


async def test_a_failed_capture_of_the_chat_region_is_reported(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)

    app, _ = _app_with_copy(tmp_path, profile_root, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)

        def boom(region: ScreenRegion) -> RegionImage:
            raise CaptureError("no display")

        monkeypatch.setattr(main_mod, "capture_region", boom)

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "capture failed" in _copy_label(app), "capture failure shown")


async def test_flow_snaps_focus_back_to_the_tool(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After clicking the browser's copy button the flow hands focus back to
    the window recorded at mount - click first, snap-back strictly after."""
    events: list[str] = []
    monkeypatch.setattr(main_mod, "foreground_window", lambda: 4242)

    app, fake = _app_with_copy(tmp_path, profile_root, seed_templates)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        events.append("click")
        # Verification needs each click to actually change the clipboard.
        fake.write_text(f"copied {len(events)}")
        return True

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (MATCH, None))
    focus_calls: list[int] = []

    def fake_focus(handle: int) -> bool:
        focus_calls.append(handle)
        events.append("focus")
        return True

    monkeypatch.setattr(main_mod, "focus_window_verified", fake_focus)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot, monkeypatch)
        assert main._own_window == 4242  # recorded at mount

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: focus_calls == [4242], "focus snapped back")
        assert events[-2:] == ["click", "focus"]


# -- the verified, retried copy click --------------------------------------------


async def test_verified_click_retries_at_an_offset_on_no_change(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sometimes the click lands on the right spot but nothing gets copied -
    the flow must retry at a slightly offset point still inside the icon
    before giving up."""
    clicks: list[tuple[ScreenRegion, float]] = []

    app, fake = _app_with_copy(tmp_path, profile_root, seed_templates)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks.append((region, settle_s))
        if len(clicks) == 3:  # the SECOND copy attempt is the one that "takes"
            fake.write_text("copied!")
        return True

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (MATCH, None))
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)

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
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the clipboard never changes across all three attempts, the flow
    reports the failure and deliberately does NOT snap focus back - the
    browser stays focused so the user can click the copy button themselves."""
    clicks: list[tuple[ScreenRegion, float]] = []
    focus_calls: list[int] = []

    app, _fake = _app_with_copy(tmp_path, profile_root, seed_templates)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks.append((region, settle_s))
        return True  # the clipboard never actually changes

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (MATCH, None))
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: focus_calls.append(handle) or True)

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
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    new_chat_click_lands: None,
) -> None:
    """The copy button's appearance describes the service, so it survives /new
    (and the app itself) - but the arm/streak belong to the dead session's
    verdicts, so they reset with it."""
    _patch_capture(monkeypatch)
    app, _ = _app_with_copy(tmp_path, profile_root, seed_templates)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
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
        assert main._awaiting_pasted_reply is False  # nor is any reply still due
        # The sidebar's summary is where a surviving capture shows now: the
        # copy line is a live click verdict, not a capture readout.
        assert "1/7 captured" in _profile_note(app)
