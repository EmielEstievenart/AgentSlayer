"""AUTO_COPY: the model stopped - go and get the reply.

``auto_copy_flow`` / ``run_auto_copy_flow``, lifted whole (docs/design/ui-monitor.md
§6.2's table): focus the browser, park the pointer on the transcript, snap it to
the bottom, find the newest (lowest) copy-button icon anywhere in the chat region
and click it until the clipboard actually changes. The watcher ingests what that
click produced.

Every search here is a monitor verb now (§2.3) - ``snap_to_bottom``, ``locate``,
``hover_scan`` - and what is left is the POLICY around their answers: which kinds
a match may NOT be, how many rounds a hunt is worth, whether a miss deserves the
user's real mouse, and what they are told when it comes up empty.

The whole recipe runs inside the flow bracket. Whatever the body does (return,
raise, or get cancelled by a pre-empt), the suspension lifts and every tracker
forgets the frames the flow's own scrolling and hover-scanning produced: polling
resumes from a clean post-flow baseline instead of reading our own mouse work as
a new generation.
"""

from __future__ import annotations

import asyncio

from agentclip.config import SCROLL_END, SCROLL_PAGE_DOWN
from agentclip.driver.automation.flow import (
    COPY_SNAP_ROUNDS,
    SNAP_SETTLE_S,
    above_chatbox,
    how_close,
)
from agentclip.driver.automation.harness_log import KIND_COPY
from agentclip.driver.automation.recipes import acts, chatbox
from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region

# The two layouts one service's chat box can be drawn in, as one tuple: handed to
# ``locate`` as the appearances a copy icon may NOT turn out to be.
CHATBOX_KINDS: tuple[TemplateKind, ...] = (
    TemplateKind.CHATBOX_INITIAL,
    TemplateKind.CHATBOX_ONGOING,
)


async def run(ctx: RecipeContext) -> Outcome:
    """The harvest, inside the flow-suspension bracket."""
    try:
        # Uncancellable for the same reason the delivery is: the middle of this
        # is a scroll, a click and a session ingest.
        with ctx.acting_on_the_machine():
            return await harvest(ctx)
    finally:
        ctx.end_flow()


