"""Agent slots: one calibration set per browser chat window AgentClip drives.

AgentClip clicks around *someone else's* chat window, and everything it needs to
do that - where the window is, which input box is on screen, what "still
generating" looks like, where the copy button is, where the new-chat button is -
is drawn by the user and lives for the app run: it describes where the service's
windows are, not what one conversation said, so it survives ``/new``. Sub-agent
delegation adds a second such window, so those calibrations stop being
screen-wide singletons and become a set *per slot*: :data:`AgentSlot.MASTER` is
the chat the main agent talks in, :data:`AgentSlot.SUBAGENT` the one a delegated
sub-agent gets.

Two slot pointers ride on top of this data (owned by the TUI, not here):
*calibrating* - which slot the sidebar's pickers write into - and *live* - which
slot the automation (paste click, finish detector, auto-copy) is driving right
now. They are deliberately independent: the user must be able to calibrate the
sub-agent window while the master chat is mid-turn.

``can_delegate`` is the single source of truth for "delegation is available",
and it is strict on purpose: without ``new_chat`` we cannot open a fresh chat,
without a copy button we cannot harvest the reply, and without a finish detector
we never learn when to harvest it. A half-calibrated slot must read as *not
available* rather than fail halfway through a delegation.

Pure data, stdlib only - no Textual, no OS calls - so the readiness rules are
unit-testable on their own (tests/screen/test_slot.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion


class AgentSlot(StrEnum):
    """Which chat window a calibration set describes."""

    MASTER = "master"
    SUBAGENT = "subagent"

    @property
    def label(self) -> str:
        """Short all-caps name for the sidebar picker."""
        return "MASTER" if self is AgentSlot.MASTER else "SUB-AGENT"


# Human-readable names for the pieces ``can_delegate`` insists on, in the order
# the user is asked to draw them. They double as the sidebar's "still missing"
# readout and (later) as the body of the error the master agent gets back when
# it calls ``delegate`` against an uncalibrated slot.
MISSING_CHATBOX = "chat box"
MISSING_FINISH = "finish detector"
MISSING_COPY = "copy button"
MISSING_NEWCHAT = "new-chat button"


@dataclass(slots=True)
class SlotCalibration:
    """Everything the user drew for one chat window.

    Mutable and long-lived: the sidebar's pickers write single fields into it as
    the user calibrates, and the set outlives ``/new`` - a new conversation in
    the same windows needs no re-drawing. ``clear`` empties the whole set at
    once for when the windows themselves are gone.

    The busy/idle finish detectors are region + baseline pairs rather than
    fixed regions because their comparison is the *inverse* of a search: the
    busy baseline is captured while the model generates, so a match means
    "still going". The stale detector is a bare region with no stored baseline
    at all - its tracker's first polled frame IS the baseline, and every later
    frame is compared to the one before it.

    The chat input boxes and the new-chat button used to live here too. They
    don't any more: where they are is answered by searching the SERVICE's
    captured appearance inside ``chat_region`` (screen.profile), so the only
    thing that is per window is the box the user drew.
    """

    slot: AgentSlot = AgentSlot.MASTER
    # The whole browser window hosting the chat: where every appearance is
    # searched for, the last-resort click target, and the vertical span of the
    # copy-button search band.
    chat_region: ScreenRegion | None = None
    # Calibrated WHILE generating: a later match means "still generating".
    busy_region: ScreenRegion | None = None
    busy_baseline: RegionImage | None = None
    # Calibrated while IDLE: a later match means "finished".
    idle_region: ScreenRegion | None = None
    idle_baseline: RegionImage | None = None
    # The response area itself, for the stability ("stale") detector: once it
    # stops changing frame to frame the model is done. No baseline stored -
    # the poller's tracker keeps its own previous frame.
    stale_region: ScreenRegion | None = None

    def clear(self) -> None:
        """Forget every calibration. The slot identity stays."""
        self.chat_region = None
        self.busy_region = None
        self.busy_baseline = None
        self.idle_region = None
        self.idle_baseline = None
        self.stale_region = None

    @property
    def can_paste(self) -> bool:
        """Is there anywhere to click before sending Ctrl+V?

        The drawn chat window, and nothing else: the input box is found by
        searching the service's captured appearance inside it, and failing that
        the window itself is the click target.
        """
        return self.chat_region is not None

    @property
    def can_finish(self) -> bool:
        """Can we tell when the model stopped? Any one detector is enough. For
        busy/idle the baseline is what the poller actually needs, so that is
        what is checked; the stale detector needs only its region."""
        return (
            self.busy_baseline is not None
            or self.idle_baseline is not None
            or self.stale_region is not None
        )


# Readiness is no longer a property of the slot alone: half the answer is what
# the SERVICE looks like (the copy button today, more of it shortly), and that
# lives in a ServiceProfile shared by both slots. So the rules move out of the
# dataclass and become functions of the pair - which also keeps them honest
# about the fact that pointing a second slot at the same service inherits every
# captured appearance for free.


def _findable(cal: SlotCalibration, profile: ServiceProfile, kind: TemplateKind) -> bool:
    """Can ``kind`` be located in this slot? Both halves are needed: a window
    to search in, and a captured appearance to search for."""
    return cal.chat_region is not None and profile.has(kind)


def can_copy(cal: SlotCalibration, profile: ServiceProfile) -> bool:
    """Can the reply be harvested without the user clicking anything?"""
    return _findable(cal, profile, TemplateKind.COPY)


def can_delegate(cal: SlotCalibration, profile: ServiceProfile) -> bool:
    """Is this slot ready to host a full unattended sub-agent run?

    All four, deliberately: a fresh chat to run in, somewhere to paste, a way
    to know the reply is done, and a way to copy it back out.
    """
    return (
        _findable(cal, profile, TemplateKind.NEW_CHAT)
        and cal.can_paste
        and cal.can_finish
        and can_copy(cal, profile)
    )


def missing(cal: SlotCalibration, profile: ServiceProfile) -> tuple[str, ...]:
    """The gaps between this slot and ``can_delegate``, in calibration order.

    Empty exactly when ``can_delegate`` is True.
    """
    gaps: list[str] = []
    if not cal.can_paste:
        gaps.append(MISSING_CHATBOX)
    if not cal.can_finish:
        gaps.append(MISSING_FINISH)
    if not can_copy(cal, profile):
        gaps.append(MISSING_COPY)
    if not _findable(cal, profile, TemplateKind.NEW_CHAT):
        gaps.append(MISSING_NEWCHAT)
    return tuple(gaps)


def new_slots() -> dict[AgentSlot, SlotCalibration]:
    """A fresh, empty calibration set per slot - MainScreen's initial state."""
    return {slot: SlotCalibration(slot) for slot in AgentSlot}
