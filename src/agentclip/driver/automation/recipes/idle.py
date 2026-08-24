"""IDLE: nothing is outstanding, so wait for the user's next message to become
an outbound payload.

The loop's resting place, and the only recipe that touches neither the screen
nor the clock. ``AutomationController.copy_outbound`` posts to the mailbox and
wakes this (docs/design/ui-monitor.md §6.2); everything else the shell can do
from here - a session reset, a link dropping - arrives as a pre-empt instead.
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome


async def run(ctx: RecipeContext) -> Outcome:
    await ctx.wait_payload()
    return Outcome.PAYLOAD_READY
