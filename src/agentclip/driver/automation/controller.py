"""AutomationController: what the screen automation IS, minus everything it DOES.

Sibling of :class:`~agentclip.shell.app.controller.SessionController`: the state
behind what AgentClip does *to* the browser chat window, lifted out of any one
shell so both can drive the identical loop, and talking to the UI only through
the :class:`~agentclip.driver.automation.view.AutomationView` port.

Phase 6.2 of docs/design/ui-monitor.md took the decisions out of it. What each
state of the loop DOES is a recipe (``automation/recipes/``), where each answer
GOES is one pure table (``recipes/transitions.py``), and one asyncio task walks
between them (``recipes/loop.py``). What is left here is what those need to
exist: the **loop task** (owned here, started and stopped here and nowhere
else), the **slot pointers** and their calibration, the **armed switch**
(``automation/armed.py`` - policy, so it stays local, section 2.3), the
**wiring** (the view to paint through, the host to ask, the monitor to act on),
and the **narration** (``automation/narration.py``) - where ``set_loop_state``
is still the one door every state change comes through, and where a SHELL that
moves the loop itself (a session reset, a lost link, a harvested reply)
pre-empts it.

Everything on the far side of the screen is one object below: the
:class:`~agentclip.driver.monitor.protocol.UIMonitor` owns the poll loop, the
trackers, the generation stamp, the mouse, the keyboard, the clipboard, every
SEARCH, and the two consecutive-tick counts the finish decision reads.

**No threads.** The loop pulls (``tick = await ui.observe()``) instead of being
pushed into, so every writer below is on the event-loop thread and there is
nothing left to serialize: the ``threading`` import, the tick lock and the five
``consume_*`` methods all went with phase 2 (4.4, 4.7). The one thing still
delivered on the monitor's own thread is the DETECTION READOUT
(``automation/readout.py``), which paints what a tick SAYS and decides nothing
at all - exactly what the ``AutomationView`` contract has always allowed.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

from agentclip.config import ServicePreset
from agentclip.driver.automation import readout
from agentclip.driver.automation.alerts import AttentionAlarm
from agentclip.driver.automation.armed import ArmedSwitch
from agentclip.driver.automation.finish import (
    SEND_READY_ARMED,
    SEND_READY_HOLDING,
    SEND_READY_RESTING,
    SEND_READY_SEEN,
    SendGate,
)
from agentclip.driver.automation.flow import ELEMENT_CLICK_SETTLE_S
from agentclip.driver.automation.harness_log import HarnessEntry
from agentclip.driver.automation.host import AutomationHost, NullHost
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.machine import (
    CropFn,
    MonitorLike,
    accept_all,
    drop_capture,
    nothing_captured,
    uncut,
)
from agentclip.driver.automation.narration import LoopNarration
from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.automation.recipes import acts, auto_insert, windows
from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.loop import RECIPES, run_loop
from agentclip.driver.automation.recipes.outcomes import REASONS, Outcome
from agentclip.driver.automation.recipes.reply import ReplyWatch
from agentclip.driver.automation.recipes.transitions import TRANSITIONS
from agentclip.driver.automation.view import AutomationView
from agentclip.driver.clip.watcher import SelfWriteSet
from agentclip.driver.monitor.protocol import Tick
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import Sighting
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot, SlotCalibration, new_slots


class AutomationController:
    """The automation's state, driving one :class:`AutomationView`."""

    def __init__(
        self,
        view: AutomationView,
        *,
        monitor: MonitorLike,
        host: AutomationHost | None = None,
        services: Mapping[str, str] | None = None,
        accepts: Callable[[str], bool] | None = None,
        on_clipboard_captured: Callable[[str], None] | None = None,
        crop_elements: CropFn | None = None,
        has_appearance: Callable[[TemplateKind], bool] | None = None,
        alarm: AttentionAlarm | None = None,
    ) -> None:
        self._view = view
        # The other half of the seam: what the recipes ASK a shell, and the
        # machine they act on - a whole process boundary in waiting (§3).
        self._host: AutomationHost = host if host is not None else NullHost()
        self._monitor = monitor
        # The armed switch, and the clipboard watcher it takes away and gives back.
        self._armed = ArmedSwitch(monitor, view)
        # Every user-drawn calibration, one per agent slot: where a window IS,
        # not what one conversation said, so it survives /new (the pointers reset).
        self._slots = new_slots()
        self._calibrating: AgentSlot = AgentSlot.MASTER
        self._live: AgentSlot = AgentSlot.MASTER
        # The service each browser WINDOW is pointed at, keyed by whatever the
        # shell calls its windows - opaque strings here, deliberately.
        self._services: dict[str, str] = dict(services or {})
        # The two callbacks either side of the watcher (the monitor owns the rest
        # of it, §2.11). The protocol pre-filter is passed in because
        # ``agentclip.protocol`` is above this layer (tests/test_layering.py).
        self._accepts: Callable[[str], bool] = accepts if accepts is not None else accept_all
        self._on_capture: Callable[[str], None] = (
            on_clipboard_captured if on_clipboard_captured is not None else drop_capture
        )
        # The one question the loop cannot answer for itself: what the LIVE
        # window's service has a capture of.
        self._has_appearance: Callable[[TemplateKind], bool] = (
            has_appearance if has_appearance is not None else nothing_captured
        )
        self._crop_elements: CropFn = crop_elements if crop_elements is not None else uncut
        # Which FINISH detectors the run reports, as the shell's retarget last
        # answered. Readout only since phase 2: the fold reads a tick's probes.
        self._active_detectors: tuple[str, ...] = ()
        # Where the loop is, why it went there and what that sounds like - one
        # object, because they may never disagree. ``set_loop_state`` and
        # ``enter`` are its only writers.
        self._narration = LoopNarration(
            view,
            live_preset=self.live_preset,
            alarm=alarm if alarm is not None else AttentionAlarm(),
        )
        # The shell's OWN window, recorded by whoever can see that the user is
        # provably interacting with the tool. OS state, so it lives here.
        self._own_window: int | None = None
        # -- the loop ----------------------------------------------------------
        # Everything the recipes read and write, and the task that walks between
        # them - the only thing here that ever runs concurrently with anything.
        self._ctx = RecipeContext(self)
        self._loop_task: asyncio.Task[None] | None = None
        # The monitor's READER hooks: paint-only, the first two, and never
        # unhooked - this object and its monitor share a lifetime.
        monitor.subscribe(self._on_tick)
        monitor.on_frame(self._on_frame)
        monitor.on_clip(self._on_clip)

    # == the wiring, for the recipes ==========================================

    @property
    def monitor(self) -> MonitorLike:
        """The machine: the screen, the mouse, the keyboard and the clipboard."""
        return self._monitor

    @property
    def view(self) -> AutomationView:
        """The paint port."""
        return self._view

    @property
    def host(self) -> AutomationHost:
        """The handful of things the automation still has to ASK a shell."""
        return self._host

    def has_appearance(self, kind: TemplateKind) -> bool:
        """Has the LIVE window's service a capture of ``kind``?"""
        return self._has_appearance(kind)

    # == the loop task ========================================================

    def start_loop(self) -> None:
        """Start the one task that runs the loop. Idempotent, and a no-op with no
        event loop running - the doors below then drive a recipe directly."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop_task = loop.create_task(run_loop(self._ctx))

    def stop_loop(self) -> None:
        """Cancel the loop task. Safe to call twice, and safe when none started."""
        task, self._loop_task = self._loop_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _drive(self, state: LoopState) -> Outcome:
        """Run ONE recipe here and now, and take the transition it earns - what a
        caller that is not the loop task gets (a suite; a shell with no loop yet)."""
        outcome = await RECIPES[state](self._ctx)
        self.enter(TRANSITIONS[state, outcome], self._ctx.take_reason(outcome))
        return outcome

    # == the ARMED switch and the clipboard watcher (``automation/armed.py``) ==

    @property
    def os_armed(self) -> bool:
        """May the tool touch the machine right now?"""
        return self._armed.armed

    def set_os_armed(self, target: bool | None) -> bool:
        """Arm or disarm, move the watcher, repaint - and hand back what is in force."""
        return self._armed.set(target)

    @property
    def watching(self) -> bool:
        """Is the monitor polling the clipboard right now?"""
        return self._armed.watching

    def start_input(self) -> None:
        """A session wants the clipboard watched (``ChatView.start_input``)."""
        self._armed.start_input()

    def stop_input(self) -> None:
        """Stop watching (``ChatView.stop_input``), without waiting for it."""
        self._armed.stop_input()

    def _on_clip(self, text: str) -> None:
        """One clipboard change the watcher accepted, on its way to the shell."""
        if self._accepts(text):
            self._on_capture(text)

    @property
    def self_writes(self) -> SelfWriteSet:
        """Hashes of every clipboard write the monitor made on our behalf."""
        return self._monitor.self_writes

    # == the DETECTION readout ================================================
    # What a tick SAYS, painted where the user reads it (``automation/readout.py``).
    # No decision is taken here - those are the recipes', off ``observe()`` - so
    # these hooks may arrive on the monitor's own thread, which is exactly what
    # the ``AutomationView`` contract has always allowed.

    def _on_tick(self, tick: Tick) -> None:
        """One observation of the chat window, as lines of readout."""
        readout.paint_tick(self._view, tick)

    def _on_frame(
        self, scene: RegionImage, sightings: Mapping[TemplateKind, Sighting | None]
    ) -> None:
        """One tick's pictures, cut to panel size by the shell's own renderer."""
        readout.paint_frame(self._view, self._crop_elements, scene, sightings)

    @property
    def detector_generation(self) -> int:
        """Which run of the monitor the live window is being watched in."""
        return self._monitor.generation

    def reset_trackers(self) -> None:
        """Make every live tracker forget the frames it has seen - the debounce
        only, never the verdicts."""
        self._monitor.reset_trackers()

    @property
    def active_detectors(self) -> tuple[str, ...]:
        """Which FINISH detectors the current run reports, in the fixed
        busy -> idle -> stale build order."""
        return self._active_detectors

    @active_detectors.setter
    def active_detectors(self, names: tuple[str, ...]) -> None:
        self._active_detectors = tuple(names)

    # == the loop's narration (``automation/narration.py``) ===================
    # The rail, the log and the alarm move together or not at all.

    @property
    def loop_state(self) -> LoopState:
        """Where the browser-automation loop is right now."""
        return self._narration.state

    @property
    def harness_log(self) -> deque[HarnessEntry]:
        """The decision log itself, oldest first - the live deque, not a copy."""
        return self._narration.log

    def enter(self, state: LoopState, reason: str) -> None:
        """Move the loop, the way the LOOP moves it: no pre-empt."""
        self._narration.moved(state, reason)

    def set_loop_state(self, state: LoopState, reason: str) -> None:
        """Move the loop, the way a SHELL moves it: over the top of whatever recipe
        is running."""
        self._narration.moved(state, reason)
        # Outside the "did it change?" test on purpose: a shell that asks for the
        # state the loop is already in still means "start that over" - the second
        # of two back-to-back deliveries is exactly that ask.
        self._ctx.preempt.set()

    def log_harness(self, kind: str, text: str) -> None:
        """Append one decision to the harness log (`/log`)."""
        self._narration.entry(kind, text)

    def sound_attention_once(self) -> None:
        """One uh-oh for a re-sync the loop never hears about."""
        self._narration.chime()

    def stop_alert(self) -> None:
        """Silence the alarm for good (shutdown)."""
        self._narration.hush()

    # == the turn: what the recipes are deciding about ========================
    # Windows onto ``RecipeContext``: the state belongs to the run, and a shell
    # reads it at the address it always did.

    @property
    def reply(self) -> ReplyWatch | None:
        """The reply currently being waited for - the send gate, the detector
        verdicts and the auto-copy arm - or None when nothing is outstanding."""
        return self._ctx.reply

    @property
    def flow_running(self) -> bool:
        """Is the auto-copy flow driving the mouse right now?"""
        return self._ctx.flow_running

    @flow_running.setter
    def flow_running(self, value: bool) -> None:
        self._ctx.flow_running = value

    @property
    def prose_window(self) -> bool:
        """May the harvest about to arrive be ingested even with no CLIP blocks?"""
        return self._ctx.prose_window

    @property
    def pending_insert(self) -> str | None:
        """What a retry would re-deliver; None before the first outbound."""
        return self._ctx.pending_insert

    def forget_pending_insert(self) -> None:
        """/new: nothing is left for the retry button to re-deliver."""
        self._ctx.pending_insert = None

    def open_reply_gate(self) -> None:
        """An outbound just went into the chat: a reply is now due, so the
        detectors may arm and fire until it has been harvested. Idempotent."""
        self._ctx.open_reply_gate()

    def close_reply_gate(self) -> None:
        """No reply is outstanding any more, so nothing may move the mouse."""
        self._ctx.close_reply_gate()

    def forget_verdicts(self) -> None:
        """Drop everything a rebuilt detector set makes obsolete - the verdicts,
        not the arm (recapturing a button does not un-observe a generation)."""
        if self._ctx.reply is not None:
            self._ctx.reply.forget_verdicts()
        self._active_detectors = ()

    def reset_finish_trigger(self) -> None:
        """Forget every detector verdict AND the auto-copy arm."""
        if self._ctx.reply is not None:
            self._ctx.reply.reset_trigger()

    def end_flow(self) -> None:
        """The auto-copy flow finished (or failed, or was cancelled): lift the
        suspension and throw away the frames the flow itself produced."""
        self._ctx.prose_window = False
        self._ctx.flow_running = False
        self.reset_trackers()
        if self._ctx.reply is not None:
            self._ctx.reply.stale_diff = None

    # -- the send gate's readout and its budgets -------------------------------

    def send_gate_line(self) -> str:
        """The send line, re-derived from the gate rather than stored."""
        gate = None if self._ctx.reply is None else self._ctx.reply.gate
        if gate is SendGate.HOLD:
            return SEND_READY_HOLDING
        if gate is SendGate.SEEN:
            return SEND_READY_SEEN
        if self._has_appearance(TemplateKind.SEND_READY):
            return SEND_READY_ARMED
        return SEND_READY_RESTING

    # == the slot pointers ====================================================

    @property
    def live_slot(self) -> AgentSlot:
        """Which slot the automation is driving (paste click, monitor, auto-copy)."""
        return self._live

    @property
    def calibrating_slot(self) -> AgentSlot:
        """Which slot the sidebar/settings surface is configuring."""
        return self._calibrating

    @property
    def live(self) -> SlotCalibration:
        """The driven slot's calibration."""
        return self._slots[self._live]

    @property
    def calibrating(self) -> SlotCalibration:
        """The configured slot's calibration."""
        return self._slots[self._calibrating]

    def calibration(self, slot: AgentSlot) -> SlotCalibration:
        """One slot's calibration."""
        return self._slots[slot]

    def set_calibration(self, slot: AgentSlot, region: ScreenRegion | None) -> None:
        """Adopt (or forget) the box the user drew around a chat window."""
        self._slots[slot].chat_region = region

    def select_live_slot(self, slot: AgentSlot) -> None:
        """Point the automation at another chat window."""
        self._live = slot

    def select_calibrating_slot(self, slot: AgentSlot) -> None:
        """Point the configuration surface at another chat window. Never moves
        ``live``: selecting a tab must not retarget the automation."""
        self._calibrating = slot

    # == a service per window =================================================

    def service_of(self, window: str) -> str:
        """The service key a window is pointed at, or ``""`` for an unknown one."""
        return self._services.get(window, "")

    def set_service(self, window: str, key: str) -> None:
        """Point one window at a service."""
        self._services[window] = key

    def services(self) -> dict[str, str]:
        """Every window's service key - a copy, safe to iterate while writing."""
        return dict(self._services)

    def live_preset(self) -> ServicePreset:
        """The preset of the window the automation is driving."""
        return self._host.live_preset()

    def captured(self, slot: AgentSlot) -> tuple[TemplateKind, ...]:
        """Which appearances the MONITOR holds for ``slot``'s service (§11.3).

        The kinds and nothing else: this side owns no pictures, so "may I click
        the new-chat button?" is answered out of the monitor's ``Watched``
        rather than out of a profile store on this machine.
        """
        return self._host.captured_for(slot)

    def live_captured(self) -> tuple[TemplateKind, ...]:
        """The same question about the window the automation is driving."""
        return self._host.captured_for(self._live)

    # == the OS-acting primitives (``recipes/acts.py``) =======================
    # The doors a shell still reaches them through, and the handle they aim at.

    @property
    def own_window(self) -> int | None:
        """The shell's own window handle, or None if none was ever recorded."""
        return self._own_window

    def set_own_window(self, handle: int | None) -> None:
        """Record where "back to the tool" is."""
        if handle is not None:
            self._own_window = handle

    async def snap_back_after_click(self) -> bool:
        """Let a click in the browser register, then take the foreground back."""
        return await acts.snap_back_after_click(self._ctx)

    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        """Every place ``kind`` is on screen right now, in absolute coordinates."""
        if scene is not None and slot is not None:
            raise ValueError("find_all takes a slot or a captured scene, never both")
        return list(await self._monitor.find_all(kind))

    async def click_profile_element(
        self, slot: AgentSlot, kind: TemplateKind, *, settle_s: float = ELEMENT_CLICK_SETTLE_S
    ) -> ElementClick:
        """The one programmatic click on a service appearance (``recipes/acts.py``)."""
        return await acts.click_profile_element(self._ctx, slot, kind, settle_s=settle_s)

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        """Click the copy button until the clipboard changes (``recipes/acts.py``)."""
        return await acts.verified_copy_click(self._ctx, target)

    # == the delivery, as a shell asks for it (``recipes/auto_insert.py``) ====
    # The doors onto the sequence, and what each one is allowed to refuse.

    async def copy_outbound(self, text: str) -> None:
        """Deliver one outbound payload: post it, and see it inserted."""
        await self._insert(text, REASONS[Outcome.PAYLOAD_READY])

    async def retry_insert(self) -> None:
        """Do the insert again: park the last payload back on the clipboard,
        click the chat box, settle, paste, and auto-submit if the service does."""
        text = self._ctx.pending_insert
        if text is None:
            self._view.notify("nothing to re-insert yet - no outbound payload has been copied")
            return
        if not self.may_redeliver():
            return
        # Back to AUTO_INSERT even though the user asked for this by hand: the
        # rail says what the automation is DOING, and what it is about to do is
        # the auto insert over again.
        await self._insert(text, "the insert is being retried from the sidebar")

    async def _insert(self, text: str, reason: str) -> None:
        """Post one payload and let the loop insert it - or insert it here."""
        payload = self._ctx.post(text)
        if self.loop_state is LoopState.DISCONNECTED:
            return
        self.set_loop_state(LoopState.AUTO_INSERT, reason)
        if self._loop_task is None or self._loop_task.done():
            await self._drive(LoopState.AUTO_INSERT)
            return
        await payload.done.wait()

    async def park_outbound(self, text: str) -> None:
        """Put the last outbound back on the clipboard and stop there."""
        await self.park_on_clipboard(text)

    async def park_on_clipboard(self, text: str) -> bool:
        """Put the whole outbound on the clipboard, as a self-write. False = no
        real backend, so the shell was handed the payload to park however it can."""
        return await auto_insert.park_on_clipboard(self._ctx, text)

    async def deliver(self, text: str, *, clipboard_ok: bool) -> bool:
        """The OS half of one delivery, and the move it earns. True only when the
        payload really landed in the box."""
        outcome = await auto_insert.deliver(self._ctx, text, clipboard_ok=clipboard_ok)
        self.enter(TRANSITIONS[LoopState.AUTO_INSERT, outcome], self._ctx.take_reason(outcome))
        return outcome is Outcome.PASTED

    def may_redeliver(self) -> bool:
        """May the `c` double tap (or the retry button) escalate to a real
        delivery right now? The two refusals, said in the words that name the way
        out of each."""
        if not self.os_armed:
            self._view.notify(
                "disarmed - AgentClip may not click or type: press F5 to arm, or paste "
                "the payload into the chat yourself",
                severity="warning",
            )
            return False
        if self._ctx.flow_running:
            self._view.notify("the auto-copy flow is driving the mouse - let it finish first")
            return False
        return True

    # == the harvest, as a shell asks for it ==================================

    async def run_auto_copy_flow(self, flow: Callable[[], Awaitable[None]] | None = None) -> None:
        """One harvest, inside the flow-suspension bracket - for a caller that is
        not the loop. With a loop running nobody calls this: the AUTO_COPY recipe
        IS the harvest, and it brings its own bracket (``recipes/auto_copy.py``)."""
        self._ctx.flow_running = True
        try:
            if flow is None:
                await self._drive(LoopState.AUTO_COPY)
            else:
                await flow()
        finally:
            self.end_flow()

    async def auto_copy_flow(self) -> None:
        """The harvest itself, and the move it earns (``recipes/auto_copy.py``)."""
        await self._drive(LoopState.AUTO_COPY)

    # == moving the automation between browser windows ========================

    async def start_browser_chat(self, slot: AgentSlot) -> bool:
        """Open a fresh browser chat in ``slot`` and make it the live one, or do
        nothing at all (``recipes/windows.py``)."""
        return await windows.start_browser_chat(self._ctx, slot)

    def end_browser_chat(self) -> None:
        """Hand the automation back to the master chat when a delegation ends."""
        windows.end_browser_chat(self._ctx)
