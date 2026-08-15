"""Pilot tests for the hover-scan fallback in the auto-copy flow.

Claude's chat only renders a response's copy button while the pointer is over
that response, so the cheap static captures of the chat region find nothing.
The flow then walks the real cursor up the chat region, re-capturing after each
stop, and stops at the FIRST frame the icon appears in - but only for a service
whose preset asked for it (``ServicePreset.hover_scan``, off by default, since
the scan takes the user's mouse over and most chats do not need it).

The scan is the LAST resort and runs once, after all ``_COPY_SNAP_ROUNDS`` of
the static snap-and-search have missed: walking someone's mouse across their
screen three times over would be three times the intrusion for one answer. So
the stubbed searches below count those rounds off before the scan begins.

Everything that touches the OS - picker, capture, cursor move, click, focus -
is monkeypatched at its use site (agentclip.tui.screens.main), including the
per-step settle so the tests do not sleep their way up a region.
``AutomationController.feed_probe`` is the documented injectable path for the
poller, used here to fire the flow.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.hover import hover_scan_points
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.template import RegionMatch, Template
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
COPY_ICON = ScreenRegion(1830, 612, 24, 24)
# Where the flow parks the pointer before it snaps the transcript, on every run
# and whatever the scroll action is - chat pages that scroll only the pane under
# the cursor need it there (test_scroll_action_ui owns that rule). It is a
# ``move_cursor`` like the scan's own stops, so it is the first thing every
# ``moves`` list here records, and ``_scan`` below is what tells them apart.
PARK = CHAT_REGION.center

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


def _make_app(
    tmp_path: Path, profile_root: Path, *, hover_scan: bool = True
) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    # The hover scan is opt-in per service (``ServicePreset.hover_scan``, off by
    # default - it drives the user's real mouse), and this whole suite is about
    # what a service that DID opt in gets, so every preset here has it on. The
    # off case lives in test_hover_scan_is_opt_in_per_service below.
    config = replace(
        config,
        services={key: replace(p, hover_scan=hover_scan) for key, p in config.services.items()},
    )
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
    return str(app.main_screen.query_one("#side-tpl-copy", Static).render())


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


def _frame(region: ScreenRegion) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


@pytest.fixture(autouse=True)
def _no_detector_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the poller out: these tests inject the finish sequence themselves,
    and a live one would interleave its own stale verdicts with it (as well as
    reflowing the sidebar under the pointer between a click's mouse-down and
    mouse-up). ``_active_detectors`` is what stands in for it - see ``_fire``."""
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)


async def _fire(main: MainScreen, pilot: Pilot) -> None:
    """MATCH then two CHANGED: the busy detector's finish sequence.

    ``_active_detectors`` says which detectors the poller would be posting -
    verdicts from any other are dropped as leftovers of a cancelled loop - so
    declaring the busy tracker live is what makes these injected probes count.
    The session gate is opened the way ``copy_outbound`` opens it, because a
    verdict may only reach for the mouse while a reply is outstanding
    (test_finish_signal_ui owns that rule).
    """
    main._active_detectors = ("busy",)
    main._open_reply_gate()
    # The MATCH is a frame that really found the busy appearance - the third
    # field - because only a sighting arms the trigger (test_finish_signal_ui).
    for state in (BusyState.MATCH, BusyState.CHANGED, BusyState.CHANGED):
        main._automation.feed_probe("busy", BusyProbe(state, 0.2, state is BusyState.MATCH))
        await pilot.pause()


async def _calibrate(
    app: AgentClipApp, pilot: Pilot, monkeypatch: pytest.MonkeyPatch, seed: Callable[..., None]
) -> MainScreen:
    """Draw the chat region (where the hunt happens) and give the service a
    copy-icon appearance to hunt for.

    The appearance is written straight into the profile store - the same files
    the service editor's capture leaves behind - and the cache dropped through
    ``update_config``, which is what an editor visit does to this screen. Its
    size is what matters here: the flow translates a match back to a screen
    region using the template's own width and height.
    """
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

    seed(main._selected_service(), TemplateKind.COPY, size=(COPY_ICON.width, COPY_ICON.height))
    main._profiles.clear()
    main.update_config(main._config)
    await _wait_for(
        pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy icon appearance known"
    )

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: CHAT_REGION)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._chat_region == CHAT_REGION, "chat region adopted")
    return main


def _patch_flow_io(
    monkeypatch: pytest.MonkeyPatch, fake: FakeClipboard
) -> tuple[list[ScreenRegion], list[tuple[int, int]], list[ScreenRegion]]:
    """Record clicks, cursor moves and captured regions; make clicks "take"."""
    clicks: list[ScreenRegion] = []
    moves: list[tuple[int, int]] = []
    captures: list[ScreenRegion] = []

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks.append(region)
        # A real click lands a copy; the verification step needs the change.
        fake.write_text(f"copied {len(clicks)}")
        return True

    def fake_capture(region: ScreenRegion) -> RegionImage:
        captures.append(region)
        return _frame(region)

    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "capture_region", fake_capture)
    monkeypatch.setattr(main_mod, "move_cursor", lambda x, y: bool(moves.append((x, y))) or True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)
    monkeypatch.setattr(main_mod, "_HOVER_STEP_DELAY_S", 0.0)
    return clicks, moves, captures


