"""The loop: run the recipe for the state we are in, look up where its answer
goes, go there, repeat.

docs/design/ui-monitor.md §2.4. One asyncio task, owned by
:class:`~agentclip.driver.automation.controller.AutomationController`, and the
only writer of a transition. Everything it needs is in two places - the recipe
map below and :data:`~agentclip.driver.automation.recipes.transitions.TRANSITIONS`
- so "what can happen next" is a table anyone can read rather than a call graph.

**One task** is the load-bearing part (§4.1): a second harvest cannot start while
the first recipe is still running, because there is nothing else running. It is
also why nothing here takes a lock (§4.4) - every writer below is on the event
loop - and why the paints a recipe makes are same-thread paints.

**Pre-emption.** A shell can move the loop itself: a session reset, a link
dropping, a harvested reply being ingested (``set_loop_state``). That raises
``ctx.preempt``, and the loop drops what it is doing and carries on from the
state the shell put it in. A recipe is exempt while it is DRIVING THE MACHINE
(``ctx.acting``) - cancelling a delivery half way through a paste, or a harvest
half way through an ingest, is worse than finishing it - so those stretches are
let run to the end and their outcome is thrown away instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from agentclip.driver.automation.harness_log import KIND_STATE
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.recipes import (
    auto_copy,
    auto_insert,
    disconnected,
    idle,
    interpreting,
    manual_copy,
    manual_insert,
    wait_generate,
    wait_send,
)
from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.automation.recipes.transitions import TRANSITIONS

Recipe = Callable[[RecipeContext], Awaitable[Outcome]]

# One recipe per state, and the map is TOTAL over ``LoopState`` - a state with no
# recipe would be a state the loop parks in for ever with nothing to park on.
RECIPES: dict[LoopState, Recipe] = {
    LoopState.IDLE: idle.run,
    LoopState.AUTO_INSERT: auto_insert.run,
    LoopState.MANUAL_INSERT: manual_insert.run,
    LoopState.WAIT_SEND: wait_send.run,
    LoopState.WAIT_GENERATE: wait_generate.run,
    LoopState.AUTO_COPY: auto_copy.run,
    LoopState.MANUAL_COPY: manual_copy.run,
    LoopState.INTERPRETING: interpreting.run,
    LoopState.DISCONNECTED: disconnected.run,
}


async def run_loop(ctx: RecipeContext) -> None:
    """Drive the loop until cancelled. The controller's one task."""
    while True:
        state = ctx.state
        ctx.preempt.clear()
        recipe = asyncio.ensure_future(RECIPES[state](ctx))
        preempted = asyncio.ensure_future(ctx.preempt.wait())
        try:
            await asyncio.wait({recipe, preempted}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            recipe.cancel()
            preempted.cancel()
            raise
        preempted.cancel()
        if not recipe.done() and not ctx.acting:
            recipe.cancel()
            await asyncio.gather(recipe, return_exceptions=True)
            continue
        try:
            outcome = await recipe
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a recipe must never kill the loop
            # Say so where the user looks for it, and then wait for the shell
            # rather than re-running: a recipe that raised once on this screen
            # will raise again, and a loop spinning on it is a loop that hides the
            # first failure behind a thousand more.
            ctx.log_harness(KIND_STATE, f"the {state.name} recipe failed: {exc!r}")
            await ctx.preempt.wait()
            continue
        if ctx.preempt.is_set():
            # The shell spoke while the recipe was finishing. Its word wins: the
            # rail already says so, and the outcome describes a state the loop is
            # no longer in.
            continue
        ctx.enter(TRANSITIONS[(state, outcome)], ctx.take_reason(outcome))
