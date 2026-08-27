"""Moving the automation between browser windows.

Not a state of the loop and not a recipe: a delegation starting or ending is the
SESSION's move, and what it needs from this layer is one all-or-nothing act
(open a fresh chat over there and re-point everything at it) and its unconditional
mirror (come home). They live beside the recipes because they are made of the same
vocabulary - a verified click, a retarget, a reset - and because leaving them on
the controller would leave the longest sequence in the app in the one file phase 2
emptied.
"""

from __future__ import annotations

import asyncio

from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.automation.recipes import acts
from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.monitor import beats
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.slot import AgentSlot


async def start_browser_chat(ctx: RecipeContext, slot: AgentSlot) -> bool:
    """Open a fresh browser chat in ``slot`` and make it the live one.

    All-or-nothing, and that is the whole point. True means the new-chat button
    verified against its capture, the click landed, and the automation (paste
    click, monitor, auto-copy) now targets that window. False means **nothing
    happened at all** - no click, no retarget, no trigger reset - so the caller
    can abort the delegation before anything is pasted: pasting a sub-agent's
    bootstrap into the master chat would corrupt that conversation irrecoverably,
    so every failure here is a refusal rather than a best effort.
    """
    outcome = await acts.click_profile_element(ctx, slot, TemplateKind.NEW_CHAT)
    if outcome is ElementClick.DISARMED:
        ctx.view.notify(
            f"disarmed - the {slot.label} chat was not opened, so nothing was "
            "delegated; press F5 to arm",
            severity="error",
        )
        return False
    if outcome is ElementClick.NOT_CALIBRATED:
        # Which half is missing is not this side's to say beyond the two facts it
        # holds: the drawn region is ours, the pictures are the monitor's. Naming
        # the monitor is §11.3's point - the button may be plainly on screen and
        # still unclickable, because the machine watching it has no capture of it.
        ctx.view.notify(
            f"the monitor has no {TemplateKind.NEW_CHAT.label} captured for the "
            f"{slot.label} chat's service - capture one in the Monitor UI",
            severity="error",
        )
        return False
    if outcome is not ElementClick.CLICKED:
        # AMBIGUOUS is the one worth spelling out: nothing is broken, the drawn
        # region simply holds two chats, and the fix is a redraw rather than a
        # recapture.
        reasons = {
            ElementClick.MISMATCH: "is not on screen",
            ElementClick.AMBIGUOUS: (
                "was found in several places in the drawn window - redraw it so it "
                "contains only this chat"
            ),
        }
        reason = reasons.get(outcome, "could not be clicked (it is Windows-only)")
        ctx.view.notify(
            f"the {slot.label} chat's new-chat button {reason} - nothing was clicked "
            "and nothing was pasted",
            severity="error",
        )
        return False
    ctx.select_live_slot(slot)
    ctx.reset_finish_trigger()
    # The master's outstanding reply is not this window's business: the
    # sub-agent's own bootstrap copy re-opens the gate a moment from now.
    ctx.close_reply_gate()
    ctx.view.hide_paste_flash()
    ctx.host.rebuild_detectors()  # baseline + regions from the new live slot
    await asyncio.sleep(beats.NEW_CHAT_SETTLE_S)  # let the fresh chat render its box
    return True


def end_browser_chat(ctx: RecipeContext) -> None:
    """Hand the automation back to the master chat when a delegation ends.

    Unconditional and never fails: the master window is where the session lives,
    so returning to it must work even after the sub-run blew up. Symmetrically to
    the open above, the sub-run's last reply is done with and the master's turn
    resumes by composing and copying its next outbound.
    """
    ctx.select_live_slot(AgentSlot.MASTER)
    ctx.reset_finish_trigger()
    ctx.close_reply_gate()
    ctx.view.hide_paste_flash()
    ctx.host.rebuild_detectors()
