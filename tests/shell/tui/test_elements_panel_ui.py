"""Pilot tests for the ELEMENTS column (tui.md 1.7 / 3.4e).

The column shows the PIXELS the detectors recognised: whenever a template
search verifies a match, the matched rectangle is cut out of that frame and
drawn here, beside how well it matched. What is worth testing is not the
pixels - those are pinned in test_pixels.py, without a terminal - but the
wiring and the ownership rules:

* a tick's crops reach the rows they belong to, and a row nobody searched this
  tick is left alone rather than blanked,
* every CALIBRATED appearance reaches the column on every tick - including the
  send button with no gate open, the copy button with no flow running, the chat
  boxes and new-chat button nothing on the timer consumes at all, and a busy or
  idle icon whose finish signal is unticked, which is the whole point of the
  detector being independent of the state machine (screen/detector.py): a
  capture is enough, and what the checklist decides is what may END a response,
  never what the user is allowed to see,
* every TemplateKind has a row, because a column that shows four of the seven
  things the tool can recognise is a picture of the automation rather than of
  what the tool can see,
* crops from a poller run that is no longer live do NOT land (the generation
  stamp, exactly as for the four probes),
* a detector rebuild clears the column, because its heading may have just been
  repointed at the other browser window,
* nothing driven by the SELECTED tab repaints it, because the crops are of the
  LIVE window and the two part company for the whole of a delegation,
* F7 hides and shows the whole column, as F3 does its neighbour,
* and the auto-copy flow still posts its own copy-button crop, from the frame
  it aimed the click at.

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

import agentclip.shell.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import RUNTIME_KINDS
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.template import RegionMatch
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.graphics import (
    NO_SIXEL,
    TerminalGraphics,
    crop_rows,
    set_terminal_graphics,
)
from agentclip.shell.tui.messages import ElementCrop
from agentclip.shell.tui.pixels import HALF_BLOCK, thumbnail
from agentclip.shell.tui.screens.main import MASTER_WINDOW, SUBAGENT_WINDOW, MainScreen
from agentclip.shell.tui.widgets.elements import (
    ELEMENT_CROP_COLS,
    ELEMENT_CROP_ROWS,
    ELEMENT_LABEL,
    ELEMENT_MISSING,
    ELEMENT_ORDER,
    ELEMENT_RESTING,
    ELEMENTS_TITLE,
    element_crop_id,
    element_crop_image,
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
    """Feed one tick's recognitions in as the poll loop would - the documented
    injection point (``AutomationController.feed_probe``)."""
    assert app.main_screen is not None
    main = app.main_screen
    main._automation.feed_probe("elements", crops, main._detector_generation + ahead)


async def _polling(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """A screen with a drawn chat window, so a poller run exists to speak for."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed")
    await _press(app, pilot, "#set-region-btn")
    await _wait_for(pilot, lambda: main._detector_worker is not None, "poller built")
    return main


# -- what the column shows ----------------------------------------------------


def test_the_column_has_a_row_for_every_appearance_the_tool_can_recognise() -> None:
    """"Everything it recognises" is the contract, so the list is the enum.

    Pinned against ``TemplateKind`` rather than against a copy of it, and
    against the detector's own report order, because the two lists are one
    thing: the detector searches in this order and the column draws it, so a
    row can never be a picture of some other row's search.
    """
    assert ELEMENT_ORDER == RUNTIME_KINDS
    assert set(ELEMENT_ORDER) == set(TemplateKind)
    assert all(kind in ELEMENT_LABEL for kind in ELEMENT_ORDER)
    # The column is 20 cells, 16 of them usable once the scrollbar shows.
    assert all(len(label) <= ELEMENT_CROP_COLS for label in ELEMENT_LABEL.values())


async def test_every_row_rests_on_a_line_saying_what_it_is(
    tmp_path: Path, profile_root: Path
) -> None:
    """Seven blank picture-shaped holes are not a readout: before anything has
    matched, each row still names its element and says nothing has."""
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        # Named from the first paint: the detector machinery runs on mount even
        # with no region drawn, and an unlabelled column is read as the selected
        # tab's the moment a delegation makes the two differ.
        assert _title(app).startswith(ELEMENTS_TITLE)
        assert "MASTER" in _title(app)
        for kind in ELEMENT_ORDER:
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
    """A tick that never looked must not blank a row.

    The three-state contract is unchanged by the detector extraction; what
    changed is which kinds land in the third state. A tick now says something
    about every kind the live service is CALIBRATED for - the send button
    outside the gate window included - so the row this protects is the row of an
    appearance nobody has captured, plus the tick a detector rebuild leaves
    mid-flight. The map is still the carrier of "not searched", and posting one
    without a kind in it must still leave that kind alone.
    """
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)

        _post(app, {TemplateKind.SEND_READY: crop_of()})
        await pilot.pause()
        painted = _picture(app, TemplateKind.SEND_READY)
        assert HALF_BLOCK in painted

        # A later tick from a run with no send capture carries busy only.
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


