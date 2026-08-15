"""The clipboard watcher thread, driven for real - no Textual, no OS clipboard.

The loop itself (``agentclip.driver.clip.watcher.watch``) has its own unit tests; what
is asserted here is the half that just moved down from ``MainScreen``:
*ownership*. Who starts a thread and who does not, what a capture does on its way
out of it, what a disarm does to it, and that a stop actually ends it - the last
one with a genuine ``join``, because a watcher that only *looks* stopped is
exactly the bug this slice could introduce and the one a mocked thread would
hide. So the threads are real and every test joins its own.

The Pilot suite in ``tests/shell/tui/test_armed_ui.py`` stays as the wiring check -
that the real screen is still plugged into this.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

import pytest

from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.clip.base import ClipboardProvider, ManualOnlyProvider
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.clip.watcher import SelfWriteSet, write_via

from .conftest import FakeAutomationView

# Fast enough that a test never waits on a poll, slow enough to still be a poll.
TICK_MS = 1
TIMEOUT_S = 5.0


def _wait_until(predicate: Callable[[], bool], what: str, timeout: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


class Wiring:
    """One controller wired to an in-memory clipboard, plus what came out of it."""

    def __init__(self, automation: AutomationController, captures: list[str]) -> None:
        self.automation = automation
        self.captures = captures


@pytest.fixture
def wire(view: FakeAutomationView) -> Iterator[Callable[..., Wiring]]:
    """Build wired controllers, and make sure none of them outlives the test.

    The teardown is the point: these are real threads polling a real (in-memory)
    provider, so a test that forgot to stop one would leak it into every test
    after it. Stopping is asked for here even when the test already did it -
    ``stop_input`` is idempotent - and then joined, so a watcher that ignored its
    stop flag fails the test that started it rather than some later one.
    """
    started: list[tuple[AutomationController, list[threading.Thread]]] = []

    def build(
        clipboard: ClipboardProvider | None = None,
        *,
        accepts: Callable[[str], bool] | None = None,
        self_writes: SelfWriteSet | None = None,
    ) -> Wiring:
        captures: list[str] = []
        automation = AutomationController(
            view=view,
            clipboard=clipboard,
            self_writes=self_writes,
            poll_interval_ms=TICK_MS,
            accepts=accepts,
            on_clipboard_captured=captures.append,
        )
        threads: list[threading.Thread] = []
        started.append((automation, threads))
        return Wiring(automation, captures)

    yield build

    for automation, threads in started:
        thread = automation.watcher_thread
        if thread is not None:
            threads.append(thread)
        automation.stop_input()
        for leftover in threads:
            leftover.join(timeout=TIMEOUT_S)
            assert not leftover.is_alive(), "a watcher thread outlived its test"


# == what starts a thread, and what does not ===================================


def test_nothing_polls_until_a_session_asks(wire: Callable[..., Wiring]) -> None:
    """Construction wires a watcher up; it does not run one. The clipboard is the
    user's until a session says otherwise."""
    wiring = wire(FakeClipboard())
    assert wiring.automation.watching is False
    assert wiring.automation.watcher_thread is None


def test_start_input_starts_one_thread_and_start_again_does_not(
    wire: Callable[..., Wiring],
) -> None:
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    thread = wiring.automation.watcher_thread
    assert thread is not None and thread.is_alive()

    wiring.automation.start_input()
    wiring.automation.start_watching()
    assert wiring.automation.watcher_thread is thread


def test_manual_mode_explains_itself_instead_of_polling(
    wire: Callable[..., Wiring], view: FakeAutomationView
) -> None:
    """There is no clipboard to poll, so the session is told how to work by hand -
    once, from here, rather than by each shell inventing its own wording."""
    wiring = wire(ManualOnlyProvider())
    wiring.automation.start_input()

    assert wiring.automation.watching is False
    assert view.notifications and view.notifications[-1][1] == "warning"
    assert "manual clipboard mode" in view.notifications[-1][0]


def test_a_controller_with_no_clipboard_at_all_stays_quiet(
    wire: Callable[..., Wiring], view: FakeAutomationView
) -> None:
    """A shell that never handed one in (the headless tests) gets no thread - and
    no toast either, because nothing about a clipboard was ever promised."""
    wiring = wire(None)
    wiring.automation.start_input()

    assert wiring.automation.watching is False
    assert view.notifications == []


# == the capture path ==========================================================


def test_a_copy_reaches_the_callback(wire: Callable[..., Wiring]) -> None:
    """The whole point of the thread: something else copied, and the shell hears
    about it on the callback it handed in."""
    clipboard = FakeClipboard()
    wiring = wire(clipboard)
    wiring.automation.start_input()

    clipboard.set_text("the model's reply")
    _wait_until(lambda: wiring.captures == ["the model's reply"], "the capture")