async def harvest(ctx: RecipeContext) -> Outcome:
    """Focus the browser, snap the transcript to its bottom, find the newest copy
    icon and click it.

    The search is the whole chat region, not a same-width band beneath a
    remembered icon: the icon appears once per response down the transcript, and
    *lowest inside the window the user drew* is the same answer without anyone
    having to remember a column.

    It is its OWN search, deliberately, even though the monitor has been looking
    for the same icon twice a second all along: this flow has just clicked,
    scrolled and waited for the page to render, so **a location up to half a
    second old is not a click target**. The poll's record IS good for explaining a
    miss, which is where it is read (``AutomationHost.copy_seen_note``).

    **The snap gets ``COPY_SNAP_ROUNDS`` goes, not one.** The commonest cause of a
    miss is not a drifted capture at all - it is a page that had not finished
    arriving. A streamed reply keeps growing after the detectors call it finished,
    a virtualized transcript renders the rows it just scrolled to a beat later,
    and either way the bottom moves out from under a single snap. The focus click
    and the pointer park are **not** repeated: nothing between rounds touches the
    mouse or the focus, and re-clicking a transcript risks selecting text or
    following a link.

    The hover scan (opt-in, per service) runs after the LAST static miss, not the
    first: it drives the user's real cursor across the screen. The failure report
    keeps the BEST ``best_miss`` of all the rounds - the closest the capture ever
    came is the number that separates "drifted, recapture it" from "no candidate
    at all".
    """
    region = ctx.live.chat_region
    templates = ctx.live_profile().variants(TemplateKind.COPY)
    if region is None or not templates:
        missing_part = (
            "no chat window is drawn" if region is None else "no copy button is captured"
        )
        ctx.log_harness(KIND_COPY, f"auto-copy flow could not start: {missing_part}")
        ctx.say("there is nothing for the auto-copy flow to search")
        return Outcome.NOT_HARVESTED

    # Focus the chat window and park the pointer on the transcript. Both are done
    # ONCE, in front of the first snap.
    #
    # The keyboard forms ride this focus click - keys go to whatever has focus -
    # so the click may not land in the CHAT BOX the way the paste path's
    # deliberately does: a caret in the input field swallows End outright (it
    # means "end of line" there) and the transcript never moves. The padding just
    # above the box focuses the same window with nothing typable under the
    # pointer. The wheel is aimed by coordinates and does not care either way.
    #
    # Whatever the click focused, the pointer is then parked on the transcript's
    # centre before anything scrolls, because some chat pages only scroll the pane
    # the pointer is over. It has to be a real synthetic MOVE, since a teleported
    # pointer does not reliably make a browser fire the hover chain those pages
    # track. Best-effort: a move that does not happen leaves the snap exactly as
    # it was before this step existed.
    scroll_action = ctx.live_preset().scroll_action
    box_and_target = await chatbox.target(ctx)  # the live chat box, else the region
    if box_and_target is not None:
        box, aim = box_and_target
        if scroll_action in (SCROLL_PAGE_DOWN, SCROLL_END):
            # Measured off the RECTANGLE, not off the click point: the padding
            # this aims at is over the box's top edge.
            aim = above_chatbox(box, region) or aim
        await acts.focus_click(ctx, aim)
    await asyncio.sleep(0.15)
    await ctx.monitor.move_cursor(*region.center)
    await asyncio.sleep(0.1)  # let the page's hover tracking register it

    found: ScreenRegion | None = None
    best_miss: float | None = None
    for attempt in range(1, COPY_SNAP_ROUNDS + 1):
        await ctx.monitor.snap_to_bottom(scroll_action)
        await asyncio.sleep(SNAP_SETTLE_S)  # let the page render what it scrolled to

        # One monitor verb per round, and the whole hunt is inside it: a capture
        # of the configured region, the copy stack searched across it, the
        # bottom-most hit and - on a miss - how close the closest rejected
        # candidate came. A capture that failed reads as a miss here, deliberately.
        #
        # ``exclude_kinds`` is the chat box, both layouts. The copy icon is hunted
        # across the WHOLE drawn window, the composer included, and a service
        # whose copy capture also matches a corner of its input box would have the
        # harvest click into the chat box and copy nothing.
        located = await ctx.monitor.locate(TemplateKind.COPY, exclude_kinds=CHATBOX_KINDS)
        found = located.region
        # The closest ANY round came, not the last one's: see the docstring.
        if located.best_miss is not None and (best_miss is None or located.best_miss < best_miss):
            best_miss = located.best_miss
        # ``ambiguous`` is deliberately NOT consulted. Every response in a
        # transcript stamps its own copy icon, so several on screen is the
        # ordinary case rather than the two-windows trouble it means for a button
        # there is only ever one of - and ``locate`` already answers with the
        # LOWEST, which is the newest reply's.
        if found is not None:
            break
        if attempt < COPY_SNAP_ROUNDS:
            # Deliberately NOT the word "not found": that line is the flow's
            # verdict, and a hunt that is still scrolling has not reached one.
            ctx.copy_status(f"re-snapping ({attempt + 1}/{COPY_SNAP_ROUNDS})")
            ctx.log_harness(
                KIND_COPY,
                f"copy button not found on round {attempt}/{COPY_SNAP_ROUNDS} "
                f"({how_close(best_miss)}) - snapping to the bottom again",
            )
    if found is None and ctx.live_preset().hover_scan:
        # Nothing in the static frame: this service is one of the chats that only
        # paint the icon under the pointer, so try again while hovering up the
        # region. Opt-in per service because the scan drives the user's real mouse
        # across the screen.
        ctx.copy_status("hover-scanning")
        found = await ctx.monitor.hover_scan(TemplateKind.COPY)
    if found is None:
        ctx.view.notify("copy button not found on screen", severity="warning")
        ctx.copy_status("not found")
        # The number goes on the ``copy`` entry, the consequence on the ``state``
        # one: the two print on adjacent lines, and repeating the parenthetical on
        # both made the log read as a stutter.
        ctx.log_harness(
            KIND_COPY,
            f"copy button not found after {COPY_SNAP_ROUNDS} snaps "
            f"({how_close(best_miss)}{ctx.host.copy_seen_note()})",
        )
        ctx.say("the copy button was not found on screen")
        return Outcome.NOT_HARVESTED

    # The rectangle the search came back with, reduced to the one pixel this
    # service aims its copy click at (the centre unless the user moved it).
    # Reduced HERE rather than inside the click, so the small retry offsets
    # ``verified_copy_click`` walks through stay around the point the user chose -
    # which is also why this click does not go through ``UIMonitor.click_element``:
    # the copy click is CLIPBOARD-verified, and that verification is policy.
    target = click_point_region(found, *ctx.live_profile().click_point(TemplateKind.COPY))
    # Arm the prose window for THIS click and nothing else: whatever the clipboard
    # holds when the click verifies is the model's reply, so the harvest may show
    # it even with no CLIP blocks in it. Disarmed the moment the harvest returns -
    # and by ``end_flow`` on every other way out of here.
    ctx.prose_window = True
    clicked = await ctx.host.verified_copy_click(target)
    if not clicked:
        # Every attempt clicked but the clipboard never changed - leave the
        # browser focused so the user can click the copy button themselves.
        ctx.view.notify(
            "copy click did not take - click the response's copy button yourself",
            severity="warning",
        )
        ctx.copy_status("click did not take")
        ctx.log_harness(
            KIND_COPY, "the copy button was found and clicked, but the clipboard never changed"
        )
        ctx.say("the copy click did not take - click the response's copy button yourself")
        return Outcome.NOT_HARVESTED

    ctx.view.notify("copy button clicked")
    ctx.copy_status("clicked")
    ctx.log_harness(
        KIND_COPY,
        "copy button found and clicked; the clipboard changed, so the reply is on its way in",
    )
    # The response is on its way to the clipboard - hand focus back to AgentClip
    # so the user watches the ingest here, not the browser... unless the service
    # says not to (``ServicePreset.snap_back``), the same debugging switch the
    # auto-send snap reads: an aid that covered only the delivery would be no aid
    # at all, since the harvest fires seconds later on the same turn.
    if ctx.live_preset().snap_back:
        await acts.snap_back_after_click(ctx)
    try:
        await ctx.host.ingest_harvest()
    finally:
        # Back to strict checking the instant the harvest is in.
        ctx.prose_window = False
    return Outcome.HARVESTED
