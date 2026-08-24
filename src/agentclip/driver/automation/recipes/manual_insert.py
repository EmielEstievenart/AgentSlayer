"""MANUAL_INSERT: the Ctrl+V is the user's - watch for proof that they did it.

The delivery could not put the payload in the box (the app is disarmed, no chat
window is drawn, the chat box was not on screen, the focus click was refused, or
the synthetic Ctrl+V went nowhere), so the payload is parked on the clipboard
and the banner is asking. The loop is not stuck: it is watching the same screen
``WAIT_SEND`` watches, for the same two pieces of evidence, and the only
difference is what each of them PROVES here.

* the ready-to-send button appearing proves the paste landed - it only renders
  over a non-empty composer - so the loop catches up to ``WAIT_SEND`` and waits
  for the Enter from there;
* the model visibly generating proves the whole thing happened while we were not
  looking (a service with no send button captured has no other way to say so),
  and the loop jumps straight to ``WAIT_GENERATE``.

The nag itself - the banner and the audible alert - belongs to the transition
that got here, not to this recipe (see ``manual_copy``).
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.automation.recipes.park import park
from agentclip.driver.automation.recipes.reply import FinishEvent, GateEvent


async def run(ctx: RecipeContext) -> Outcome:
    # The payload is out - however badly - so the reply gate opens here exactly
    # as it does for a paste that landed: whether the Ctrl+V was ours or theirs,
    # the next thing to happen in that chat is the answer to it. Idempotent.
    ctx.open_reply_gate()
    while True:
        tick = await ctx.monitor.observe()
        watch = ctx.reply
        if watch is None:
            await park(ctx)
        if watch.feed_finish(ctx, tick) is not FinishEvent.NOTHING:
            ctx.say(watch.why)
            return Outcome.GENERATING
        if watch.feed_gate(ctx, tick) is GateEvent.SEEN:
            return Outcome.SEND_PROVEN
