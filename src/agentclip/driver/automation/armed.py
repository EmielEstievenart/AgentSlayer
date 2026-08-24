"""The ARMED switch, and the one consequence it owns itself.

``/armed`` and F5. DISARMED means the tool stops ACTING on the machine - no
clicks, no synthetic paste, no cursor moves, no focus stealing, no clipboard
watching - while every read-only half (the monitor's own polling, the whole
sidebar readout) stays live. Detection is not what disarming turns off.

It is policy, so it stays in the brain (docs/design/ui-monitor.md §2.3) and below
both shells rather than inside one: with two of them, a view-owned flag is a flag
that drifts. The consequence bundled in here is the one made of state - whether
the monitor is watching the clipboard, and the memory of what that watcher was
doing when a disarm took it away. The rest (the chokepoints in the recipes, the
toasts, the status bar) is somebody else's, which is why :meth:`ArmedSwitch.set`
returns the state now in force.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentclip.driver.automation.view import AutomationView

if TYPE_CHECKING:
    from agentclip.driver.automation.machine import MonitorLike


class ArmedSwitch:
    """May the tool touch the machine right now - and is anyone watching the
    clipboard while it may?"""

    def __init__(self, monitor: MonitorLike, view: AutomationView) -> None:
        self._monitor = monitor
        self._view = view
        # True is every version of this app before the switch existed, and it
        # stays the default: the tool is useful precisely because it acts.
        self._armed = True
        # Whether a watcher is polling right now, as the monitor last answered.
        self._watching = False
        # What the watcher was doing when a disarm took it away, so re-arming
        # restores THAT rather than a guess: a user who paused it themselves,
        # disarmed and re-armed does not get handed back a watcher they switched
        # off. Written on transitions only.
        self._before_disarm = False

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def watching(self) -> bool:
        """Is the monitor polling the clipboard right now?"""
        return self._watching

    def set(self, target: bool | None) -> bool:
        """Arm or disarm, move the watcher, and repaint. ``None`` toggles.

        Order inside: the flag, then the machine, then the paint, so that anything
        a shell draws from this is drawn from a finished transition.

        Painting is **unconditional**, matching the behaviour the switch has always
        had: an explicit `/armed off` typed twice repaints rather than looking
        ignored. The watcher half, in contrast, moves only on a real TRANSITION - a
        second `/armed off` that re-read the (already stopped) watcher would
        remember "it was off" and quietly lose the watcher the first one took away.
        Re-arming undoes the disarm, nothing more.
        """
        was_armed = self._armed
        self._armed = (not was_armed) if target is None else target
        if self._armed and not was_armed:
            if self._before_disarm:
                self.watch(True)  # no-ops in manual mode, and if already up
            self._before_disarm = False
        elif was_armed and not self._armed:
            self._before_disarm = self._watching
            self.watch(False)
        self._view.paint_armed(self._armed)
        return self._armed

    def watch(self, on: bool) -> None:
        """Ask for a watcher, or for it to stop, and record what came back.

        The single door, because "asked for" and "running" are not the same thing:
        a machine with no clipboard backend, or with the write-only manual one,
        honours neither request and says so - and everything that reads
        :attr:`watching` has to see that rather than our intention.
        """
        self._watching = self._monitor.watch_clipboard(on)

    def start_input(self) -> None:
        """A session wants the clipboard watched (``ChatView.start_input``).

        Three answers, and only one of them starts anything. Disarmed: the session
        still WANTS a watcher, so the request is *remembered* and the next re-arm
        honours it - without which a user who started a session while disarmed
        would have to press F5 and then `w` to get back to a normal app. No real
        backend: say so, once, because from here on the user is copying and
        pasting by hand.
        """
        if not self._armed:
            self._before_disarm = True
            return
        if self._monitor.clipboard_kind == "manual":
            self._view.notify(
                "manual clipboard mode: press i and paste the model's reply into the box; "
                "outbound payloads go out via the terminal's OSC-52 copy",
                severity="warning",
                timeout=10,
            )
            return
        self.watch(True)

    def stop_input(self) -> None:
        """Stop watching (``ChatView.stop_input``), without waiting for it.

        Deliberately no join anywhere under this: the caller is the UI thread and
        the watcher only notices between polls, so joining would freeze the
        interface. "Stopped" is true immediately for everything that asks, and a
        capture that lands in the window the last poll leaves open is a real
        capture the user really made.
        """
        self.watch(False)
