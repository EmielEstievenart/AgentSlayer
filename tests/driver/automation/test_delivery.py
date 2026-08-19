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

Two seams make that possible and neither is the paint port. The machine, both
beats and the chunk size are reached through
:class:`~agentclip.driver.automation.ops.ScreenOps` - subclassed below into a scripted
one, which is exactly what the Textual shell hands in. Everything the sequence
still has to ASK a shell is :class:`~agentclip.driver.automation.host.AutomationHost`,
including the one step that could not come down with the rest: where a payload
goes when the clipboard provider refuses it (the TUI's OSC-52 escape).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

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
from agentclip.driver.automation.ops import ScreenOps
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot

from .conftest import FakeAutomationView

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
PAYLOAD = "===CLIP:BEGIN=== the outbound ===CLIP:END==="
# Small enough that a readable payload is several chunks, big enough that a short
# one is exactly one.
CHUNK = 12
# The two window handles the activation wait and the snap back are about: ours
# (what a shell recorded through ``set_own_window``) and whatever the focus click
# brought forward.
OUR_WINDOW = 4242
BROWSER_WINDOW = 1717
# The pre-paste focus click is a DOUBLE click (``delivery.FOCUS_CLICK_GAP_S``):
# one click to wake the browser window, one to land in the box it is now able to
# route to. So every trace a delivery leaves opens with two of them, and the
# assertions below say so once rather than sixteen times.
FOCUS = ["click", "click"]
# ...except when the OS refuses the first: the second is never asked for, and
# nothing is pasted into a window that may not be the browser's.
FOCUS_REFUSED = ["click"]


def _image(width: int, height: int) -> RegionImage:
    size = width * height * 4
    return RegionImage(width, height, bytes(size))


class ScriptedOps(ScreenOps):
    """Every OS call the delivery makes, recorded rather than performed - and
    every beat it paces itself by, shrunk to nothing.

    A subclass rather than a monkeypatch of ``agentclip.driver.automation.ops``' names,
    because that is the substitution the port is FOR: the Textual shell hands in
    one of these too (``_MainScreenOps``), so a test that uses the same seam is
    testing the same arrangement the app runs.
    """

    def __init__(self) -> None:
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
        self.clipboard: FakeClipboard | None = None
        # Who the OS says holds the foreground, one reading per ask, the last
        # one repeating for ever - so a script is written as "ours, ours, then
        # the browser" and a machine that never hands it over is one entry long.
        # The browser by default: the click landed, which is the ordinary case.
        self.foreground: list[int | None] = [BROWSER_WINDOW]
        self.foreground_reads = 0
        # Every handle the delivery asked to have brought back, in order.
        self.focused: list[int] = []

    def capture(self, region: ScreenRegion) -> RegionImage:
        return _image(region.width, region.height)

    def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        self.events.append("click")
        # Beside the event trace rather than in it: almost every assertion in
        # this file is about the ORDER things happened in, and only the
        # chat-box aiming cares where the pointer went.
        self.clicked.append(region)
        return self.click_lands

    def foreground_window(self) -> int | None:
        # Deliberately NOT on ``events``: a read is not something done TO the
        # machine, and every "what actually happened" assertion in this file
        # would grow a poll's worth of noise.
        self.foreground_reads += 1
        return self.foreground.pop(0) if len(self.foreground) > 1 else self.foreground[0]

    def focus_window(self, handle: int) -> bool:
        self.events.append("focus")
        self.focused.append(handle)
        return True

    def send_paste(self) -> bool:
        self.events.append("paste")
        if self.clipboard is not None:
            self.pasted.append(self.clipboard.read_text() or "")
        return self.paste_lands

    def send_enter(self) -> bool:
        self.events.append("enter")
        return self.enter_lands

    def activation_attempts(self) -> int:
        # Three rather than the real ten: the budget is a CEILING here, and a
        # test for "it ran out" only has to spend one.
        return 3

    def activation_poll(self) -> float:
        return 0.0

    def focus_click_gap(self) -> float:
        return 0.0

    def paste_settle(self) -> float:
        return 0.0

    def snap_back_settle(self) -> float:
        return 0.0

    def submit_settle(self) -> float:
        return 0.0

    def stream_chunk_settle(self) -> float:
        return 0.0

    def stream_chunk_chars(self) -> int:
        return CHUNK


class FakeHost:
    """The scripted ``AutomationHost`` the delivery asks: what the live service
    is, where its appearances are, and where a payload goes when the clipboard
    will not take it."""

    def __init__(self, preset: ServicePreset) -> None:
        self.preset = preset
        self.profile = ServiceProfile(key="fake")
        self.on_screen: dict[TemplateKind, list[ScreenRegion]] = {}
        # Every payload handed to the shell's own fallback channel (the TUI's
        # OSC-52 escape), in order.
        self.parked_off_clipboard: list[str] = []

    def live_preset(self) -> ServicePreset:
        return self.preset

    def profile_for(self, slot: AgentSlot) -> ServiceProfile:
        return self.profile

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

    A subclass rather than a stub for the same reason ``ScriptedOps`` is one:
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
        key="fake", label="Fake", max_paste_chars=10_000, total_context_chars=100_000
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture
def ops() -> ScriptedOps:
    return ScriptedOps()


@pytest.fixture
def host() -> FakeHost:
    return FakeHost(_preset())


@pytest.fixture
def clipboard(ops: ScriptedOps) -> FakeClipboard:
    fake = FakeClipboard()
    ops.clipboard = fake
    return fake


@pytest.fixture
def alarm() -> RecordingAlarm:
    return RecordingAlarm()


@pytest.fixture
def delivery(
    view: FakeAutomationView,
    host: FakeHost,
    ops: ScriptedOps,
    clipboard: FakeClipboard,
    alarm: RecordingAlarm,
) -> AutomationController:
    """A controller with a drawn chat window and a real (fake) clipboard - the
    state an outbound payload is delivered out of. No chat box appearance is
    captured, so the focus click lands on the drawn window itself, which is the
    delivery's documented fallback. The alarm is a recording one throughout, so
    nothing in this file can make the machine beep."""
    automation = AutomationController(
        view=view, host=host, ops=ops, clipboard=clipboard, alarm=alarm
    )
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
    ops: ScriptedOps,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """The whole happy path in one order: the clipboard first (it is what every
    manual recovery pastes), then the focus click, then the burst."""
    await delivery.copy_outbound(PAYLOAD)

    assert clipboard.written == [PAYLOAD]
    assert ops.events == [*FOCUS, "paste"]
    assert ops.pasted == [PAYLOAD]
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
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """The payload is out either way - by our Ctrl+V or by the one the banner is
    about to ask for - so a reply is due either way."""
    ops.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)

    assert delivery.awaiting_pasted_reply is True


