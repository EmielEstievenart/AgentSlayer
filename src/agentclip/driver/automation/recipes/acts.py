"""The OS-acting steps every recipe is built out of.

One level below a recipe and one above the monitor: each of these is a small
CHOREOGRAPHY - a click and a warning, a beat and a verified focus swap, a poll
until the foreground moves, a click retried until the clipboard changes - made of
monitor verbs and of nothing else. They live together because they are shared:
the delivery and the harvest both focus a window, both hand the foreground back,
and a second copy of either would be a second thing to get right.

Every one of them is best-effort by contract. A click the OS refuses, a
foreground that never moves, a clipboard that cannot be read: none of those
raises, all of them are reported as ``False`` or as the empty answer, and the
recipe above decides what to tell the user. Refusing is always the safe answer -
the user can do it themselves.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agentclip.driver.automation.flow import (
    COPY_CLICK_OFFSETS,
    COPY_VERIFY_INTERVAL_S,
    COPY_VERIFY_READS,
    ELEMENT_CLICK_SETTLE_S,
)
from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.clip.base import ClipboardUnavailable
from agentclip.driver.monitor import beats
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot

if TYPE_CHECKING:
    from agentclip.driver.automation.recipes.context import RecipeContext


async def focus_click(ctx: RecipeContext, target: ScreenRegion) -> bool:
    """Click ``target``'s centre to put the chat's window in front, warning once -
    never per copy - if the OS refuses.

    True only when the click landed, which is what tells a caller it is safe to
    type into whatever that click just focused. WHERE to click is the caller's
    decision: the paste path wants the chat box itself (a caret in the input field
    is the point), while a keyboard scroll wants anywhere but
    (``flow.above_chatbox``).
    """
    clicked = await ctx.monitor.click(target)
    if not clicked and not ctx.region_click_warned:
        ctx.region_click_warned = True  # once, not on every copy
        ctx.view.notify(
            "the focus click did not land (it is Windows-only) - alt-tab to the chat instead",
            severity="warning",
        )
    return clicked


async def snap_focus_back(ctx: RecipeContext) -> bool:
    """Bring the shell's own window back to the foreground after a click in the
    browser. False = it never got there (nothing recorded, or Windows kept focus
    in the browser); the caller carries on either way.

    Verified and retried inside the monitor rather than asked once: the click that
    preceded this is *also* an activation request, and the browser's own is still
    in flight when we make ours - a single ``SetForegroundWindow`` wins that race
    often enough to look like it works and loses it often enough to be the bug.
    """
    handle = ctx.own_window
    if handle is None:
        return False
    return await ctx.monitor.focus_window(handle)


async def snap_back_after_click(ctx: RecipeContext) -> bool:
    """Let a click in the browser register, then take the foreground back.

    The shape every "we clicked over there and the user's next move is over HERE"
    site had copied out: a beat so the browser has the click before focus moves
    off it, then the verified snap. Deliberately NOT called after every browser
    click - one whose outcome leaves work for the user IN the browser keeps the
    browser focused, because pulling the foreground back would make them click
    into it again to do what we just told them to do.
    """
    if ctx.own_window is None:
        return False
    await asyncio.sleep(beats.SNAP_BACK_SETTLE_S)
    return await snap_focus_back(ctx)


async def await_browser_activation(ctx: RecipeContext) -> bool:
    """Wait until our own window is no longer the foreground one - i.e. the focus
    click's activation has actually been granted to the browser.

    Never a failure. A budget that runs out means we stop waiting and paste
    anyway, exactly as the blind sleep always did: the alternative is refusing to
    deliver a payload that would probably have landed, and the banner plus the
    retry button already cover a paste that goes nowhere. Skipped entirely when no
    handle was ever recorded - with no "us" to compare the foreground to, there is
    no question to answer.
    """
    handle = ctx.own_window
    if handle is None:
        return False
    for _ in range(beats.ACTIVATION_ATTEMPTS):
        current = await ctx.monitor.foreground_window()
        if current is not None and current != handle:
            return True
        await asyncio.sleep(beats.ACTIVATION_POLL_S)
    return False


async def click_profile_element(
    ctx: RecipeContext,
    slot: AgentSlot,
    kind: TemplateKind,
    *,
    settle_s: float = ELEMENT_CLICK_SETTLE_S,
) -> ElementClick:
    """Find ``kind`` inside ``slot``'s chat region right now, and click it.

    The primitive every programmatic click on a service appearance goes through:
    "click where those pixels ARE" rather than "where they used to be", which is
    both safer and the reason the browser may move.

    **Two of the six verdicts are decided HERE and four over there** (§2.3).
    DISARMED and NOT_CALIBRATED are refusals the brain makes before it asks
    anything - the armed switch is policy, and "there is nothing captured to look
    for" is answered against the drawn region this layer holds and the kind list
    the MONITOR sent (``Watched.captured``, reached through ``ctx.captured``).
    That second half is §11.3: it used to be a read of this machine's own profile
    store, which on any desktop but the one the pictures were taken on answered
    "nothing captured" and refused every click on a perfectly calibrated screen.
    DISARMED comes first, above even the calibration check: this is the only
    programmatic click on an appearance in the app, and a refusal that had already
    captured the screen would be answering a question nobody may act on.

    ``slot`` names which calibration the refusal is judged against - the
    sub-agent's new-chat button is not the master's - but it cannot aim the
    SEARCH, which happens in the window the monitor was last configured with.
    """
    if not ctx.os_armed:
        return ElementClick.DISARMED
    cal = ctx.calibration(slot)
    if cal.chat_region is None or kind not in ctx.captured(slot):
        return ElementClick.NOT_CALIBRATED
    return await ctx.monitor.click_element(kind, settle_s=settle_s)


async def verified_copy_click(ctx: RecipeContext, target: ScreenRegion) -> bool:
    """Click where the copy button was found, retrying at slightly offset points
    (still inside the icon) until the clipboard actually changes.

    ``target`` is already the ONE pixel the caller aimed at (the middle of the
    matched rectangle unless the service moved its click point), so the offsets
    walk around the point the user chose.

    Sometimes the click lands on the right spot but nothing is copied (a
    hover-rendered button that had not quite settled). Each attempt polls the
    clipboard for a change instead of trusting the click's return value, which
    only reports whether the OS accepted the input, not whether the target app
    reacted to it.

    True once a change is observed - or, when the clipboard cannot be read at all,
    after one unverified click, since retrying blind would just spam clicks with no
    way to tell if any of them worked.
    """
    try:
        before = await ctx.monitor.read_clipboard()
    except ClipboardUnavailable:
        await ctx.monitor.click(target, settle_s=0.05)
        return True

    for dx, dy in COPY_CLICK_OFFSETS:
        shifted = ScreenRegion(target.left + dx, target.top + dy, target.width, target.height)
        await ctx.monitor.click(shifted, settle_s=0.05)
        for _ in range(COPY_VERIFY_READS):
            await asyncio.sleep(COPY_VERIFY_INTERVAL_S)
            try:
                after: str | None = await ctx.monitor.read_clipboard()
            except ClipboardUnavailable:
                after = None
            if after != before:
                return True
    return False
