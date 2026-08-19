"""Pilot tests for the two halves of "the auto-insert did not land".

The insert is a click into the chat's input box followed by a synthetic Ctrl+V
(``MainScreen._insert_outbound``). Both halves here are about the gap between
those two events:

* the SETTLE (``main_mod.PASTE_SETTLE_DELAY``) - the click only tells us the OS
  accepted the input, never that the browser has finished taking focus, and a
  Ctrl+V that overtakes the activation is delivered to whatever had focus
  before. So the paste waits a beat, and it waits it on the event loop
  (``asyncio.sleep``) rather than blocking the UI thread. In front of that beat
  there is now an activation POLL as well (``_await_browser_activation``,
  ``main_mod._ACTIVATION_POLL_S``, which the directory's conftest shrinks to
  nothing) - the decisions it makes belong to the pure-unit suite next door
  (tests/driver/automation/test_delivery.py); what is pinned HERE is the flat
  beat, which is the half no window handle can report on and the half whose
  whole point is that it is a real gap on a real event loop;
* the RETRY - when the paste never landed anyway, the sidebar offers
  ``#retry-insert-btn`` under its blinking nag, and pressing it re-runs the very
  same sequence against the payload the failed attempt was carrying.

Everything that touches the OS is monkeypatched at the *use site*
(``main_mod.click_region`` / ``main_mod.send_paste`` / ``main_mod.send_enter``,
which main.py from-imports): a real burst here would click and paste into
whatever window is running the suite.
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
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.region import ScreenRegion
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.main import MainScreen
from agentclip.shell.tui.widgets.sidebar import ENTER_FLASH_TEXT, PASTE_FLASH_TEXT

# Long enough that the gap it opens cannot be mistaken for scheduling noise,
# short enough that a file full of these tests still runs in a blink.
_TEST_SETTLE_S = 0.3


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
    tmp_path: Path, *, delivery: str = "paste", auto_submit: bool = False
) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir()
    global_path = tmp_path / "config.toml"
    global_path.write_text(
        f"[services.chatgpt-attach]\ndelivery = \"{delivery}\"\n"
        f"auto_submit = {str(auto_submit).lower()}\n",
        encoding="utf-8",
    )
    config = load_config(project, global_config_path=global_path)
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
    )
    return app, fake


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main._chat_region = ScreenRegion(0, 0, 100, 20)  # something to click, so the paste runs
    return main


def _retry_button(app: AgentClipApp) -> Button:
    assert app.main_screen is not None
    return app.main_screen.query_one("#retry-insert-btn", Button)


def _flash_text(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-paste-flash", Static).render())


async def _press_retry(app: AgentClipApp, pilot: Pilot) -> None:
    button = _retry_button(app)
    await _wait_for(pilot, lambda: button.region.width > 0, "retry button laid out")
    await pilot.click("#retry-insert-btn")


def _toasts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    notes: list[str] = []
    monkeypatch.setattr(
        MainScreen, "notify", lambda self, message, *a, **kw: notes.append(str(message))
    )
    return notes


def _said(notes: list[str], fragment: str) -> bool:
    return any(fragment in note for note in notes)


@pytest.fixture(autouse=True)
def _no_real_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "click_region", lambda region: True)
    monkeypatch.setattr(main_mod, "send_paste", lambda: True)
    monkeypatch.setattr(main_mod, "send_enter", lambda: True)


# -- the settle between the click and the paste ---------------------------------


def _timed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Every OS input event the insert sends, in order, with when it happened."""
    events: list[tuple[str, float]] = []
    monkeypatch.setattr(
        main_mod, "click_region", lambda region: events.append(("click", time.monotonic())) or True
    )
    monkeypatch.setattr(
        main_mod, "send_paste", lambda: events.append(("paste", time.monotonic())) or True
    )
    return events


async def test_the_paste_waits_for_the_focus_click_to_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the beat: the Ctrl+V goes out AFTER the click, and not
    in the same instant - the window it is aimed at is still taking focus."""
    monkeypatch.setattr(main_mod, "PASTE_SETTLE_DELAY", _TEST_SETTLE_S)
    events = _timed(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()

        # Two clicks, because the focus click is a double one (the first wakes
        # the window, the second lands in the box) - and the beat this test is
        # about is the one after the LAST of them.
        assert [name for name, _ in events] == ["click", "click", "paste"]
        gap = events[2][1] - events[1][1]
        assert gap >= _TEST_SETTLE_S * 0.8, f"paste came {gap:.3f}s after the click"


async def test_the_settle_does_not_block_the_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is an ``asyncio.sleep``, not a ``time.sleep``: the app keeps running
    its own timers while the browser takes focus, so the STATE rail and the
    blinking flash carry on being drawn."""
    monkeypatch.setattr(main_mod, "PASTE_SETTLE_DELAY", _TEST_SETTLE_S)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        ticks = 0

        def _tick() -> None:
            nonlocal ticks
            ticks += 1

        main.set_interval(_TEST_SETTLE_S / 10, _tick)
        await main.copy_outbound("the payload")

        # A blocked event loop would have starved the timer completely.
        assert ticks >= 3


