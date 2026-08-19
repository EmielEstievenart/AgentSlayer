"""Pilot tests for the per-service scroll action (``ServicePreset.scroll_action``).

The auto-copy flow snaps the transcript to the bottom before hunting the newest
copy button. That snap used to be one hardcoded wheel flick; now the service
says how its page scrolls - the flick (default), a burst of Page Down taps, or
one End tap - because a virtualized transcript or a wheel-capturing chat page
can leave the flick doing nothing at all.

Both halves of the snap are tested here, because they are one move. A keyboard
snap goes to whatever has FOCUS, so the focus click in front of it decides
whether it works at all: land it in the chat box and End means "end of the
line" - a caret twitches and the transcript never moves. So a keyboard snap
aims at the padding just above the box instead, and only falls back to the box
when there is no box (or no room above it). The wheel is aimed by coordinates
and keeps the plain box click.

Then, whichever way the snap goes out, the pointer is parked on the transcript
before it: some chat pages scroll only the pane under the cursor, and a cursor
left in the input box means a wheel turning against a one-line field. That park
is a real ``move_cursor``, and it is best-effort - a refused move must not cost
the snap.

And the snap gets ``_COPY_SNAP_ROUNDS`` goes rather than one, because the
commonest reason the copy button is not on the frame is a page that had not
finished arriving - a reply still streaming, a virtualized transcript still
laying out what it just scrolled to. So each miss re-scrolls and re-searches.
The choreography in FRONT of the snap is not repeated: nothing between rounds
touches the mouse or the focus, so the click and the park stay one-time and
the counts below say so.

The flow is invoked directly (``_auto_copy_flow``); what arms and fires it is
test_copy_region_ui.py's subject. Every OS touch is monkeypatched at the use
site (``main_mod.click_region`` / ``move_cursor`` / ``scroll_region`` /
``send_scroll_key``), and the copy search is stubbed to find nothing unless a
test says otherwise - the scroll happens before the hunt, which is all these
tests are about.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest
from textual.pilot import Pilot

import agentclip.shell.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.profile_store import save_template
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.template import RegionMatch, Template
from agentclip.shell.tui.app import AgentClipApp

from .conftest import aimed_at

SERVICE = "chatgpt-attach"
CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
# The docked input box, inside the drawn window: where a paste has to be
# clicked, and the one place a keyboard snap must NOT be.
ONGOING_BOX = ScreenRegion(1300, 860, 400, 90)
# ...and where the keyboard snap's focus click goes instead - horizontally
# centred on the box, in the padding a few pixels above its top edge.
ABOVE_BOX = ScreenRegion(
    ONGOING_BOX.left + ONGOING_BOX.width // 2,
    ONGOING_BOX.top - main_mod._ABOVE_CHATBOX_PX,
    1,
    1,
)
# One round of the default snap, as ``scroll_region`` sees it, and how many
# rounds a hunt that never finds anything gets. Read off the module rather than
# retyped: the sizes are tuning numbers and these tests are about the shape.
FLICK = (CHAT_REGION, main_mod._SNAP_WHEEL_DETENTS)
ROUNDS = main_mod._COPY_SNAP_ROUNDS


def _frame(region: ScreenRegion) -> RegionImage:
    """A capture of ``region``: a cycling byte pattern keyed to its origin.

    Varied rather than flat because a seeded frame is also a template, and a
    block of one colour has no anchors for ``ServiceProfile.put`` to keep.
    """
    start = (region.left + region.top) % 251
    unit = bytes(0 if i % 4 == 3 else (start + i) % 256 for i in range(256))
    size = region.width * region.height * 4
    return RegionImage(region.width, region.height, (unit * (size // 256 + 1))[:size])


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
    tmp_path: Path, profile_root: Path, *, scroll_action: str | None
) -> tuple[AgentClipApp, FakeClipboard]:
    """An app whose default service snaps to the bottom the given way."""
    project = tmp_path / "project"
    project.mkdir()
    global_path = tmp_path / "config.toml"
    if scroll_action is not None:
        global_path.write_text(
            f'[services.{SERVICE}]\nscroll_action = "{scroll_action}"\n', encoding="utf-8"
        )
    config = load_config(project, global_config_path=global_path)
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        profile_root=profile_root,
    )
    return app, fake


@pytest.fixture(autouse=True)
def _no_real_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: True)
    monkeypatch.setattr(
        main_mod, "find_lowest_with_best_miss", lambda template, scene, **kw: (None, None)
    )


async def _run_flow(
    app: AgentClipApp, pilot: Pilot
) -> None:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main._chat_region = CHAT_REGION
    assert main._active_profile().has(TemplateKind.COPY)
    await main._auto_copy_flow()
    await pilot.pause()


class _Snap(NamedTuple):
    """What the snap did to the machine, and the ORDER it did it in.

    The order is half the contract: parking the pointer over the transcript is
    worth something only while the scroll is still to come, and three
    independent lists cannot tell that apart from a move that arrived after it.
    """

    moves: list[tuple[int, int]]
    scrolls: list[tuple[ScreenRegion, int]]
    keys: list[tuple[str, int]]
    order: list[str]


def _recorders(monkeypatch: pytest.MonkeyPatch, *, moved: bool = True) -> _Snap:
    """Record (never perform) the three primitives the snap can reach for.

    ``moved`` is what ``move_cursor`` reports back - False is the answer every
    platform that cannot move a pointer gives, and the flow has to carry on
    scrolling regardless.
    """
    snap = _Snap([], [], [], [])

    def move(x: int, y: int) -> bool:
        snap.moves.append((x, y))
        snap.order.append("move")
        return moved

    def scroll(region: ScreenRegion, clicks: int) -> bool:
        snap.scrolls.append((region, clicks))
        snap.order.append("scroll")
        return True

    def scroll_key(key: str, taps: int = 1) -> bool:
        snap.keys.append((key, taps))
        snap.order.append("keys")
        return True

    monkeypatch.setattr(main_mod, "move_cursor", move)
    monkeypatch.setattr(main_mod, "scroll_region", scroll)
    monkeypatch.setattr(main_mod, "send_scroll_key", scroll_key)
    return snap


def _clicks(monkeypatch: pytest.MonkeyPatch) -> list[ScreenRegion]:
    """Where the flow's focus click aims - the whole subject of the second half
    of this file. The copy click never happens: nothing is found."""
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(
        main_mod, "click_region", lambda region, **kw: clicks.append(region) or True
    )
    return clicks


async def _clicked_it(self: object, target: ScreenRegion) -> bool:
    """A copy click that took, without the clipboard round-trip.

    Stands in for ``MainScreen._verified_copy_click`` in the one test that lets
    the hunt succeed: verifying a click means three offset clicks and up to six
    clipboard reads a fifth of a second apart, and none of that is about where
    the transcript scrolled to.
    """
    return True


def _seed_chatbox(profile_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calibrate the docked input box and put it on screen where ONGOING_BOX
    says, the way a capture in the service editor plus a real search would.

    Without this the flow has no box to aim around and ``_chatbox_region``
    hands back the whole drawn window - which is the other case tested here.
    """
    save_template(profile_root, SERVICE, TemplateKind.CHATBOX_ONGOING, _frame(ONGOING_BOX))
    local = RegionMatch(
        ONGOING_BOX.left - CHAT_REGION.left, ONGOING_BOX.top - CHAT_REGION.top, 0.01
    )

    def fake_find_all(template: Template, scene: RegionImage, **kw: object) -> list[RegionMatch]:
        return [local]

    monkeypatch.setattr(main_mod, "find_all_in_region", fake_find_all)


