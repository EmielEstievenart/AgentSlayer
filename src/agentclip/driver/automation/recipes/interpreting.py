"""INTERPRETING: the reply is in, the turn is running - wait for what it produces.

Outside the round trip on purpose: what happens here is the SESSION's (parse,
gate, execute), and this loop has nothing to do until that turn hands it
something. Two ways out and neither is a screen reading:

* the turn produces the next outbound payload, which restarts the round trip -
  the mailbox, exactly as in ``IDLE``;
* the turn ends with the floor back with the user, which is the shell's own
  ``set_loop_state(IDLE, ...)`` and reaches this recipe as a pre-empt. That call
  is the "ingest finished" signal §6.2 asks for, and it is deliberately the
  method the shell already makes rather than a new one: the shell is the only
  thing that knows a turn is over.
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome


async def run(ctx: RecipeContext) -> Outcome:
    await ctx.wait_payload()
    return Outcome.PAYLOAD_READY
