"""AUTO_INSERT: put the outbound payload into the chat box ourselves.

``deliver`` and the clipboard I/O block, lifted whole (docs/design/ui-monitor.md
§6.2's table): park the payload on the clipboard, click the chat's input box,
wait out the focus activation, paste it (in one burst or a stream of them), tap
Enter for a service that asked us to, and say on the banner whose move it is now.

Two rules run through all of it.

**No blind paste** (§4.6). The click target is
:func:`~agentclip.driver.automation.recipes.chatbox.verified_target` and nothing
else: with no captured chat box verifying inside the drawn region this refuses
the whole OS half and puts the "your move" banner up instead. A paste into the
void is recoverable; a paste into the wrong place is what the user has to notice
and undo.

**Disarmed stops here** (§4.5), one line below the clipboard write and above
every OS call - which is the whole shape of the feature: the payload is where the
user can paste it, and the click and the synthetic Ctrl+V simply do not happen.
Everything after that is the existing "the click never landed" path, which is
exactly the disarmed UX and needs no second implementation.
"""

from __future__ import annotations

import asyncio

from agentclip.config import DELIVERY_STREAM
from agentclip.driver.automation.delivery import (
    AUTO_SEND_FLASH_TEXT,
    ENTER_FLASH_TEXT,
    PASTE_FLASH_TEXT,
    stream_flash_text,
)
from agentclip.driver.automation.harness_log import KIND_GATE
from agentclip.driver.automation.recipes import acts, chatbox
from agentclip.driver.automation.recipes.context import RecipeContext
from agentclip.driver.automation.recipes.outcomes import Outcome
from agentclip.driver.clip import chunking
from agentclip.driver.clip.base import ClipboardUnavailable

# The beats every step below paces itself by, as a MODULE rather than as names:
# cadence belongs to the machine being driven (§2.10), and reading ``beats.X`` at
# the call site is what lets a suite shrink a beat by writing to it.
from agentclip.driver.monitor import beats
from agentclip.driver.screen.region import ScreenRegion


async def run(ctx: RecipeContext) -> Outcome:
    """Take the payload off the mailbox and deliver it."""
    if ctx.payload is None:
        # The state was entered before the post landed (a shell that moved the
        # rail itself). There is nothing to insert until something is posted, and
        # AUTO_INSERT means there will be.
        await ctx.wait_payload()
    payload = ctx.take()
    assert payload is not None  # noqa: S101 - the wait above is what guarantees it
    try:
        # From here to the banner is one act: it clicks a browser window and types
        # into it, and a pre-empt landing in the middle would leave a caret in
        # somebody's chat box with half a payload behind it.
        with ctx.acting_on_the_machine():
            # The WHOLE payload goes on the clipboard first, whichever way it is
            # about to be delivered: a stream leaves its last chunk there, and this
            # is the write every manual recovery (the user's own Ctrl+V, /copy, the
            # retry button) is aimed at.
            clipboard_ok = await park_on_clipboard(ctx, payload.text)
            # What a retry would re-deliver, recorded before the first attempt so
            # it is right whichever way that attempt ends.
            ctx.pending_insert = payload.text
            return await deliver(ctx, payload.text, clipboard_ok=clipboard_ok)
    finally:
        # Whichever way this ended - pasted, refused, or cancelled by a pre-empt -
        # the caller that awaited the delivery is let go. Nobody is left holding an
        # event nothing will set.
        payload.done.set()


async def park_on_clipboard(ctx: RecipeContext, text: str) -> bool:
    """Put the whole outbound on the clipboard, as a self-write.

    ``False`` = no real clipboard backend, so the shell was handed the payload to
    park however it can (``AutomationHost.park_off_clipboard`` - the TUI's OSC-52
    escape, which is write-only) and a synthetic Ctrl+V has nothing here to paste.
    """
    try:
        await ctx.monitor.write_clipboard(text)
    except ClipboardUnavailable:
        ctx.host.park_off_clipboard(text)
        ctx.view.notify(
            "no clipboard backend - sent via the terminal's OSC-52 escape; if pasting "
            "fails, copy from .agentclip/sessions/<id>/outbound/",
            severity="warning",
        )
        return False
    return True