async def test_the_send_and_copy_rows_are_alive_with_nothing_going_on(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the detector was pulled out of the state machine.

    No session, no outbound, no gate holding and no flow running - and the send
    button and the copy button are both recognised in the captured frame and
    drawn. Before this, the send row was only searched inside the gate's window
    and the copy row only by the harvest, so a user whose capture had drifted
    could not find that out until an automation silently failed.
    """
    app = _make_app(tmp_path, profile_root)
    service = sorted(app.app_config.services)[0]
    if app.app_config.general.service in app.app_config.services:
        service = app.app_config.general.service
    seed_templates(service, TemplateKind.SEND_READY, size=(24, 24))
    seed_templates(service, TemplateKind.COPY, size=(28, 28))

    # One frame with both appearances stamped into it, well apart.
    scene = bytearray(_frame(REGION).pixels)
    for image, left, top in (
        (template_image(24, 24), 200, 100),
        (template_image(28, 28), 60, 400),
    ):
        for row in range(image.height):
            start = ((top + row) * REGION.width + left) * 4
            width = image.width * 4
            scene[start : start + width] = image.pixels[row * width : (row + 1) * width]
    frame = RegionImage(REGION.width, REGION.height, bytes(scene))

    monkeypatch.setattr(main_mod, "pick_region", _Picker(REGION))
    monkeypatch.setattr(main_mod, "capture_region", lambda region: frame)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)

    async with app.run_test(size=SIZE) as pilot:
        main = await _polling(app, pilot)
        assert main._send_gate is None  # nothing is waiting for the button
        assert main._awaiting_pasted_reply is False

        await _wait_for(
            pilot,
            lambda: _drawn(app, TemplateKind.SEND_READY) and _drawn(app, TemplateKind.COPY),
            "the send and copy crops reach the column",
        )
        assert "found" in _label(app, TemplateKind.SEND_READY)
        assert "found" in _label(app, TemplateKind.COPY)


async def test_the_chat_box_and_new_chat_rows_are_alive_too(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three the loop never consumes are on the timer like everything else.

    Nothing in the automation reads a chat box or the new-chat button from a
    poll tick - both are re-searched by the click that is about to use them -
    and that is exactly why their rows used to be dead. "Every UI element it
    recognises should be shown": the panel is a readout of what the tool can
    see, not of what the state machine happens to want. The wide, short chat-box
    capture also proves a lopsided appearance survives the crop path instead of
    scaling to an invisible hairline.
    """
    app = _make_app(tmp_path, profile_root)
    service = sorted(app.app_config.services)[0]
    if app.app_config.general.service in app.app_config.services:
        service = app.app_config.general.service
    seed_templates(service, TemplateKind.CHATBOX_ONGOING, size=(120, 6))
    seed_templates(service, TemplateKind.NEW_CHAT, size=(36, 36))

    scene = bytearray(_frame(REGION).pixels)
    for image, left, top in (
        (template_image(120, 6), 40, 420),
        (template_image(36, 36), 300, 40),
    ):
        for row in range(image.height):
            start = ((top + row) * REGION.width + left) * 4
            width = image.width * 4
            scene[start : start + width] = image.pixels[row * width : (row + 1) * width]
    frame = RegionImage(REGION.width, REGION.height, bytes(scene))

    monkeypatch.setattr(main_mod, "pick_region", _Picker(REGION))
    monkeypatch.setattr(main_mod, "capture_region", lambda region: frame)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)

    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)

        await _wait_for(
            pilot,
            lambda: _drawn(app, TemplateKind.CHATBOX_ONGOING)
            and _drawn(app, TemplateKind.NEW_CHAT),
            "the chat-box and new-chat crops reach the column",
        )
        assert "found" in _label(app, TemplateKind.CHATBOX_ONGOING)
        assert "found" in _label(app, TemplateKind.NEW_CHAT)
        # The other layout is calibrated for nothing, so it is never claimed
        # about - the two chat boxes are mutually exclusive on screen anyway.
        assert ELEMENT_RESTING in _label(app, TemplateKind.CHATBOX_INITIAL)