def _scan(moves: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The stops the HOVER SCAN made: everything after the pre-snap park."""
    assert moves[:1] == [PARK], f"the flow did not park the pointer first: {moves[:1]}"
    return moves[1:]


async def test_hover_scan_stops_at_the_first_appearance_and_clicks(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The icon only renders once the pointer has climbed three stops - the
    scan must stop right there, not carry on to the top of the region."""
    match = RegionMatch(x=700, y=180, diff=0.04)
    app, fake = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrate(app, pilot, monkeypatch, seed_templates)
        clicks, moves, _captures = _patch_flow_io(monkeypatch, fake)

        looks = {"n": 0}

        static = main_mod._COPY_SNAP_ROUNDS

        def fake_find(
            template: Template, scene: RegionImage, **kw: object
        ) -> tuple[RegionMatch | None, float | None]:
            # The first ``static`` looks are the cheap snap-and-search rounds -
            # the flow re-scrolls and re-searches before it will touch the
            # mouse - and then one look per hover stop. The icon appears on the
            # third stop.
            looks["n"] += 1
            return (match, None) if looks["n"] > static + 2 else (None, 0.21)

        monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", fake_find)

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "clicked (diff 0.04)" in _copy_label(app), "copy clicked")

        assert _scan(moves) == hover_scan_points(CHAT_REGION)[:3], (
            "scanned exactly up to the first appearance"
        )
        expected = ScreenRegion(
            CHAT_REGION.left + match.x, CHAT_REGION.top + match.y, COPY_ICON.width, COPY_ICON.height
        )
        assert clicks[-1] == expected  # region-local offset translated back to the screen


async def test_a_static_hit_never_starts_a_scan(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap path stays cheap: chats that render the icon unconditionally
    must not pay for a hover scan. The one move they do pay for is the park in
    front of the snap, which every run makes before anything is searched for."""
    app, fake = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrate(app, pilot, monkeypatch, seed_templates)
        clicks, moves, _captures = _patch_flow_io(monkeypatch, fake)
        monkeypatch.setattr(
            main_mod,
            "find_lowest_with_best_miss",
            lambda template, scene, **kw: (RegionMatch(x=7, y=7, diff=0.01), None),
        )

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "clicked (diff 0.01)" in _copy_label(app), "copy clicked")
        assert _scan(moves) == []
        assert clicks[-1].top == CHAT_REGION.top + 7


async def test_an_exhausted_scan_reports_not_found_and_never_clicks_the_icon(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same outcome as the plain not-found path - a toast and a sidebar note -
    after the whole region has been hovered."""
    app, fake = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrate(app, pilot, monkeypatch, seed_templates)
        clicks, moves, _captures = _patch_flow_io(monkeypatch, fake)
        monkeypatch.setattr(
            main_mod, "find_lowest_with_best_miss", lambda template, scene, **kw: (None, 0.21)
        )

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "not found" in _copy_label(app), "not-found reported")

        assert _scan(moves) == hover_scan_points(CHAT_REGION)  # the whole region, bottom to top
        # The only click was the focus poke at the chat region, never the icon.
        assert clicks == [CHAT_REGION]


async def test_a_refused_cursor_move_ends_the_scan_immediately(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off Windows every move is refused - the scan must bail out rather than
    sleep its way up a region it can never influence. The park in front of the
    snap is refused by the same stub and is the opposite case: nothing reads its
    answer, so the flow gets all the way here regardless."""
    app, fake = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrate(app, pilot, monkeypatch, seed_templates)
        _clicks, moves, _captures = _patch_flow_io(monkeypatch, fake)
        monkeypatch.setattr(
            main_mod, "find_lowest_with_best_miss", lambda template, scene, **kw: (None, 0.21)
        )
        monkeypatch.setattr(main_mod, "move_cursor", lambda x, y: bool(moves.append((x, y))))

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "not found" in _copy_label(app), "not-found reported")
        assert len(_scan(moves)) == 1


async def test_hover_scan_is_opt_in_per_service(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``hover_scan`` off - the default - a static miss is simply a miss:
    the same not-found report as an exhausted scan, and the user's cursor is
    never walked up the transcript (the snap's own park aside)."""
    app, fake = _make_app(tmp_path, profile_root, hover_scan=False)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrate(app, pilot, monkeypatch, seed_templates)
        clicks, moves, _captures = _patch_flow_io(monkeypatch, fake)
        monkeypatch.setattr(
            main_mod, "find_lowest_with_best_miss", lambda template, scene, **kw: (None, 0.21)
        )

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "not found" in _copy_label(app), "not-found reported")

        assert _scan(moves) == []  # no stop-by-stop climb, just the park
        assert "hover" not in _copy_label(app)
        assert clicks == [CHAT_REGION]  # only the focus poke, never the icon


async def test_the_scan_runs_off_the_ui_thread(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every stop blocks on a settle pause and a capture, so the scan must
    never execute on the thread painting the TUI."""
    import threading

    ui_thread = threading.get_ident()
    scan_threads: list[int] = []
    app, fake = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _calibrate(app, pilot, monkeypatch, seed_templates)
        _patch_flow_io(monkeypatch, fake)

        looks = {"n": 0}

        def fake_find(
            template: Template, scene: RegionImage, **kw: object
        ) -> tuple[RegionMatch | None, float | None]:
            looks["n"] += 1
            if looks["n"] == 1:
                return None, 0.21  # the static pass, still on the flow's worker
            scan_threads.append(threading.get_ident())
            return RegionMatch(x=12, y=12, diff=0.02), None

        monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", fake_find)

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: "clicked (diff 0.02)" in _copy_label(app), "copy clicked")
        assert scan_threads and all(ident != ui_thread for ident in scan_threads)