async def deliver(ctx: RecipeContext, text: str, *, clipboard_ok: bool) -> Outcome:
    """Click the chat's input box, let the focus settle, paste, and - for a
    service that opted in - tap Enter.

    The single door every outbound goes through: the bootstrap, a turn's results,
    the `c` re-delivery, the retry button and a sub-agent's first paste are all
    one call onto this, so the refusal above is one rule rather than five.

    ``clipboard_ok`` is how the payload got parked, and the only thing it decides
    is whether the STREAM path is available: a stream writes each chunk through
    the clipboard, so a service that asked for one still falls back to the single
    burst when there is no backend behind it.
    """
    # The chat box is resolved HERE rather than inside the click, because "there
    # is nowhere to aim" and "the aim was refused" are two different things to say
    # on the banner.
    target: ScreenRegion | None = None
    if ctx.os_armed:
        target = await chatbox.verified_target(ctx)
        clicked = target is not None and await _click_after_response(ctx, target)
    else:
        clicked = False
        ctx.view.notify(
            "disarmed - the payload is on your clipboard: click the chat box and "
            "press Ctrl+V yourself (F5 arms)",
            severity="warning",
        )
    # Only paste when the click actually landed - focus could be on any window
    # otherwise, and pasting into an unknown app is the one unforgivable failure
    # mode here.
    pasted = False
    if clicked:
        # THE seam between the click and the paste, and the one place it exists:
        # the click above only tells us the OS accepted the input, never that the
        # chat box has finished taking focus. Two halves, because the wait has
        # two halves. First WAIT FOR THE ACTIVATION - poll the foreground until it
        # is somebody else's window - then the flat beat for the half no handle
        # reports on: the page still has to route the click into the chat box and
        # put a caret there.
        await acts.await_browser_activation(ctx)
        await asyncio.sleep(beats.PASTE_SETTLE_DELAY)
        if ctx.live_preset().delivery == DELIVERY_STREAM and clipboard_ok:
            pasted = await _stream_outbound(ctx, text)
        else:
            pasted = await ctx.monitor.send_paste()
    # The auto-insert resolved. Four reasons, not one, because "the Ctrl+V is
    # yours" has four very different causes and only two of them are failures -
    # the switch the user threw themselves reads as a fault otherwise, and a chat
    # box that is simply not on screen is a different thing to fix than a click
    # the OS refused.
    auto_sent = False
    outcome = Outcome.PASTED
    if pasted:
        if ctx.live_preset().auto_submit:
            # Opt-in per service: tap Enter ourselves instead of waiting for the
            # user's. Still WAIT_SEND, and deliberately so - the tap is an
            # attempt, not a fact, and the send gate's evidence stays the only
            # thing that moves the loop on, exactly as for a human Enter.
            await asyncio.sleep(beats.SUBMIT_SETTLE_S)
            auto_sent = await ctx.monitor.send_enter()
            ctx.log_harness(
                KIND_GATE,
                "auto-submit tapped Enter after the paste"
                if auto_sent
                else "auto-submit could not type Enter - the send is yours",
            )
    else:
        outcome = Outcome.NOT_PASTED
        if not ctx.os_armed:
            ctx.say(
                "auto-insert suppressed: disarmed - the payload is on your clipboard "
                "to paste yourself"
            )
        elif target is None:
            # Nothing that looks like the chat box verified inside the drawn
            # region, so nothing was clicked and nothing was pasted. Two sentences
            # rather than one, because the two roads want different things done
            # about them: an undrawn window is a setup step the user has not taken
            # yet, while a box that did not verify inside a window they DID draw is
            # a page to look at (or a capture to re-take).
            if ctx.live.chat_region is None:
                ctx.view.notify(
                    "no chat window is drawn, so there was nowhere to paste - the payload "
                    "is on your clipboard: draw the window (SET REGION), or paste it "
                    "yourself",
                    severity="warning",
                )
                ctx.say(
                    "no chat window is drawn, so nothing was clicked - paste it yourself"
                )
            else:
                ctx.view.notify(
                    "the chat box was not found on screen - nothing was clicked: the "
                    "payload is on your clipboard, click the chat box and press Ctrl+V "
                    "yourself",
                    severity="warning",
                )
                ctx.say(
                    "the chat box was not found on screen - click it and paste yourself "
                    "(press c to re-copy)"
                )
        elif not clicked:
            ctx.say(
                "the chat box was found but the focus click on it was refused, "
                "so nothing was pasted"
            )
        else:
            ctx.say("the chat box was focused but the synthetic Ctrl+V did not go through")
    # The payload now waits on the user's Enter (pasted), Ctrl+V+Enter (not
    # pasted), or on the send gate confirming the Enter auto-submit already
    # tapped - nag until the busy region reports the model chewing.
    ctx.view.show_paste_flash(
        AUTO_SEND_FLASH_TEXT if auto_sent else ENTER_FLASH_TEXT if pasted else PASTE_FLASH_TEXT,
        # ...and offer the one-press re-run beside that nag, exactly when the nag
        # is the "you paste it yourself" one. An insert that landed has nothing to
        # retry, and a button offering to paste a second payload on top of the
        # first is worse than no button at all.
        retry=not pasted,
    )
    # ...and, on the ONE outcome that leaves the user nothing to do in the
    # browser, bring them back here. The other two deliberately keep the browser
    # focused - ">>> PRESS ENTER <<<" and ">>> PRESS CTRL+V <<<" are instructions
    # to act over THERE, and stealing the foreground while asking for a keystroke
    # in another window is how the banner ends up lying about what a press will
    # do. Unless the service says not to (``ServicePreset.snap_back``), which is a
    # debugging aid: with the foreground left in the browser the user can see for
    # themselves where the click landed.
    if auto_sent and ctx.live_preset().snap_back:
        await acts.snap_back_after_click(ctx)
    return outcome


