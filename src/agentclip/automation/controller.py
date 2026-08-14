"""AutomationController: the UI-agnostic screen-automation core.

Sibling of :class:`~agentclip.app.controller.SessionController` and the same
kind of object: the state and the decisions behind what AgentClip does *to* the
browser chat window, lifted out of the Textual ``MainScreen`` so a second shell
can drive the identical loop. It talks to the UI only through the
:class:`~agentclip.automation.view.AutomationView` port and therefore imports no
Textual (docs/design/gui.md §1).

It is being filled one slice at a time; today it holds the state that everything
else in the loop is read against, plus the first of the threads:

**The armed flag.** ``/armed`` and F5. DISARMED means the tool stops ACTING on
the machine - no clicks, no synthetic paste, no cursor moves, no focus stealing,
no clipboard watching - while every read-only half (capture, the finish
detectors, the whole sidebar readout) stays live. It lives *below* both shells
rather than inside one, because with two of them a view-owned flag is a flag
that drifts. The consequences it owns *itself* are the ones made of state down
here: the clipboard watcher a disarm stops and the memory of what that watcher
was doing. The rest - the three remaining chokepoints, the toasts, the status
bar - is still the shell's, which is why ``set_os_armed`` returns the state now
in force.

**The clipboard watcher.** One plain ``threading.Thread`` running
:func:`agentclip.clip.watcher.watch`, which was always thread-agnostic - a
blocking poll loop taking ``should_stop``/``on_capture`` callbacks - so only its
OWNER moved down here. Captures leave the thread through the
``on_clipboard_captured`` callback the shell hands in at construction (the
Textual shell posts a message from it; the GUI will enqueue onto its bridge),
which is the same non-blocking, thread-safe contract every ``AutomationView``
method has.

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

Threading: every method here is called from the UI thread, and the watcher
thread only ever calls *out* (through ``on_clipboard_captured``). The one piece
of state it shares with the loop it started is a ``threading.Event``, which is
what makes "stop" a flag rather than a lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from agentclip.automation.view import AutomationView
from agentclip.clip.base import ClipboardProvider
from agentclip.clip.watcher import SelfWriteSet, watch
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, SlotCalibration, new_slots


def _accept_all(_text: str) -> bool:
    """Watcher filter for a controller nobody handed one to."""
    return True


def _drop_capture(_text: str) -> None:
    """Capture sink for a controller nobody handed one to."""


class AutomationController:
    """The automation's state, driving one :class:`AutomationView`."""

    def __init__(
        self,
        view: AutomationView,
        *,
        services: Mapping[str, str] | None = None,
        clipboard: ClipboardProvider | None = None,
        self_writes: SelfWriteSet | None = None,
        poll_interval_ms: int = 300,
        accepts: Callable[[str], bool] | None = None,
        on_clipboard_captured: Callable[[str], None] | None = None,
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
        # -- the clipboard watcher ---------------------------------------------
        # The backend is CONSTRUCTED by the shell (cli.py picks it at startup and
        # hands it down) and only driven here: which clipboard exists is a
        # startup question about the machine, not an automation decision. None
        # means a controller nobody wired one into - the headless tests - and
        # behaves exactly like the manual provider: nothing to poll.
        self._clipboard = clipboard
        # Hashes of what WE put on the clipboard, so the watcher cannot ingest
        # our own outbound back as a reply. Shared with the shell rather than
        # owned here, because the writes are still the shell's (``write_via``)
        # until the delivery path comes down in a later slice.
        self._self_writes = self_writes if self_writes is not None else SelfWriteSet()
        self._poll_interval_ms = poll_interval_ms
        # The protocol pre-filter, passed in for the same reason ``watch`` takes
        # it: ``agentclip.protocol`` is above this layer (tests/test_layering.py),
        # and a watcher that accepted everything would drive a turn off any copy.
        self._accepts: Callable[[str], bool] = accepts if accepts is not None else _accept_all
        self._on_capture: Callable[[str], None] = (
            on_clipboard_captured if on_clipboard_captured is not None else _drop_capture
        )
        # The running watcher and its stop flag, or None/None when nothing is
        # polling. They move together and only on the UI thread.
        self._watcher: threading.Thread | None = None
        self._watcher_stop: threading.Event | None = None
        # What the watcher was doing when a disarm took it away, so re-arming
        # restores THAT rather than a guess: a user who paused it themselves,
        # disarmed and re-armed does not get handed back a watcher they switched
        # off. Written on transitions only - see ``set_os_armed``.
        self._watch_before_disarm = False

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
        """Arm or disarm, move the watcher, and repaint. ``None`` toggles.

        Returns the state that is now in force, so a caller can drive the
        consequences it still owns off one call rather than re-reading and
        hoping - the status segment, the footer's bindings and the toast are all
        the shell's, and all of them run *after* this returns.

        Order inside: the flag, then the machine, then the paint. The watcher is
        settled before ``paint_armed`` so that anything a shell draws from this
        object is drawn from a finished transition; the shell's own status bar
        (which reports the watcher too) repaints after the call for the same
        reason.

        Painting is **unconditional**, matching the behaviour the switch has
        always had: an explicit `/armed off` typed twice repaints rather than
        looking ignored, and the shell re-toasts on top of that for the same
        reason. The watcher half, in contrast, moves only on a real TRANSITION:
        a second `/armed off` that re-read the (already stopped) watcher would
        remember "it was off" and quietly lose the watcher the first one took
        away.

        The rule the transition implements, of the two on offer: disarming
        forces the watcher off and remembers what it was; re-arming puts *that*
        back. Re-arming undoes the disarm, nothing more.
        """
        was_armed = self._os_armed
        self._os_armed = (not was_armed) if target is None else target
        armed = self._os_armed
        if armed and not was_armed:
            if self._watch_before_disarm:
                self.start_watching()  # no-ops in manual mode, and if already up
            self._watch_before_disarm = False
        elif was_armed and not armed:
            self._watch_before_disarm = self.watching
            self.stop_input()
        self._view.paint_armed(armed)
        return armed

    # == the clipboard watcher =================================================

    @property
    def watching(self) -> bool:
        """Is a watcher thread polling the clipboard right now?"""
        return self._watcher is not None

    @property
    def watcher_thread(self) -> threading.Thread | None:
        """The watcher thread itself, or None. For shells that mirror its
        existence into their own chrome, and for tests that join it."""
        return self._watcher

    def start_input(self) -> None:
        """A session wants the clipboard watched (``ChatView.start_input``).

        Three answers, and only one of them starts a thread. Disarmed: the
        session still WANTS a watcher, so the request is *remembered* and the
        next re-arm honours it - this is the one place the remembered state is
        set from an intention rather than from an observation, and without it a
        user who started a session while disarmed would have to press F5 and
        then `w` to get back to a normal app. No real backend: say so, once,
        because from here on the user is copying and pasting by hand.
        """
        if not self._os_armed:
            self._watch_before_disarm = True
            return
        if self._clipboard is not None and self._clipboard.name == "manual":
            self._view.notify(
                "manual clipboard mode: press i and paste the model's reply into the box; "
                "outbound payloads go out via the terminal's OSC-52 copy",
                severity="warning",
                timeout=10,
            )
            return
        self.start_watching()

    def start_watching(self) -> None:
        """Start the poll loop, unless there is nothing to poll or it is already
        running. The raw start behind ``start_input``, the re-arm and the shell's
        pause/resume key alike - no arming check of its own, because each of
        those callers has already made that decision."""
        if self._watcher is not None or self._clipboard is None:
            return
        if self._clipboard.name == "manual":
            return
        stop = threading.Event()
        provider = self._clipboard
        interval = self._poll_interval_ms
        accepts = self._accepts
        on_capture = self._on_capture
        self_writes = self._self_writes

        def loop() -> None:
            watch(
                provider,
                interval,
                should_stop=stop.is_set,
                accepts=accepts,
                on_capture=on_capture,
                self_writes=self_writes,
            )

        thread = threading.Thread(target=loop, name="agentclip-clipwatch", daemon=True)
        self._watcher = thread
        self._watcher_stop = stop
        thread.start()

    def stop_input(self) -> None:
        """Stop watching (``ChatView.stop_input``), without waiting for it.

        Deliberately no join: the caller is the UI thread and the loop only
        notices between ticks, so joining would freeze the interface for up to a
        poll interval. Dropping the handles makes "stopped" true immediately for
        everything that asks, and the thread it leaves finishing its last tick
        holds nothing anyone else waits on - a capture that lands in that window
        is a real capture the user really made, exactly as it was when a Textual
        worker's ``cancel()`` owned this.
        """
        if self._watcher_stop is not None:
            self._watcher_stop.set()
        self._watcher = None
        self._watcher_stop = None

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
