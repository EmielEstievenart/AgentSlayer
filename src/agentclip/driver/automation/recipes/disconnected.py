"""DISCONNECTED: there is no machine to look at, so there is nothing to run.

``IDLE``'s recipe with a different label, and the difference is worth naming
(docs/design/ui-monitor.md §2.9): idle is waiting for the USER, this is waiting
for a MACHINE. The redial belongs to the shell - it owns the link, its backoff
and the window that says so - and it lands here as
``set_loop_state(IDLE, "monitor link up ...")``, which pre-empts this recipe out
of the way. On the way back the brain RE-DERIVES from the screen: the trackers
are rebuilt, the streaks restart, and the recipe for whatever state the link
dropped in is never resumed. Nothing is buffered and nothing is replayed.

A payload posted while the link is down simply stays on the mailbox: the
``IDLE`` recipe the reconnect lands in takes it, and the insert happens against
a machine that is there.
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.automation.recipes.park import park


async def run(ctx: RecipeContext) -> Outcome:
    await park(ctx)
