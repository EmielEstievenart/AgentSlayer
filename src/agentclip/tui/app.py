"""AgentClipApp: app shell, embedded CSS (PyInstaller-friendly), global keys.

The session flow itself lives on MainScreen (tui/screens/main.py). The app owns
the screen stack, the F1/F2 global keys, and the quit-mid-turn confirmation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from agentclip.clip.base import ClipboardProvider
from agentclip.config import Config
from agentclip.engine.engine import Engine
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.help import HelpScreen
from agentclip.tui.screens.main import MainScreen


class AgentClipApp(App[None]):
    TITLE = "AgentClip"

    BINDINGS = [
        Binding("f1", "help", "help"),
        Binding("question_mark", "help", "help", show=False),
        Binding("f2", "settings", "settings", show=False),
    ]

    # CSS lives in the class var, not a .tcss file: zero --add-data for PyInstaller.
    CSS = """
    MainScreen {
        layout: vertical;
    }
    TranscriptPanel {
        height: 1fr;
        padding: 0 1;
    }
    TranscriptPanel > * {
        height: auto;
    }
    TranscriptPanel .ev-user {
        border-left: thick $success;
        padding: 0 1;
        margin-top: 1;
    }
    TranscriptPanel .ev-prose {
        border-left: thick $primary;
        padding: 0 1;
        margin-top: 1;
    }
    TranscriptPanel .ev-call {
        height: auto;
        margin-left: 2;
    }
    TranscriptPanel .ev-note {
        color: $text-muted;
        margin-left: 2;
    }
    TranscriptPanel .ev-error {
        background: $error 30%;
        padding: 0 1;
        margin-top: 1;
    }
    TranscriptPanel .call-summary {
        text-style: bold;
    }

    ActionPanel {
        height: auto;
        max-height: 60%;
        border-top: heavy $warning;
        background: $surface;
        padding: 0 1;
    }
    #action-title {
        text-style: bold;
        color: $warning;
    }
    #action-queue {
        color: $text-muted;
    }
    #action-body {
        height: auto;
        max-height: 24;
        margin-top: 1;
    }
    #action-footer {
        height: auto;
        margin-top: 1;
    }
    #action-hints {
        width: auto;
        color: $text-muted;
        padding: 0 1;
    }
    #reject-reason {
        width: 1fr;
    }
    #answer {
        height: 6;
    }

    StatusBar {
        height: 1;
        background: $panel;
    }
    StatusBar .seg {
        width: auto;
        padding: 0 1;
    }
    #seg-root {
        width: 1fr;
        text-align: right;
        color: $text-muted;
        text-overflow: ellipsis;
    }
    .st-armed {
        color: $success;
        text-style: bold;
    }
    .st-attn {
        color: $warning;
        text-style: bold reverse;
    }
    .st-busy {
        color: $warning;
    }
    .st-dim {
        color: $text-muted;
    }
    .st-err {
        color: $error;
        text-style: bold;
    }
    .st-done {
        color: $success;
        text-style: bold;
    }

    NewSessionScreen, ConfirmScreen, SummaryScreen, HelpScreen, TextEntryScreen {
        align: center middle;
    }
    .modal-box {
        width: 90;
        max-width: 95%;
        height: auto;
        max-height: 85%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    .modal-box .title {
        text-style: bold;
        margin-bottom: 1;
    }
    .modal-box .hint {
        color: $text-muted;
        margin-top: 1;
    }
    .modal-box TextArea {
        height: 8;
        margin-top: 1;
    }
    .modal-box Select {
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[str], Engine],
        project_root: Path,
    ) -> None:
        super().__init__()
        self.app_config = config
        self.provider = provider
        self.engine_factory = engine_factory
        self.project_root = project_root
        self.main_screen: MainScreen | None = None

    def on_mount(self) -> None:
        self.main_screen = MainScreen(
            self.app_config, self.provider, self.engine_factory, self.project_root
        )
        self.push_screen(self.main_screen)
        for warning in self.app_config.warnings:
            self.notify(warning, severity="warning", timeout=8)

    def action_help(self) -> None:
        if isinstance(self.screen, HelpScreen):
            return
        self.push_screen(HelpScreen())

    def action_settings(self) -> None:
        self.notify("the settings screen lands in M3 - edit .agentclip.toml in your project root")

    async def action_quit(self) -> None:
        main = self.main_screen
        mid_turn = main is not None and (main.busy or main.pending_approval or main.awaiting_answer)
        if mid_turn and not isinstance(self.screen, ConfirmScreen):
            self.run_worker(self._confirm_quit(), group="quit", exclusive=True)
            return
        self.exit()

    async def _confirm_quit(self) -> None:
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                "Quit mid-turn?",
                "The current turn is incomplete and its results were never sent to the "
                "model. Per-turn backups are kept on disk.",
            )
        )
        if confirmed:
            self.exit()
