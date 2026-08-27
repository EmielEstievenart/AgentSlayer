"""Where the chat's input box is, and whether we are allowed to click it.

Shared by the two recipes that aim at it and by nothing else: the delivery (a
click that is about to be followed by a synthetic Ctrl+V) and the harvest (a
click that only has to put the page in front). They ask DIFFERENT questions of
the same search, which is why there are two doors onto one hunt:

* :func:`target` takes the weak answer - the whole drawn window when nothing
  matched - because the harvest's click lands in a transcript either way;
* :func:`verified_target` refuses it. **No blind paste** (§4.6): a payload goes
  into a box a capture actually verified, or it goes nowhere.

Since phase 2 the search itself is one ``UIMonitor.locate`` per layout and the
frame never comes up here (§2.3).
"""

from __future__ import annotations

from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion


async def match(
    ctx: RecipeContext,
) -> tuple[ScreenRegion, ScreenRegion, TemplateKind | None] | None:
    """The chat box, the one pixel to click in it, and WHICH appearance found it
    - ``None`` kind for the fallback, and ``None`` outright when nothing at all
    is drawn.

    The pixel comes back from the SEARCH (``Located.target``, §11.3) rather than
    being computed here: the click point belongs to the appearance, and
    appearances live on the monitor. A search that answered a rectangle with no
    aim reads as a miss, which keeps this total without a click into a guess.

    A fresh chat centres its input box and an ongoing one docks it at the bottom,
    so both layouts are asked about, ongoing first: mid-session it is the common
    case, and the hunt stops at the first hit.

    No ``exclude_kinds``, deliberately, and it is the one place that wants
    saying: the two layouts are the same CONTROL drawn two ways, so a service
    whose captures of them overlap would have each veto the other and the
    delivery would refuse a chat box that is plainly on screen.

    When neither is found - the page is mid-transition, a dialog covers it, or
    the service has no chat box captured at all - the whole chat window is the
    answer, with no kind beside it. An AMBIGUOUS answer takes that same road: an
    appearance belongs to the SERVICE, so a second window of it under one drawn
    region resolves the same box twice and picking one is a coin toss between two
    conversations.

    Always the LIVE slot: the monitor watches the window it was configured with,
    and mid-delegation that is the sub-agent's.
    """
    region = ctx.live.chat_region
    if region is None:
        return None
    # The fallback below is the whole drawn window aimed at ITSELF, deliberately:
    # a per-image click point describes where inside THAT PICTURE to click, and a
    # window the user drew round their whole chat is not that picture.
    for kind in (TemplateKind.CHATBOX_ONGOING, TemplateKind.CHATBOX_INITIAL):
        found = await ctx.monitor.locate(kind)
        if found.ambiguous:
            ctx.view.notify(
                f"several things look like the {kind.label} in the chat window - "
                "AgentClip will not paste into a maybe-wrong one; redraw the "
                "window so it contains only this chat",
                severity="warning",
            )
            return region, region, None
        if found.region is not None and found.target is not None:
            return found.region, found.target, kind
    return region, region, None


async def target(ctx: RecipeContext) -> tuple[ScreenRegion, ScreenRegion] | None:
    """The chat box's rectangle AND the one pixel in it to click, or ``None``.

    Both, because the caller needs both: the point is where the caret goes, and
    the rectangle is what a keyboard scroll measures its "just above the box"
    from (``flow.above_chatbox``).

    The point is the service's own click point (applied on the monitor, where
    the pictures are) only when a capture actually matched. The
    whole-drawn-window fallback keeps its centre, which is what :func:`match`
    hands back for it.
    """
    found = await match(ctx)
    if found is None:
        return None
    box, aim, _kind = found
    return box, aim


async def verified_target(ctx: RecipeContext) -> ScreenRegion | None:
    """The one pixel to click when the chat box is really ON SCREEN - or
    ``None``, which means "do not click at all".

    The delivery's question, and the one rule the paste path has. There used to
    be no such rule - the delivery took :func:`target`'s whole-drawn-window
    fallback and clicked the middle of the user's own rectangle - and the middle
    of a chat window is the TRANSCRIPT: the click selects a word of an old
    response, or lands on a link, and the synthetic Ctrl+V goes wherever that
    left the caret. So the fallback is refused here, on all three of its roads at
    once: neither appearance matched, two of one layout matched, or the service
    has no chat box captured at all.

    All three land on the same banner (``MANUAL_INSERT``): the payload is already
    on the clipboard, so the user clicks their own chat box and presses Ctrl+V.
    """
    found = await match(ctx)
    if found is None:
        return None
    _box, aim, kind = found
    if kind is None:
        return None
    return aim
