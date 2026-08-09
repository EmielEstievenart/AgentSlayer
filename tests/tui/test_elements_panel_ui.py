"""Pilot tests for the ELEMENTS column (tui.md 1.7 / 3.4e).

The column shows the PIXELS the detectors recognised: whenever a template
search verifies a match, the matched rectangle is cut out of that frame and
drawn here, beside how well it matched. What is worth testing is not the
pixels - those are pinned in test_pixels.py, without a terminal - but the
wiring and the ownership rules:

* a tick's crops reach the rows they belong to, and a row nobody searched this
  tick is left alone rather than blanked,
* crops from a poller run that is no longer live do NOT land (the generation
  stamp, exactly as for the four probes),
* a detector rebuild clears the column, because its heading may have just been
  repointed at the other browser window,
* nothing driven by the SELECTED tab repaints it, because the crops are of the
  LIVE window and the two part company for the whole of a delegation,
* F7 hides and shows the whole column, as F3 does its neighbour,
* and the copy button - which is not a per-tick detector - gets its one crop
  from the auto-copy flow's own search.

``capture_region`` is monkeypatched at its use site (``main_mod``) throughout -
no test here goes near the real screen, every RegionImage is hand-built, and
the autouse OS gate in tests/conftest.py fails loudly if a picker escapes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

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
from agentclip.screen.slot import AgentSlot
from agentclip.screen.template import RegionMatch
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ElementCrop, ElementsMatched
from agentclip.tui.pixels import HALF_BLOCK, thumbnail
from agentclip.tui.screens.main import MASTER_WINDOW, SUBAGENT_WINDOW, MainScreen
from agentclip.tui.widgets.elements import (
    ELEMENT_CROP_COLS,
    ELEMENT_CROP_ROWS,
    ELEMENT_MISSING,
    ELEMENT_RESTING,
    ELEMENTS_TITLE,
    element_crop_id,
    element_label_id,
)

from .conftest import template_image

REGION = ScreenRegion(1000, 200, 600, 500)
COPY_ICON = (24, 24)
MATCH = RegionMatch(x=120, y=300, diff=0.03)
SIZE = (110, 100)


def icon(width: int = 24, height: int = 24) -> RegionImage:
    """A little appearance with structure in it, so its crop is not one colour."""
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 9) % 256, (y * 11) % 256, (x * y) % 256, 0))
    return RegionImage(width, height, bytes(pixels))


ICON = icon()


def _frame(region: ScreenRegion) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


def crop_of(image: RegionImage = ICON) -> ElementCrop:
    """One recognised element as the poll worker would hand it over: already
    sized to the panel's budget, with the diff that verified it."""
    small = thumbnail(image, ELEMENT_CROP_COLS, ELEMENT_CROP_ROWS)
    assert small is not None
    return ElementCrop(small, 0.012)


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


def _label(app: AgentClipApp, kind: TemplateKind) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(f"#{element_label_id(kind)}", Static).render())


def _picture(app: AgentClipApp, kind: TemplateKind) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(f"#{element_crop_id(kind)}", Static).render())


def _drawn(app: AgentClipApp, kind: TemplateKind) -> bool:
    return HALF_BLOCK in _picture(app, kind)


def _title(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#elements-title", Static).render())


class _Picker:
    def __init__(self, region: ScreenRegion | None) -> None:
        self.region = region

    def __call__(self, prompt: str = "") -> ScreenRegion | None:
        return self.region


def _patch_picker(monkeypatch: pytest.MonkeyPatch, *, poll_s: float = 0.02) -> None:
    monkeypatch.setattr(main_mod, "pick_region", _Picker(REGION))
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", poll_s)


class _FrozenWorker:
    """A poll worker that never polls - see test_stale_detector_ui for why it
    has to be present rather than None."""

    def __init__(self) -> None:
        self.is_cancelled = False

    def cancel(self) -> None:
        self.is_cancelled = True


def _freeze_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the poll THREAD, keep the composition - so the tests below can post
    crops themselves without a live loop painting over them."""

    def fake_spawn(self: MainScreen, loop: object) -> None:
        self._detector_worker = cast(Any, _FrozenWorker())

    monkeypatch.setattr(MainScreen, "_spawn_detector_worker", fake_spawn)


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


def _post(app: AgentClipApp, crops: dict[TemplateKind, ElementCrop | None], *, ahead: int = 0) -> None:
    """Post one tick's recognitions as the poll worker would - the documented
    injection point, exactly like posting a BusyProbed."""
    assert app.main_screen is not None
    main = app.main_screen
    main.post_message(ElementsMatched(crops, main._detector_generation + ahead))


async def _polling(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """A screen with a drawn chat window, so a poller run exists to speak for."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed")
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._detector_worker is not None, "poller built")
    return main


# -- what the column shows ----------------------------------------------------


