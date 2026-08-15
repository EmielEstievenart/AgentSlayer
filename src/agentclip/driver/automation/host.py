"""The ``AutomationHost`` port: what the OS-acting sequences still ask the shell.

The counterpart of :mod:`agentclip.driver.automation.view`, and deliberately its
opposite half. ``AutomationView`` is what the automation TELLS a shell (paint
this); this is the short list of things it still has to ASK one, because the
answers are made of state a shell owns and this layer does not:

* what the live window's service IS (a ``ServicePreset`` resolved out of the
  shell's ``Config``) and what it LOOKS like (a ``ServiceProfile`` read off disk
  into the shell's own cache) - the same reason ``has_appearance`` has always
  been a callback;
* where an appearance is on screen right now (``find_all``), which is here for
  the reason the copy click below is and no other: it is the one search that
  already had a shell-side stand-in the suites substitute, and a sequence that
  called past the shell could not be handed an imaginary screen.
  :meth:`AutomationController.find_all` is the implementation both shells
  delegate to - the search itself is no longer a shell's;
* the two acts that end a harvest and cannot live here: handing a non-protocol
  reply to the SESSION (``agentclip.app`` is above this layer) and rebuilding
  the detector set around a window the automation has just moved to (which
  needs the profile cache and the shell's readout);
* the verified copy click, which is here rather than inlined for one reason
  only: it is the seam the Textual suites stub to skip a clipboard round-trip.
  :meth:`AutomationController.verified_copy_click` is the implementation a shell
  delegates back to;
* where an outbound payload goes when the clipboard PROVIDER refuses it. The
  write itself is this layer's (the controller holds the provider and the
  self-write set), but the fallback is not: the TUI's is the terminal's OSC-52
  escape, which is a Textual call and exists in no other shell (docs/design/gui.md
  §0). So the controller writes, and hands a shell the payload it could not
  place - which is the same seam ``deliver``'s ``clipboard_ok`` reports on.

Same thread contract as everything else this layer hands out, with one
difference worth naming: unlike ``AutomationView``, these are called from the
EVENT LOOP - the sequences that ask them are coroutines a shell scheduled - so
an implementation may block on a thread the way ``find_all`` already does, and
may touch widgets.

A controller nobody wired a host into gets :class:`NullHost`, which answers
"nothing is calibrated" to everything. That is the honest reading of "no shell",
and it makes every sequence below refuse rather than guess.
"""

from __future__ import annotations

from typing import Protocol

from agentclip.config import ServicePreset
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot


class AutomationHost(Protocol):
    # -- what a service is, and what it looks like ----------------------------
    def live_preset(self) -> ServicePreset: ...

    def profile_for(self, slot: AgentSlot) -> ServiceProfile: ...

    # -- where an appearance is on screen -------------------------------------
    # Every place ``kind`` is inside a slot's drawn chat region, in absolute
    # coordinates. ``scene`` reuses a frame the caller already captured and may
    # never be combined with ``slot``.
    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]: ...

    # -- the acts that finish a harvest ---------------------------------------
    async def verified_copy_click(self, target: ScreenRegion) -> bool: ...

    async def ingest_harvest(self) -> None: ...

    def copy_seen_note(self) -> str: ...

    def rebuild_detectors(self) -> None: ...

    # -- where a payload goes when the clipboard will not take it --------------
    # Called only after the provider has raised, and only to PARK the text
    # somewhere a human can still reach it. A shell with no second channel does
    # nothing; the delivery carries on either way, because the branch that
    # follows is the existing "the paste is yours to do" one.
    def park_off_clipboard(self, text: str) -> None: ...


class NullHost:
    """The answers a controller with no shell behind it gives: nothing is
    calibrated, nothing is on screen, and no click can land."""

    def live_preset(self) -> ServicePreset:
        return ServicePreset(key="", label="", max_paste_chars=0, total_context_chars=0)

    def profile_for(self, slot: AgentSlot) -> ServiceProfile:
        return ServiceProfile(key="")

    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        return []

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        return False

    async def ingest_harvest(self) -> None:
        """Nothing to hand a reply to."""

    def copy_seen_note(self) -> str:
        return ""

    def rebuild_detectors(self) -> None:
        """No detector set to rebuild."""

    def park_off_clipboard(self, text: str) -> None:
        """No second channel: the payload stays wherever the session wrote it."""