async def test_the_settle_covers_a_streamed_delivery_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A streaming service pastes its first chunk into the same freshly clicked
    box, so it waits out the same activation."""
    monkeypatch.setattr(main_mod, "PASTE_SETTLE_DELAY", _TEST_SETTLE_S)
    monkeypatch.setattr(main_mod, "_STREAM_CHUNK_SETTLE_S", 0.0)
    monkeypatch.setattr(main_mod, "STREAM_CHUNK_CHARS", 8)
    events = _timed(monkeypatch)
    app, _ = _make_app(tmp_path, delivery="stream")
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("a payload long enough to be several chunks")
        await pilot.pause()

        assert [name for name, _ in events[:3]] == ["click", "click", "paste"]
        assert len(events) > 3  # it really did stream
        assert events[2][1] - events[1][1] >= _TEST_SETTLE_S * 0.8


def test_the_shipped_settle_is_long_enough_to_be_worth_having() -> None:
    """A guard on the constant itself: the failure it exists for is a focus race
    measured in tens of milliseconds, and shaving this to nothing would quietly
    bring the dropped pastes back."""
    assert main_mod.PASTE_SETTLE_DELAY >= 0.2


# -- the retry button ------------------------------------------------------------


async def test_a_failed_insert_offers_the_retry_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        assert _retry_button(app).display is False  # nothing has been inserted yet

        await main.copy_outbound("the payload")
        await pilot.pause()

        assert main._loop_state is LoopState.MANUAL_INSERT
        assert PASTE_FLASH_TEXT.splitlines()[0] in _flash_text(app)
        assert _retry_button(app).display is True


async def test_an_insert_that_landed_offers_nothing_to_retry(tmp_path: Path) -> None:
    """The payload is in the box; a button that would click in and paste it a
    second time is the wrong offer."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()

        assert ENTER_FLASH_TEXT.splitlines()[0] in _flash_text(app)
        assert _retry_button(app).display is False


async def test_the_retry_button_re_runs_the_click_and_the_paste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery, end to end: the first Ctrl+V went nowhere, the second one
    (a single press later) lands, and the loop moves on as if it always had."""
    pastes: list[bool] = []
    landing = False
    monkeypatch.setattr(main_mod, "click_region", lambda region: True)
    monkeypatch.setattr(
        main_mod, "send_paste", lambda: pastes.append(landing) or landing
    )
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert pastes == [False]
        assert main._loop_state is LoopState.MANUAL_INSERT

        landing = True  # whatever was in the way has gone
        await _press_retry(app, pilot)
        await _wait_for(pilot, lambda: len(pastes) == 2, "the retry's Ctrl+V")
        await _wait_for(
            pilot, lambda: main._loop_state is LoopState.WAIT_SEND, "the retry landing"
        )

        assert ENTER_FLASH_TEXT.splitlines()[0] in _flash_text(app)
        assert _retry_button(app).display is False  # nothing left to retry


async def test_the_retry_delivers_the_payload_not_whatever_is_on_the_clipboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the failure and the press the user may well have copied something
    of their own, so the retry puts the outbound back on the clipboard first -
    the Ctrl+V it is about to send must not deliver a stray copy into the chat."""
    pasted_text: list[str] = []
    app, fake = _make_app(tmp_path)
    monkeypatch.setattr(
        main_mod, "send_paste", lambda: pasted_text.append(fake.read_text() or "") or False
    )
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        fake.set_text("something the user copied while reading the error")

        await _press_retry(app, pilot)
        await _wait_for(pilot, lambda: len(pasted_text) == 2, "the retry's Ctrl+V")

        assert pasted_text == ["the payload", "the payload"]


async def test_the_retry_taps_enter_for_an_auto_submitting_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It re-runs the WHOLE insert, submit and all - a retry that pasted but
    left the message sitting there would be a different flow than the one it is
    standing in for."""
    monkeypatch.setattr(main_mod, "_SUBMIT_SETTLE_S", 0.0)
    taps: list[None] = []
    monkeypatch.setattr(main_mod, "send_enter", lambda: taps.append(None) or True)
    landing = False
    monkeypatch.setattr(main_mod, "send_paste", lambda: landing)
    app, _ = _make_app(tmp_path, auto_submit=True)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert taps == []

        landing = True
        await _press_retry(app, pilot)
        await _wait_for(pilot, lambda: taps == [None], "the retry's Enter")


async def test_the_retry_refuses_while_disarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISARMED is a promise that nothing here clicks or types, and a button is
    not an exemption from it - the toast names the switch that is."""
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()

        notes = _toasts(monkeypatch)
        clicks: list[None] = []
        monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(None) or True)
        main.set_os_armed(False)
        notes.clear()  # the switch announces itself; what it says is not this test's
        await main.retry_insert()

        assert clicks == []
        assert _said(notes, "F5")


async def test_the_retry_does_nothing_before_anything_has_been_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard behind the hidden button: pressed with no outbound behind it,
    it says so and touches nothing."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        notes = _toasts(monkeypatch)
        clicks: list[None] = []
        monkeypatch.setattr(main_mod, "click_region", lambda region: clicks.append(None) or True)

        await main.retry_insert()

        assert clicks == []
        assert _said(notes, "nothing to re-insert")


async def test_a_session_reset_forgets_what_there_was_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/new tears the session down, and the last outbound belonged to it."""
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("the payload")
        await pilot.pause()
        assert _retry_button(app).display is True

        await main.clear_transcript()
        await pilot.pause()

        assert _retry_button(app).display is False
        assert main._pending_insert is None
