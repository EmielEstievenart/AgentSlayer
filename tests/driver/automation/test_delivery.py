"""The outbound delivery's decisions, with no terminal, no browser and no mouse.

Since slice 7 the paste path is the AutomationController's: park the payload,
click the chat's input box, wait out the focus activation, paste it (in one
burst or a stream of them), tap Enter for a service that asked us to, and say on
the banner whose move it is now. This is where the CHOICES are asserted - which
of those two paths a payload takes, when the Enter tap happens, what the banner
says, and what a re-delivery or a retry may do at all.

The real TIMING stays with the Pilot suites next door (``tests/shell/tui``): the settle
between the click and the paste is the one behaviour whose whole point is that it
is a real beat on a real event loop, and a fake that returns 0.0 would assert
nothing about it. So every beat here is shrunk to nothing on purpose, and what is
left is the decision tree.

Two seams make that possible and neither is the paint port. The machine is the
:class:`~agentclip.driver.monitor.protocol.UIMonitor` and nothing under it: since
phase 6.2 the controller reaches no ``ScreenOps`` at all, so the double here is a
:class:`~agentclip.driver.monitor.fake.FakeUIMonitor` subclass whose verbs record
instead of act - the chat box is a ``locate`` answer now, not a template search
somebody stubbed. The beats are the other half: they are module constants
(``driver/monitor/beats``) read at the call site, so a suite shrinks one by
writing to it. Everything the sequence still has to ASK a shell is
:class:`~agentclip.driver.automation.host.AutomationHost`, including the one step that
could not come down with the rest: where a payload goes when the clipboard
provider refuses it (the TUI's OSC-52 escape).
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest

import agentclip.driver.automation.delivery as delivery_mod
from agentclip.config import DELIVERY_STREAM, ServicePreset
from agentclip.driver.automation.alerts import AttentionAlarm
from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.delivery import (
    AUTO_SEND_FLASH_TEXT,
    ENTER_FLASH_TEXT,
    PASTE_FLASH_TEXT,
    stream_flash_text,
)
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip import chunking
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.monitor import beats
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.protocol import Located
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.slot import AgentSlot

from .conftest import FakeAutomationView, settle

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
# The docked input box inside it, and where a click on it lands. Every delivery
# here needs one: since the blind-click fallback went, a payload is only ever
# pasted into a chat box that a captured appearance really MATCHED, so a suite
# with nothing on screen would assert the banner and never the paste. The aim is
# the service's own click point, applied by the MONITOR and handed back on
# ``Located.target`` (§11.3) - the middle of the picture until a test moves it,
# which is what ``AIMED`` spells out.
CHAT_BOX = ScreenRegion(1100, 800, 601, 41)
AIMED = click_point_region(CHAT_BOX, 50, 50)
PAYLOAD = "===CLIP:BEGIN=== the outbound ===CLIP:END==="
# Small enough that a readable payload is several chunks, big enough that a short
# one is exactly one.
CHUNK = 12
# The two window handles the activation wait and the snap back are about: ours
# (what a shell recorded through ``set_own_window``) and whatever the focus click
# brought forward.
OUR_WINDOW = 4242
BROWSER_WINDOW = 1717
# The pre-paste focus click is a PAIR of clicks (``delivery.FOCUS_CLICK_GAP_S``,
# half a second apart): one click to wake the browser window, one to land in the
# box it is now able to route to. So every trace a delivery leaves opens with two
# of them, and the assertions below say so once rather than sixteen times.
FOCUS = ["click", "click"]
# ...except when the OS refuses the first: the second is never asked for, and
# nothing is pasted into a window that may not be the browser's.
FOCUS_REFUSED = ["click"]


class ScriptedMonitor(FakeUIMonitor):
    """Every OS call the delivery makes, recorded rather than performed - and
    every appearance it asks about, answered out of the host's screen.

    A ``FakeUIMonitor`` subclass rather than a stub of the machine one layer
    down, because the monitor IS the machine now (ui-monitor.md 2.3): the
    chat-box hunt the delivery makes is one ``locate`` round trip, and there is
    no frame, no template and no tolerance left above the seam for a test to
    substitute. ``on_screen`` stays on the host, where every test already writes
    it - one dict, read by whichever half is asked.
    """

    def __init__(
        self,
        host: FakeHost,
        clipboard: FakeClipboard | None = None,
        *,
        has_clipboard: bool = True,
    ) -> None:
        super().__init__(clipboard=clipboard, has_clipboard=has_clipboard)
        self.host = host
        # Every input event in the order it went out - the whole assertion for
        # "what actually happened to the machine".
        self.events: list[str] = []
        # Every rectangle a click was aimed at, in order.
        self.clicked: list[ScreenRegion] = []
        self.click_lands = True
        self.paste_lands = True
        self.enter_lands = True
        # What the clipboard held at each paste, sampled from inside the burst:
        # by the time a delivery returns, a stream has moved on.
        self.pasted: list[str] = []
        # The screen that cannot be read at all - a capture that failed, which
        # the monitor reports as the same empty answer as "not on screen"
        # because a caller that may not click has the same next move for both.
        self.blind = False
        # Who the OS says holds the foreground, one reading per ask, the last
        # one repeating for ever - so a script is written as "ours, ours, then
        # the browser" and a machine that never hands it over is one entry long.
        # The browser by default: the click landed, which is the ordinary case.
        self.foreground: list[int | None] = [BROWSER_WINDOW]
        self.foreground_reads = 0
        # Every handle the delivery asked to have brought back, in order.
        self.focused: list[int] = []

    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        self.events.append("click")
        # Beside the event trace rather than in it: almost every assertion in
        # this file is about the ORDER things happened in, and only the
        # chat-box aiming cares where the pointer went.
        self.clicked.append(region)
        return self.click_lands

    async def foreground_window(self) -> int | None:
        # Deliberately NOT on ``events``: a read is not something done TO the
        # machine, and every "what actually happened" assertion in this file
        # would grow a poll's worth of noise.
        self.foreground_reads += 1
        return self.foreground.pop(0) if len(self.foreground) > 1 else self.foreground[0]

    async def focus_window(self, handle: int) -> bool:
        self.events.append("focus")
        self.focused.append(handle)
        return True

    async def send_paste(self) -> bool:
        self.events.append("paste")
        if self.clipboard is not None:
            self.pasted.append(self.clipboard.read_text() or "")
        return self.paste_lands

    async def send_enter(self) -> bool:
        self.events.append("enter")
        return self.enter_lands

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        """The host's screen as the monitor would answer about it: the LOWEST of
        the rectangles, whether there were several, on a miss how close the
        search came - and, on a hit, the ONE pixel to press.

        The aim is filled in HERE because that is the side it lives on since
        §11.3: the click point belongs to the picture, and the pictures are the
        monitor's. ``click_points`` is what a test moves to move it.
        """
        if self.blind:
            return Located(None, False, None)
        found = self.host.on_screen.get(kind, [])
        if not found:
            return Located(None, False, 0.21)
        lowest = max(found, key=lambda rect: rect.top)
        return Located(lowest, len(found) > 1, None, self.aim(kind, lowest))


class FakeHost:
    """The scripted ``AutomationHost`` the delivery asks: what the live service
    is, where its appearances are, and where a payload goes when the clipboard
    will not take it."""

    def __init__(self, preset: ServicePreset) -> None:
        self.preset = preset
        # WHICH appearances the monitor holds for the live service (§11.3). Every
        # kind by default: what this suite gates a paste on is whether a capture
        # MATCHED, which is ``on_screen``.
        self.captured: tuple[TemplateKind, ...] = tuple(TemplateKind)
        self.on_screen: dict[TemplateKind, list[ScreenRegion]] = {}
        # Every payload handed to the shell's own fallback channel (the TUI's
        # OSC-52 escape), in order.
        self.parked_off_clipboard: list[str] = []

    def live_preset(self) -> ServicePreset:
        return self.preset

    def captured_for(self, slot: AgentSlot) -> tuple[TemplateKind, ...]:
        return self.captured

    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        return list(self.on_screen.get(kind, []))

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        return False

    async def ingest_harvest(self) -> None:
        """Nothing to hand a reply to."""

    def copy_seen_note(self) -> str:
        return ""

    def rebuild_detectors(self) -> None:
        """No detector set to rebuild."""

    def park_off_clipboard(self, text: str) -> None:
        self.parked_off_clipboard.append(text)


class RecordingAlarm(AttentionAlarm):
    """Every arm, chime and disarm in order, and not one sound.

    A subclass rather than a stub for the same reason ``ScriptedMonitor`` is one:
    the controller takes an ``AttentionAlarm``, so a test that hands in one of
    these is testing the arrangement the app runs - minus the thread and the
    tone generator, which are what ``tests/driver/automation/test_alerts.py``
    is for.
    """

    def __init__(self) -> None:
        super().__init__(lambda: None)
        self.calls: list[str] = []

    def arm(self, *, repeat_seconds: float = 0.0) -> None:
        self.calls.append(f"arm:{repeat_seconds}")

    def chime(self) -> None:
        self.calls.append("chime")

    def disarm(self) -> None:
        self.calls.append("disarm")


def _preset(**overrides: Any) -> ServicePreset:
    base = ServicePreset(
        key="fake",
        label="Fake",
        max_paste_chars=10_000,
        total_context_chars=100_000,
        # The beat before the auto-submit Enter is a PRESET field now (§11.8),
        # so shrinking it is a fact about the service under test rather than a
        # constant to monkeypatch: zero here, and the one story that is about
        # the wait names its own number.
        submit_delay_s=0.0,
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture(autouse=True)
def quick_beats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every beat the delivery paces itself by, shrunk to nothing.

    Written onto the modules the controller reads them from, which is the whole
    point of it reading them there: the real TIMING is the Pilot suites' subject
    (a settle that returns 0.0 asserts nothing about a settle), and what is left
    here is the decision tree. The activation budget is three rather than ten
    for the same reason - it is a CEILING, and a test for "it ran out" only has
    to spend one.
    """
    monkeypatch.setattr(beats, "ACTIVATION_ATTEMPTS", 3)
    monkeypatch.setattr(beats, "ACTIVATION_POLL_S", 0.0)
    monkeypatch.setattr(beats, "FOCUS_CLICK_GAP_S", 0.0)
    monkeypatch.setattr(beats, "PASTE_SETTLE_DELAY", 0.0)
    monkeypatch.setattr(beats, "SNAP_BACK_SETTLE_S", 0.0)
    monkeypatch.setattr(beats, "STREAM_CHUNK_SETTLE_S", 0.0)
    # Small enough that a readable payload is several chunks, big enough that a
    # short one is exactly one.
    monkeypatch.setattr(chunking, "STREAM_CHUNK_CHARS", CHUNK)


