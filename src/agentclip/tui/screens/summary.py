"""SummaryScreen: the session summary + stats, shown on demand via the e key.

It is NOT auto-pushed on task_done: task_done completes the session but the
user may continue (protocol.md section 8), so the controller leaves them in the
chat and the summary is one keypress away instead of a wall.

Dismisses with one of "undo" | "new" | "close" | "export". The caller treats
"export" specially: it writes the chat log and re-shows this screen.
"""

from __future__ import annotations

from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static


class SummaryScreen(ModalScreen[str]):
    BINDINGS = [
        Binding("u", "undo", "undo last turn"),
        Binding("t", "new", "new session"),
        Binding("l", "export", "export chat log"),
        Binding("escape", "close", "close"),
    ]

    def __init__(self, stats: Table, summary: str) -> None:
        super().__init__()
        self._stats = stats
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static("SESSION SUMMARY", classes="title")
            yield Static(self._stats)
            yield Markdown(self._summary or "*(the model sent no summary)*")
            yield Static(
                "u undo last turn · t new session · l export chat log · escape close",
                classes="hint",
            )

    def action_undo(self) -> None:
        self.dismiss("undo")

    def action_new(self) -> None:
        self.dismiss("new")

    def action_export(self) -> None:
        self.dismiss("export")

    def action_close(self) -> None:
        self.dismiss("close")
