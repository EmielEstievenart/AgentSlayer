"""Pilot tests for the two stages of ``c`` (tui.md 3.4a).

The key means two different-sized things and the split is the feature. One press
puts the last outbound back on the CLIPBOARD and stops there - no focus click,
no synthetic Ctrl+V, nothing that touches the machine. A second press inside
``_RECOPY_DOUBLE_TAP_S`` escalates to the real delivery, which is the very
``copy_outbound`` a normal send goes through: park, click, settle, paste, and
the opt-in Enter tap. The double tap IS the confirmation dialog.

So every test here is ultimately about *which OS calls happened*, and every one
of those is monkeypatched at the use site (``main_mod.click_region`` /
``send_paste`` / ``send_enter``, which main.py from-imports): a real burst would
click and paste into whatever window is running the suite. The stage-one tests
would still be meaningful without the stubs - nothing should reach them - but
they are stubbed anyway, because the whole point of a test that asserts "no
paste happened" is that a regression makes it happen for real.

The controller half (the arm, the window, the ``_busy`` refusal) is driven
through ``MainScreen.action_recopy`` rather than reimplemented: the binding, the
controller's decision and the screen's two entry points are one path, and it is
the path a keystroke takes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot

import agentclip.app.controller as controller_mod
import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.protocol.types import Outbound
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen

PAYLOAD = "===CLIP:BEGIN=== the last outbound ===CLIP:END==="


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, *, auto_submit: bool = False) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir()
    if auto_submit:
        global_path = tmp_path / "config.toml"
        global_path.write_text(
            "[services.chatgpt-attach]\nauto_submit = true\n", encoding="utf-8"
        )
    else:
        global_path = project / "no-such-global.toml"
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
    # Something to click, so a delivery that IS attempted gets all the way to
    # the paste - "nothing was drawn" would refuse the click for the wrong
    # reason and every stage-one assertion below would pass vacuously.
    main._chat_region = ScreenRegion(0, 0, 100, 20)
    return main


def _has_outbound(main: MainScreen, text: str = PAYLOAD) -> None:
    """Put the screen in the one state ``c`` is live in: a payload has been
    composed and copied at some point. The reactive is what ``check_action``
    reads; the controller's field is what the key actually re-copies."""
    main._controller._last_outbound = text
    main.has_outbound = True
    # A freshly launched app is parked inside ``_session_flow`` waiting for the
    # task, and a parked flow is a BUSY controller - which stage two refuses on
    # purpose (that refusal has its own test below). The state ``c`` is really
    # pressed in is an idle AWAITING_REPLY, where the flow slot is free, so that
    # is the state these tests put the controller in.
    main._controller._busy = False


