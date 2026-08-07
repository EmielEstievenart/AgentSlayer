"""AgentClipApp: app shell, embedded CSS (PyInstaller-friendly), global keys.

The session flow itself lives on MainScreen (tui/screens/main.py). The app owns
the screen stack, the F1/F2 global keys, and the quit-mid-turn confirmation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.theme import Theme

from agentclip.clip.base import ClipboardProvider
from agentclip.config import (
    DEFAULT_THEME,
    Config,
    default_global_config_path,
    save_services,
    save_theme,
)
from agentclip.engine.engine import Engine
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.help import HelpScreen
from agentclip.tui.screens.main import MainScreen
from agentclip.tui.screens.service_editor import ServiceEditorScreen
from agentclip.tui.screens.settings import SettingsScreen

# Anthropic-flavoured warm themes, registered on mount alongside Textual's
# built-in textual-light/textual-dark (Settings > Appearance offers all four -
# config.VALID_THEMES must stay in sync with these two names + the builtins).
CLAUDE_WARM_THEME = Theme(
    name="claude-warm",
    dark=False,
    primary="#c96442",
    secondary="#8a6552",
    accent="#cc785c",
    warning="#b8720f",
    error="#b3261e",
    success="#3f7d4f",
    foreground="#3d3929",
    background="#faf9f5",
    surface="#f0eee6",
    panel="#e8e4d8",
    variables={
        "button-color-foreground": "#faf9f5",
    },
)

CLAUDE_DARK_THEME = Theme(
    name="claude-dark",
    dark=True,
    primary="#d97757",
    secondary="#c98a6b",
    accent="#e08a5f",
    warning="#e0a548",
    error="#e0605a",
    success="#7fb069",
    foreground="#f0eee6",
    background="#262624",
    surface="#30302e",
    panel="#3a3936",
    variables={
        "button-color-foreground": "#262624",
    },
)


class AgentClipApp(App[None]):
    TITLE = "AgentClip"

    BINDINGS = [
        Binding("f1", "help", "help"),
        Binding("question_mark", "help", "help", show=False),
        Binding("f2", "settings", "settings", show=False),
        # f4, not f3: MainScreen already claims f3 (priority binding, toggle_sidebar).
        Binding("f4", "preferences", "preferences", show=False),
    ]

    # CSS lives in the class var, not a .tcss file: zero --add-data for PyInstaller.
    CSS = """
    MainScreen {
        layout: vertical;
    }
    #body {
        height: 1fr;
    }
    #main-col {
        width: 1fr;
        height: 1fr;
    }
    /* The transcript tabs (one pane per session view). TabbedContent sizes to
       its content by default, which inside #main-col's vertical flex would
       collapse the transcript to nothing - so every layer down to the panel is
       pinned to the available height. */
    #chats {
        height: 1fr;
    }
    #chats ContentSwitcher {
        height: 1fr;
    }
    #chats TabPane {
        height: 1fr;
        padding: 0;
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
    TranscriptPanel .msg-head {
        text-style: bold;
    }
    TranscriptPanel .msg-you {
        color: $success;
    }
    TranscriptPanel .msg-assistant {
        color: $primary;
    }

    ActionPanel {
        height: auto;
        max-height: 60%;
        border: heavy $warning;
        background: $surface;
        padding: 0 1;
        margin: 0 1;
    }
    #action-title {
        text-style: bold;
        color: $text;
        background: $warning;
        padding: 0 1;
    }
    #action-queue {
        color: $text-muted;
    }
    #action-body {
        height: auto;
        max-height: 20;
        margin-top: 1;
    }
    #action-buttons {
        height: auto;
        margin-top: 1;
    }
    #action-buttons Button {
        margin-right: 2;
    }
    #action-footer {
        height: auto;
        margin-top: 1;
    }
    #action-hints {
        width: 1fr;
        color: $text-muted;
        padding: 0 1;
    }
    #reject-reason {
        width: 1fr;
    }

    #running {
        height: 1;
        color: $warning;
        text-style: bold;
        padding: 0 1;
        margin: 0 1;
    }
    #composer {
        height: 5;
        border: round $primary;
        background: $surface;
        margin: 0 1;
        padding: 0 1;
    }
    #composer:focus {
        border: round $accent;
    }
    #composer:disabled {
        color: $text-muted;
        border: round $panel;
    }

    Sidebar {
        width: 32;
        height: 1fr;
        background: $panel;
        border-left: solid $primary;
        padding: 0 1;
    }
    Sidebar .side-title {
        text-style: bold;
        color: $text;
        margin-top: 1;
    }
    Sidebar #side-root {
        color: $text-muted;
        text-overflow: ellipsis;
    }
    Sidebar Select {
        width: 1fr;
    }
    Sidebar #side-service-label {
        color: $text-muted;
    }
    Sidebar Button {
        width: 1fr;
        margin-top: 1;
    }
    Sidebar #side-region, Sidebar #side-click, Sidebar #side-copy {
        color: $text-muted;
    }
    Sidebar #side-paste-flash {
        display: none;
        margin-top: 1;
        padding: 0 1;
        text-align: center;
        text-style: bold;
        color: white;
        background: red;
        border: heavy yellow;
    }
    Sidebar #side-paste-flash.flash-alt {
        color: red;
        background: yellow;
        border: heavy red;
    }
    Sidebar .side-hint {
        color: $text-muted;
        margin-top: 1;
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
    .st-yolo {
        color: $text;
        background: $error;
        text-style: bold;
    }

    ConfirmScreen, SummaryScreen, HelpScreen, TextEntryScreen, ServiceEditorScreen, SettingsScreen {
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

    #service-editor-box {
        width: 112;
    }
    #svc-body {
        height: auto;
    }
    #svc-list-col {
        width: 36;
        margin-right: 2;
    }
    #svc-list-col Select {
        width: 1fr;
        margin-top: 0;
    }
    #svc-form-col {
        width: 1fr;
    }
    #svc-form-col .side-title {
        margin-top: 1;
    }
    #svc-form-col .side-title:first-of-type {
        margin-top: 0;
    }
    #svc-error {
        color: $error;
        height: auto;
        min-height: 1;
        margin-top: 1;
    }
    #svc-actions {
        height: auto;
        margin-top: 1;
    }
    #svc-actions Button {
        margin-right: 2;
        width: auto;
    }

    #settings-box {
        width: 70;
    }
    #theme-radio-set {
        width: 1fr;
        height: auto;
    }
    #settings-actions {
        height: auto;
        margin-top: 1;
    }
    #settings-actions Button {
        margin-right: 2;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[str], Engine],
        project_root: Path,
        global_config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.app_config = config
        self.provider = provider
        self.engine_factory = engine_factory
        self.project_root = project_root
        # Where the service editor and the settings screen persist edits.
        # Defaults to the real global config.toml; tests override it to a tmp
        # path so they never touch the user's actual config.
        self._global_config_path = (
            global_config_path if global_config_path is not None else default_global_config_path()
        )
        self.main_screen: MainScreen | None = None

    def on_mount(self) -> None:
        self.register_theme(CLAUDE_WARM_THEME)
        self.register_theme(CLAUDE_DARK_THEME)
        # config.load_config() already falls back to DEFAULT_THEME for unknown
        # names, but stay defensive here too rather than let App._validate_theme
        # raise if app_config was constructed by hand (as tests sometimes do).
        wanted = self.app_config.general.theme
        self.theme = wanted if wanted in self.available_themes else DEFAULT_THEME

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
        # Also the target of the sidebar's "Edit services..." button. Bound directly
        # to the F2 key, so Textual dispatches it OUTSIDE a worker - push_screen_wait
        # requires one, hence the run_worker hand-off (same pattern as _confirm_quit).
        if isinstance(self.screen, ServiceEditorScreen):
            return  # already open
        self.run_worker(self._open_service_editor(), group="settings", exclusive=True)

    async def _open_service_editor(self) -> None:
        result = await self.push_screen_wait(ServiceEditorScreen(self.app_config))
        if result is None:
            return  # closed with no changes - nothing to persist or propagate
        save_services(result, self._global_config_path)
        new_config = replace(self.app_config, services=result)
        self.app_config = new_config
        main = self.main_screen
        if main is not None:
            main.update_config(new_config)
            main.sidebar.refresh_services(new_config)
        self.notify("service presets saved", timeout=4)

    def action_preferences(self) -> None:
        # Bound directly to the F4 key, so Textual dispatches it OUTSIDE a
        # worker - push_screen_wait requires one, hence the run_worker hand-off
        # (same pattern as action_settings/_open_service_editor above).
        if isinstance(self.screen, SettingsScreen):
            return  # already open
        self.run_worker(self._open_settings(), group="preferences", exclusive=True)

    async def _open_settings(self) -> None:
        chosen = await self.push_screen_wait(SettingsScreen(self.theme))
        if chosen is None:
            return  # cancelled/escaped - the screen already reverted the live preview
        save_theme(chosen, self._global_config_path)
        self.app_config = replace(
            self.app_config, general=replace(self.app_config.general, theme=chosen)
        )
        self.notify("theme saved", timeout=4)

    async def action_quit(self) -> None:
        main = self.main_screen
        # NB: while the inline start flow waits for the first message the session
        # worker is technically "busy" - but there is no turn to lose, so quitting
        # from the empty start screen must not raise the mid-turn warning.
        mid_turn = (
            main is not None
            and not main.awaiting_new_session
            and (main.busy or main.pending_approval or main.awaiting_answer)
        )
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
