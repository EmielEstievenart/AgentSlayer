"""Agent slots: one drawn box per browser chat window AgentClip drives.

A slot used to be a pile of six calibrations. It is now a single rectangle,
because the two questions it was conflating turned out to have different
owners:

* **What does this thing look like?** - the stop button, the copy icon, the
  chat input box, the new-chat control. That is a property of the *service*,
  identical in every window it is ever opened in, so it lives in a
  :class:`~agentclip.driver.screen.profile.ServiceProfile`: captured once, persisted,
  reused every run and shared by every slot pointed at that service.
* **Where should I look for it?** - genuinely per window, and the only thing
  left here. The user drags one box around the browser window hosting the
  chat, and everything is recognised *inside* it on the spot.

The payoff shows up twice. A browser the user moved, resized or dragged to
another monitor costs one redrawn box instead of six recaptures. And a second
window - :data:`AgentSlot.SUBAGENT`, the chat a delegated sub-agent gets -
costs exactly one drag, because it inherits the master's captures for free.
:data:`AgentSlot.MASTER` is the chat the main agent talks in.

Two slot pointers ride on top of this data (owned by
:class:`~agentclip.driver.automation.controller.AutomationController`, not here):
*calibrating* - which slot the region picker writes into - and *live* - which
slot the automation (paste click, finish detector, auto-copy) is driving right
now. They are deliberately independent: the user must be able to draw the
sub-agent window while the master chat is mid-turn.

Readiness is therefore a function of the *pair* (drawn box, captured kinds)
rather than a property of either, which is why :func:`can_delegate` and friends
are module-level functions. The second half is a tuple of
:class:`~agentclip.driver.screen.profile.TemplateKind` rather than a whole
profile, and since docs/design/ui-monitor.md §11.3 that is a rule and not a
convenience: the pictures live on the MONITOR, the brain is told only WHICH
appearances exist (``Watched.captured``), and a readiness rule that took a
``ServiceProfile`` could only ever be handed an empty one over there. ``can_delegate`` is the single source of truth for
"delegation is available" and it is strict on purpose: without a new-chat
button we cannot open a fresh chat, without a copy button we cannot harvest the
reply, and without a drawn window we can neither paste nor tell when the reply
is done. A half-calibrated slot must read as *not available* rather than fail
halfway through a delegation.

Pure data, stdlib only - no Textual, no OS calls - so the readiness rules are
unit-testable on their own (tests/driver/screen/test_slot.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion


class AgentSlot(StrEnum):
    """Which chat window a slot describes."""

    MASTER = "master"
    SUBAGENT = "subagent"

    @property
    def label(self) -> str:
        """Short all-caps name for the sidebar picker."""
        return "MASTER" if self is AgentSlot.MASTER else "SUB-AGENT"


# Human-readable names for the pieces ``can_delegate`` insists on, in the order
# the user is asked for them. They double as the sidebar's "still missing"
# readout and as the body of the error the master agent gets back when it calls
# ``delegate`` against an uncalibrated slot - so they name what the user has to
# DO, not what the code calls it.
MISSING_CHAT_REGION = "chat region"
MISSING_COPY = "copy button"
MISSING_NEWCHAT = "new-chat button"


@dataclass(slots=True)
class SlotCalibration:
    """The one thing the user drew for one chat window.

    Mutable and long-lived: the sidebar's picker writes into it as the user
    calibrates, and it outlives ``/new`` - a new conversation in the same
    window needs no re-drawing. ``clear`` empties it for when the window itself
    is gone.
    """

    slot: AgentSlot = AgentSlot.MASTER
    # The whole browser window hosting the chat. It is where every appearance
    # is searched for, the last-resort click target when the input box is not
    # found, the scroll target of the auto-copy flow, and - all by itself - the
    # staleness finish detector, since a rectangle that has stopped changing is
    # a finished response whatever a service's pixel cues do.
    chat_region: ScreenRegion | None = None

    def clear(self) -> None:
        """Forget the drawn window. The slot identity stays."""
        self.chat_region = None

    @property
    def is_set(self) -> bool:
        """Has this slot been pointed at a window yet?"""
        return self.chat_region is not None


def can_paste(cal: SlotCalibration, captured: tuple[TemplateKind, ...]) -> bool:
    """Is there anywhere to click before sending Ctrl+V?

    The drawn window, and nothing else: the input box is found by searching the
    service's captured appearance inside it, and failing that the window itself
    is a perfectly good click target. ``captured`` is unused and deliberately
    still in the signature - every readiness rule takes the same pair, so a
    caller never has to remember which of them needs what.
    """
    return cal.is_set


def can_finish(cal: SlotCalibration, captured: tuple[TemplateKind, ...]) -> bool:
    """Can we tell when the model stopped?

    Also the drawn window alone: the staleness detector needs no captured cue,
    only a rectangle to watch stop changing. Busy and idle appearances
    reinforce that verdict when the service has them; neither is required.
    """
    return cal.is_set


def can_copy(cal: SlotCalibration, captured: tuple[TemplateKind, ...]) -> bool:
    """Can the reply be harvested without the user clicking anything?

    The first rule that needs both halves: a window to search, and a captured
    icon to search for.
    """
    return cal.is_set and TemplateKind.COPY in captured


def can_delegate(cal: SlotCalibration, captured: tuple[TemplateKind, ...]) -> bool:
    """Is this slot ready to host a full unattended sub-agent run?

    All of it, deliberately: a fresh chat to run in, somewhere to paste, a way
    to know the reply is done, and a way to copy it back out.
    """
    return (
        cal.is_set
        and TemplateKind.NEW_CHAT in captured
        and can_paste(cal, captured)
        and can_finish(cal, captured)
        and can_copy(cal, captured)
    )


def missing(cal: SlotCalibration, captured: tuple[TemplateKind, ...]) -> tuple[str, ...]:
    """The gaps between this slot and ``can_delegate``, in calibration order.

    Empty exactly when ``can_delegate`` is True. Losing the window takes the
    two buttons with it - there is nowhere left to look for them - which is
    honest rather than noisy: all three are one drag away from being fixed.
    """
    gaps: list[str] = []
    if not cal.is_set:
        gaps.append(MISSING_CHAT_REGION)
    if not can_copy(cal, captured):
        gaps.append(MISSING_COPY)
    if not (cal.is_set and TemplateKind.NEW_CHAT in captured):
        gaps.append(MISSING_NEWCHAT)
    return tuple(gaps)


def new_slots() -> dict[AgentSlot, SlotCalibration]:
    """A fresh, empty slot per window - MainScreen's initial state."""
    return {slot: SlotCalibration(slot) for slot in AgentSlot}