def test_only_what_accepts_says_yes_to_is_captured(wire: Callable[..., Wiring]) -> None:
    """The protocol pre-filter is passed in (this layer may not import
    ``agentclip.protocol``), and it decides - a watcher that captured every copy
    would drive a turn off a stray Ctrl+C."""
    clipboard = FakeClipboard()
    wiring = wire(clipboard, accepts=lambda text: text.startswith("REPLY"))
    wiring.automation.start_input()

    clipboard.set_text("a shopping list")
    clipboard.set_text("REPLY: here you go")
    _wait_until(lambda: wiring.captures == ["REPLY: here you go"], "the accepted capture")


def test_our_own_writes_are_never_captured_back(wire: Callable[..., Wiring]) -> None:
    """The outbound payload IS protocol-shaped, so nothing but the self-write
    registry stops the tool from ingesting its own message as a reply. The
    registry is shared with the shell that does the writing."""
    clipboard = FakeClipboard()
    self_writes = SelfWriteSet()
    wiring = wire(clipboard, self_writes=self_writes)
    wiring.automation.start_input()

    write_via(clipboard, self_writes, "===CLIP:TASK=== ours")
    clipboard.set_text("theirs")
    _wait_until(lambda: "theirs" in wiring.captures, "the foreign capture")
    assert "===CLIP:TASK=== ours" not in wiring.captures


def test_a_stopped_watcher_really_stops(wire: Callable[..., Wiring]) -> None:
    """``stop_input`` returns before the thread does (joining would freeze a UI
    thread for up to a poll interval), so the guarantee is that it ends *soon* -
    and this is the test that would catch a stop flag nobody reads."""
    clipboard = FakeClipboard()
    wiring = wire(clipboard)
    wiring.automation.start_input()
    thread = wiring.automation.watcher_thread
    assert thread is not None

    wiring.automation.stop_input()
    assert wiring.automation.watching is False  # true for every reader at once
    thread.join(timeout=TIMEOUT_S)
    assert not thread.is_alive()

    clipboard.set_text("copied after the stop")
    time.sleep(0.05)
    assert wiring.captures == []


def test_stopping_twice_is_harmless(wire: Callable[..., Wiring]) -> None:
    wiring = wire(FakeClipboard())
    wiring.automation.start_input()
    wiring.automation.stop_input()
    wiring.automation.stop_input()
    assert wiring.automation.watching is False


def test_a_restarted_watcher_captures_again(wire: Callable[..., Wiring]) -> None:
    """The `w` key's pause/resume, at this level: a fresh thread with a fresh stop
    flag, not a resurrected one - the old thread's flag is set forever."""
    clipboard = FakeClipboard()
    wiring = wire(clipboard)
    wiring.automation.start_input()
    first = wiring.automation.watcher_thread
    assert first is not None
    wiring.automation.stop_input()
    # Joined only so the count below is about the NEW thread: a stop is
    # deliberately not a join in production (see ``stop_input``), so the old
    # loop is still free to finish the tick it was in.
    first.join(timeout=TIMEOUT_S)

    wiring.automation.start_watching()
    second = wiring.automation.watcher_thread
    assert second is not None and second is not first

    clipboard.set_text("after the resume")
    _wait_until(lambda: wiring.captures == ["after the resume"], "the capture after resuming")


# == the armed switch's watcher half ===========================================


def test_disarming_stops_the_watcher_and_re_arming_starts_it_again(
    wire: Callable[..., Wiring],
) -> None:
    """Watching a clipboard the user has not offered us is an action too - it is
    the one that ingests, and ingesting drives a whole turn. So it stops."""
    clipboard = FakeClipboard()
    wiring = wire(clipboard)
    wiring.automation.start_input()
    assert wiring.automation.watching is True
    stopped = wiring.automation.watcher_thread
    assert stopped is not None

    wiring.automation.set_os_armed(False)
    assert wiring.automation.watching is False
    stopped.join(timeout=TIMEOUT_S)  # so the capture below is provably the new one

    wiring.automation.set_os_armed(True)
    assert wiring.automation.watching is True
    clipboard.set_text("after the re-arm")
    _wait_until(lambda: wiring.captures == ["after the re-arm"], "the capture after re-arming")


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


def test_re_arming_without_a_session_starts_nothing(wire: Callable[..., Wiring]) -> None:
    """Nobody asked for input, so there is nothing for the re-arm to restore."""
    wiring = wire(FakeClipboard())
    wiring.automation.set_os_armed(False)
    wiring.automation.set_os_armed(True)
    assert wiring.automation.watching is False


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