@pytest.fixture
def host() -> FakeHost:
    """A service whose docked input box is on screen where ``CHAT_BOX`` says.

    Seeded here rather than per test because it is a PRECONDITION of every
    delivery now: with no chat box matching inside the drawn region the paste
    path refuses to click at all, so a host with an empty screen would turn
    every assertion in this file into the same one about the banner. The suites
    that are about the refusal clear it (``host.on_screen.clear()``).
    """
    fake = FakeHost(_preset())
    fake.on_screen[TemplateKind.CHATBOX_ONGOING] = [CHAT_BOX]
    return fake


@pytest.fixture
def clipboard() -> FakeClipboard:
    return FakeClipboard()


@pytest.fixture
def machine(host: FakeHost, clipboard: FakeClipboard) -> ScriptedMonitor:
    return ScriptedMonitor(host, clipboard)


@pytest.fixture
def alarm() -> RecordingAlarm:
    return RecordingAlarm()


@pytest.fixture
def delivery(
    view: FakeAutomationView,
    host: FakeHost,
    machine: ScriptedMonitor,
    alarm: RecordingAlarm,
) -> AutomationController:
    """A controller with a drawn chat window, a chat box on screen inside it and
    a real (fake) clipboard - the state an outbound payload is delivered out of.
    The alarm is a recording one throughout, so nothing in this file can make the
    machine beep."""
    automation = AutomationController(view=view, host=host, monitor=machine, alarm=alarm)
    automation.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    return automation


