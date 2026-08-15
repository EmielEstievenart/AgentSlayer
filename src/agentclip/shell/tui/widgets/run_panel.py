"""RUN PANEL: what the turn is actually doing, while it does it (tui.md section 8a).

Its predecessor was one line - *"Working - running 3 tool calls..."* - which
told the user that something was happening and nothing whatsoever about what.
For a turn of three reads that was enough, because it was over before it was
read. For the turn that spends four minutes in ``npm run build`` it was the
whole problem: no way to tell which call is running, no way to tell what is
queued behind it, and not one character of the command's output until the batch
finished and the transcript got it all at once.

So the region grows a body. It lists **one row per planned call** for as long as
the turn executes:

    ✓ 1 read_file    src/utils.py
    ▶ 2 run_command  npm run build                       ctrl+o output
    • 3 write_file   src/build.log

Pending rows are dim, the running row carries the spinner, and finished rows get
the same ✓/✗/− alphabet the approval queue strip already uses - so a row's state
is readable at a glance and does not change language halfway through the turn.

**The running command's output is one keypress away.** ``ctrl+o`` (or a click on
the rows) reveals a tail pane under the list: the last
:data:`RUN_TAIL_LINES` lines of what the command has printed so far, growing
live. It is collapsed by default because most turns do not need it and a panel
that jumps to twelve rows high on every ``ls`` would be worse than the one line
it replaced.

Following the harness log pane's division of labour exactly (widgets/log_pane.py):
**MainScreen owns the buffer**, this is a view of it. Nothing is painted while
the tail is collapsed - the deque is still filling behind it - and revealing it
refills from the deque in one write. The panel is torn back down to nothing when
the turn ends; the model's copy of the output was never this, it is the
tail-capped result already on its way to the transcript.

The spinner line itself is still :class:`~agentclip.shell.tui.widgets.running_bar.RunningBar`,
mounted here as this panel's header: it owns the animation, the label and the
cancel hint, and it is what ``ctrl+x``'s advertisement lives on.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from agentclip.shell.app.view import RunCall
from agentclip.shell.tui.widgets.running_bar import RunningBar

# How many of a command's output lines the tail shows. Twelve is a compiler's
# last error plus its context, which is what a user opens this for.
RUN_TAIL_LINES = 12

# How many lines MainScreen keeps per call. Deeper than the pane shows, so the
# view can be made taller later without the history having been thrown away
# already - and bounded, because a command that prints a million lines must
# cost the same as one that prints twelve.
RUN_OUTPUT_LINES = 400

# The panel never grows past this many rows: a turn is advised to hold ~8 calls
# and the composer must not be pushed off screen by a model that ignored that.
_MAX_ROWS = 8

_PENDING = "•"
_RUNNING = "▶"

# Row styles by glyph, from the app's palette (app.py CSS defines the classes;
# these are inline styles because one Static paints every row).
_ROW_STYLES = {
    _PENDING: "dim",
    _RUNNING: "bold",
    "✓": "green",
    "✗": "red",
    "−": "dim",
}

_OUTPUT_HINT = "ctrl+o output"
_TAIL_TITLE = "OUTPUT"
_TAIL_HINT = "live · ctrl+o hides"


class RunPanel(Vertical):
    """The spinner, the call list and the running command's live tail."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._rows: list[RunCall] = []
        self._running_id: int | None = None  # call id of the row with the spinner
        self._expanded = False

    def compose(self) -> ComposeResult:
        yield RunningBar(id="run-status")
        yield Static("", id="run-rows")
        yield Static("", id="run-tail")

    def on_mount(self) -> None:
        self.display = False
        self.tail.display = False
        self.tail.border_title = _TAIL_TITLE
        self.tail.border_subtitle = _TAIL_HINT

    # -- parts ---------------------------------------------------------------

    @property
    def bar(self) -> RunningBar:
        return self.query_one("#run-status", RunningBar)

    @property
    def rows_view(self) -> Static:
        return self.query_one("#run-rows", Static)

    @property
    def tail(self) -> Static:
        return self.query_one("#run-tail", Static)

    @property
    def expanded(self) -> bool:
        """Is the running command's output pane showing?"""
        return self._expanded

    @property
    def streaming_call(self) -> int | None:
        """The running call that can actually produce output - or None.

        What ``ctrl+o`` acts on and what the tail is a view of: expanding a
        ``write_file`` row would open an empty pane and teach the user the key
        does nothing.
        """
        row = self._row(self._running_id) if self._running_id is not None else None
        return row.call_id if row is not None and row.streams else None

    # -- lifecycle -----------------------------------------------------------

    def start(self, label: str, calls: Sequence[RunCall]) -> None:
        """Show the panel for a turn that is starting to execute."""
        self._rows = list(calls)
        self._running_id = next((r.call_id for r in self._rows if r.glyph == _RUNNING), None)
        self._expanded = False
        self.display = True
        self.tail.display = False
        self.bar.start(label)
        self._paint_rows()

    def stop(self) -> None:
        """The turn ended: the panel goes away whole, rows and tail with it."""
        self.bar.stop()
        self._rows = []
        self._running_id = None
        self._expanded = False
        self.tail.display = False
        self.display = False

    # -- per-call updates ----------------------------------------------------

    def call_started(self, call_id: int, tool: str, detail: str) -> None:
        """Mark one row running, adding it if the panel never heard of it."""
        if self._row(call_id) is None:
            self._rows.append(
                RunCall(call_id=call_id, tool=tool, detail=detail, streams=tool == "run_command")
            )
        self._running_id = call_id
        self._set_glyph(call_id, _RUNNING)
        self._paint_rows()

    def call_finished(self, call_id: int, glyph: str) -> None:
        if self._running_id == call_id:
            self._running_id = None
            # The tail belonged to the command that just ended; the next one
            # starts from an empty pane rather than inheriting its predecessor's.
            self._expanded = False
            self.tail.display = False
        self._set_glyph(call_id, glyph)
        self._paint_rows()

    def toggle_output(self, lines: Iterable[str]) -> bool:
        """Show/hide the running command's tail. Returns the new state."""
        if not self._expanded and self.streaming_call is None:
            return False
        self._expanded = not self._expanded
        self.tail.display = self._expanded
        if self._expanded:
            self.show_output(lines)  # refill: nothing was painted while it was away
        self._paint_rows()
        return self._expanded

    def show_output(self, lines: Iterable[str]) -> None:
        """Repaint the tail from the screen's deque. A no-op while collapsed.

        The deque is the truth and it keeps filling either way; painting into a
        pane with ``display: none`` would be a render per chunk for nobody.
        """
        if not self._expanded:
            return
        rows = list(lines)[-RUN_TAIL_LINES:]
        self.tail.update(Text("\n".join(rows) if rows else "(no output yet)"))

    # -- painting ------------------------------------------------------------

    def _row(self, call_id: int | None) -> RunCall | None:
        return next((r for r in self._rows if r.call_id == call_id), None)

    def _set_glyph(self, call_id: int, glyph: str) -> None:
        self._rows = [
            replace(r, glyph=glyph) if r.call_id == call_id else r for r in self._rows
        ]

    def _paint_rows(self) -> None:
        shown = self._visible_rows()
        width = max((len(r.tool) for r in shown), default=0)
        text = Text()
        for i, row in enumerate(shown):
            if i:
                text.append("\n")
            style = _ROW_STYLES.get(row.glyph, "")
            line = f"{row.glyph} {row.call_id} {row.tool.ljust(width)}  {row.detail}".rstrip()
            text.append(line, style=style)
            if row.streams and row.call_id == self._running_id and not self._expanded:
                text.append(f"   {_OUTPUT_HINT}", style="dim")
        hidden = len(self._rows) - len(shown)
        if hidden > 0:
            text.append(f"\n  … +{hidden} more", style="dim")
        self.rows_view.update(text)

    def _visible_rows(self) -> list[RunCall]:
        """At most _MAX_ROWS, and always the ones around what is happening now."""
        if len(self._rows) <= _MAX_ROWS:
            return list(self._rows)
        pivot = next((i for i, r in enumerate(self._rows) if r.glyph == _RUNNING), 0)
        start = max(0, min(pivot - _MAX_ROWS // 2, len(self._rows) - _MAX_ROWS))
        return self._rows[start : start + _MAX_ROWS]

    # -- input ---------------------------------------------------------------

    class OutputToggleRequested(Message):
        """A click asked for the running command's output (the ctrl+o request)."""

    def on_click(self) -> None:
        """A click anywhere on the panel is the same request as ctrl+o.

        The panel is a few rows tall and its only interactive state is that one
        toggle, so hunting for a disclosure triangle would be ceremony: the
        whole thing is the control. MainScreen owns the buffer, so it does the
        toggling - this only asks.
        """
        self.post_message(self.OutputToggleRequested())
