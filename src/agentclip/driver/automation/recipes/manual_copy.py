"""MANUAL_COPY: the reply is finished and the harvest is the user's.

Four roads reach here and the rail draws one box for all of them (the log is
where they are told apart): the app is disarmed, the service has no captured
copy button, the button was not found on screen, or the click never took. From
the loop's point of view they are one situation - **the next move is not this
loop's** - so the recipe parks.

It parks rather than watching, deliberately. Whatever the user copies is caught
by the clipboard WATCHER, which is the monitor's thread and the shell's ingest;
that ingest is what moves the loop on, through ``set_loop_state(INTERPRETING)``,
and it arrives here as a pre-empt.

**The nag** (``ServicePreset.alert_sound``) is armed by the transition rather
than by this recipe. ``set_loop_state`` is the one door every attention state
comes through - including the two this loop never runs a recipe for, a manual
insert and a lost link - and an alarm hung off a recipe would be an alarm that
does not sound when a shell moves the loop itself.
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.automation.recipes.park import park


async def run(ctx: RecipeContext) -> Outcome:
    await park(ctx)
