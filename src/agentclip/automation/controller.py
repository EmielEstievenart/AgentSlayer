"""AutomationController: the UI-agnostic screen-automation core.

Sibling of :class:`~agentclip.app.controller.SessionController` and the same
kind of object: the state and the decisions behind what AgentClip does *to* the
browser chat window, lifted out of the Textual ``MainScreen`` so a second shell
can drive the identical loop. It talks to the UI only through the
:class:`~agentclip.automation.view.AutomationView` port and therefore imports no
Textual (docs/design/gui.md §1).

It is being filled one slice at a time; today it holds the two pieces of state
that everything else in the loop is read against:

**The armed flag.** ``/armed`` and F5. DISARMED means the tool stops ACTING on
the machine - no clicks, no synthetic paste, no cursor moves, no focus stealing,
no clipboard watching - while every read-only half (capture, the finish
detectors, the whole sidebar readout) stays live. It lives *below* both shells
rather than inside one, because with two of them a view-owned flag is a flag
that drifts. What the shells still own is the *consequences*: the clipboard
watcher a disarm stops (and the memory of what it was doing), the four
chokepoints that consult the flag, and the toasts. This object owns the answer
to "may we touch the machine?", and nothing else.

**The slot pointers.** Which chat window is being configured and which one is
being driven, plus the drawn box and the service key behind each. Two pointers,
independent on purpose: *calibrating* is the slot behind the selected window tab
- what the sidebar's region picker writes into - and *live* is the slot the
automation (paste click, detector poller, auto-copy) is driving right now. The
user must be able to draw the sub-agent's window while the master chat is
mid-turn, and a delegation must be able to retarget the automation without
dragging the user's view along with it.

What is emphatically NOT here is the *content* behind those pointers. A
:class:`~agentclip.screen.profile.ServiceProfile` - what a service LOOKS like -
is loaded from disk and cached by the shell, which hands resolved values in;
this object holds the service KEY per window and nothing about what that key
resolves to. Same rule for the calibration: the controller owns which slot is
which, the shell owns what it does with the rectangle.

Threading: every method here is called from the UI thread today. The port's
thread contract (see ``AutomationView``) is what the *later* slices need, when
this object grows the watcher and poller threads that call it back.
"""

from __future__ import annotations

from collections.abc import Mapping

from agentclip.automation.view import AutomationView
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, SlotCalibration, new_slots


class AutomationController:
    """The automation's state, driving one :class:`AutomationView`."""

    def __init__(
        self,
        view: AutomationView,
        *,
        services: Mapping[str, str] | None = None,
    ) -> None:
        self._view = view
        # True is every version of this app before the switch existed, and it
        # stays the default: the tool is useful precisely because it acts.
        self._os_armed = True
        # Every user-drawn calibration, one set per agent slot - which since the
        # appearance model is exactly one thing: the chat window. That single
        # box is where every appearance is searched for, the click target of
        # last resort, and the whole calibration of the staleness detector. It
        # describes where a service's window IS, not what one conversation said,
        # so it survives /new; only the pointers below reset.
        self._slots = new_slots()
        self._calibrating: AgentSlot = AgentSlot.MASTER
        self._live: AgentSlot = AgentSlot.MASTER
        # The service each browser WINDOW is pointed at, keyed by whatever the
        # shell calls its windows - opaque strings here, deliberately: window
        # ids are the shell's tab vocabulary and this object never interprets
        # them. Two windows, two services: a big-context chat for the
        # conversation the user steers, something cheap and fast for delegated
        # sub-tasks.
        self._services: dict[str, str] = dict(services or {})

    # == the ARMED switch =====================================================

    @property
    def os_armed(self) -> bool:
        """May the tool touch the machine right now?

        The awkward name is deliberate: "armed" already means three unrelated
        things in the TUI (``_copy_armed``, the ``st-armed`` status style,
        ``SEND_READY_ARMED``), and this is the only one about the OS.
        """
        return self._os_armed

    def set_os_armed(self, target: bool | None) -> bool:
        """Arm or disarm, and repaint. ``None`` toggles (bare `/armed`, F5).

        Returns the state that is now in force, so a caller can drive the
        consequences it owns off one call rather than re-reading and hoping.

        Painting is **unconditional**, matching the behaviour the switch has
        always had: an explicit `/armed off` typed twice repaints rather than
        looking ignored, and the shell re-toasts on top of that for the same
        reason. Anything that must move only on a real TRANSITION
        - the clipboard watcher, whose remembered state a second disarm would
        overwrite with "it was off" - compares the return value against what it
        read before the call, which is the shell's job while the watcher is.
        """
        self._os_armed = (not self._os_armed) if target is None else target
        self._view.paint_armed(self._os_armed)
        return self._os_armed

    # == the slot pointers =====================================================

    @property
    def live_slot(self) -> AgentSlot:
        """Which slot the automation is driving (paste click, poller, auto-copy)."""
        return self._live

    @property
    def calibrating_slot(self) -> AgentSlot:
        """Which slot the sidebar/settings surface is configuring."""
        return self._calibrating

    @property
    def live(self) -> SlotCalibration:
        """The driven slot's calibration."""
        return self._slots[self._live]

    @property
    def calibrating(self) -> SlotCalibration:
        """The configured slot's calibration."""
        return self._slots[self._calibrating]

    @property
    def slots(self) -> dict[AgentSlot, SlotCalibration]:
        """Every slot's calibration, by slot.

        The live mapping, not a copy: a ``SlotCalibration`` is mutable and
        long-lived by design (``screen/slot.py``), so handing out the dict says
        no more than ``calibration`` already does one slot at a time.
        """
        return self._slots

    def calibration(self, slot: AgentSlot) -> SlotCalibration:
        """One slot's calibration."""
        return self._slots[slot]

    def set_calibration(self, slot: AgentSlot, region: ScreenRegion | None) -> None:
        """Adopt (or forget) the box the user drew around a chat window.

        ``slot`` is a parameter rather than a read of ``calibrating`` because
        the picker blocks for as long as the user takes to drag, and the
        pointers move on their own meanwhile - what was selected when the picker
        opened is what the user was answering.
        """
        self._slots[slot].chat_region = region

    def select_live_slot(self, slot: AgentSlot) -> None:
        """Point the automation at another chat window (a delegation starting or
        ending, and ``/new`` going home to the master)."""
        self._live = slot

    def select_calibrating_slot(self, slot: AgentSlot) -> None:
        """Point the configuration surface at another chat window. Never moves
        ``live``: selecting a tab must not retarget the automation."""
        self._calibrating = slot

    # == a service per window ==================================================

    def service_of(self, window: str) -> str:
        """The service key a window is pointed at, or ``""`` for an unknown one.

        Raw: resolving a stale or blank key against the config is the caller's,
        because the config is the caller's.
        """
        return self._services.get(window, "")

    def set_service(self, window: str, key: str) -> None:
        """Point one window at a service."""
        self._services[window] = key

    def services(self) -> dict[str, str]:
        """Every window's service key - a copy, safe to iterate while writing."""
        return dict(self._services)