def _flash(view: FakeAutomationView) -> tuple[str, bool]:
    """The last thing the banner was asked to say, and whether it offered the
    retry button beside it."""
    assert view.paste_flashes
    return view.paste_flashes[-1]


def _said(view: FakeAutomationView, fragment: str) -> bool:
    return any(fragment in message for message, _severity in view.notifications)


# -- the ordinary delivery -------------------------------------------------------


async def test_a_payload_is_parked_then_clicked_then_pasted(
    delivery: AutomationController,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """The whole happy path in one order: the clipboard first (it is what every
    manual recovery pastes), then the focus click, then the burst."""
    await delivery.copy_outbound(PAYLOAD)

    assert clipboard.written == [PAYLOAD]
    assert machine.events == [*FOCUS, "paste"]
    assert machine.pasted == [PAYLOAD]
    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _flash(view) == (ENTER_FLASH_TEXT, False)


async def test_the_payload_is_registered_as_our_own_write(
    delivery: AutomationController,
) -> None:
    """The watcher polls the clipboard we just wrote to; an unregistered write
    would come straight back in as a "reply" to itself."""
    await delivery.copy_outbound(PAYLOAD)

    assert delivery.self_writes.contains_text(PAYLOAD)


async def test_the_reply_gate_opens_whether_or_not_the_paste_landed(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """The payload is out either way - by our Ctrl+V or by the one the banner is
    about to ask for - so a reply is due either way.

    The one test in this file that runs the LOOP, because since phase 2 the gate
    is opened by the state that has a reply to wait for (``WAIT_SEND`` /
    ``MANUAL_INSERT``, whichever the delivery earns) rather than by the delivery
    itself - which is what makes a manual paste and an automatic one open the
    same one.
    """
    machine.paste_lands = False
    delivery.start_loop()
    try:
        await delivery.copy_outbound(PAYLOAD)
        await settle()
        assert delivery.loop_state is LoopState.MANUAL_INSERT
        assert delivery.reply is not None
    finally:
        delivery.stop_loop()


async def test_a_click_that_never_landed_pastes_nothing(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """Focus could be on any window, and pasting into an unknown app is the one
    unforgivable failure here - so the Ctrl+V is the user's to make."""
    machine.click_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == FOCUS_REFUSED
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert _flash(view) == (PASTE_FLASH_TEXT, True)


async def test_no_drawn_window_means_no_click_at_all(
    view: FakeAutomationView, host: FakeHost, machine: ScriptedMonitor, clipboard: FakeClipboard
) -> None:
    """Nothing calibrated is not a click target: there is nowhere to aim."""
    automation = AutomationController(view=view, host=host, monitor=machine)

    await automation.copy_outbound(PAYLOAD)

    assert machine.events == []
    assert clipboard.written == [PAYLOAD]  # ...but the payload is still parked
    assert automation.loop_state is LoopState.MANUAL_INSERT
    assert _flash(view) == (PASTE_FLASH_TEXT, True)


# -- no chat box on screen: the refusal ------------------------------------------
# The one rule the paste path has. There used to be a fallback here - no chat box
# found meant clicking the middle of the region the user drew and pasting into
# whatever that focused - and the middle of a chat window is the TRANSCRIPT: the
# click selects a word of an old response or lands on a link, and the synthetic
# Ctrl+V goes wherever the page left the caret. Every road to "no verified box"
# now lands on the same banner instead, with the payload parked.


async def test_a_chat_box_that_is_not_on_screen_is_never_clicked_or_pasted_into(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """The whole bug report in one test: mid-transition, behind a dialog, or a
    capture that has drifted - nothing verified, so nothing is clicked, nothing
    is typed, and the user is told."""
    host.on_screen.clear()

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == []
    assert machine.clicked == []
    assert clipboard.written == [PAYLOAD]  # ...and it is still theirs to paste
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert _flash(view) == (PASTE_FLASH_TEXT, True)
    assert _said(view, "the chat box was not found on screen")
    assert any("the chat box was not found on screen" in e.text for e in delivery.harness_log)


async def test_a_service_with_no_chat_box_captured_lands_on_the_same_banner(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
) -> None:
    """The pre-calibration degraded mode - one drawn window and no appearance
    behind it - is not a licence to click either: a rectangle the user drew
    around a whole chat says where the CHAT is, never where its input box is."""
    host.captured = ()  # the monitor has no pictures of this service at all
    host.on_screen.clear()

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == []
    assert clipboard.written == [PAYLOAD]
    assert delivery.loop_state is LoopState.MANUAL_INSERT


async def test_two_boxes_of_one_layout_refuse_the_paste_rather_than_guess(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    view: FakeAutomationView,
) -> None:
    """Two windows of one service under a single drawn region resolve the same
    appearance twice, and picking one is a coin toss between two conversations -
    the losing one gets a whole turn pasted into it."""
    second = ScreenRegion(CHAT_BOX.left, CHAT_BOX.top + 300, CHAT_BOX.width, CHAT_BOX.height)
    host.on_screen[TemplateKind.CHATBOX_ONGOING] = [CHAT_BOX, second]

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == []
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert _said(view, "redraw the window so it contains only this chat")


async def test_a_screen_that_cannot_be_read_is_not_a_licence_to_click(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """A capture that failed says nothing about where the box is, which is
    exactly the state a click may not be aimed from - and the monitor answers it
    with the same empty ``Located`` as "not on screen", because the delivery's
    next move is identical for both."""
    machine.blind = True

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == []
    assert delivery.loop_state is LoopState.MANUAL_INSERT


async def test_a_stream_service_streams_nothing_without_a_box_to_stream_into(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
) -> None:
    """The chunked path rides the same focus click, so it refuses with it - and
    the clipboard is left holding the WHOLE payload rather than a chunk."""
    host.preset = _preset(delivery=DELIVERY_STREAM)
    host.on_screen.clear()

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == []
    assert clipboard.written == [PAYLOAD]
    assert clipboard.read_text() == PAYLOAD


async def test_the_retry_refuses_the_same_way_when_the_box_is_still_gone(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """The retry button re-runs ``deliver``, so it inherits the refusal instead
    of being a second way in."""
    host.on_screen.clear()
    await delivery.copy_outbound(PAYLOAD)
    machine.events.clear()

    await delivery.retry_insert()

    assert machine.events == []
    assert delivery.loop_state is LoopState.MANUAL_INSERT


async def test_a_box_that_comes_back_delivers_normally_on_the_next_try(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """The refusal is about THIS frame, not about the session: a box that was
    momentarily missing (a dialog, a reflow) is delivered into as soon as it is
    back, which is what makes the retry worth pressing."""
    host.on_screen.clear()
    await delivery.copy_outbound(PAYLOAD)
    host.on_screen[TemplateKind.CHATBOX_ONGOING] = [CHAT_BOX]
    machine.events.clear()

    await delivery.retry_insert()

    assert machine.events == [*FOCUS, "paste"]
    assert machine.clicked == [AIMED, AIMED]
    assert delivery.loop_state is LoopState.WAIT_SEND


async def test_a_paste_that_did_not_go_through_says_so_in_its_own_words(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """Three roads into MANUAL_INSERT, and the log has to separate them: this is
    the one where the box WAS focused."""
    machine.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert any("synthetic Ctrl+V did not go through" in e.text for e in delivery.harness_log)


async def test_disarmed_parks_the_payload_and_touches_nothing(
    delivery: AutomationController,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """DISARMED stops one line below the clipboard write and above every OS call:
    the payload is where the user can paste it, and the click simply does not
    happen. The rail says which of the three roads this was."""
    delivery.set_os_armed(False)

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == []
    assert clipboard.written == [PAYLOAD]
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert any("auto-insert suppressed: disarmed" in e.text for e in delivery.harness_log)
    assert _said(view, "click the chat box and press Ctrl+V yourself")
    assert _flash(view) == (PASTE_FLASH_TEXT, True)


# -- waiting for the click's activation ------------------------------------------


async def test_the_paste_waits_until_the_foreground_is_no_longer_ours(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """The click is an activation REQUEST, granted asynchronously, and a Ctrl+V
    that overtakes it lands in whatever held focus a moment ago. So the delivery
    asks the OS who has the foreground until the answer stops being us."""
    delivery.set_own_window(OUR_WINDOW)
    machine.foreground = [OUR_WINDOW, OUR_WINDOW, BROWSER_WINDOW]

    await delivery.copy_outbound(PAYLOAD)

    assert machine.foreground_reads == 3  # asked until the answer changed, then stopped
    assert machine.events == [*FOCUS, "paste"]
    assert delivery.loop_state is LoopState.WAIT_SEND


async def test_a_foreground_that_never_moves_pastes_anyway_once_the_budget_runs_out(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """The wait is a ceiling, not a precondition: refusing to deliver a payload
    that would probably have landed is worse than pasting on a stale reading,
    and the banner plus the retry button already cover a paste that goes
    nowhere."""
    delivery.set_own_window(OUR_WINDOW)
    machine.foreground = [OUR_WINDOW]  # ...and it stays ours for ever

    await delivery.copy_outbound(PAYLOAD)

    assert machine.foreground_reads == 3  # the whole budget, and not one ask more
    assert machine.events == [*FOCUS, "paste"]
    assert delivery.loop_state is LoopState.WAIT_SEND


async def test_with_no_window_of_our_own_the_wait_is_skipped_rather_than_spent(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """Nothing recorded is nothing to compare the foreground to, so there is no
    question to answer - and a shell that never called ``set_own_window`` must
    not pay the whole budget on every delivery for it."""
    await delivery.copy_outbound(PAYLOAD)

    assert machine.foreground_reads == 0
    assert machine.events == [*FOCUS, "paste"]


async def test_a_click_that_never_landed_never_waits_for_an_activation(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """No click, no activation to wait for - and nothing to paste into either."""
    delivery.set_own_window(OUR_WINDOW)
    machine.click_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert machine.foreground_reads == 0
    assert machine.events == FOCUS_REFUSED


# -- the opt-in Enter tap --------------------------------------------------------


async def test_auto_submit_taps_enter_after_a_paste_that_landed(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    view: FakeAutomationView,
) -> None:
    host.preset = _preset(auto_submit=True)

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == [*FOCUS, "paste", "enter"]
    # Still WAIT_SEND: the tap is an attempt, and only the send gate's own
    # evidence says the send actually landed.
    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _flash(view) == (AUTO_SEND_FLASH_TEXT, False)


async def test_the_tap_waits_the_watched_services_own_beat(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """§11.8: how long a composer takes to swallow a paste is a fact about the
    PAGE, so the wait before the Enter is the service's ``submit_delay_s`` and
    not a constant. Timed rather than monkeypatched, because the thing being
    asserted IS that the number in the preset is the number that is slept - a
    stubbed sleep would pass over a delivery that read the old constant."""
    host.preset = _preset(auto_submit=True, submit_delay_s=0.25)

    started = time.perf_counter()
    await delivery.copy_outbound(PAYLOAD)
    elapsed = time.perf_counter() - started

    assert machine.events == [*FOCUS, "paste", "enter"]
    assert elapsed >= 0.2
    # ...and the same delivery against a service that asked for none is not
    # paying that quarter second, which is what makes the number the preset's.
    host.preset = _preset(auto_submit=True, submit_delay_s=0.0)
    started = time.perf_counter()
    await delivery.copy_outbound(PAYLOAD)
    assert time.perf_counter() - started < 0.2


async def test_no_tap_without_the_opt_in(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    await delivery.copy_outbound(PAYLOAD)

    assert "enter" not in machine.events
    assert _flash(view) == (ENTER_FLASH_TEXT, False)


async def test_no_tap_when_the_paste_never_landed(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """An Enter into a chat box that holds nothing is exactly the accident the
    pasted-first order exists to prevent."""
    host.preset = _preset(auto_submit=True)
    machine.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == [*FOCUS, "paste"]


async def test_a_refused_tap_falls_back_to_asking_for_enter(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    view: FakeAutomationView,
) -> None:
    """Nothing was typed, so the banner must keep asking rather than claim the
    send happened - and the log says whose Enter it is now."""
    host.preset = _preset(auto_submit=True)
    machine.enter_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _flash(view) == (ENTER_FLASH_TEXT, False)
    assert any("auto-submit could not type Enter" in e.text for e in delivery.harness_log)
    # ...and the send is theirs to make in the browser, so the browser keeps
    # the focus - a tap that did not take is not an auto-sent delivery.
    assert "focus" not in machine.events


# -- who holds the focus when the delivery is over -------------------------------


async def test_an_auto_sent_delivery_hands_the_foreground_back(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """Pasted AND sent leaves the user nothing to do in the browser, so the next
    thing worth watching is this window's rail - and alt-tabbing back to it by
    hand is not the user's job."""
    host.preset = _preset(auto_submit=True)
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == [*FOCUS, "paste", "enter", "focus"]
    assert machine.focused == [OUR_WINDOW]


async def test_a_service_can_refuse_to_take_the_foreground_back(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """``ServicePreset.snap_back`` off is the debugging aid: everything else
    about the delivery is unchanged, and the browser simply keeps the focus so
    the user can see for themselves where the click landed."""
    host.preset = _preset(auto_submit=True, snap_back=False)
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == [*FOCUS, "paste", "enter"]
    assert machine.focused == []


async def test_a_streamed_delivery_that_auto_sent_hands_it_back_too(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """The stream's auto-submit is the same tap on the same flag, so it cannot
    end up with a different answer to "whose window is this now"."""
    host.preset = _preset(auto_submit=True, delivery=DELIVERY_STREAM)
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events[-2:] == ["enter", "focus"]
    assert machine.focused == [OUR_WINDOW]


async def test_a_paste_still_waiting_on_the_users_enter_leaves_the_browser_focused(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """ ">>> PRESS ENTER <<<" is an instruction to act over THERE, and stealing
    the foreground would make the user click back into the browser to obey a
    banner that has already stopped being true."""
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert _flash(view) == (ENTER_FLASH_TEXT, False)
    assert "focus" not in machine.events
    assert machine.focused == []


async def test_a_paste_that_never_landed_leaves_the_browser_focused(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """Same rule, harder case: the banner is asking for a Ctrl+V in the chat
    box, which is the one window this must not take the focus away from."""
    delivery.set_own_window(OUR_WINDOW)
    machine.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert _flash(view) == (PASTE_FLASH_TEXT, True)
    assert "focus" not in machine.events
    assert machine.focused == []


# -- the audible "your move" -----------------------------------------------------


async def test_the_click_that_focuses_the_chat_box_is_a_double_click(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """One click is spent waking the browser window; a page still activating
    never routes it to the input field, and the Ctrl+V lands nowhere the user
    can see. The gap between them (``FOCUS_CLICK_GAP_S``) is half a second -
    long enough for the woken window to be ready for the second one, and past
    the OS double-click threshold, so the pair is two single clicks and cannot
    select anything."""
    await delivery.copy_outbound(PAYLOAD)

    assert machine.events[:2] == FOCUS
    assert delivery_mod.FOCUS_CLICK_GAP_S >= 0.5


async def test_both_focus_clicks_land_where_the_service_aims_them(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """A chat box that is clickable end to end takes its click where the
    service says (the click point the monitor applied) - and the reinforcing
    second click has to land in the same place, or it would undo the first."""
    box = ScreenRegion(1100, 800, 601, 41)
    host.on_screen[TemplateKind.CHATBOX_ONGOING] = [box]
    machine.click_points[TemplateKind.CHATBOX_ONGOING] = (25, 0)

    await delivery.copy_outbound(PAYLOAD)

    aimed = ScreenRegion(1250, 800, 1, 1)
    assert machine.clicked == [aimed, aimed]


async def test_the_default_aim_is_the_middle_of_the_matched_box(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """A service that never moved its click point aims at the centre of the
    picture that matched - not at the centre of the drawn window."""
    await delivery.copy_outbound(PAYLOAD)

    assert machine.clicked == [AIMED, AIMED]


async def test_a_refused_first_click_is_never_followed_by_a_second(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """A click the OS would not take is the one signal that the target is not
    clickable at all, so the sequence stops rather than hammering it."""
    machine.click_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == FOCUS_REFUSED


async def test_a_stalled_loop_sounds_the_alarm_when_the_service_asked_for_one(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor, alarm: RecordingAlarm
) -> None:
    """MANUAL_INSERT is the loop saying "the Ctrl+V is yours" to a user who may
    not be looking. The hook is on the state, not on this delivery - which is
    what keeps the other eight roads into an attention state from each needing
    their own beep."""
    host.preset = _preset(alert_sound=True)
    machine.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert alarm.calls[-1] == "arm:0"


async def test_the_repeat_interval_rides_the_preset(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor, alarm: RecordingAlarm
) -> None:
    host.preset = _preset(alert_sound=True, alert_repeat_seconds=30)
    machine.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert alarm.calls[-1] == "arm:30"


async def test_a_service_without_the_alert_never_makes_a_sound(
    delivery: AutomationController, machine: ScriptedMonitor, alarm: RecordingAlarm
) -> None:
    """Off is the default and off means silent - including in the very state
    the alarm exists for."""
    machine.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert not any(call.startswith("arm") for call in alarm.calls)


async def test_leaving_the_attention_state_silences_the_alarm(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor, alarm: RecordingAlarm
) -> None:
    """The user pasted it themselves and the send gate saw it: nothing is
    waiting on them any more, so neither is the noise."""
    host.preset = _preset(alert_sound=True, alert_repeat_seconds=5)
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)

    delivery.set_loop_state(LoopState.WAIT_SEND, "the user pasted it themselves")

    assert alarm.calls[-1] == "disarm"


async def test_a_re_sync_with_no_loop_state_behind_it_chimes_once(
    delivery: AutomationController, host: FakeHost, alarm: RecordingAlarm
) -> None:
    """The protocol-error path: nothing ran, no state moved, and the turn only
    goes on once the user has gone back to the browser to re-copy."""
    host.preset = _preset(alert_sound=True)

    delivery.sound_attention_once()

    assert alarm.calls == ["chime"]


async def test_a_re_sync_is_silent_for_a_service_without_the_alert(
    delivery: AutomationController, alarm: RecordingAlarm
) -> None:
    delivery.sound_attention_once()

    assert alarm.calls == []


async def test_shutdown_silences_a_repeating_alarm(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor, alarm: RecordingAlarm
) -> None:
    """An alarm still repeating into a closed app is a beep with nobody left to
    answer it."""
    host.preset = _preset(alert_sound=True, alert_repeat_seconds=5)
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)

    delivery.stop_alert()

    assert alarm.calls[-1] == "disarm"


# -- burst or stream -------------------------------------------------------------


async def test_a_stream_service_walks_a_long_payload_in_chunk_by_chunk(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """One clipboard write and one Ctrl+V per chunk, in order, rejoining into
    exactly the payload that was handed over - and every chunk registered as our
    own write, or the watcher would ingest one back as a reply."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.copy_outbound(PAYLOAD)

    assert len(machine.pasted) > 1
    assert "".join(machine.pasted) == PAYLOAD
    # The whole payload lands first (it is what every manual recovery pastes),
    # then the chunks in order.
    assert clipboard.written == [PAYLOAD, *machine.pasted]
    for chunk in machine.pasted:
        assert delivery.self_writes.contains_text(chunk), chunk
    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _flash(view) == (ENTER_FLASH_TEXT, False)


async def test_the_banner_counts_the_chunks_while_they_go_in(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    view: FakeAutomationView,
) -> None:
    """The user is looking at the browser, so the count is the only thing saying
    a big payload is still going in rather than stuck."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.copy_outbound(PAYLOAD)

    total = len(machine.pasted)
    counted = [text for text, _retry in view.paste_flashes if "STREAMING" in text]
    assert counted == [stream_flash_text(n, total) for n in range(1, total + 1)]


async def test_a_short_payload_in_stream_mode_is_a_single_burst(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
) -> None:
    """Nothing to show progress about, so the stream costs one extra clipboard
    write and nothing else."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.copy_outbound("short")

    assert machine.events == [*FOCUS, "paste"]
    assert clipboard.written == ["short", "short"]


async def test_a_paste_service_sends_one_burst_however_long_the_payload(
    delivery: AutomationController, machine: ScriptedMonitor, clipboard: FakeClipboard
) -> None:
    """A payload far past the chunk size, delivered by a service that did not ask
    for streaming, behaves exactly as it did before streaming existed."""
    await delivery.copy_outbound(PAYLOAD * 4)

    assert machine.events == [*FOCUS, "paste"]
    assert clipboard.written == [PAYLOAD * 4]


async def test_a_failed_chunk_stops_the_stream_and_restores_the_whole_payload(
    delivery: AutomationController,
    host: FakeHost,
    machine: ScriptedMonitor,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """The box now holds a fragment the user has to clear, so the clipboard has
    to hold the WHOLE message for the manual Ctrl+V that replaces it."""
    host.preset = _preset(delivery=DELIVERY_STREAM)
    machine.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert machine.events == [*FOCUS, "paste"]  # stopped, rather than ploughing on
    assert clipboard.written[-1] == PAYLOAD
    assert clipboard.read_text() == PAYLOAD
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert _flash(view) == (PASTE_FLASH_TEXT, True)
    assert _said(view, "holds a partial")


# -- when the clipboard provider will not take it --------------------------------


async def test_no_provider_hands_the_payload_to_the_shell_and_says_so(
    view: FakeAutomationView, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """The write is this layer's; the FALLBACK is not (the TUI's OSC-52 escape is
    a Textual call and exists in no other shell). So the payload crosses back to
    whoever can still park it, and the user is told once."""
    automation = AutomationController(
        view=view, host=host, monitor=ScriptedMonitor(host, has_clipboard=False)
    )
    automation.set_calibration(AgentSlot.MASTER, CHAT_REGION)

    assert await automation.park_on_clipboard(PAYLOAD) is False

    assert host.parked_off_clipboard == [PAYLOAD]
    assert _said(view, "no clipboard backend")


async def test_a_stream_service_falls_back_to_one_burst_with_no_clipboard(
    view: FakeAutomationView, host: FakeHost
) -> None:
    """Streaming needs a clipboard to write each chunk through: with none, the
    single burst of whatever the shell parked is all there is."""
    host.preset = _preset(delivery=DELIVERY_STREAM)
    machine = ScriptedMonitor(host, has_clipboard=False)
    automation = AutomationController(view=view, host=host, monitor=machine)
    automation.set_calibration(AgentSlot.MASTER, CHAT_REGION)

    await automation.copy_outbound(PAYLOAD)

    assert machine.events == [*FOCUS, "paste"]
    assert host.parked_off_clipboard == [PAYLOAD]


async def test_clipboard_ok_is_the_callers_answer_not_a_re_reading(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """The seam is a parameter for a reason: a shell may have parked the payload
    somewhere this layer cannot see, and ``deliver`` is told, not asked."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.deliver(PAYLOAD, clipboard_ok=False)

    assert machine.events == [*FOCUS, "paste"]  # one burst, though a stream was asked for


# -- retrying, and re-delivering -------------------------------------------------


async def test_the_retry_re_runs_the_whole_insert_against_the_pending_payload(
    delivery: AutomationController, machine: ScriptedMonitor, clipboard: FakeClipboard
) -> None:
    """Between the failure and the press the user may well have copied something
    of their own, so the retry parks the outbound again before it pastes."""
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    assert delivery.pending_insert == PAYLOAD
    clipboard.set_text("something the user copied while reading the error")
    machine.paste_lands = True

    await delivery.retry_insert()

    assert machine.events == [*FOCUS, "paste", *FOCUS, "paste"]
    assert machine.pasted == [PAYLOAD, PAYLOAD]
    assert delivery.loop_state is LoopState.WAIT_SEND


async def test_the_retry_re_runs_the_auto_submit_too(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """A retry that pasted but left the message sitting there would be a
    different flow than the one it stands in for."""
    host.preset = _preset(auto_submit=True)
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    machine.paste_lands = True

    await delivery.retry_insert()

    assert machine.events[-4:] == [*FOCUS, "paste", "enter"]


async def test_the_retry_does_nothing_before_anything_has_been_copied(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    await delivery.retry_insert()

    assert machine.events == []
    assert _said(view, "nothing to re-insert")


async def test_the_retry_refuses_while_disarmed(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """DISARMED is a promise that nothing here clicks or types, and a button is
    not an exemption from it - the toast names the switch that is."""
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    delivery.set_os_armed(False)
    machine.events.clear()

    await delivery.retry_insert()

    assert machine.events == []
    assert _said(view, "press F5 to arm")


async def test_the_retry_refuses_while_the_auto_copy_flow_runs(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """The flow is driving the mouse through a scroll-and-hover hunt, and shoving
    a focus click through the middle of it wrecks both."""
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    delivery.flow_running = True
    machine.events.clear()

    await delivery.retry_insert()

    assert machine.events == []
    assert _said(view, "driving the mouse")


async def test_a_session_reset_forgets_what_there_was_to_retry(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """/new tears the session down, and the last outbound belonged to it."""
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)

    delivery.forget_pending_insert()

    assert delivery.pending_insert is None


async def test_parking_the_outbound_touches_nothing_else(
    delivery: AutomationController, machine: ScriptedMonitor, clipboard: FakeClipboard
) -> None:
    """Stage one of the `c` re-copy: the payload is back where a Ctrl+V of the
    user's own can reach it, the mouse never moved, and the rail did not budge -
    nothing about the browser round trip has changed."""
    before = delivery.loop_state

    await delivery.park_outbound(PAYLOAD)

    assert machine.events == []
    assert clipboard.written == [PAYLOAD]
    assert delivery.self_writes.contains_text(PAYLOAD)
    assert delivery.loop_state is before


async def test_parking_leaves_the_pending_payload_alone(
    delivery: AutomationController, machine: ScriptedMonitor
) -> None:
    """It is what the retry button would re-deliver, and re-copying the payload
    that is already the pending one changes nothing about that."""
    machine.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)

    await delivery.park_outbound("something else entirely")

    assert delivery.pending_insert == PAYLOAD


async def test_a_re_delivery_is_allowed_when_nothing_is_in_the_way(
    delivery: AutomationController, view: FakeAutomationView
) -> None:
    assert delivery.may_redeliver() is True
    assert view.notifications == []


async def test_a_re_delivery_is_refused_while_disarmed(
    delivery: AutomationController, view: FakeAutomationView
) -> None:
    """The second press of `c` is the only half of it that would break the
    promise, so the refusal says which switch to throw."""
    delivery.set_os_armed(False)

    assert delivery.may_redeliver() is False
    assert _said(view, "press F5 to arm")


async def test_a_re_delivery_is_refused_while_the_auto_copy_flow_runs(
    delivery: AutomationController, view: FakeAutomationView
) -> None:
    delivery.flow_running = True

    assert delivery.may_redeliver() is False
    assert _said(view, "driving the mouse")


# -- the sidebar's two nudges (ui-monitor.md §11.8) -----------------------------


async def test_press_enter_focuses_the_box_and_taps_enter(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """The auto-submit's last step, done again by hand: the same focus pair as a
    delivery, then Enter and nothing else - no paste, the payload is already in
    the box."""
    await delivery.copy_outbound(PAYLOAD)
    machine.events.clear()

    await delivery.press_enter()

    assert machine.events == [*FOCUS, "enter"]
    assert any("Enter tapped from the sidebar" in e.text for e in delivery.harness_log)
    assert _flash(view) == (AUTO_SEND_FLASH_TEXT, False)


async def test_press_enter_refuses_while_disarmed(
    delivery: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    delivery.set_os_armed(False)

    await delivery.press_enter()

    assert machine.events == []
    assert _said(view, "disarmed")


async def test_press_enter_types_nothing_when_the_box_is_not_on_screen(
    delivery: AutomationController, host: FakeHost, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """Un-aimed keys go to whatever has focus, so an Enter with no verified click
    in front of it is the one thing this door must never send."""
    host.on_screen.clear()

    await delivery.press_enter()

    assert machine.events == []
    assert _said(view, "the chat box was not found on screen")


async def test_copy_again_runs_the_harvest_now(
    delivery: AutomationController,
) -> None:
    """The nudge runs the AUTO_COPY recipe, whatever the loop was waiting for:
    here the harvest finds no copy button on the fake screen and lands where a
    fired-but-empty harvest lands, MANUAL_COPY - the point is that it RAN."""
    await delivery.copy_outbound(PAYLOAD)
    assert delivery.loop_state is LoopState.WAIT_SEND

    await delivery.copy_again()

    assert delivery.loop_state is LoopState.MANUAL_COPY
    assert any("run from the sidebar" in e.text for e in delivery.harness_log)


async def test_copy_again_refuses_while_disarmed(
    delivery: AutomationController, view: FakeAutomationView
) -> None:
    await delivery.copy_outbound(PAYLOAD)
    delivery.set_os_armed(False)

    await delivery.copy_again()

    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _said(view, "disarmed")
