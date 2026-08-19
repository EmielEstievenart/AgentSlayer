"""ConfirmScreen: the generic y/n modal (undo, end-session, quit-mid-turn)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y,enter", "confirm", "yes"),
        Binding("n,escape", "deny", "no"),
    ]

    def __init__(self, title: str, body: str = "") -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(Text(self._title), classes="title")
            if self._body:
                yield Static(Text(self._body))
            yield Static("y yes · n / escape no", classes="hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
