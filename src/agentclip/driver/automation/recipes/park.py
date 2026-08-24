"""Waiting for somebody else: the shared body of every recipe that cannot move
the loop on by itself.

Two states are like this - ``MANUAL_COPY`` (the user's copy) and
``DISCONNECTED`` (a redial) - and one is *almost*: ``IDLE`` waits on the mailbox
instead. What they have in common is that the thing that ends the wait is not a
tick, so there is nothing to observe and nothing to decide; the move arrives as
``AutomationController.set_loop_state``, which pre-empts the loop out of here.

Written as a call that never returns rather than as an empty ``while True``
because that is what it is: the loop is holding this task open so the recipe can
be cancelled cleanly at the exact instant the shell speaks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from agentclip.driver.automation.recipes.context import RecipeContext


async def park(ctx: RecipeContext) -> NoReturn:
    """Wait for the shell. Never returns - the loop cancels this."""
    await ctx.preempt.wait()
    # Unreachable in practice: the loop is racing this recipe against the very
    # same event and drops the task the moment it is set. Spelled out anyway, so
    # a caller that somehow got here fails loudly instead of returning None into
    # a transition lookup.
    raise RuntimeError("parked recipe resumed: the loop should have dropped it")