async def test_every_row_rests_on_a_line_saying_what_it_is(
    tmp_path: Path, profile_root: Path
) -> None:
    """Four blank picture-shaped holes are not a readout: before anything has
    matched, each row still names its element and says nothing has."""
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        # Named from the first paint: the detector machinery runs on mount even
        # with no region drawn, and an unlabelled column is read as the selected
        # tab's the moment a delegation makes the two differ.
        assert _title(app).startswith(ELEMENTS_TITLE)
        assert "MASTER" in _title(app)
        for kind in (
            TemplateKind.SEND_READY,
            TemplateKind.BUSY,
            TemplateKind.IDLE,
            TemplateKind.COPY,
        ):
            assert ELEMENT_RESTING in _label(app, kind)
            assert _picture(app, kind) == ""


async def test_a_posted_crop_is_drawn_with_its_diff(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One crop in, one block of half-block cells out - inside the rows the
    column reserved for it, and labelled with the number that verified it."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)

        _post(app, {TemplateKind.BUSY: crop_of()})
        await pilot.pause()

        drawn = _picture(app, TemplateKind.BUSY)
        lines = drawn.splitlines()
        assert HALF_BLOCK in drawn
        assert 0 < len(lines) <= ELEMENT_CROP_ROWS
        assert all(len(line) <= ELEMENT_CROP_COLS for line in lines)
        assert "1.2%" in _label(app, TemplateKind.BUSY)


async def test_a_searched_and_missing_element_says_so(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"We looked and it is not there" is a different answer from "nothing has
    been looked for yet" - and it is the one that explains a send gate that
    will not release."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)

        _post(app, {TemplateKind.IDLE: crop_of()})
        await pilot.pause()
        assert _drawn(app, TemplateKind.IDLE)

        _post(app, {TemplateKind.IDLE: None})
        await pilot.pause()
        assert ELEMENT_MISSING in _label(app, TemplateKind.IDLE)
        assert _picture(app, TemplateKind.IDLE) == ""


async def test_a_kind_the_tick_never_searched_is_left_alone(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The send button is only looked for while the gate holds and the copy
    button is not a per-tick detector at all, so most ticks say nothing about
    them. A tick that never looked must not blank a row."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)

        _post(app, {TemplateKind.SEND_READY: crop_of()})
        await pilot.pause()
        painted = _picture(app, TemplateKind.SEND_READY)
        assert HALF_BLOCK in painted

        # A later tick outside the gate carries busy only.
        _post(app, {TemplateKind.BUSY: None})
        await pilot.pause()
        assert _picture(app, TemplateKind.SEND_READY) == painted
        assert ELEMENT_MISSING not in _label(app, TemplateKind.SEND_READY)


async def test_a_ghost_generations_crops_are_dropped(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled run finishes the tick it was interrupted in. Its crops are
    pictures from a window that may no longer be the live one, and the heading
    above them has already been repointed - so they must not land."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)

        _post(app, {TemplateKind.BUSY: crop_of()}, ahead=-1)
        await pilot.pause()

        assert _picture(app, TemplateKind.BUSY) == ""
        assert ELEMENT_RESTING in _label(app, TemplateKind.BUSY)


async def test_rebuilding_the_detectors_clears_every_crop(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heading names the live window; a rebuild may have repointed it. An
    old window's send button under a new window's name is a lie, so the
    pictures go with the verdicts."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _polling(app, pilot)
        _post(app, {TemplateKind.BUSY: crop_of(), TemplateKind.IDLE: crop_of()})
        await pilot.pause()
        assert _drawn(app, TemplateKind.BUSY)

        main._start_detector_worker()
        await pilot.pause()

        for kind in (TemplateKind.BUSY, TemplateKind.IDLE):
            assert _picture(app, kind) == ""
            assert ELEMENT_RESTING in _label(app, kind)


async def test_the_heading_names_the_live_window(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-delegation the crops are the sub-agent's while the user may be
    reading the master's transcript, so the column has to say whose they are."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _polling(app, pilot)
        assert "MASTER" in _title(app)

        main._live = AgentSlot.SUBAGENT
        main._start_detector_worker()
        await pilot.pause()
        assert "SUB-AGENT" in _title(app)


async def test_selecting_a_window_tab_does_not_touch_the_crops(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tui.md 3.4e: the column describes the LIVE window, and reading the other
    tab's transcript mid-delegation must not clobber it. The pictures obey the
    same rule as the sidebar's five lines."""
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = await _polling(app, pilot)
        _post(app, {TemplateKind.BUSY: crop_of()})
        await pilot.pause()
        painted = _picture(app, TemplateKind.BUSY)
        title = _title(app)
        assert HALF_BLOCK in painted

        main._select_window(SUBAGENT_WINDOW)
        await _wait_for(pilot, lambda: main._calibrating is AgentSlot.SUBAGENT, "tab moved")
        await pilot.pause()
        assert _picture(app, TemplateKind.BUSY) == painted
        assert _title(app) == title

        main._select_window(MASTER_WINDOW)
        await _wait_for(pilot, lambda: main._calibrating is AgentSlot.MASTER, "tab back")
        await pilot.pause()
        assert _picture(app, TemplateKind.BUSY) == painted


# -- the column itself --------------------------------------------------------


async def test_f7_hides_and_shows_the_column(tmp_path: Path, profile_root: Path) -> None:
    """Its own F3: what a user reclaims is horizontal room for a diff, so the
    whole column goes - and comes back with what it was showing."""
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed")
        panel = main.elements_panel
        assert panel.display is True

        await pilot.press("f7")
        await pilot.pause()
        assert panel.display is False

        await pilot.press("f7")
        await pilot.pause()
        assert panel.display is True


async def test_f7_works_while_the_composer_has_focus(
    tmp_path: Path, profile_root: Path
) -> None:
    """The composer is a TextArea and eats plain keys; the binding is priority
    for exactly the reason F3's is."""
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed")
        main.composer.focus()
        await pilot.pause()

        await pilot.press("f7")
        await pilot.pause()
        assert main.elements_panel.display is False


async def test_the_sidebar_keeps_its_own_column(tmp_path: Path, profile_root: Path) -> None:
    """Two columns, two keys: hiding one must not take the other with it."""
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed")

        await pilot.press("f7")
        await pilot.pause()
        assert main.sidebar.display is True

        await pilot.press("f3")
        await pilot.pause()
        assert main.sidebar.display is False
        assert main.elements_panel.display is False


# -- what the poll worker does ------------------------------------------------


def _with_signals(main: MainScreen, *signals: str) -> None:
    """Rewrite the active service's finish-signal checklist and adopt it - the
    documented path that restarts the poller against the new composition."""
    key = main._selected_service()
    services = dict(main._config.services)
    services[key] = replace(services[key], finish_signals=signals)
    main.update_config(replace(main._config, services=services))


async def test_the_running_poller_draws_what_it_recognised(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end, with the real thread worker and the real template search: a
    busy indicator actually present in the captured frame reaches the column as
    a picture."""
    app = _make_app(tmp_path, profile_root)
    service = sorted(app.app_config.services)[0]
    if app.app_config.general.service in app.app_config.services:
        service = app.app_config.general.service
    seed_templates(service, TemplateKind.BUSY, size=(24, 24))

    # The scene the poller "captures": the region, with the seeded appearance
    # stamped into it at a known place. Hand-built - nothing here touches a
    # real screen.
    scene = bytearray(_frame(REGION).pixels)
    patch = template_image(24, 24)
    for row in range(24):
        start = ((100 + row) * REGION.width + 200) * 4
        scene[start : start + 24 * 4] = patch.pixels[row * 24 * 4 : (row + 1) * 24 * 4]
    frame = RegionImage(REGION.width, REGION.height, bytes(scene))

    monkeypatch.setattr(main_mod, "pick_region", _Picker(REGION))
    monkeypatch.setattr(main_mod, "capture_region", lambda region: frame)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)

    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed")
        _with_signals(main, "busy")
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")

        await _wait_for(
            pilot, lambda: _drawn(app, TemplateKind.BUSY), "the busy crop reaches the column"
        )
        assert "found" in _label(app, TemplateKind.BUSY)


async def test_the_auto_copy_flow_posts_the_copy_buttons_crop(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy button is searched for exactly once per response, by the flow
    rather than the poller, so the flow posts its own crop."""
    app = _make_app(tmp_path, profile_root)
    service = sorted(app.app_config.services)[0]
    if app.app_config.general.service in app.app_config.services:
        service = app.app_config.general.service
    seed_templates(service, TemplateKind.COPY, size=COPY_ICON)

    monkeypatch.setattr(main_mod, "pick_region", _Picker(REGION))
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "click_region", lambda region, settle_s=0.0: True)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (MATCH, None))
    _freeze_detector(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _polling(app, pilot)
        assert _picture(app, TemplateKind.COPY) == ""

        await main._auto_copy_flow()
        await pilot.pause()

        assert _drawn(app, TemplateKind.COPY)
        assert "3.0%" in _label(app, TemplateKind.COPY)


async def test_a_copy_button_that_is_not_there_clears_its_row(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row that says why the harvest failed: the flow looked and found
    nothing, which is the same answer the poller's rows give."""
    app = _make_app(tmp_path, profile_root)
    service = sorted(app.app_config.services)[0]
    if app.app_config.general.service in app.app_config.services:
        service = app.app_config.general.service
    seed_templates(service, TemplateKind.COPY, size=COPY_ICON)

    monkeypatch.setattr(main_mod, "pick_region", _Picker(REGION))
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "click_region", lambda region, settle_s=0.0: True)
    monkeypatch.setattr(
        main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (None, 0.21)
    )
    _freeze_detector(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _polling(app, pilot)

        await main._auto_copy_flow()
        await pilot.pause()

        assert ELEMENT_MISSING in _label(app, TemplateKind.COPY)
        assert _picture(app, TemplateKind.COPY) == ""
