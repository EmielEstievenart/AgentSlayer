"""The controller's half of the clipboard watcher - no thread, no OS clipboard.

The thread itself moved down to the monitor in docs/design/ui-monitor.md phase
6.1 (§2.11: "the clipboard is a monitor resource"), and so did the provider, the
poll interval and the self-write register. Its own tests go with it, to
``tests/driver/monitor``; the loop under all of it
(``agentclip.driver.clip.watcher.watch``) has had unit tests all along.

What is asserted here is what the CONTROLLER still decides, which is the half
that cannot live below it:

* who asks for a watcher and who does not - a session, a re-arm, and the three
  answers ``start_input`` gives (armed, manual mode, no backend at all);
* what a disarm does to one, and what a re-arm puts back;
* what a capture does on its way out - the accept filter this layer owns
  because ``agentclip.protocol`` is above it (tests/test_layering.py).

The Pilot suite in ``tests/shell/tui/test_armed_ui.py`` stays as the wiring check -
that the real screen is still plugged into this.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.clip.base import ManualOnlyProvider
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.monitor.fake import FakeUIMonitor

from .conftest import FakeAutomationView


class Wiring:
    """One controller, the machine under it, and what came out of the hook."""

    def __init__(
        self,
        automation: AutomationController,
        monitor: FakeUIMonitor,
        captures: list[str],
    ) -> None:
        self.automation = automation
        self.monitor = monitor
        self.captures = captures

    def watch_calls(self) -> list[bool]:
        """Every ``watch_clipboard`` the controller made, in order - the whole
        of what it asks the machine for, and the only thing it can ask."""
        return [
            bool(args[0]) for verb, args in self.monitor.calls if verb == "watch_clipboard"
        ]


@pytest.fixture
def wire(view: FakeAutomationView) -> Callable[..., Wiring]:
    """Build a controller over a machine with (or without) a clipboard.

    No teardown any more, and that is the point of the phase: there is no thread
    to leak. ``FakeUIMonitor.watching`` is a flag it raises when asked, exactly
    as the real one raises a thread.
    """

    def build(
        clipboard: object = None,
        *,
        has_clipboard: bool = True,
        accepts: Callable[[str], bool] | None = None,
    ) -> Wiring:
        captures: list[str] = []
        monitor = FakeUIMonitor(clipboard=clipboard, has_clipboard=has_clipboard)  # type: ignore[arg-type]
        automation = AutomationController(
            view=view,
            monitor=monitor,
            accepts=accepts,
            on_clipboard_captured=captures.append,
        )
        return Wiring(automation, monitor, captures)

    return build


# == who asks for a watcher, and who does not ==================================


def test_nothing_is_watched_until_a_session_asks(wire: Callable[..., Wiring]) -> None:
    """Construction wires a watcher up; it does not ask for one. The clipboard
    is the user's until a session says otherwise."""
    wiring = wire(FakeClipboard())
    assert wiring.automation.watching is False
    assert wiring.watch_calls() == []


def test_start_input_asks_once_and_asking_again_is_idempotent(
    wire: Callable[..., Wiring],
) -> None:
    """The request is idempotent at the seam rather than guarded here: "is one
    already running" is a fact about the machine, and the machine is where it
    is now kept."""
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    assert wiring.automation.watching is True

    wiring.automation.start_input()
    assert wiring.watch_calls() == [True, True]
    assert wiring.monitor.watching is True


def test_manual_mode_explains_itself_instead_of_asking(
    wire: Callable[..., Wiring], view: FakeAutomationView
) -> None:
    """There is no clipboard to poll, so the session is told how to work by hand -
    once, from here, rather than by each shell inventing its own wording. And
    nothing is asked of the machine at all: the refusal is this object's."""
    wiring = wire(ManualOnlyProvider())
    wiring.automation.start_input()

    assert wiring.automation.watching is False
    assert wiring.watch_calls() == []
    assert view.notifications and view.notifications[-1][1] == "warning"
    assert "manual clipboard mode" in view.notifications[-1][0]


def test_a_machine_with_no_clipboard_at_all_stays_quiet(
    wire: Callable[..., Wiring], view: FakeAutomationView
) -> None:
    """A machine that never had one (the headless tests) gets no watcher - and
    no toast either, because nothing about a clipboard was ever promised. The
    request is still made; the machine is what refuses it."""
    wiring = wire(has_clipboard=False)
    wiring.automation.start_input()

    assert wiring.automation.watching is False
    assert wiring.watch_calls() == [True]
    assert view.notifications == []


def test_stop_input_asks_for_the_watcher_to_stop(wire: Callable[..., Wiring]) -> None:
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    wiring.automation.stop_input()

    assert wiring.automation.watching is False
    assert wiring.watch_calls() == [True, False]


def test_stopping_twice_is_harmless(wire: Callable[..., Wiring]) -> None:
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    wiring.automation.stop_input()
    wiring.automation.stop_input()
    assert wiring.automation.watching is False


