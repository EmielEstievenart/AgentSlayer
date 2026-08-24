"""What a recipe is handed: the machine, the shell, and the run's own state.

Two halves, on purpose.

The first half is a WINDOW onto :class:`~agentclip.driver.automation.controller.AutomationController` -
the armed switch, the slot pointers and their calibration, the view to paint
through, the host to ask, the harness log to write to. None of that is the
loop's: it outlives any one turn, several of the pieces are what a shell sets,
and a recipe only ever reads them. Forwarding rather than reaching in keeps the
recipes readable as `focus / wait / click / observe` and keeps the controller
free to change its mind about where a value lives.

The second half is the RUN's own state, and it lives here rather than on the
controller because it belongs to the loop and to nothing else: the outbound
mailbox the delivery takes its payload off, the reply currently being waited for
(:class:`~agentclip.driver.automation.recipes.reply.ReplyWatch`), whether a
harvest is driving the mouse, the one-shot prose window, and the sentence the
next transition will be narrated with. Phase 2 moved every one of those off the
controller (docs/design/ui-monitor.md §6.2); the controller keeps a property
where a shell used to read one, and that property reads this object.

The context is built once, by the controller, and lives as long as it does - so
a recipe may hold no state of its own between runs, and the loop can drop a
recipe mid-await (a pre-empt, §2.9) without losing the turn it was in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentclip.config import ServicePreset
from agentclip.driver.automation.finish import (
    SEND_ARM_MIN_DIFF,
    SEND_ARM_TICKS,
    SEND_GATE_SEEN_TIMEOUT_TICKS,
    SEND_GATE_TIMEOUT_TICKS,
    SendGate,
)
from agentclip.driver.automation.harness_log import KIND_GATE
from agentclip.driver.automation.host import AutomationHost
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.recipes.outcomes import REASONS, Outcome
from agentclip.driver.automation.recipes.reply import ReplyWatch
from agentclip.driver.automation.view import AutomationView
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.slot import AgentSlot, SlotCalibration

if TYPE_CHECKING:
    from agentclip.driver.automation.controller import AutomationController, MonitorLike


@dataclass
class Payload:
    """One outbound payload on its way into the chat box, and the event that
    says it got there.

    The event is what lets ``copy_outbound`` stay a coroutine a shell awaits
    while the DELIVERY belongs to the loop: the caller posts and waits, the
    ``AUTO_INSERT`` recipe takes it off the mailbox and sets the event whichever
    way it ends (pasted, refused, or cancelled by a pre-empt). Nobody is left
    holding a future nothing will resolve.
    """

    text: str
    done: asyncio.Event = field(default_factory=asyncio.Event)


class RecipeContext:
    """One loop's whole world. Built by the controller, passed to every recipe."""

    def __init__(self, controller: AutomationController) -> None:
        self._controller = controller
        # -- the outbound mailbox (the IDLE recipe's whole wait) --------------
        # One slot, not a queue: a second payload while the first is still being
        # inserted would be a second conversation, and the shell serialises its
        # deliveries anyway (the GUI's exclusive group). ``arrived`` is what a
        # parked recipe waits on.
        self.payload: Payload | None = None
        self.arrived = asyncio.Event()
        # -- the turn ---------------------------------------------------------
        self.reply: ReplyWatch | None = None
        # Is the recipe running right now in the middle of DRIVING THE MACHINE?
        # The loop reads it to decide whether a pre-empt may drop the recipe where
        # it stands: a cancelled click, paste or ingest leaves the browser (and
        # the session) in a state nobody can reason about, so those stretches are
        # let finish and their outcome is thrown away instead. Everything else -
        # a recipe waiting on a tick, on the mailbox, or on the user - is dropped
        # the instant the shell speaks.
        self.acting = False
        self.flow_running = False
        self.prose_window = False
        self.pending_insert: str | None = None
        # Said once, never per copy: a focus click the OS refuses is a standing
        # fact about the machine (it is Windows-only), and a toast per outbound
        # would be noise about something the user already knows.
        self.region_click_warned = False
        # -- the loop's own plumbing ------------------------------------------
        # The sentence the NEXT transition is narrated with, when the recipe has
        # something more specific to say than its outcome's default.
        self._reason: str | None = None
        # Raised by ``set_loop_state`` - the shell speaking over the loop (a
        # session reset, a link lost, a harvest ingested). The loop drops the
        # recipe it is running (or, for the two that are driving the mouse, lets
        # it finish and throws its outcome away) and carries on from the state
        # the shell put it in.
        self.preempt = asyncio.Event()

    # == the controller's half =================================================

    @property
    def monitor(self) -> MonitorLike:
        return self._controller.monitor

    @property
    def view(self) -> AutomationView:
        return self._controller.view

    @property
    def host(self) -> AutomationHost:
        return self._controller.host

    @property
    def state(self) -> LoopState:
        return self._controller.loop_state

    @property
    def os_armed(self) -> bool:
        return self._controller.os_armed

    @property
    def live(self) -> SlotCalibration:
        """The driven slot's calibration - the box the user drew round the chat."""
        return self._controller.live

    def live_preset(self) -> ServicePreset:
        return self._controller.live_preset()

    def live_profile(self) -> ServiceProfile:
        return self._controller.live_profile()

    def has_appearance(self, kind: TemplateKind) -> bool:
        """Has the LIVE window's service a capture of ``kind``? The shell's own
        answer (its profile cache is keyed off its Config)."""
        return self._controller.has_appearance(kind)

    def log_harness(self, kind: str, text: str) -> None:
        self._controller.log_harness(kind, text)

    def paint_send_gate(self, text: str | None = None) -> None:
        """Repaint the send line - with an outcome, or from the gate's state."""
        self.view.paint_detection(
            TemplateKind.SEND_READY,
            text if text is not None else self._controller.send_gate_line(),
        )

    def copy_status(self, text: str) -> None:
        """Repaint the copy button's status line, keeping its captured size in
        front of whatever the harvest has to report.

        The first image's size, plus how many more are being ORed with it: a line
        that named one size while three pictures were being searched for would
        misreport the calibration.
        """
        templates = self.live_profile().variants(TemplateKind.COPY)
        size = ""
        if templates:
            extra = f" +{len(templates) - 1}" if len(templates) > 1 else ""
            size = f"{templates[0].width}×{templates[0].height}{extra} · "
        self.view.paint_detection(TemplateKind.COPY, f"{size}{text}")

    def select_live_slot(self, slot: AgentSlot) -> None:
        """Point the automation at another chat window."""
        self._controller.select_live_slot(slot)

    def reset_finish_trigger(self) -> None:
        """Forget every detector verdict and the auto-copy arm."""
        self._controller.reset_finish_trigger()

    def reset_trackers(self) -> None:
        self._controller.reset_trackers()

    def end_flow(self) -> None:
        """The harvest is over: lift the suspension and forget the frames it
        produced itself."""
        self._controller.end_flow()

    def enter(self, state: LoopState, reason: str) -> None:
        """Move the loop - the LOOP's own door, and the only caller is
        :func:`~agentclip.driver.automation.recipes.loop.run_loop`."""
        self._controller.enter(state, reason)

    def calibration(self, slot: AgentSlot) -> SlotCalibration:
        """One slot's calibration - the box the user drew around that window."""
        return self._controller.calibration(slot)

    @property
    def own_window(self) -> int | None:
        """The shell's own window handle: where "back to the tool" is."""
        return self._controller.own_window

    # -- the send gate's budgets ----------------------------------------------
    # ``MonitorSpec`` fields (§2.10): they are counted in TICKS, the monitor is
    # what a tick is, so the monitor is what carries them. Until a spec is set
    # there is nothing to read and the module's own defaults are the honest
    # answer - a loop nobody configured has no window to watch.

    @property
    def send_arm_ticks(self) -> int:
        spec = self.monitor.spec
        return SEND_ARM_TICKS if spec is None else spec.send_arm_ticks

    @property
    def send_arm_min_diff(self) -> float:
        spec = self.monitor.spec
        return SEND_ARM_MIN_DIFF if spec is None else spec.send_arm_min_diff

    @property
    def send_gate_timeout_ticks(self) -> int:
        spec = self.monitor.spec
        return SEND_GATE_TIMEOUT_TICKS if spec is None else spec.send_gate_timeout_ticks

    @property
    def send_gate_seen_timeout_ticks(self) -> int:
        spec = self.monitor.spec
        return (
            SEND_GATE_SEEN_TIMEOUT_TICKS if spec is None else spec.send_gate_seen_timeout_ticks
        )

    # == the run's half ========================================================

    @contextmanager
    def acting_on_the_machine(self) -> Iterator[None]:
        """Bracket the stretch of a recipe that may not be cancelled half way."""
        self.acting = True
        try:
            yield
        finally:
            self.acting = False

    # -- the outbound mailbox --------------------------------------------------

    def post(self, text: str) -> Payload:
        """Hand one payload to the loop and wake whatever is parked."""
        payload = Payload(text)
        self.payload = payload
        self.arrived.set()
        return payload

    def take(self) -> Payload | None:
        """Take the payload off the mailbox (the delivery's first act)."""
        payload, self.payload = self.payload, None
        self.arrived.clear()
        return payload

    async def wait_payload(self) -> None:
        """Park until there is something to insert."""
        await self.arrived.wait()

    # -- the reply gate --------------------------------------------------------

    def open_reply_gate(self) -> None:
        """An outbound is in the chat: a reply is now due, so the detectors may
        arm and fire until it has been harvested.

        Idempotent, and that matters: the delivery's own state
        (``WAIT_SEND`` / ``MANUAL_INSERT``) opens it on entry, and a shell that
        opened one itself - or a recipe re-run after a pre-empt - must not reset
        the turn a second time or narrate the gate twice.

        Everything the detectors observed BEFORE this instant is thrown away with
        the gate opening: the focus click, the synthetic Ctrl+V and an overlay
        closing are all large frame deltas about AgentClip's own doing, and a
        sustained run of them left standing would arm the trigger on a chat
        nobody has answered yet.
        """
        if self.reply is not None:
            return
        gate = SendGate.HOLD if self.has_appearance(TemplateKind.SEND_READY) else None
        self.reply = ReplyWatch(gate=gate)
        self.reset_trackers()
        if gate is SendGate.HOLD:
            self.log_harness(
                KIND_GATE,
                "holding finish detection until the send is seen - between the paste "
                "and your Enter the chat is still, and a still chat reads as finished",
            )
        self.paint_send_gate()

    def close_reply_gate(self) -> None:
        """No reply is outstanding any more, so nothing may move the mouse.

        Four moments close it: the harvest firing (that IS the reply), ``/new``
        tearing the session down, and the live slot moving in either direction.
        """
        self.reply = None
        self.paint_send_gate()

    # -- what the next transition will say ------------------------------------

    def say(self, reason: str) -> None:
        """Phrase the move this recipe is about to earn, in the user's language.

        One ``NOT_PASTED`` has four stories behind it and the rail draws one box
        for all of them; the log is where they are told apart, so a recipe that
        knows which road it took says so here.
        """
        self._reason = reason

    def take_reason(self, outcome: Outcome) -> str:
        """The words for this move: the recipe's own, else the outcome's."""
        reason, self._reason = self._reason, None
        return reason if reason is not None else REASONS[outcome]