# -- which primitive the snap uses ------------------------------------------------


async def test_the_default_snap_is_still_the_wheel_flick(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _recorders(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action=None)
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert snap.scrolls == [FLICK] * ROUNDS
    assert snap.keys == []


async def test_page_down_taps_replace_the_flick_when_configured(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of taps, like the flick's detents: an over-shoot that stops at
    the bottom, not a measured scroll."""
    snap = _recorders(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action="page_down")
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert snap.scrolls == []
    assert snap.keys == [("page_down", main_mod._PAGE_DOWN_TAPS)] * ROUNDS


async def test_one_end_tap_replaces_the_flick_when_configured(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End is the bottom by definition - one tap, no burst."""
    snap = _recorders(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action="end")
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert snap.scrolls == []
    # One tap per round, and never a burst: the count is what the flick's is
    # over-shooting towards, and End is already there.
    assert snap.keys == [("end", 1)] * ROUNDS


# -- where the focus click in front of the snap aims ------------------------------


async def test_the_end_snap_clicks_above_the_chat_box_never_inside_it(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this aim exists for: End typed into a focused chat box moves the
    caret to the end of the line and the transcript never scrolls, so the copy
    hunt that follows searches the wrong part of the page. The padding above the
    box focuses the same window with nothing typable under the pointer."""
    _recorders(monkeypatch)
    clicks = _clicks(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action="end")
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    _seed_chatbox(profile_root, monkeypatch)
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert clicks == [ABOVE_BOX]
    assert ABOVE_BOX.top < ONGOING_BOX.top
    assert ABOVE_BOX.center[0] == ONGOING_BOX.center[0]  # the same column


async def test_the_page_down_snap_aims_above_the_box_too(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page Down mostly falls through a chat box to the page behind it, so this
    one "worked" - on the inputs that let it through. Both keys ride the same
    click, and there is nothing to gain from aiming a keyboard snap at a text
    field."""
    _recorders(monkeypatch)
    clicks = _clicks(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action="page_down")
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    _seed_chatbox(profile_root, monkeypatch)
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert clicks == [ABOVE_BOX]


async def test_the_wheel_flick_still_clicks_the_chat_box_itself(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flick is aimed by coordinates and does not care what has focus, so
    it keeps the plain box click every other path uses."""
    _recorders(monkeypatch)
    clicks = _clicks(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action=None)
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    _seed_chatbox(profile_root, monkeypatch)
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert clicks == [aimed_at(ONGOING_BOX)]


async def test_with_no_chat_box_calibrated_the_snap_clicks_the_drawn_window(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_chatbox_region``'s fallback is the whole drawn window, which has no
    padding above it inside itself - aiming above THAT would click out of the
    chat entirely. So the old behaviour stands, keyboard snap or not."""
    _recorders(monkeypatch)
    clicks = _clicks(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action="end")
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert clicks == [CHAT_REGION]


# -- where the pointer sits when the snap goes out --------------------------------


async def test_the_wheel_flick_parks_the_pointer_on_the_transcript_first(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The focus click leaves the cursor in the chat box, and a page that scrolls
    only the pane under the pointer would then wheel a one-line input field. So
    the pointer moves to the middle of the drawn window before the flick - a real
    MOVE, which is the only thing that makes a browser fire the hover chain those
    pages track (``scroll_region``'s own SetCursorPos teleport does not)."""
    snap = _recorders(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action=None)
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    _seed_chatbox(profile_root, monkeypatch)
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert snap.moves == [CHAT_REGION.center]
    assert snap.order == ["move"] + ["scroll"] * ROUNDS


@pytest.mark.parametrize("action", ["page_down", "end"])
async def test_a_keyboard_snap_parks_the_pointer_on_the_transcript_too(
    action: str,
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys go to whatever has focus, but the pages that need the keyboard form
    are the same ones that route a scroll by hover, and some of them read the
    pointer for the keys as well. The park costs one cursor move either way, and
    it happens after the focus click that aimed above the box - so the click
    still decides focus and the pointer still ends up over the transcript."""
    snap = _recorders(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action=action)
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    _seed_chatbox(profile_root, monkeypatch)
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert snap.moves == [CHAT_REGION.center]
    assert snap.order == ["move"] + ["keys"] * ROUNDS


async def test_a_refused_move_still_lets_the_snap_go_out(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The park is best-effort: off Windows (and under the test suite's own OS
    gate) ``move_cursor`` reports False without touching anything, and a snap
    that gave up there would be a regression on every path that worked before
    the park existed."""
    snap = _recorders(monkeypatch, moved=False)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action=None)
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert snap.moves == [CHAT_REGION.center]
    assert snap.scrolls == [FLICK] * ROUNDS


# -- and how many times it goes out -----------------------------------------------


async def test_a_missed_hunt_snaps_again_instead_of_giving_up(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the rounds exist. A reply that is still streaming, or a
    transcript still laying out what it just scrolled to, puts the newest copy
    button on the page a beat AFTER the first capture - so one miss is no
    evidence at all and the flow snaps again rather than falling to MANUAL_COPY
    on the strength of a frame that arrived early."""
    snap = _recorders(monkeypatch)
    clicks = _clicks(monkeypatch)
    monkeypatch.setattr(main_mod.MainScreen, "_verified_copy_click", _clicked_it)
    seen: list[int] = []

    def found_on_the_second_look(template: Template, scene: RegionImage, **kw: object) -> object:
        seen.append(1)
        return (None, 0.30) if len(seen) == 1 else (RegionMatch(20, 30, 0.02), None)

    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", found_on_the_second_look)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action=None)
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    _seed_chatbox(profile_root, monkeypatch)
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    # Two snaps, and the flow stops the moment a round finds one - the third
    # round is a retry budget, not a schedule.
    assert snap.scrolls == [FLICK] * 2
    # ...and the choreography in front of the snap happened once, for the first
    # round only: nothing between rounds moves the pointer or the focus, and
    # re-clicking a transcript risks selecting text or following a link.
    assert clicks == [aimed_at(ONGOING_BOX)]
    assert snap.moves == [CHAT_REGION.center]
    assert snap.order == ["move", "scroll", "scroll"]


async def test_the_focus_click_and_the_park_are_not_repeated_per_round(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same rule against the longest run: three rounds, all missing, and the
    click and the park are still one each. The pointer is where round 1 parked
    it for the whole hunt, because a capture and a template search touch
    nothing."""
    snap = _recorders(monkeypatch)
    clicks = _clicks(monkeypatch)
    app, _fake = _make_app(tmp_path, profile_root, scroll_action=None)
    seed_templates(SERVICE, TemplateKind.COPY, size=(24, 24))
    _seed_chatbox(profile_root, monkeypatch)
    async with app.run_test(size=(110, 55)) as pilot:
        await _run_flow(app, pilot)

    assert clicks == [aimed_at(ONGOING_BOX)]
    assert snap.moves == [CHAT_REGION.center]
    assert len(snap.scrolls) == ROUNDS
    assert snap.order == ["move"] + ["scroll"] * ROUNDS
