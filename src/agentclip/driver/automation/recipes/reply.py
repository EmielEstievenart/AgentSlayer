"""The reply we are waiting for: the send gate, the detector verdicts, and the
one decision they add up to.

Everything here was ``AutomationController.evaluate_finish`` and its send-gate
block (docs/design/ui-monitor.md §6.2's table). It moved for the reason the
whole phase moved: all of it is POLICY about a screen - what a run of two ticks
is worth, whether a still chat is a finished one, which evidence is proof and
which is inference - and none of it is anything the machine can answer.

One object per outstanding reply, opened by the state that has one to wait for
(``WAIT_SEND`` / ``MANUAL_INSERT``, both through ``ctx.open_reply_gate``) and
dropped the moment the harvest starts. That lifetime is exactly the old
``awaiting_pasted_reply`` flag, spelled as the thing it was always guarding:
calibration is not consent, and a tab with no reply outstanding has no watch and
therefore cannot arm, fire, or move the mouse.

``copy_armed`` lives here rather than on the controller for the same reason: it
is the trigger's memory of ONE reply, and a brain that kept it across replies
would fire the harvest at a chat nobody asked anything.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from agentclip.driver.automation.finish import (
    SEND_READY_OVERRIDDEN,
    SEND_READY_RELEASED,
    SEND_READY_STUCK,
    SEND_READY_TIMEOUT,
    SendGate,
    busy_verdict,
    idle_verdict,
    stale_verdict,
)
from agentclip.driver.automation.harness_log import KIND_GATE, KIND_TRIGGER
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.monitor.protocol import Tick
from agentclip.driver.screen.profile import TemplateKind

if TYPE_CHECKING:
    from agentclip.driver.automation.recipes.context import RecipeContext


class GateEvent(Enum):
    """What one look for the ready-to-send button did to the gate."""

    NOTHING = "nothing"  # not gating, or the clock simply ran on
    SEEN = "seen"  # on screen: there is unsent text in the box
    RELEASED = "released"  # seen, then gone - the user's Enter
    TIMED_OUT = "timed_out"  # it never appeared, or never went away


class FinishEvent(Enum):
    """What one tick did to the finish decision."""

    NOTHING = "nothing"
    ARMED = "armed"  # the model is visibly generating; the trigger is armed
    FINISHED = "finished"  # every live detector agrees, and we may click
    NO_HARVEST = "no_harvest"  # finished, but the copy is the user's


class ReplyWatch:
    """One outstanding reply: the gate holding finish detection back, the latest
    verdict per detector, and whether the trigger has seen the model generating.

    Fed one whole :class:`~agentclip.driver.monitor.protocol.Tick` at a time, by
    whichever recipe is running - which is what makes the tick-closing rule the
    old push shape needed disappear: there is no half-reported tick to fold any
    more, because a tick IS all of the probes.
    """

    def __init__(self, *, gate: SendGate | None) -> None:
        self.gate = gate
        self.gate_ticks = 0
        self.copy_armed = False
        # Why the last event happened, in the user's language: the evidence that
        # armed the trigger, or the reason the harvest is theirs. Kept on the
        # watch rather than pushed straight at the context because an ARM does
        # not always move the loop - inside ``WAIT_GENERATE`` it is a note, not a
        # transition - and a reason nobody consumed must not narrate the NEXT
        # move (see ``RecipeContext.say``).
        self.why = ""
        self.busy_seen = False
        self.idle_seen = False
        self.stale_seen = False
        self.busy_finished: bool | None = None
        self.idle_finished: bool | None = None
        self.stale_finished: bool | None = None
        # The raw per-frame fact each icon detector's last probe carried: did
        # THAT frame's own search see the model generating? A de-bounced False
        # verdict does not say so - a freshly reset tracker reports "generating"
        # for its whole grace period on no evidence at all, and the reset happens
        # at the paste.
        self.busy_generating_now = False
        self.idle_generating_now = False
        self.stale_diff: float | None = None

    # -- what an icon SAW, which is the only single-frame evidence there is ----

    def icon_evidence(self) -> bool:
        """Did an icon detector's LATEST FRAME see the model generating?

        The strongest evidence a tick produces, and the only kind two rules trust
        on one frame: a reasoning appearance on screen is something no still,
        unanswered chat can fake. Staleness deliberately does not count - a
        blinking caret changes a region too.
        """
        return self.busy_generating_now or self.idle_generating_now

    # -- the ready-to-send gate ------------------------------------------------

    def feed_gate(self, ctx: RecipeContext, tick: Tick) -> GateEvent:
        """Fold one look for the ready-to-send button into the gate.

        Three answers, three jobs. FOUND while holding is the sighting the gate
        is waiting for. NOT FOUND *after* a sighting is the send itself - the
        button only exists while there is something to send, so its
        disappearance is the user's Enter. Anything else - not found before any
        sighting, a failed capture, or a button that goes on being found long
        after the send - only runs the clock down.

        BOTH phases are on a clock, on two very different budgets: the sighting
        should arrive within a second or two, while the SEEN phase is waiting for
        a human and may not expire on one who paused to read. The clock restarts
        at the transition, so a slow sighting does not eat the reading time.

        No debounce on the disappearance, deliberately: one dropped frame costs
        an early release, which is exactly the behaviour that shipped before this
        gate existed, while one dropped frame the other way would hold a session
        open on a button that is already gone.
        """
        if self.gate is None:
            return GateEvent.NOTHING
        # A tick that captured nothing searched nothing, so its map is empty -
        # and the gate still has to hear about it, or a browser that has stopped
        # being capturable would hold the session open for ever.
        if not (tick.searched(TemplateKind.SEND_READY) or not tick.captured):
            return GateEvent.NOTHING
        found = tick.present(TemplateKind.SEND_READY)
        if found and self.gate is SendGate.HOLD:
            # The one productive tick in the whole gate: the phase changes, so
            # the clock restarts rather than counting on into the next budget.
            self.gate = SendGate.SEEN
            self.gate_ticks = 0
            ctx.log_harness(
                KIND_GATE,
                "the ready-to-send button is on screen: there is unsent text in the "
                "chat box, so the send has not happened yet",
            )
            ctx.paint_send_gate()
            return GateEvent.SEEN
        self.gate_ticks += 1
        if found is False and self.gate is SendGate.SEEN:
            self.release(ctx)
            return GateEvent.RELEASED
        budget = (
            ctx.send_gate_seen_timeout_ticks
            if self.gate is SendGate.SEEN
            else ctx.send_gate_timeout_ticks
        )
        if self.gate_ticks >= budget:
            self.time_out(ctx, seen=self.gate is SendGate.SEEN)
            return GateEvent.TIMED_OUT
        return GateEvent.NOTHING

    def clear_gate(self) -> None:
        """Stop gating, without saying anything about why."""
        self.gate = None
        self.gate_ticks = 0

    def release(self, ctx: RecipeContext) -> None:
        """Seen, then gone: the user pressed Enter, so let the detectors go.

        Everything from here is the behaviour that shipped before the gate
        existed - and it starts from *here* rather than from the paste, which is
        why the trigger and every tracker's debounce are reset on the way out:
        the frames the gate held through show a chat box with an unsent message
        in it, and a streak built out of those describes the user typing.
        """
        self.clear_gate()
        self.reset_trigger()
        ctx.reset_trackers()
        ctx.log_harness(
            KIND_GATE,
            "the ready-to-send button was seen and is now gone: finish detection is "
            "released, and it starts from the send rather than from the paste",
        )
        ctx.view.hide_paste_flash()
        ctx.paint_send_gate(SEND_READY_RELEASED)

    def override(self, ctx: RecipeContext) -> None:
        """The model is generating, so the send already happened: let go.

        Deliberately NOT :meth:`release`: that resets the trigger and every
        tracker's debounce, on the grounds that the frames the gate held through
        show an unsent composer. Here the very frame doing the releasing is a
        genuine post-send reading of a generating chat, and throwing it away
        would cost the caller the arm it is about to take from it.
        """
        self.clear_gate()
        ctx.log_harness(
            KIND_GATE,
            "gate overridden by better evidence: a busy/idle icon is on screen, and "
            "nothing generates a reply to a message that was never sent",
        )
        ctx.paint_send_gate(SEND_READY_OVERRIDDEN)

    def time_out(self, ctx: RecipeContext, *, seen: bool) -> None:
        """Give up waiting on a button, and say so.

        The gate may delay a session; it may never deadlock one - and BOTH of its
        phases can be waited on for ever. Loudly, and with the two cases named
        apart, because the user's fix differs: one capture never matches and the
        other never stops.
        """
        self.clear_gate()
        ctx.paint_send_gate(SEND_READY_STUCK if seen else SEND_READY_TIMEOUT)
        what = (
            "the ready-to-send button never went away after the paste"
            if seen
            else "the ready-to-send button never appeared after the paste"
        )
        ctx.log_harness(
            KIND_GATE, f"gate timed out: {what} - finish detection is running as usual"
        )
        ctx.view.notify(
            f"{what} - finish detection is running as usual; recapture it in F2 if the "
            "chat has changed",
            severity="warning",
        )

    # -- the combined verdict --------------------------------------------------

    def forget_verdicts(self) -> None:
        """Drop everything a rebuilt detector set makes obsolete.

        Every tracker is rebuilt around the window's calibration as it stands
        NOW, so the verdicts they produced belong to detectors that no longer
        exist. The trigger's ARM survives deliberately: it records that the model
        was generating, which recapturing a button does not un-observe - which is
        why this is not :meth:`reset_trigger`.
        """
        self.busy_seen = self.idle_seen = self.stale_seen = False
        self.busy_finished = self.idle_finished = self.stale_finished = None
        self.stale_diff = None

    def reset_trigger(self) -> None:
        """Forget every detector reading AND the auto-copy arm.

        What a slot move, a session teardown and a released gate all make
        obsolete: verdicts describe a window (or a screen we produced ourselves),
        so carrying them across could fire the auto-copy at the wrong chat.
        """
        self.forget_verdicts()
        self.busy_generating_now = self.idle_generating_now = False
        self.copy_armed = False

    def feed_finish(self, ctx: RecipeContext, tick: Tick) -> FinishEvent:
        """Fold one tick's verdicts into one "the model stopped" decision.

        * ANY detector saying "generating" breaks the finished-streak. That
          includes a tracker still settling after a reset, whose "generating" is
          a default rather than a reading - biasing AWAY from "finished" on no
          evidence is exactly right.
        * A busy/idle detector that SAW its icon on the frame just probed
          (:meth:`icon_evidence`) ARMS the trigger immediately and stops the
          paste nag - a reasoning icon is evidence nothing else produces.
        * The STALE detector arms it only as part of a sustained large delta:
          ``send_arm_ticks`` consecutive ticks whose diff cleared
          ``send_arm_min_diff``, counted by the MONITOR and read off the tick
          (§2.2). A caret blinking in the composer is a CHANGING verdict too.
        * The trigger fires only when EVERY live detector says "finished" on two
          consecutive ticks (``Tick.changed_streak``, the monitor's other count).
        * A capture error (no verdict) breaks the streak but leaves the arm
          alone: one bad frame must not silently cancel an in-flight finish.

        Suspended while the READY-TO-SEND gate holds: the outbound is sitting in
        the chat box UNSENT, so every verdict about that screen is about a
        message nobody has asked anything with... unless an icon SEES the model
        generating on the frame just probed, which overrides the gate outright -
        nothing generates a reply to a message that was never sent.

        And when the app is DISARMED the decision is still reached, still shown,
        and simply lands on the state where the harvest is the user's. Detection
        is not what disarming turns off.
        """
        if tick.busy is not None:
            self.busy_seen = True
            self.busy_finished = busy_verdict(tick.busy)
            self.busy_generating_now = tick.busy.generating_now
        if tick.idle is not None:
            self.idle_seen = True
            self.idle_finished = idle_verdict(tick.idle)
            self.idle_generating_now = tick.idle.generating_now
        if tick.stale is not None:
            self.stale_seen = True
            self.stale_finished = stale_verdict(tick.stale)
            self.stale_diff = tick.stale.diff
        if self.gate is not None:
            if not self.icon_evidence():
                return FinishEvent.NOTHING
            self.override(ctx)
        verdicts: list[bool | None] = []
        if self.busy_seen:
            verdicts.append(self.busy_finished)
        if self.idle_seen:
            verdicts.append(self.idle_finished)
        if self.stale_seen:
            verdicts.append(self.stale_finished)
        if not verdicts:
            return FinishEvent.NOTHING
        if any(verdict is False for verdict in verdicts):
            if self.icon_evidence() or tick.stale_arm_streak >= ctx.send_arm_ticks:
                # WHICH evidence armed it is the whole difference between "a
                # reasoning icon is on screen" (proof) and "the region kept
                # changing a lot" (inference), and only the second one can be
                # fooled by a video the user has open.
                why = (
                    "a busy/idle icon shows the model generating"
                    if self.icon_evidence()
                    else f"{ctx.send_arm_ticks} sustained large frame deltas in a row "
                    f"(≥ {ctx.send_arm_min_diff:.2f})"
                )
                if not self.copy_armed:
                    ctx.log_harness(KIND_TRIGGER, f"auto-copy trigger armed: {why}")
                self.copy_armed = True
                # The send demonstrably happened - the Ctrl+V landed and the user
                # pressed Enter, so stop nagging them to.
                ctx.view.hide_paste_flash()
                self.why = why
                return FinishEvent.ARMED
            return FinishEvent.NOTHING
        if not all(verdict is True for verdict in verdicts) or not self.copy_armed:
            return FinishEvent.NOTHING
        if tick.changed_streak < 2:
            return FinishEvent.NOTHING
        if not ctx.os_armed:
            # The finish is real and everything above it stays true - which is
            # the point: disarming stops the ACTING, so the rail still tracks the
            # turn and simply lands on the state where the harvest is the user's.
            if ctx.state is not LoopState.MANUAL_COPY:
                ctx.view.notify(
                    "disarmed - the reply looks finished: copy it yourself, then press "
                    "i to ingest it (the watcher is off too)",
                    severity="warning",
                    timeout=8,
                )
            self.why = (
                "auto-copy suppressed: disarmed - the reply looks finished but the "
                "tool may not click, so copy it yourself and press i"
            )
            return FinishEvent.NO_HARVEST
        if not ctx.has_appearance(TemplateKind.COPY):
            # Finished, but there is no captured copy button to click. Display
            # only - the trigger stays exactly as armed as it always was.
            self.why = (
                "no copy button is captured for this service, so there is nothing "
                "to click (capture one in F2)"
            )
            return FinishEvent.NO_HARVEST
        self.copy_armed = False
        return FinishEvent.FINISHED
