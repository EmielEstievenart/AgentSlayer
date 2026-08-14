"""HARNESS LOG: the decision log, live, along the bottom of the screen.

Its predecessor was a modal (`/log` pushed a screen holding a *snapshot*), on
the reasoning that a log which reflowed as the poller appended would move the
line the user is halfway through reading. That reasoning was right about the
hazard and wrong about the fix: a snapshot answers *why did it do that?* only
for decisions already taken, and the moment a user actually reaches for this is
the moment something is happening RIGHT NOW - a send gate that will not release,
an auto-copy that fires on nothing. A modal you have to close, re-open and
re-scroll to see the next entry is the wrong instrument for watching a loop.

So it is a pane, and the hazard is answered directly instead: **the pane follows
the tail only while the user is already at the tail**. Scroll up and it freezes
- new entries land below the fold and the line being read does not move - and
scrolling back to the bottom (or `End`, the native binding, once the pane has
focus) resumes following. That is one property read at append time
(:attr:`following`), not a mode with a switch.

**Full width, between the columns and the status bar.** Not a fourth column and
not a drawer inside the chat column: an entry is a timestamp, a kind and a whole
sentence of reason (~120 cells), and the only place on this screen wide enough
for that is the whole terminal. It costs the columns above it ~30% of their
height while it is open, which is what `F8` (and `/log`) is for.

**The deque stays the source of truth.** ``MainScreen._harness_log`` is the log;
this widget is a view of it. Appends are mirrored here one entry at a time while
it is visible, and while it is HIDDEN nothing is painted at all - the pane
simply remembers that it is behind and refills itself from the deque the moment
it is revealed. Two bounds, deliberately the same number
(:data:`~agentclip.automation.harness_log.HARNESS_LOG_MAX`): with wrapping off, one
entry is exactly one line, so the widget's ``max_lines`` prunes in lockstep with
the deque and the two cannot drift apart during a long run with the pane open.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text
from textual.widgets import RichLog

from agentclip.automation.harness_log import HARNESS_LOG_MAX, HarnessEntry, render_entries

LOG_PANE_TITLE = "HARNESS DECISION LOG"
LOG_PANE_HINT = "newest last · F8 hides"


class HarnessLogPane(RichLog):
    """The live tail of the harness decision log (`/log`, `F8`)."""

    def __init__(self, *, id: str | None = None) -> None:
        # markup off and highlight off: entries are the app's own prose, and a
        # reason that happens to contain brackets is a reason, not markup. Wrap
        # off keeps one entry on one line - the printed form is a COLUMN layout
        # (time, kind, sentence) and a wrapped sentence would break the column
        # the eye scans - so a long line scrolls sideways rather than reflowing.
        super().__init__(
            id=id,
            max_lines=HARNESS_LOG_MAX,
            markup=False,
            highlight=False,
            wrap=False,
            auto_scroll=False,  # the follow decision is taken per append, below
        )
        self.border_title = LOG_PANE_TITLE
        self.border_subtitle = LOG_PANE_HINT
        # What is painted right now: nothing yet (never revealed), the empty
        # placeholder, or entries. Only ``_behind`` drives a refill; the empty
        # flag is there so the first real entry replaces the placeholder rather
        # than being appended under it.
        self._behind = True
        self._empty = False

    @property
    def following(self) -> bool:
        """Is the pane pinned to the newest entry?

        True whenever the view is already at the bottom - which is where it
        starts and where it stays unless the user scrolls up. Reading the scroll
        position rather than holding a flag means there is no way for "following"
        and "actually at the tail" to disagree: a wheel click back down to the
        bottom resumes following without anything having to notice it happened.
        """
        return self.is_vertical_scroll_end

    def append(self, entry: HarnessEntry) -> None:
        """Mirror one freshly-logged decision, if anyone can see it.

        Hidden, this only records that the pane is behind: painting into a pane
        with ``display: none`` costs a render for nothing, and the refill on
        reveal is one write of the whole deque anyway.
        """
        if not self.display:
            self._behind = True
            return
        if self._empty:
            self.clear()
            self._empty = False
        # The freeze, in one line: scroll to the new entry only if the user was
        # already reading the newest one.
        self.write(Text(entry.line), scroll_end=self.following)

    def reveal(self, entries: Iterable[HarnessEntry]) -> None:
        """Show the pane, holding everything logged while it was away.

        Called with the live deque. A pane that came back showing where it left
        off would be lying about a log whose whole purpose is to say what just
        happened, so a pane that missed anything is rebuilt from scratch.
        """
        self.display = True
        if self._behind:
            self.refill(entries)

    def refill(self, entries: Iterable[HarnessEntry]) -> None:
        """Repaint from the deque: the one rendering of "the log as shown"."""
        rows = list(entries)
        self.clear()
        self._empty = not rows
        self.write(Text(render_entries(rows)), scroll_end=True)
        self._behind = False
