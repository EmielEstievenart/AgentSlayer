"""NewSessionScreen: service preset + multi-line task entry (tui.md section 1.3)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Select, Static, TextArea

from agentclip.config import Config


@dataclass(frozen=True, slots=True)
class SessionSpec:
    task: str
    service: str


class NewSessionScreen(ModalScreen["SessionSpec | None"]):
    """Dismisses with a SessionSpec, or None when the user wants to quit."""

    BINDINGS = [
        Binding("ctrl+enter", "submit", "start session"),
        Binding("escape", "cancel", "quit"),
    ]

    def __init__(self, config: Config, project_root: Path) -> None:
        super().__init__()
        self._config = config
        self._project_root = project_root

    def compose(self) -> ComposeResult:
        presets = sorted(self._config.services.values(), key=lambda p: p.key)
        options = [(f"{p.key} · {p.label} · {p.max_paste_chars:,} chars", p.key) for p in presets]
        with Vertical(classes="modal-box"):
            yield Static(Text(f"New session — {self._project_root}"), classes="title")
            yield Select(
                options,
                value=self._config.general.service,
                allow_blank=False,
                id="preset",
            )
            yield TextArea(id="task")
            yield Static(
                "describe the task above · ctrl+enter start · escape quit",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.query_one("#task", TextArea).focus()

    def action_submit(self) -> None:
        task = self.query_one("#task", TextArea).text.strip()
        if not task:
            self.notify("describe the task first", severity="warning")
            return
        value = self.query_one("#preset", Select).value
        service = self._config.general.service if value is Select.NULL else str(value)
        self.dismiss(SessionSpec(task=task, service=service))

    def action_cancel(self) -> None:
        self.dismiss(None)