# == the capture path ==========================================================


def test_a_copy_reaches_the_callback(wire: Callable[..., Wiring]) -> None:
    """The whole point of the hook: something else copied, the monitor's watcher
    caught it, and the shell hears about it on the callback it handed in."""
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()

    wiring.monitor.push_clip("the model's reply")

    assert wiring.captures == ["the model's reply"]


def test_only_what_accepts_says_yes_to_is_captured(wire: Callable[..., Wiring]) -> None:
    """The protocol pre-filter is passed in (this layer may not import
    ``agentclip.protocol``), and it decides - a hook that forwarded every copy
    would drive a turn off a stray Ctrl+C."""
    wiring = wire(FakeClipboard(), accepts=lambda text: text.startswith("REPLY"))
    wiring.automation.start_input()

    wiring.monitor.push_clip("a shopping list")
    wiring.monitor.push_clip("REPLY: here you go")

    assert wiring.captures == ["REPLY: here you go"]


def test_a_capture_still_arrives_when_nobody_asked_for_a_watcher(
    wire: Callable[..., Wiring],
) -> None:
    """The hook is registered for the controller's whole lifetime, deliberately:
    who is polling is the monitor's business, and a controller that unsubscribed
    and resubscribed around every session would be one more thing to get out of
    step with it. Nothing pushes when nothing is watching, so this only says the
    wiring has no on/off of its own."""
    wiring = wire(FakeClipboard())
    wiring.monitor.push_clip("out of the blue")
    assert wiring.captures == ["out of the blue"]


# == the armed switch's watcher half ===========================================


def test_disarming_stops_the_watcher_and_re_arming_starts_it_again(
    wire: Callable[..., Wiring],
) -> None:
    """Watching a clipboard the user has not offered us is an action too - it is
    the one that ingests, and ingesting drives a whole turn. So it stops."""
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    assert wiring.automation.watching is True

    wiring.automation.set_os_armed(False)
    assert wiring.automation.watching is False
    assert wiring.monitor.watching is False

    wiring.automation.set_os_armed(True)
    assert wiring.automation.watching is True
    assert wiring.watch_calls() == [True, False, True]


def test_re_arming_restores_the_watcher_the_user_had_not_the_one_they_paused(
    wire: Callable[..., Wiring],
) -> None:
    """Re-arming undoes the disarm, nothing more: a user who switched the watcher
    off themselves is not handed it back."""
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    wiring.automation.stop_input()  # the user's own `w`

    wiring.automation.set_os_armed(False)
    wiring.automation.set_os_armed(True)
    assert wiring.automation.watching is False


def test_disarming_twice_does_not_lose_the_watcher_on_re_arm(
    wire: Callable[..., Wiring],
) -> None:
    """Regression. `/armed off` typed twice must not let the second call re-read
    the already-stopped watcher and remember "it was off" - that would silently
    swallow the watcher the first call took away, and the user would re-arm into
    an app that never ingests again. The bookkeeping moves on transitions only."""
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()

    wiring.automation.set_os_armed(False)
    wiring.automation.set_os_armed(False)
    assert wiring.automation.watching is False

    wiring.automation.set_os_armed(True)
    assert wiring.automation.watching is True


def test_a_session_started_while_disarmed_gets_its_watcher_on_re_arm(
    wire: Callable[..., Wiring],
) -> None:
    """The session asked for a watcher and was refused; re-arming is when that
    request is finally honoured. Without this the user would have to press F5 and
    then `w` to get back to a normal app."""
    wiring = wire(FakeClipboard())
    wiring.automation.set_os_armed(False)
    wiring.automation.start_input()
    assert wiring.automation.watching is False  # the session never got one

    wiring.automation.set_os_armed(True)
    assert wiring.automation.watching is True
    # The disarm's own stop is unconditional (it always was) - what the session
    # asked for is remembered, not re-asked, until the re-arm honours it.
    assert wiring.watch_calls() == [False, True]


def test_re_arming_without_a_session_starts_nothing(wire: Callable[..., Wiring]) -> None:
    """Nobody asked for input, so there is nothing for the re-arm to restore."""
    wiring = wire(FakeClipboard())
    wiring.automation.set_os_armed(False)
    wiring.automation.set_os_armed(True)
    assert wiring.automation.watching is False
    assert wiring.watch_calls() == [False]  # the disarm's stop, and nothing else


def test_the_watcher_is_settled_before_the_view_is_painted(
    wire: Callable[..., Wiring], view: FakeAutomationView
) -> None:
    """Ordering, asserted rather than assumed: whatever a shell draws when it is
    told the flag moved must be drawn from a finished transition - the status bar
    reports the watcher, and it repaints off this same call returning."""
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    seen: list[bool] = []

    def spy(armed: bool) -> None:
        seen.append(wiring.automation.watching)

    view.paint_armed = spy  # type: ignore[method-assign]
    wiring.automation.set_os_armed(False)
    assert seen == [False], "the watcher was still up when the view was painted"
