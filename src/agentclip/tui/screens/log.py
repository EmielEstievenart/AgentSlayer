"""LogScreen: the harness decision log, read back (`/log`).

A snapshot, not a live view: the entries are handed over when the screen is
pushed and never change under the reader. The log is a debugging aid consulted
*while something is wrong*, and a pane that reflowed as the poller kept
appending would move the line the user is halfway through reading.

Patterned on HelpScreen - a ``.modal-box`` with a title and an escape hint -
with one deliberate difference: the body is a ``VerticalScroll``. A bare
``Vertical`` inside the shared box CLIPS, because ``.modal-box`` caps its height
at 85% of the terminal and sets no overflow rule, so a 60-entry log would simply
have its tail cut off with nothing to say so. The box therefore takes a fixed
height (``#log-box``) and gives the scroll the slack (``#log-body``), which is
also what makes ``scroll_end`` on mount meaningful: the newest entry is the
last one, and it is the one the reader came for.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from agentclip.tui.harness_log import HarnessEntry, render_entries


class LogScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q", "close", "close")]

    def __init__(self, entries: list[HarnessEntry]) -> None:
        super().__init__()
        self._entries = list(entries)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box", id="log-box"):
            yield Static("HARNESS DECISION LOG", classes="title")
            with VerticalScroll(id="log-body"):
                yield Static(Text(render_entries(self._entries)), id="log-entries")
            yield Static("newest last · escape close", classes="hint")

    def on_mount(self) -> None:
        # Start at the end: the decision that made the user open this is the
        # most recent one, and scrolling down to find it every time would be a
        # tax on exactly the state this screen exists for.
        self.query_one("#log-body", VerticalScroll).scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)