async def submit(ctx: RecipeContext) -> bool:
    """Tap Enter in the chat box, on the user's say-so: the sidebar's PRESS
    ENTER button (ui-monitor.md §11.8).

    The auto-submit's own last step (``deliver``), done again from the top -
    focus the chat box, wait for the browser to take the foreground, then the
    Enter - for the prompt that :func:`deliver` pasted and whose auto-submit
    the page dropped (a composer still building its attachment chip, a send
    control that was disabled for the beat). Un-aimed in the same way: the
    click puts the caret where the payload already sits, and Enter goes to
    whatever the focus click focused, which is why the click is verified
    before anything is typed. True only when the Enter was typed.
    """
    if not ctx.os_armed:
        ctx.view.notify(
            "disarmed - AgentClip may not type: press F5 to arm, or press Enter in "
            "the chat yourself",
            severity="warning",
        )
        return False
    target = await chatbox.verified_target(ctx)
    if target is None:
        ctx.view.notify(
            "the chat box was not found on screen - nothing was clicked, so no Enter "
            "was typed: press it in the chat yourself",
            severity="warning",
        )
        ctx.log_harness(KIND_GATE, "press Enter refused: the chat box is not on screen")
        return False
    if not await _click_after_response(ctx, target):
        ctx.log_harness(KIND_GATE, "press Enter refused: the focus click did not land")
        return False
    await acts.await_browser_activation(ctx)
    await asyncio.sleep(beats.PASTE_SETTLE_DELAY)
    sent = await ctx.monitor.send_enter()
    ctx.log_harness(
        KIND_GATE,
        "Enter tapped from the sidebar"
        if sent
        else "the sidebar's Enter could not be typed - the send is yours",
    )
    if sent:
        ctx.view.show_paste_flash(AUTO_SEND_FLASH_TEXT)
    return sent


async def _click_after_response(ctx: RecipeContext, target: ScreenRegion) -> bool:
    """Poke the chat box at ``target`` so the browser has focus and the paste
    lands without alt-tab. True only when the click landed.

    TWO clicks, ``beats.FOCUS_CLICK_GAP_S`` apart. The first is spent waking the
    browser: a window that is not in the foreground takes the click as its
    activation and the page never sees it routed to the input field, which leaves
    the window focused, the caret nowhere, and the Ctrl+V going into nothing the
    user can see. The second lands on a window that is already awake, and the gap
    is wide enough that a busy page has finished reflowing its composer AND that
    the OS reads the pair as two single clicks rather than a double one.

    The verdict is the FIRST click's: it is the one that proves the OS accepts
    input for that target at all, so the reinforcement is best effort.
    """
    clicked = await acts.focus_click(ctx, target)
    if not clicked:
        return False
    await asyncio.sleep(beats.FOCUS_CLICK_GAP_S)
    await acts.focus_click(ctx, target)
    return True


async def _stream_outbound(ctx: RecipeContext, text: str) -> bool:
    """Walk ``text`` into the focused chat box a chunk at a time (opt-in per
    service, ``ServicePreset.delivery``). True only if every chunk landed.

    Chunked CLIPBOARD PASTES rather than synthetic typing: a typed newline is
    Enter in most chat boxes, which would submit half a payload - the exact
    accident this whole flow exists to avoid. Every chunk goes through the
    monitor's write, so each is registered as a self-write and the watcher can
    never ingest our own outbound back as a reply.

    A chunk that fails to paste ends the stream: the box then holds a partial
    payload the user has to clear, so the FULL text goes back on the clipboard and
    the caller's existing MANUAL_INSERT path takes over.
    """
    chunks = chunking.split_for_stream(text, chunking.STREAM_CHUNK_CHARS)
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        ctx.view.show_paste_flash(stream_flash_text(index, total))
        try:
            await ctx.monitor.write_clipboard(chunk)
        except ClipboardUnavailable:
            landed = False
        else:
            landed = await ctx.monitor.send_paste()
        if not landed:
            await _restore_after_partial_stream(ctx, text, index, total)
            return False
        if index < total:
            await asyncio.sleep(beats.STREAM_CHUNK_SETTLE_S)
    return True


async def _restore_after_partial_stream(
    ctx: RecipeContext, text: str, index: int, total: int
) -> None:
    """Undo what a half-delivered stream left behind, as far as it can be undone
    from here: the clipboard gets the whole payload back, and the toast says the
    chat box is the part only the user can fix."""
    try:
        await ctx.monitor.write_clipboard(text)
    except ClipboardUnavailable:
        ctx.host.park_off_clipboard(text)
    ctx.view.notify(
        f"streaming stopped at chunk {index}/{total} - the chat box holds a partial "
        "message: clear it, then press Ctrl+V for the whole payload",
        severity="warning",
    )
