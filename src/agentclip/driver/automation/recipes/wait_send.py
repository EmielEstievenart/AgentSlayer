"""WAIT_SEND: the payload is in the chat box - wait for the user's Enter.

The send-gate block, lifted whole (docs/design/ui-monitor.md §6.2's table). What
it is watching for, in the order the evidence is trusted:

* **the model generating** (an icon on the frame just probed, or a sustained run
  of large deltas). Better evidence than the button, and what rescues a session
  whose button never yields a clean not-found frame - nothing generates a reply
  to a message that was never sent.
* **the ready-to-send button going away** after it was seen. The button only
  exists while there is something to send, so its disappearance IS the Enter.
* **the clock**, on both of the gate's phases. The gate may delay a session; it
  may never deadlock one - a timeout hands finish detection straight back and
  this recipe simply carries on watching for the generation.

A service with no captured send button is not gated at all: the watch's gate is
``None``, every look is a no-op, and the only way out is the first bullet - which
is exactly the behaviour that shipped before the gate existed.
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.automation.recipes.park import park
from agentclip.driver.automation.recipes.reply import FinishEvent, GateEvent


async def run(ctx: RecipeContext) -> Outcome:
    # The payload is out, so a reply is due: this is the state that says so, and
    # opening the gate here (rather than at the end of the delivery) is what
    # makes a manual paste and an automatic one open the same one. Idempotent.
    ctx.open_reply_gate()
    while True:
        tick = await ctx.monitor.observe()
        watch = ctx.reply
        if watch is None:
            # The shell closed the gate under us (``/new``, a slot move, a
            # delegation): there is no reply outstanding any more, so there is
            # nothing here to wait for. Every one of those closers moves the loop
            # in the same breath, so the pre-empt that ends this is already on
            # its way - park rather than re-opening a gate somebody just shut.
            await park(ctx)
        finished = watch.feed_finish(ctx, tick)
        if finished is not FinishEvent.NOTHING:
            # ARMED is the ordinary answer. FINISHED and NO_HARVEST cannot be
            # reached from here - both need a trigger armed by an earlier tick,
            # and the arm is what leaves this state - but if one ever were, the
            # honest move is still forward: WAIT_GENERATE is where a finish is
            # decided, and it re-folds the very next tick.
            ctx.say(watch.why)
            return Outcome.GENERATING
        if watch.feed_gate(ctx, tick) is GateEvent.RELEASED:
            return Outcome.SENT