async def test_an_uncaptured_appearance_is_the_row_that_stays_resting(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same rule: what a row resting at "no match yet"
    means is now precise - this service has no capture of that appearance.
    Everything captured is searched for on every tick."""
    app = _make_app(tmp_path, profile_root)
    service = sorted(app.app_config.services)[0]
    if app.app_config.general.service in app.app_config.services:
        service = app.app_config.general.service
    seed_templates(service, TemplateKind.COPY, size=COPY_ICON)

    monkeypatch.setattr(main_mod, "pick_region", _Picker(REGION))
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)

    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)

        # The copy button IS captured, so it is searched and reported missing
        # from this blank frame...
        await _wait_for(
            pilot,
            lambda: ELEMENT_MISSING in _label(app, TemplateKind.COPY),
            "the copy row reports what the search found",
        )
        # ...while the send button is not captured at all, so nothing is ever
        # claimed about it.
        assert ELEMENT_RESTING in _label(app, TemplateKind.SEND_READY)


async def test_an_unticked_busy_capture_still_reaches_the_column(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row that used to be dead for the worst possible reason.

    The finish-signal checklist decides what may END a response; it never
    decided what the column may show, and gating the search on it meant a user
    could not see whether their stop-button capture still matched anything until
    after they had ticked it - which is precisely when they needed to know. The
    column shows what button would be used if it were going to be used, whatever
    is switched on: here the checklist ticks stale and nothing else, the busy
    icon is captured and on screen, and its row says so.
    """
    app = _make_app(tmp_path, profile_root)
    service = sorted(app.app_config.services)[0]
    if app.app_config.general.service in app.app_config.services:
        service = app.app_config.general.service
    seed_templates(service, TemplateKind.BUSY, size=(24, 24))

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
        _with_signals(main, "stale")
        await _press(app, pilot, "#set-region-btn")
        await _wait_for(pilot, lambda: main._detector_worker is not None, "poller started")

        await _wait_for(
            pilot, lambda: _drawn(app, TemplateKind.BUSY), "the busy crop reaches the column"
        )
        assert "found" in _label(app, TemplateKind.BUSY)
        # ...and it decides nothing: no tracker was built, so nothing about it
        # can close a tick or fold into a finish verdict.
        assert main._active_detectors == ("stale",)
        assert main._busy_tracker is None


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
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)
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


# -- which renderer draws it --------------------------------------------------
#
# A pytest run has no terminal to draw sixels on, so the verdict is DECLARED
# (tui.graphics.set_terminal_graphics - the documented way in, and reset for
# every test by the autouse fixture in tests/conftest.py). What that buys is
# real: the widget really is textual-image's sixel widget, really is asked to
# render inside a running Textual, and the escape sequence it produces can be
# read straight off its strips. That is the exact thing that silently failed
# before - auto-detection picked half cells and nothing said so.
#
# The declared cell size is the one textual_image's own get_cell_size() falls
# back to off a terminal (10x20), because the widget scales the image with that
# function rather than with our verdict - in production the two are the same
# number because the probe caches what it returned. In a TEST process they can
# come apart, and which cell size the widget uses is not this suite's to decide:
# ``textual_image.widget.sixel`` binds ``get_cell_size`` by name at import time,
# so whichever test first pulls that module in decides what it is bound to for
# the rest of the run (tests/shell/tui/test_graphics.py imports it while its own
# monkeypatched cell size is installed, and running this file after it used to
# fail a hard-coded raster assertion). So the raster attributes below are read
# back from the same binding the widget reads them from - the pixel geometry is
# still pinned, just not against a bet on test order.

SIXEL_TERMINAL = TerminalGraphics(sixel=True, cell_width=10, cell_height=20)


def _raster(cols: int, rows: int) -> str:
    """The sixel raster attributes a ``cols x rows`` cell box comes out as."""
    from textual_image.widget.sixel import get_cell_size

    cell = get_cell_size()
    return f'"1;1;{cols * cell.width};{rows * cell.height}'


def _sixel_strips(app: AgentClipApp, kind: TemplateKind) -> str:
    """Everything the crop widget would write to the terminal this frame."""
    assert app.main_screen is not None
    widget = app.main_screen.query_one(f"#{element_crop_id(kind)}")
    text = ""
    for child in widget.children:
        for strip in child.render_lines(child.region.reset_offset):
            text += "".join(segment.text for segment in strip)
    return text


