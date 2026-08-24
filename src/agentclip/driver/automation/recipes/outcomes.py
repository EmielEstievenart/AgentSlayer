"""What a recipe can come back with, and the sentence each answer says.

An ``Outcome`` is deliberately NOT a next state: a recipe knows what happened on
the screen in front of it and nothing about where that puts the loop, which is
:mod:`.transitions`' business. Keeping the two apart is what makes the table
readable as a picture of the machine and the recipes readable as choreography.

Every outcome carries a DEFAULT reason - the words the harness log and the STATE
rail show for the move it causes (``harness_log``). A recipe that has something
more specific to say overrides it for that one return (``ctx.say``), which is
how one ``NOT_PASTED`` tells four different stories: no window drawn, no chat box
on screen, a click the OS refused, a Ctrl+V that went nowhere.
"""

from __future__ import annotations

from enum import Enum


class Outcome(Enum):
    """One recipe's whole answer."""

    # idle / interpreting: a payload is on the mailbox and wants inserting.
    PAYLOAD_READY = "payload_ready"
    # auto_insert: the synthetic Ctrl+V landed in a chat box we could see...
    PASTED = "pasted"
    # ...or it did not, and the paste is the user's (four reasons, one outcome).
    NOT_PASTED = "not_pasted"
    # manual_insert: the ready-to-send button appeared, which only happens over a
    # non-empty composer - so the user's own paste demonstrably landed.
    SEND_PROVEN = "send_proven"
    # wait_send: the button was seen and is now gone, which is the user's Enter.
    SENT = "sent"
    # manual_insert / wait_send: the model is visibly generating, whatever the
    # gate did or did not see - nothing answers a message that was never sent.
    GENERATING = "generating"
    # wait_generate: every live detector says the model stopped, and there is a
    # copy button to click.
    FINISHED = "finished"
    # wait_generate: finished, but the harvest is the user's (disarmed, or no
    # copy button is captured for this service).
    NO_HARVEST = "no_harvest"
    # auto_copy: the copy button was found, clicked, and the clipboard changed.
    HARVESTED = "harvested"
    # auto_copy: any way that did not happen - nothing to search, nothing found,
    # or a click that never took.
    NOT_HARVESTED = "not_harvested"


# The words each move is narrated with when the recipe does not phrase its own.
# Written here rather than at the return sites so the ordinary path of the loop
# reads as one story in one place; the exceptional ones say why in their own
# words (``ctx.say``).
REASONS: dict[Outcome, str] = {
    Outcome.PAYLOAD_READY: "an outbound payload is ready to go into the chat box",
    Outcome.PASTED: "the payload was pasted into the chat box",
    Outcome.NOT_PASTED: "the payload was not pasted - the Ctrl+V is yours",
    Outcome.SEND_PROVEN: "the ready-to-send button appeared, which proves your paste landed",
    Outcome.SENT: "the ready-to-send button went away, which is your Enter",
    Outcome.GENERATING: "the model is visibly generating",
    Outcome.FINISHED: "every live detector said the model stopped on two ticks running",
    Outcome.NO_HARVEST: "the reply looks finished, but the harvest is yours",
    Outcome.HARVESTED: "the copy button was clicked and the reply is on its way in",
    Outcome.NOT_HARVESTED: "the copy button was not clicked - copy the reply yourself",
}
