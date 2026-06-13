"""TextEntryScreen: a multi-line text modal (follow-up messages, manual paste)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea


class TextEntryScreen(ModalScreen["str | None"]):
    """Dismisses with the entered text, or None on cancel/empty."""

    BINDINGS = [
        # ctrl+s is the reliable submit key; ctrl+enter works only on terminals
        # that deliver it distinctly. priority=True so the TextArea can't eat it.
        Binding("ctrl+s", "submit", "submit", priority=True),
        Binding("ctrl+enter", "submit", "submit", priority=True, show=False),
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, title: str, hint: str = "ctrl+s (or ctrl+enter) submit · escape cancel") -> None:
        super().__init__()
        self._title = title
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(Text(self._title), classes="title")
            yield TextArea(id="entry")
            yield Static(Text(self._hint), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#entry", TextArea).focus()

    def action_submit(self) -> None:
        text = self.query_one("#entry", TextArea).text
        self.dismiss(text if text.strip() else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
