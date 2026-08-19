"""The noise the loop makes when it cannot go on without the user.

Everything else AgentClip says, it says on screen - and that is the problem
this module exists for: the whole point of the tool is that the user is doing
something else while a turn runs. A loop that stalls on MANUAL_INSERT or
MANUAL_COPY is stalled until a human looks at it, and a rail that says so to an
empty chair says nothing at all.

So: two descending tones, "uuuh-oooh", which is a sound nobody mistakes for a
notification chime. Opt-in per service (``ServicePreset.alert_sound``), because
a tool that beeps at somebody already watching it is noise, and repeatable
(``alert_repeat_seconds``) for the user who walked away.

The player is BLOCKING - ``winsound.Beep`` holds its thread for the length of
the tone - so nothing here may be called from the event loop or the UI thread.
:class:`AttentionAlarm` owns that rule: every sound it makes happens on a
daemon thread of its own, and callers only ever arm and disarm it. The player
itself is injectable for the same reason the clipboard is: a test suite that
made the machine beep is a suite nobody runs twice.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from contextlib import suppress

# Descending, and wide enough apart to read as two notes rather than a warble:
# (hertz, milliseconds). Under half a second in total, which is short enough to
# repeat every few seconds without becoming an alarm clock.
UH_OH_TONES: tuple[tuple[int, int], ...] = ((660, 180), (440, 240))
# The name every thread this module starts carries, so a hung one is
# identifiable in a stack dump.
ALERT_THREAD_NAME = "agentclip-alert"


def play_uh_oh() -> None:
    """Sound one "uh-oh". Blocking - never call this on the event loop.

    Windows gets the real thing through ``winsound``, which is the platform the
    tool is used on and the only one with a tone generator in the standard
    library. Everywhere else falls back to the terminal bell, twice, which is
    not two tones but is at least two sounds.

    The bell is written to ``sys.__stdout__`` rather than ``sys.stdout``: a
    Textual app has redirected the latter into its own capture, where a BEL
    byte would end up in a log instead of reaching the terminal emulator that
    has to ring it.
    """
    if sys.platform == "win32":  # pragma: no cover - platform branch
        import winsound

        for frequency, milliseconds in UH_OH_TONES:
            winsound.Beep(frequency, milliseconds)
        return
    stream = sys.__stdout__ or sys.stdout  # pragma: no cover - platform branch
    if stream is None:  # pragma: no cover - a fully detached process
        return
    stream.write("\a\a")
    stream.flush()


class AttentionAlarm:
    """Arm while the loop is waiting on the user; disarm when it stops.

    Armed is a STATE, not an event: ``arm`` while already armed is deliberately
    a no-op, so a loop that walks from one attention state straight into
    another (a failed paste that becomes a manual copy) is one uh-oh and one
    repeat schedule rather than two of each. ``disarm`` is what ends it, and it
    is safe to call when nothing is sounding - which is what lets the single
    hook in ``set_loop_state`` be "arm if this state needs the user, disarm
    otherwise" with no bookkeeping of its own.

    Every sound happens on a daemon thread, so an exit never waits for one, and
    they are serialised behind one lock so two overlapping asks queue instead of
    interleaving into a chord.
    """

    def __init__(self, play: Callable[[], None] | None = None) -> None:
        self._play = play if play is not None else play_uh_oh
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        # Set together and only under ``_lock``: the flag that says "armed" IS
        # the stop event's existence.
        self._stop: threading.Event | None = None

    @property
    def armed(self) -> bool:
        """Is the alarm currently sounding for an attention state?"""
        with self._lock:
            return self._stop is not None

    def arm(self, *, repeat_seconds: float = 0.0) -> None:
        """Sound one uh-oh now, and - for ``repeat_seconds`` above zero - again
        every that many seconds until ``disarm``."""
        with self._lock:
            if self._stop is not None:
                return
            stop = threading.Event()
            self._stop = stop
        threading.Thread(
            target=self._run,
            args=(stop, repeat_seconds),
            name=ALERT_THREAD_NAME,
            daemon=True,
        ).start()

    def chime(self) -> None:
        """One uh-oh, for a caller with no state to leave - a re-sync the loop
        itself never sees, like a protocol error the user has to answer by
        re-copying. Never repeats and never arms: there is nothing to disarm it
        afterwards."""
        threading.Thread(target=self._sound, name=ALERT_THREAD_NAME, daemon=True).start()

    def disarm(self) -> None:
        """Stop repeating. A tone already in flight finishes - it is milliseconds
        and there is no interrupting ``winsound.Beep`` anyway."""
        with self._lock:
            stop, self._stop = self._stop, None
        if stop is not None:
            stop.set()

    def _run(self, stop: threading.Event, repeat_seconds: float) -> None:
        self._sound()
        if repeat_seconds <= 0:
            return
        # ``wait`` rather than ``sleep``: a disarm has to land inside the gap,
        # not after it.
        while not stop.wait(repeat_seconds):
            self._sound()

    def _sound(self) -> None:
        # A machine with no sound device, a winsound that refuses, a closed
        # stdout: the loop is already asking the user for help, and losing the
        # sound is not worth losing the turn - or the thread.
        with self._play_lock, suppress(Exception):
            self._play()