def _os_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every OS input the delivery would send, in order. An empty list is the
    stage-one contract."""
    events: list[str] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region: events.append("click") or True)
    monkeypatch.setattr(main_mod, "send_paste", lambda: events.append("paste") or True)
    monkeypatch.setattr(main_mod, "send_enter", lambda: events.append("enter") or True)
    return events


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
    """The belt to every test's braces: even a test that never records events
    must not be able to fire a real click or Ctrl+V at the machine."""
    monkeypatch.setattr(main_mod, "click_region", lambda region: True)
    monkeypatch.setattr(main_mod, "send_paste", lambda: True)
    monkeypatch.setattr(main_mod, "send_enter", lambda: True)


# -- stage one: the clipboard, and nothing else ---------------------------------


async def test_one_press_copies_and_touches_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole promise of the first press: the payload is back where a Ctrl+V
    of the user's own can reach it, and the mouse never moved."""
    events = _os_events(monkeypatch)
    notes = _toasts(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the payload on the clipboard")
        await pilot.pause()

        assert events == []
        assert _said(notes, "re-copied the last outbound")


async def test_the_first_toast_advertises_the_second_press(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A double tap nobody is told about is a feature nobody has: the escalation
    is discovered from the toast the first press raises, so the toast has to
    name the key."""
    notes = _toasts(monkeypatch)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)

        main.action_recopy()
        await _wait_for(pilot, lambda: _said(notes, "re-copied"), "the re-copy toast")

        assert _said(notes, "press c again to deliver it")


async def test_the_key_is_reachable_as_a_keystroke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding, not just the action: ``c`` is gated on ``has_outbound`` and
    reaches the controller from a real press once the composer has let go."""
    events = _os_events(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)
        main.set_focus(None)  # command mode - the composer would eat the letter
        await pilot.pause()

        await pilot.press("c")
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the payload on the clipboard")

        assert events == []


# -- stage two: the double tap delivers -----------------------------------------


async def test_two_presses_deliver_through_the_ordinary_send_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escalation is ``copy_outbound`` itself, so it produces exactly what a
    normal send produces: the focus click, the settle, the Ctrl+V."""
    events = _os_events(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the first press to copy")
        main.action_recopy()
        await _wait_for(pilot, lambda: events == ["click", "paste"], "the re-delivery")

        # ...and the payload is still the whole payload on the clipboard, which
        # is what the synthetic Ctrl+V pastes.
        assert fake.read_text() == PAYLOAD


async def test_the_re_delivery_obeys_the_service_auto_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reusing the send path means reusing ALL of it: a service that taps Enter
    for itself taps it for a re-delivery too, without this feature knowing that
    such a tick exists."""
    monkeypatch.setattr(main_mod, "_SUBMIT_SETTLE_S", 0.0)
    events = _os_events(monkeypatch)
    app, fake = _make_app(tmp_path, auto_submit=True)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)
        assert main._live_preset().auto_submit  # the tick this test is about

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the first press to copy")
        main.action_recopy()
        await _wait_for(
            pilot, lambda: events == ["click", "paste", "enter"], "the re-delivery + auto-submit"
        )


async def test_a_press_after_the_window_is_a_fresh_first_press(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arm expires. A ``c`` pressed now and another one pressed
    absent-mindedly later are two separate requests for the clipboard, not a
    gesture - and the second must not move the mouse."""
    monkeypatch.setattr(controller_mod, "_RECOPY_DOUBLE_TAP_S", 0.05)
    events = _os_events(monkeypatch)
    notes = _toasts(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the first press to copy")
        await asyncio.sleep(0.2)  # well past the window
        main.action_recopy()
        await _wait_for(
            pilot,
            lambda: sum("re-copied the last outbound" in note for note in notes) == 2,
            "a second re-copy toast",
        )
        await pilot.pause()

        assert events == []


async def test_the_shipped_window_is_a_gesture_not_a_pause() -> None:
    """A guard on the constant itself: long enough that two deliberate presses
    fit, short enough that it cannot span a moment's thought - past which a
    press that suddenly drove the mouse would be a surprise."""
    assert 0.5 <= controller_mod._RECOPY_DOUBLE_TAP_S <= 3.0


async def test_a_new_outbound_disarms_a_half_finished_gesture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second press means "that one again". Once a NEW payload has gone out,
    "that one" no longer exists - and it has just been delivered anyway."""
    events = _os_events(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the first press to copy")
        await main._controller._copy_outbound(
            Outbound(kind="results", chunks=("a newer payload",), total_chars=15, turn=2)
        )
        await _wait_for(pilot, lambda: events == ["click", "paste"], "the new payload's delivery")
        events.clear()

        main.action_recopy()
        await _wait_for(
            pilot, lambda: fake.read_text() == "a newer payload", "the second press to copy"
        )
        await pilot.pause()

        assert events == []  # a fresh first press, not the gesture's second half


# -- the refusals ---------------------------------------------------------------


async def test_nothing_to_recopy_stays_nothing_at_both_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No outbound has ever been composed: neither press does anything at all,
    and a second one cannot conjure a delivery out of the first's silence."""
    events = _os_events(monkeypatch)
    notes = _toasts(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        assert main._controller._last_outbound is None

        main.action_recopy()
        main.action_recopy()
        await pilot.pause()
        await pilot.pause()

        assert events == []
        assert fake.read_text() is None
        assert not _said(notes, "re-copied")


async def test_the_double_tap_is_refused_while_a_turn_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage two takes the flow-spawning actions' guard: a payload pasted on top
    of a turn that is about to compose its own would put two messages in the
    box. Stage one is deliberately NOT guarded - a clipboard write is safe
    mid-turn, and mid-turn is exactly when a user reaches for it."""
    events = _os_events(monkeypatch)
    notes = _toasts(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)
        main._controller._busy = True

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the first press to copy")
        main.action_recopy()
        await _wait_for(pilot, lambda: _said(notes, "a turn is running"), "the refusal")
        await pilot.pause()

        assert events == []


async def test_the_double_tap_is_refused_while_disarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISARMED is a promise about the machine, and the second press is the only
    half of ``c`` that would break it - so the first still copies and the second
    says which switch to throw."""
    events = _os_events(monkeypatch)
    notes = _toasts(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)
        main.set_os_armed(False)
        await pilot.pause()

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the first press to copy")
        main.action_recopy()
        await _wait_for(pilot, lambda: _said(notes, "disarmed"), "the refusal")
        await pilot.pause()

        assert events == []


async def test_the_double_tap_is_refused_while_the_auto_copy_flow_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same refusal the sidebar's retry button makes, for the same reason:
    the flow is driving the mouse through a scroll-and-hover hunt, and shoving a
    focus click through the middle of it wrecks both."""
    events = _os_events(monkeypatch)
    notes = _toasts(monkeypatch)
    app, fake = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        _has_outbound(main)
        main._flow_running = True

        main.action_recopy()
        await _wait_for(pilot, lambda: fake.read_text() == PAYLOAD, "the first press to copy")
        main.action_recopy()
        await _wait_for(pilot, lambda: _said(notes, "driving the mouse"), "the refusal")
        await pilot.pause()

        assert events == []
