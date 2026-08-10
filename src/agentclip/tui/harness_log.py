"""The harness decision log: every move the loop made, and WHY it made it.

The sidebar's STATE rail says where the browser-automation loop is
(:mod:`agentclip.tui.loop_state`); this says how it got there. Those are
different questions, and the second one is the one a stuck user actually has -
"why is it asking me to copy the reply myself?" has at least four answers (the
tool is disarmed, the service has no captured copy button, the button was not
found on screen, the click did not take) and the rail draws the same
``MANUAL_COPY`` box for all of them.

So each entry carries a REASON, and the reasons are not invented here: they are
lifted from the prose the code already had at the decision - the toast it
raises, the branch comment above it, the docstring that explains the rule. A
reason that has to be written twice is a reason that will drift, so where a
notify() already says the thing, the log says the same thing.

The contract, in four lines:

* ``MainScreen._set_loop_state`` is the sole writer of ``LoopState`` and now
  demands a reason, so a transition cannot reach the rail without reaching the
  log. Everything else (the send gate, the trigger arming, the auto-copy flow's
  failures, the clipboard, the armed switch, the session boundaries) appends
  through the same small helper.
* The store is a ``deque`` bounded at :data:`HARNESS_LOG_MAX`, the same 500 the
  transcript panel prunes to: this is a debugging tail, not an archive.
* It SURVIVES ``/new``. A wedged user's first move is often to reset the
  session, and clearing the log there would destroy the very evidence they are
  about to go looking for - the reset writes its own entry instead, which is
  the boundary marker they need.
* Appends happen on the UI event loop only. Worker threads reach the screen
  through ``post_message``, and the async flows log after their awaits return,
  so the deque is never touched concurrently and needs no lock.

Nothing is written to disk. That matches the rest of the screen layer (the
appearances are the only thing it persists) and keeps a log that records what
the tool did to somebody's screen out of the filesystem until there is a
reason - the transcript's export-on-demand pattern is the door if one appears.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# One entry per decision, and a busy session makes a handful per turn; 500 is
# many hundreds of turns of tail. Deliberately the same number as
# ``TranscriptPanel.MAX_EVENTS`` - two bounded in-memory tails of the same run.
HARNESS_LOG_MAX = 500

# The kinds, as they are printed. Short and fixed-width so the log reads as a
# column rather than a paragraph: the eye finds "state" rows in a wall of
# "gate" rows without any styling at all.
KIND_STATE = "state"  # a LoopState transition (the rail moved)
KIND_GATE = "gate"  # the ready-to-send gate opening, seeing, letting go
KIND_TRIGGER = "trigger"  # the auto-copy trigger arming on detector evidence
KIND_COPY = "copy"  # a step of the auto-copy flow, mostly its failures
KIND_CLIPBOARD = "clipboard"  # a capture ingested
KIND_SESSION = "session"  # session start / end / reset boundaries
KIND_ARMED = "armed"  # the global ARMED switch moving

_KINDS = (
    KIND_STATE,
    KIND_GATE,
    KIND_TRIGGER,
    KIND_COPY,
    KIND_CLIPBOARD,
    KIND_SESSION,
    KIND_ARMED,
)
_KIND_COLUMN = max(len(kind) for kind in _KINDS)


@dataclass(frozen=True)
class HarnessEntry:
    """One decision: when it was taken, what kind it was, and what it said.

    ``time`` is local wall-clock ``HH:MM:SS`` and no date, because the log is
    read while the thing is still happening - "twelve seconds ago" is the
    question, never "which Tuesday". It is a string rather than a timestamp on
    purpose: nothing computes with it, and storing the rendered form keeps the
    entry a value the screen can print without a formatter of its own.

    ``text`` is the whole sentence, reason included. State entries read
    ``OLD → NEW — reason``; the rest are plain statements. Assembling it at the
    append site rather than storing the parts is what lets one dataclass carry
    seven kinds of decision without growing an optional field per kind.
    """

    kind: str
    text: str
    time: str = field(default_factory=lambda: time.strftime("%H:%M:%S"))

    @property
    def line(self) -> str:
        """The entry as one printed row: ``12:04:31  state     A → B — why``."""
        return f"{self.time}  {self.kind:<{_KIND_COLUMN}}  {self.text}"


EMPTY_LOG_LINE = (
    "nothing logged yet - the harness writes here as it moves through the loop "
    "(paste, send, generate, copy)."
)


def render_entries(entries: list[HarnessEntry]) -> str:
    """The log as the pane shows it: oldest first, newest last.

    Newest LAST, unlike a notification feed, because this is read as a story -
    the interesting entry is the one at the end, and the ones above it are how
    the harness got there. The pane opens at the bottom and follows it for the
    same reason (tui/widgets/log_pane.py).
    """
    if not entries:
        return EMPTY_LOG_LINE
    return "\n".join(entry.line for entry in entries)


def state_text(before: str, after: str, reason: str) -> str:
    """A transition entry's text: ``WAIT_SEND → MANUAL_COPY — <reason>``."""
    return f"{before} → {after} — {reason}"