async def test_a_click_that_never_landed_pastes_nothing(
    delivery: AutomationController, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    """Focus could be on any window, and pasting into an unknown app is the one
    unforgivable failure here - so the Ctrl+V is the user's to make."""
    ops.click_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == FOCUS_REFUSED
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert _flash(view) == (PASTE_FLASH_TEXT, True)


async def test_no_drawn_window_means_no_click_at_all(
    view: FakeAutomationView, host: FakeHost, ops: ScriptedOps, clipboard: FakeClipboard
) -> None:
    """Nothing calibrated is not a click target: there is nowhere to aim."""
    automation = AutomationController(view=view, host=host, ops=ops, clipboard=clipboard)

    await automation.copy_outbound(PAYLOAD)

    assert ops.events == []
    assert clipboard.written == [PAYLOAD]  # ...but the payload is still parked
    assert automation.loop_state is LoopState.MANUAL_INSERT
    assert _flash(view) == (PASTE_FLASH_TEXT, True)


async def test_a_paste_that_did_not_go_through_says_so_in_its_own_words(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """Three roads into MANUAL_INSERT, and the log has to separate them: this is
    the one where the box WAS focused."""
    ops.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert any("synthetic Ctrl+V did not go through" in e.text for e in delivery.harness_log)


async def test_disarmed_parks_the_payload_and_touches_nothing(
    delivery: AutomationController, ops: ScriptedOps, clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """DISARMED stops one line below the clipboard write and above every OS call:
    the payload is where the user can paste it, and the click simply does not
    happen. The rail says which of the three roads this was."""
    delivery.set_os_armed(False)

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == []
    assert clipboard.written == [PAYLOAD]
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert any("auto-insert suppressed: disarmed" in e.text for e in delivery.harness_log)
    assert _said(view, "click the chat box and press Ctrl+V yourself")
    assert _flash(view) == (PASTE_FLASH_TEXT, True)


# -- waiting for the click's activation ------------------------------------------


async def test_the_paste_waits_until_the_foreground_is_no_longer_ours(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """The click is an activation REQUEST, granted asynchronously, and a Ctrl+V
    that overtakes it lands in whatever held focus a moment ago. So the delivery
    asks the OS who has the foreground until the answer stops being us."""
    delivery.set_own_window(OUR_WINDOW)
    ops.foreground = [OUR_WINDOW, OUR_WINDOW, BROWSER_WINDOW]

    await delivery.copy_outbound(PAYLOAD)

    assert ops.foreground_reads == 3  # asked until the answer changed, then stopped
    assert ops.events == [*FOCUS, "paste"]
    assert delivery.loop_state is LoopState.WAIT_SEND


async def test_a_foreground_that_never_moves_pastes_anyway_once_the_budget_runs_out(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """The wait is a ceiling, not a precondition: refusing to deliver a payload
    that would probably have landed is worse than pasting on a stale reading,
    and the banner plus the retry button already cover a paste that goes
    nowhere."""
    delivery.set_own_window(OUR_WINDOW)
    ops.foreground = [OUR_WINDOW]  # ...and it stays ours for ever

    await delivery.copy_outbound(PAYLOAD)

    assert ops.foreground_reads == 3  # the whole budget, and not one ask more
    assert ops.events == [*FOCUS, "paste"]
    assert delivery.loop_state is LoopState.WAIT_SEND


async def test_with_no_window_of_our_own_the_wait_is_skipped_rather_than_spent(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """Nothing recorded is nothing to compare the foreground to, so there is no
    question to answer - and a shell that never called ``set_own_window`` must
    not pay the whole budget on every delivery for it."""
    await delivery.copy_outbound(PAYLOAD)

    assert ops.foreground_reads == 0
    assert ops.events == [*FOCUS, "paste"]


async def test_a_click_that_never_landed_never_waits_for_an_activation(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """No click, no activation to wait for - and nothing to paste into either."""
    delivery.set_own_window(OUR_WINDOW)
    ops.click_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert ops.foreground_reads == 0
    assert ops.events == FOCUS_REFUSED


# -- the opt-in Enter tap --------------------------------------------------------


async def test_auto_submit_taps_enter_after_a_paste_that_landed(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    host.preset = _preset(auto_submit=True)

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == [*FOCUS, "paste", "enter"]
    # Still WAIT_SEND: the tap is an attempt, and only the send gate's own
    # evidence says the send actually landed.
    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _flash(view) == (AUTO_SEND_FLASH_TEXT, False)


async def test_no_tap_without_the_opt_in(
    delivery: AutomationController, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    await delivery.copy_outbound(PAYLOAD)

    assert "enter" not in ops.events
    assert _flash(view) == (ENTER_FLASH_TEXT, False)


async def test_no_tap_when_the_paste_never_landed(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """An Enter into a chat box that holds nothing is exactly the accident the
    pasted-first order exists to prevent."""
    host.preset = _preset(auto_submit=True)
    ops.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == [*FOCUS, "paste"]


async def test_a_refused_tap_falls_back_to_asking_for_enter(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    """Nothing was typed, so the banner must keep asking rather than claim the
    send happened - and the log says whose Enter it is now."""
    host.preset = _preset(auto_submit=True)
    ops.enter_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _flash(view) == (ENTER_FLASH_TEXT, False)
    assert any("auto-submit could not type Enter" in e.text for e in delivery.harness_log)
    # ...and the send is theirs to make in the browser, so the browser keeps
    # the focus - a tap that did not take is not an auto-sent delivery.
    assert "focus" not in ops.events


# -- who holds the focus when the delivery is over -------------------------------


async def test_an_auto_sent_delivery_hands_the_foreground_back(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """Pasted AND sent leaves the user nothing to do in the browser, so the next
    thing worth watching is this window's rail - and alt-tabbing back to it by
    hand is not the user's job."""
    host.preset = _preset(auto_submit=True)
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == [*FOCUS, "paste", "enter", "focus"]
    assert ops.focused == [OUR_WINDOW]


async def test_a_service_can_refuse_to_take_the_foreground_back(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """``ServicePreset.snap_back`` off is the debugging aid: everything else
    about the delivery is unchanged, and the browser simply keeps the focus so
    the user can see for themselves where the click landed."""
    host.preset = _preset(auto_submit=True, snap_back=False)
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == [*FOCUS, "paste", "enter"]
    assert ops.focused == []


async def test_a_streamed_delivery_that_auto_sent_hands_it_back_too(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """The stream's auto-submit is the same tap on the same flag, so it cannot
    end up with a different answer to "whose window is this now"."""
    host.preset = _preset(auto_submit=True, delivery=DELIVERY_STREAM)
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events[-2:] == ["enter", "focus"]
    assert ops.focused == [OUR_WINDOW]


async def test_a_paste_still_waiting_on_the_users_enter_leaves_the_browser_focused(
    delivery: AutomationController, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    """">>> PRESS ENTER <<<" is an instruction to act over THERE, and stealing
    the foreground would make the user click back into the browser to obey a
    banner that has already stopped being true."""
    delivery.set_own_window(OUR_WINDOW)

    await delivery.copy_outbound(PAYLOAD)

    assert _flash(view) == (ENTER_FLASH_TEXT, False)
    assert "focus" not in ops.events
    assert ops.focused == []


async def test_a_paste_that_never_landed_leaves_the_browser_focused(
    delivery: AutomationController, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    """Same rule, harder case: the banner is asking for a Ctrl+V in the chat
    box, which is the one window this must not take the focus away from."""
    delivery.set_own_window(OUR_WINDOW)
    ops.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert _flash(view) == (PASTE_FLASH_TEXT, True)
    assert "focus" not in ops.events
    assert ops.focused == []


# -- the audible "your move" -----------------------------------------------------


async def test_the_click_that_focuses_the_chat_box_is_a_double_click(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """One click is spent waking the browser window; a page still activating
    never routes it to the input field, and the Ctrl+V lands nowhere the user
    can see. Safe here and only here - the box is empty, so there is no word
    for a double click to select."""
    await delivery.copy_outbound(PAYLOAD)

    assert ops.events[:2] == FOCUS


async def test_both_focus_clicks_land_where_the_service_aims_them(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """A chat box that is clickable end to end takes its click where the
    service says (screen.profile's click points) - and the reinforcing second
    click has to land in the same place, or it would undo the first."""
    box = ScreenRegion(1100, 800, 601, 41)
    host.on_screen[TemplateKind.CHATBOX_ONGOING] = [box]
    host.profile.set_click_point(TemplateKind.CHATBOX_ONGOING, 25, 0)

    await delivery.copy_outbound(PAYLOAD)

    aimed = ScreenRegion(1250, 800, 1, 1)
    assert ops.clicked == [aimed, aimed]


async def test_the_whole_window_fallback_keeps_its_centre_click(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """With no chat box on screen the target is the region the USER drew, which
    no per-picture click point describes."""
    host.profile.set_click_point(TemplateKind.CHATBOX_ONGOING, 25, 0)

    await delivery.copy_outbound(PAYLOAD)

    assert ops.clicked == [CHAT_REGION, CHAT_REGION]


async def test_a_refused_first_click_is_never_followed_by_a_second(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """A click the OS would not take is the one signal that the target is not
    clickable at all, so the sequence stops rather than hammering it."""
    ops.click_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == FOCUS_REFUSED


async def test_a_stalled_loop_sounds_the_alarm_when_the_service_asked_for_one(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, alarm: RecordingAlarm
) -> None:
    """MANUAL_INSERT is the loop saying "the Ctrl+V is yours" to a user who may
    not be looking. The hook is on the state, not on this delivery - which is
    what keeps the other eight roads into an attention state from each needing
    their own beep."""
    host.preset = _preset(alert_sound=True)
    ops.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert alarm.calls[-1] == "arm:0"


async def test_the_repeat_interval_rides_the_preset(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, alarm: RecordingAlarm
) -> None:
    host.preset = _preset(alert_sound=True, alert_repeat_seconds=30)
    ops.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert alarm.calls[-1] == "arm:30"


async def test_a_service_without_the_alert_never_makes_a_sound(
    delivery: AutomationController, ops: ScriptedOps, alarm: RecordingAlarm
) -> None:
    """Off is the default and off means silent - including in the very state
    the alarm exists for."""
    ops.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert not any(call.startswith("arm") for call in alarm.calls)


async def test_leaving_the_attention_state_silences_the_alarm(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, alarm: RecordingAlarm
) -> None:
    """The user pasted it themselves and the send gate saw it: nothing is
    waiting on them any more, so neither is the noise."""
    host.preset = _preset(alert_sound=True, alert_repeat_seconds=5)
    ops.paste_lands = False
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
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, alarm: RecordingAlarm
) -> None:
    """An alarm still repeating into a closed app is a beep with nobody left to
    answer it."""
    host.preset = _preset(alert_sound=True, alert_repeat_seconds=5)
    ops.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)

    delivery.stop_alert()

    assert alarm.calls[-1] == "disarm"


# -- burst or stream -------------------------------------------------------------


async def test_a_stream_service_walks_a_long_payload_in_chunk_by_chunk(
    delivery: AutomationController,
    host: FakeHost,
    ops: ScriptedOps,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """One clipboard write and one Ctrl+V per chunk, in order, rejoining into
    exactly the payload that was handed over - and every chunk registered as our
    own write, or the watcher would ingest one back as a reply."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.copy_outbound(PAYLOAD)

    assert len(ops.pasted) > 1
    assert "".join(ops.pasted) == PAYLOAD
    # The whole payload lands first (it is what every manual recovery pastes),
    # then the chunks in order.
    assert clipboard.written == [PAYLOAD, *ops.pasted]
    for chunk in ops.pasted:
        assert delivery.self_writes.contains_text(chunk), chunk
    assert delivery.loop_state is LoopState.WAIT_SEND
    assert _flash(view) == (ENTER_FLASH_TEXT, False)


async def test_the_banner_counts_the_chunks_while_they_go_in(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    """The user is looking at the browser, so the count is the only thing saying
    a big payload is still going in rather than stuck."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.copy_outbound(PAYLOAD)

    total = len(ops.pasted)
    counted = [text for text, _retry in view.paste_flashes if "STREAMING" in text]
    assert counted == [stream_flash_text(n, total) for n in range(1, total + 1)]


async def test_a_short_payload_in_stream_mode_is_a_single_burst(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps, clipboard: FakeClipboard
) -> None:
    """Nothing to show progress about, so the stream costs one extra clipboard
    write and nothing else."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.copy_outbound("short")

    assert ops.events == [*FOCUS, "paste"]
    assert clipboard.written == ["short", "short"]


async def test_a_paste_service_sends_one_burst_however_long_the_payload(
    delivery: AutomationController, ops: ScriptedOps, clipboard: FakeClipboard
) -> None:
    """A payload far past the chunk size, delivered by a service that did not ask
    for streaming, behaves exactly as it did before streaming existed."""
    await delivery.copy_outbound(PAYLOAD * 4)

    assert ops.events == [*FOCUS, "paste"]
    assert clipboard.written == [PAYLOAD * 4]


async def test_a_failed_chunk_stops_the_stream_and_restores_the_whole_payload(
    delivery: AutomationController,
    host: FakeHost,
    ops: ScriptedOps,
    clipboard: FakeClipboard,
    view: FakeAutomationView,
) -> None:
    """The box now holds a fragment the user has to clear, so the clipboard has
    to hold the WHOLE message for the manual Ctrl+V that replaces it."""
    host.preset = _preset(delivery=DELIVERY_STREAM)
    ops.paste_lands = False

    await delivery.copy_outbound(PAYLOAD)

    assert ops.events == [*FOCUS, "paste"]  # stopped, rather than ploughing on
    assert clipboard.written[-1] == PAYLOAD
    assert clipboard.read_text() == PAYLOAD
    assert delivery.loop_state is LoopState.MANUAL_INSERT
    assert _flash(view) == (PASTE_FLASH_TEXT, True)
    assert _said(view, "holds a partial")


# -- when the clipboard provider will not take it --------------------------------


async def test_no_provider_hands_the_payload_to_the_shell_and_says_so(
    view: FakeAutomationView, host: FakeHost, ops: ScriptedOps
) -> None:
    """The write is this layer's; the FALLBACK is not (the TUI's OSC-52 escape is
    a Textual call and exists in no other shell). So the payload crosses back to
    whoever can still park it, and the user is told once."""
    automation = AutomationController(view=view, host=host, ops=ops)
    automation.set_calibration(AgentSlot.MASTER, CHAT_REGION)

    assert await automation.park_on_clipboard(PAYLOAD) is False

    assert host.parked_off_clipboard == [PAYLOAD]
    assert _said(view, "no clipboard backend")


async def test_a_stream_service_falls_back_to_one_burst_with_no_clipboard(
    view: FakeAutomationView, host: FakeHost, ops: ScriptedOps
) -> None:
    """Streaming needs a clipboard to write each chunk through: with none, the
    single burst of whatever the shell parked is all there is."""
    host.preset = _preset(delivery=DELIVERY_STREAM)
    automation = AutomationController(view=view, host=host, ops=ops)
    automation.set_calibration(AgentSlot.MASTER, CHAT_REGION)

    await automation.copy_outbound(PAYLOAD)

    assert ops.events == [*FOCUS, "paste"]
    assert host.parked_off_clipboard == [PAYLOAD]


async def test_clipboard_ok_is_the_callers_answer_not_a_re_reading(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """The seam is a parameter for a reason: a shell may have parked the payload
    somewhere this layer cannot see, and ``deliver`` is told, not asked."""
    host.preset = _preset(delivery=DELIVERY_STREAM)

    await delivery.deliver(PAYLOAD, clipboard_ok=False)

    assert ops.events == [*FOCUS, "paste"]  # one burst, though a stream was asked for


# -- retrying, and re-delivering -------------------------------------------------


async def test_the_retry_re_runs_the_whole_insert_against_the_pending_payload(
    delivery: AutomationController, ops: ScriptedOps, clipboard: FakeClipboard
) -> None:
    """Between the failure and the press the user may well have copied something
    of their own, so the retry parks the outbound again before it pastes."""
    ops.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    assert delivery.pending_insert == PAYLOAD
    clipboard.set_text("something the user copied while reading the error")
    ops.paste_lands = True

    await delivery.retry_insert()

    assert ops.events == [*FOCUS, "paste", *FOCUS, "paste"]
    assert ops.pasted == [PAYLOAD, PAYLOAD]
    assert delivery.loop_state is LoopState.WAIT_SEND


async def test_the_retry_re_runs_the_auto_submit_too(
    delivery: AutomationController, host: FakeHost, ops: ScriptedOps
) -> None:
    """A retry that pasted but left the message sitting there would be a
    different flow than the one it stands in for."""
    host.preset = _preset(auto_submit=True)
    ops.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    ops.paste_lands = True

    await delivery.retry_insert()

    assert ops.events[-4:] == [*FOCUS, "paste", "enter"]


async def test_the_retry_does_nothing_before_anything_has_been_copied(
    delivery: AutomationController, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    await delivery.retry_insert()

    assert ops.events == []
    assert _said(view, "nothing to re-insert")


async def test_the_retry_refuses_while_disarmed(
    delivery: AutomationController, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    """DISARMED is a promise that nothing here clicks or types, and a button is
    not an exemption from it - the toast names the switch that is."""
    ops.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    delivery.set_os_armed(False)
    ops.events.clear()

    await delivery.retry_insert()

    assert ops.events == []
    assert _said(view, "press F5 to arm")


async def test_the_retry_refuses_while_the_auto_copy_flow_runs(
    delivery: AutomationController, ops: ScriptedOps, view: FakeAutomationView
) -> None:
    """The flow is driving the mouse through a scroll-and-hover hunt, and shoving
    a focus click through the middle of it wrecks both."""
    ops.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)
    delivery.flow_running = True
    ops.events.clear()

    await delivery.retry_insert()

    assert ops.events == []
    assert _said(view, "driving the mouse")


async def test_a_session_reset_forgets_what_there_was_to_retry(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """/new tears the session down, and the last outbound belonged to it."""
    ops.paste_lands = False
    await delivery.copy_outbound(PAYLOAD)

    delivery.forget_pending_insert()

    assert delivery.pending_insert is None


async def test_parking_the_outbound_touches_nothing_else(
    delivery: AutomationController, ops: ScriptedOps, clipboard: FakeClipboard
) -> None:
    """Stage one of the `c` re-copy: the payload is back where a Ctrl+V of the
    user's own can reach it, the mouse never moved, and the rail did not budge -
    nothing about the browser round trip has changed."""
    before = delivery.loop_state

    await delivery.park_outbound(PAYLOAD)

    assert ops.events == []
    assert clipboard.written == [PAYLOAD]
    assert delivery.self_writes.contains_text(PAYLOAD)
    assert delivery.loop_state is before


async def test_parking_leaves_the_pending_payload_alone(
    delivery: AutomationController, ops: ScriptedOps
) -> None:
    """It is what the retry button would re-deliver, and re-copying the payload
    that is already the pending one changes nothing about that."""
    ops.paste_lands = False
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