async def test_a_sixel_terminal_gets_the_sixel_widget(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composed from the startup probe, and by NAME: textual-image's auto alias
    resolves to half cells whenever its import-time detection lost the race with
    Textual, which is precisely the failure this column is not allowed to have."""
    from textual_image.widget.sixel import Image as SixelImage

    set_terminal_graphics(SIXEL_TERMINAL)
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.main_screen is not None
        for kind in ELEMENT_ORDER:
            widget = app.main_screen.query_one(f"#{element_crop_id(kind)}")
            assert isinstance(widget, SixelImage)
            # crop_rows(20) rows, ELEMENT_CROP_COLS wide - pinned inline so the
            # crop that was padded to exactly this box is not resized again.
            assert widget.styles.width is not None
            assert widget.styles.height is not None


async def test_a_posted_crop_reaches_the_terminal_as_sixel_data(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end of the road: a match posted by the poller comes back out of the
    widget as a sixel escape sequence, sized to the cell box the panel reserved
    (the raster attributes are ELEMENT_CROP_COLS by crop_rows(20), in pixels)."""
    set_terminal_graphics(SIXEL_TERMINAL)
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)
        assert "\x1bP" not in _sixel_strips(app, TemplateKind.BUSY)

        _post(app, {TemplateKind.BUSY: ElementCrop(ICON, 0.012)})
        await pilot.pause()
        await pilot.pause()

        drawn = _sixel_strips(app, TemplateKind.BUSY)
        assert "\x1bP" in drawn  # DCS: the sixel introducer
        # Raster attributes: the padded box, in pixels.
        assert _raster(ELEMENT_CROP_COLS, crop_rows(SIXEL_TERMINAL.cell_height)) in drawn
        assert HALF_BLOCK not in drawn
        assert "1.2%" in _label(app, TemplateKind.BUSY)


async def test_an_unchanged_crop_is_not_redrawn(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still icon cut out of frame after frame is the normal case, and the
    sixel widget rebuilds its child every time it is handed an image - which on
    a real terminal is a clear and a redraw twice a second, of a picture nobody
    could tell from the one already there. Same pixels, same child."""
    set_terminal_graphics(SIXEL_TERMINAL)
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)
        assert app.main_screen is not None
        widget = app.main_screen.query_one(f"#{element_crop_id(TemplateKind.BUSY)}")

        _post(app, {TemplateKind.BUSY: ElementCrop(ICON, 0.012)})
        await pilot.pause()
        await pilot.pause()
        drawn = widget.children[0]

        # A later tick with the same pixels - and a different diff, which is the
        # label's business, not the picture's.
        _post(app, {TemplateKind.BUSY: ElementCrop(icon(), 0.031)})
        await pilot.pause()
        await pilot.pause()
        assert widget.children[0] is drawn
        assert "3.1%" in _label(app, TemplateKind.BUSY)

        # Different pixels do land.
        _post(app, {TemplateKind.BUSY: ElementCrop(icon(20, 20), 0.012)})
        await pilot.pause()
        await pilot.pause()
        assert widget.children[0] is not drawn


async def test_a_missing_element_stops_drawing_sixels(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Searched and not on screen" has to clear the picture in both renderers,
    or the row keeps showing a button that has gone."""
    set_terminal_graphics(SIXEL_TERMINAL)
    _patch_picker(monkeypatch)
    _freeze_detector(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await _polling(app, pilot)
        _post(app, {TemplateKind.IDLE: ElementCrop(ICON, 0.012)})
        await pilot.pause()
        await pilot.pause()
        assert "\x1bP" in _sixel_strips(app, TemplateKind.IDLE)

        _post(app, {TemplateKind.IDLE: None})
        await pilot.pause()
        await pilot.pause()
        assert "\x1bP" not in _sixel_strips(app, TemplateKind.IDLE)
        assert ELEMENT_MISSING in _label(app, TemplateKind.IDLE)


async def test_the_column_says_which_renderer_it_is_using(
    tmp_path: Path, profile_root: Path
) -> None:
    """Nobody can see a sixel that was never sent. "The detector is finding the
    wrong thing" and "your terminal cannot draw pictures" look identical from
    the outside, and only the second one has a fix the user can act on."""
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.main_screen is not None
        assert "half-block" in str(app.main_screen.query_one("#elements-mode", Static).render())

    set_terminal_graphics(SIXEL_TERMINAL)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.main_screen is not None
        assert "sixel" in str(app.main_screen.query_one("#elements-mode", Static).render())


# -- what the worker hands over -----------------------------------------------


def test_half_blocks_get_the_cell_grid_and_sixel_gets_the_pixels() -> None:
    """The one thing the two renderers disagree about upstream of the panel:
    half blocks want the crop already averaged down to the cells they will draw,
    sixel wants every pixel that matched, because drawing them at their real
    size is the entire point."""
    set_terminal_graphics(NO_SIXEL)
    small = element_crop_image(ICON)
    assert small is not None
    assert (small.width, small.height) != (ICON.width, ICON.height)
    assert small.width <= ELEMENT_CROP_COLS

    set_terminal_graphics(SIXEL_TERMINAL)
    assert element_crop_image(ICON) is ICON


def test_a_degenerate_cut_is_nothing_to_draw_in_either_renderer() -> None:
    for graphics in (NO_SIXEL, SIXEL_TERMINAL):
        set_terminal_graphics(graphics)
        assert element_crop_image(RegionImage(0, 0, b"")) is None
