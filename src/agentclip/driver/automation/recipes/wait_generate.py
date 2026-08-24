"""WAIT_GENERATE: the model is answering - decide when it has stopped.

``evaluate_finish``'s policy half (docs/design/ui-monitor.md §6.2's table). Its
arithmetic half is the monitor's now and rides in on the tick: the run of
consecutive large deltas and the run of ticks every live detector agreed on are
counts about a SCREEN, and a brain that kept its own totals would lose them on
every reconnect (§2.2, §2.9). What is decided here is what those counts are
WORTH.

**The fire is one-shot** (§4.1), and the shape of this file is why: the loop is a
single asyncio task, this recipe returns exactly one outcome, and the flow flag
is raised before the return - so a second harvest cannot start until the first
one has finished and handed the loop back.
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.automation.recipes.park import park
from agentclip.driver.automation.recipes.reply import FinishEvent


async def run(ctx: RecipeContext) -> Outcome:
    while True:
        tick = await ctx.monitor.observe()
        watch = ctx.reply
        if watch is None:
            await park(ctx)
        event = watch.feed_finish(ctx, tick)
        if event is FinishEvent.FINISHED:
            # The reply we were waiting for is being harvested right now: nothing
            # is outstanding again until the next outbound goes out.
            ctx.close_reply_gate()
            # Raised HERE rather than by the harvest, and before this recipe
            # returns: it is what makes the fire one-shot, and what keeps a
            # shell's own "retry the insert" out of the mouse work about to
            # start.
            ctx.flow_running = True
            return Outcome.FINISHED
        if event is FinishEvent.NO_HARVEST:
            ctx.say(watch.why)
            return Outcome.NO_HARVEST
        # ARMED is a note here rather than a move - the loop is already in the
        # state the arm would take it to - and it is deliberately NOT narrated as
        # a transition (the trigger line is written where the arm is decided).
        # The gate is normally long gone by now; feeding it costs nothing and
        # covers the one path that reaches this state with one still open (a
        # release on the very tick that armed).
        watch.feed_gate(ctx, tick)
