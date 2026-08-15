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

from agentclip.app.types import EngineRequest
from agentclip.config import (
    DEFAULT_THEME,
    Config,
    default_global_config_path,
    default_profile_dir,
    save_services,
    save_theme,
)
from agentclip.driver.clip.base import ClipboardProvider
from agentclip.engine.engine import Engine
from agentclip.executor.mcp.client import McpManager
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
        # The global ARMED switch. show=True and app-level for the same reason
        # F1 is: it has to work in EVERY state, including before any session
        # exists and while a flow is mid-turn, so it hangs off no check_action
        # and no screen. No priority=True needed - function keys already reach
        # the app with the composer's TextArea focused (f1/f2/f4 prove it), and
        # only the letter bindings ever needed rescuing from it.
        Binding("f5", "toggle_armed", "armed"),
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
    /* The window tab bar is two fixed rows at the top of the chat column; the
       transcripts underneath take whatever is left. #chat-panels holds one
       panel per window and shows exactly one of them (MainScreen._show_panel),
       so its 1fr goes entirely to whichever is displayed. */
    #chats {
        height: 2;
    }
    #chat-panels {
        height: 1fr;
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

    /* The RUN PANEL (§8a): spinner line, one row per call, and - when ctrl+o
       asks for it - the running command's live output. `display: none` while
       idle is the widget's own doing, so the region costs nothing between
       turns; everything here is sized `auto` so a one-call turn stays one row
       tall and never pushes the composer around. */
    #running {
        height: auto;
        max-height: 22;
        padding: 0 1;
        margin: 0 1;
    }
    #run-status {
        height: 1;
        color: $warning;
        text-style: bold;
    }
    #run-rows {
        height: auto;
        color: $text;
    }
    /* Capped and scrollable: a build prints more than a screenful and the
       panel must not eat the transcript to show it. */
    #run-tail {
        height: auto;
        max-height: 12;
        overflow-y: auto;
        border: round $panel;
        color: $text-muted;
        padding: 0 1;
    }
    /* The slash-command list: sits directly on top of the composer and shares
       its margin, so the two read as one control. Height is the match count
       (one row per command, ellipsized rather than wrapped). */
    #cmd-popup {
        height: auto;
        /* The whole registry plus its border - a bare `/` offers everything, and
           a cap below that would hide the last command rather than shorten the
           list. Grows with app.commands.COMMANDS (a test pins the two). */
        max-height: 11;
        background: $surface;
        color: $text;
        border: round $accent;
        margin: 0 1;
        padding: 0 1;
        /* One command = one row, always: a long summary is cut, never wrapped,
           or the rows stop lining up with the highlight and the tallest entry
           pushes the last command out from under max-height. */
        text-wrap: nowrap;
        text-overflow: ellipsis;
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
        /* The column is taller than most terminals - the STATE rail, the
           picker, the chat window and five detector lines come to ~60 rows -
           so everything below the fold used to be simply
           unreachable. A one-cell scrollbar (the default two would cost the
           status lines a character of width) makes the bottom of the column
           gettable-to; F3 still hides the whole thing. */
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    /* The ELEMENTS column: one crop per appearance the detector recognises -
       all seven of them (§1.7) - beside the sidebar's words about the four the
       loop decides from. Deliberately the narrowest column on the screen - an
       icon is a couple of dozen pixels, so 20 cells (17 of content) draws it
       life-size-ish, and the chat column keeps the room that diffs and command
       output need. Seven rows of label-plus-picture outgrow most terminals, so
       it scrolls like the sidebar; F7 hides it exactly as F3 hides its
       neighbour. */
    ElementsPanel {
        width: 20;
        height: 1fr;
        background: $panel;
        border-left: solid $primary;
        padding: 0 1;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
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
    Sidebar #side-region {
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
    /* The flash's action button, hidden with it and shown only for the flash
       that is asking the user to paste by hand (Sidebar.show_paste_flash). */
    Sidebar #retry-insert-btn {
        display: none;
    }
    /* The paste flash's standing sibling: as loud, but it never blinks (it is a
       fact, not a request), so it has no .flash-alt half. */
    Sidebar #side-armed-banner {
        display: none;
        margin-top: 1;
        padding: 0 1;
        text-align: center;
        text-style: bold;
        color: white;
        background: red;
        border: heavy red;
    }
    Sidebar .side-hint {
        color: $text-muted;
        margin-top: 1;
    }

    /* The harness log pane (tui.md 3.3b): full width, between the three columns
       and the status bar, hidden until F8 or /log asks for it.

       A share of the terminal rather than a fixed slab, because what it costs
       is the transcript above it - but capped, because past a dozen rows it has
       stopped being a tail and started being the screen. The cap bites on any
       terminal over ~47 rows; the floor keeps it readable (a border row, a
       horizontal scrollbar and three entries) on a short one. The border is one
       edge, like the two columns' - it is what carries the title. */
    HarnessLogPane {
        display: none;
        height: 30%;
        min-height: 6;
        max-height: 14;
        background: $panel;
        border-top: solid $primary;
        padding: 0 1;
        scrollbar-size-horizontal: 1;
        scrollbar-size-vertical: 1;
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
    /* The DISARMED badge. Reverse-video red rather than a coloured word: it has
       to be unmissable at a glance on a bar the user has stopped reading, and it
       must not be confusable with the YOLO badge two segments over - which is
       why it also has a slot of its own (both can be on at once). Hidden by the
       widget whenever the app is armed, so this never becomes furniture. */
    #seg-armed {
        color: white;
        background: red;
        text-style: bold;
    }
    /* The permission-mode segment (§2.6a), leftmost cell. `ask` is the baseline
       and wears .st-dim so it cannot become furniture; the two modes that CHANGE
       what happens to a tool call are the ones that are meant to catch the eye.
       Blue for `plan` (a mode that only ever refuses more - it is safe, not
       alarming, so it borrows the accent the app already uses for borders), and
       the warning colour for `unattended`, which is the "nobody is watching"
       mode. Neither is red: red is spoken for by the two badges that mean
       approvals are OFF (⚡ YOLO) and the OS switch is out (⛔ DISARMED). */
    .st-plan {
        color: $accent;
        text-style: bold;
    }
    .st-unattended {
        color: $warning;
        text-style: bold;
    }
    /* A delegated sub-agent run owns the watcher segment while it lasts; magenta
       is used nowhere else, so "this is not your conversation" reads at a glance. */
    .st-sub {
        color: magenta;
        text-style: bold;
    }

    ConfirmScreen, SummaryScreen, HelpScreen, TextEntryScreen, ServiceEditorScreen,
    SettingsScreen {
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
        /* The tallest column plus the footer is ~33 rows, so the shared 85%
           cap clipped the bottom off a 45-row terminal. 95% also keeps the
           whole box on screen down to ~35 rows, which is what lets the
           appearance column give up scrolling entirely (see below). */
        max-height: 95%;
    }
    #svc-body {
        height: auto;
    }
    /* Vertical defaults to height: 1fr, which made the auto-height body (and so
       the whole modal) stretch to the max-height cap and push the footer off
       the bottom. The columns are as tall as their content; the tallest sets
       the box. */
    #svc-body > Vertical {
        height: auto;
    }
    /* The three columns are one form, so every section heading in them sits
       off its predecessor by a row - except the one that starts a column. */
    #svc-body .side-title {
        text-style: bold;
        margin-top: 1;
    }
    #svc-body .side-title:first-of-type {
        margin-top: 0;
    }
    #svc-list-col {
        width: 32;
        margin-right: 2;
    }
    #svc-list-col Select {
        width: 1fr;
        margin-top: 0;
    }
    /* The one column that flexes, which makes it the one that absorbs a
       narrow terminal - deliberately: the other two hold prose and pictures
       whose truncation reads as a different text ("captu"), while this one
       holds values, and a squeezed Input scrolls its content and stays fully
       editable. Its labels are chosen to fit the width the 120-cell layout
       gives it, and to end in an ellipsis rather than a chop below that. */
    #svc-form-col {
        width: 1fr;
    }
    #svc-form-col .side-title {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    /* Compact fields on the box's own background disappear into it; $boost is
       the "one layer up" tint every theme defines, so the field reads as a
       field without costing the two border rows the bordered Input spends. */
    #svc-form-col Input {
        background: $boost;
        padding: 0 1;
    }
    /* The one multi-line field on the form, painted as the Inputs above it are
       so the column reads as one set of fields. Height is pinned because
       TextArea defaults to 1fr, which in this auto-height column would eat
       every row the other two columns have and stretch the modal; four rows is
       a little box - room for a sentence to wrap and be read back - and the
       cap is itself the "keep it short" warning the label repeats
       (config.ServicePreset.extra_instructions: ~67 chars of bootstrap slack). */
    #svc-form-col TextArea {
        background: $boost;
        height: 4;
        /* The editor is a .modal-box, so the generic rule above (a full 8-row
           entry box, floated off its title) reaches this one too. Both of its
           declarations are overridden here, the margin included: on this form
           a field sits directly under the label that names it. */
        margin-top: 0;
    }
    #svc-error {
        color: $error;
        height: auto;
        /* The line is reserved even while empty so appearing does not shove
           the column - same stability rule as the two warning lines. */
        min-height: 1;
        margin-top: 1;
    }
    /* APPEARANCE. Sized so that every text in it renders whole: 12 preview
       cells, a gutter, 21 for the longest kind name and the widest status a
       capture produces, and 9 for the stacked action buttons. Fixed, because
       those texts are fixed - the form column is what gives. And neither a
       scrollbar nor a height cap, on purpose: sixel escapes bypass the
       compositor (tui.graphics), so a preview half scrolled out of a
       container keeps painting over whatever Textual thinks is there - the
       smeared bands across the terminal that the old scrolling column left.
       The rows are capped (SIXEL_PREVIEW_MAX_ROWS) so the whole column
       always fits the modal instead. */
    #svc-appearance-col {
        width: 44;
        margin-left: 2;
    }
    /* The half-block budget; overridden inline to preview_rows on a sixel
       terminal (ServiceEditorScreen._appearance_row). Pinned on the row as
       well as on the picture inside it, because a bitmap taller than its row
       is cropped - and cropped sixel is exactly the bleed above. */
    .svc-appearance-row {
        height: 2;
    }
    /* TEMPLATE_PREVIEW_COLS wide, and it keeps that width whether or not the
       kind has an image: the text beside it must not shuffle sideways as
       captures land. */
    .svc-appearance-row .svc-tpl-preview {
        width: 12;
        height: 2;
        margin-right: 1;
    }
    .svc-kind-text {
        width: 1fr;
        height: auto;
    }
    /* Never a mid-word chop: the column is sized so these fit, and a future
       status that outgrows it anyway ends in an ellipsis, not in "captu". */
    .svc-kind-text .svc-kind-label,
    .svc-kind-text .side-status {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .svc-kind-text .side-status {
        color: $text-muted;
    }
    .svc-kind-actions {
        width: 9;
        height: auto;
        margin-left: 1;
    }
    /* Button's default min-width is 16 - wider than this whole stack. */
    .svc-kind-actions Button {
        min-width: 0;
        width: 100%;
    }
    #svc-templates {
        height: 1;
        color: $text-muted;
        margin-top: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #svc-signal-warning {
        color: $warning;
        height: auto;
        min-height: 1;
        margin-top: 1;
    }
    /* MATCHING. The RadioSet loses its border: two options in a 32-wide column
       do not need framing, and the two rows it costs are two rows the modal
       does not have to grow by. The warning wraps (height: auto) for the same
       reason the signal warning does - a clipped explanation of a silent
       fallback explains nothing. */
    #svc-matcher-set {
        border: none;
        width: 1fr;
        height: auto;
        padding: 0;
    }
    #svc-matcher-warning {
        color: $warning;
        height: auto;
        min-height: 1;
    }
    #svc-tolerance {
        width: 1fr;
    }
    /* One footer row carries the hint and the add/reset/delete action -
       exactly one of the three buttons is displayed at a time, and the footer
       is the one strip whose width no column can squeeze, so their labels
       survive every terminal the modal itself survives. */
    #svc-footer {
        height: auto;
        margin-top: 1;
    }
    #svc-footer .hint {
        width: 1fr;
        margin-top: 0;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #svc-footer Button {
        margin-left: 2;
        min-width: 0;
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
        engine_factory: Callable[[EngineRequest], Engine],
        project_root: Path,
        global_config_path: Path | None = None,
        profile_root: Path | None = None,
        mcp_manager: McpManager | None = None,
    ) -> None:
        super().__init__()
        self.app_config = config
        self.provider = provider
        self.engine_factory = engine_factory
        self.project_root = project_root
        # The process-wide MCP runtime, or None when MCP is unconfigured.
        # cli.py owns its lifecycle (built in main(), closed in main()'s
        # finally) and the engine factory holds its own reference for the
        # tools; this one is handed to MainScreen at mount, which paints the
        # sidebar block + statusbar segment off statuses() and bridges
        # set_status_hook onto Textual's loop (docs/design/mcp.md section 6).
        self.mcp_manager = mcp_manager
        # Where the service editor and the settings screen persist edits.
        # Defaults to the real global config.toml; tests override it to a tmp
        # path so they never touch the user's actual config.
        self._global_config_path = (
            global_config_path if global_config_path is not None else default_global_config_path()
        )
        # Where per-service appearance profiles live (screen.profile_store).
        # Same shape and same reason as global_config_path: a real default the
        # app run uses, overridable so no test ever writes into (or reads from)
        # the user's captured templates.
        self.profile_root = (
            profile_root if profile_root is not None else default_profile_dir()
        )
        self.main_screen: MainScreen | None = None

    @property
    def global_config_path(self) -> Path:
        """Where preferences persist. Read-only and public because MainScreen
        writes the last-picked service through it (config.save_active_services)
        and must land in the SAME file the settings screens use - including the
        tmp path a test injected above."""
        return self._global_config_path

    def on_mount(self) -> None:
        self.register_theme(CLAUDE_WARM_THEME)
        self.register_theme(CLAUDE_DARK_THEME)
        # config.load_config() already falls back to DEFAULT_THEME for unknown
        # names, but stay defensive here too rather than let App._validate_theme
        # raise if app_config was constructed by hand (as tests sometimes do).
        wanted = self.app_config.general.theme
        self.theme = wanted if wanted in self.available_themes else DEFAULT_THEME

        self.main_screen = MainScreen(
            self.app_config,
            self.provider,
            self.engine_factory,
            self.project_root,
            self.profile_root,
            mcp_manager=self.mcp_manager,
        )
        self.push_screen(self.main_screen)
        for warning in self.app_config.warnings:
            self.notify(warning, severity="warning", timeout=8)

    def action_help(self) -> None:
        if isinstance(self.screen, HelpScreen):
            return
        self.push_screen(HelpScreen())

    def action_toggle_armed(self) -> None:
        """F5: flip the OS-acting switch. Reaches into MainScreen exactly like
        the settings action does - the flag is the view's, because every
        primitive it governs (click, paste, scroll, cursor, focus, the clipboard
        watcher) is called from there and nowhere else."""
        main = self.main_screen
        if main is not None:
            main.set_os_armed(None)

    def action_settings(self) -> None:
        # Also the target of the sidebar's "Edit services..." button. Bound directly
        # to the F2 key, so Textual dispatches it OUTSIDE a worker - push_screen_wait
        # requires one, hence the run_worker hand-off (same pattern as _confirm_quit).
        if isinstance(self.screen, ServiceEditorScreen):
            return  # already open
        main = self.main_screen
        if main is not None and main.picker_open:
            # The main screen's chat-region overlay is up. The editor has capture
            # buttons behind its OWN one-overlay-at-a-time flag, so opening it now
            # is the one way to get two fullscreen child processes fighting over
            # the screen - and cancelling a worker cannot kill either of them.
            self.notify(
                "a region picker is open - finish it or press Esc first", severity="warning"
            )
            return
        self.run_worker(self._open_service_editor(), group="settings", exclusive=True)

    async def _open_service_editor(self) -> None:
        """Persist and propagate whatever the editor changed.

        Two independent kinds of change come back (see ``ServiceEdits``): the
        presets table, which is ours to write to config.toml, and captured
        appearances the editor already deleted from disk. The second still has
        to reach MainScreen - it caches profiles per run, paints them in the
        sidebar and hunts for them on a poll timer - so the propagation below
        runs for either, not only for a preset edit.

        The finish detectors are suspended for the whole visit. Capturing an
        appearance in there throws the same fullscreen overlay up over the very
        browser window they are watching, and an overlay appearing and vanishing
        is exactly the sustained large delta that arms the auto-copy trigger on
        staleness alone - so a poller left running would read the settled screen
        after the editor closes as a finished response and fire the copy flow at
        a chat nobody sent anything to. The restart is in a ``finally`` because
        the early return above it is the common case: an editor closed with no
        changes propagates nothing at all.
        """
        main = self.main_screen
        if main is not None:
            main.suspend_detectors()
        try:
            initial_key = main.selected_service if main is not None else None
            result = await self.push_screen_wait(
                ServiceEditorScreen(self.app_config, self.profile_root, initial_key)
            )
            if result is None:
                return  # closed with no changes - nothing to persist or propagate
            if result.services is not None:
                save_services(result.services, self._global_config_path)
                self.app_config = replace(self.app_config, services=result.services)
            if main is not None:
                # Drops the profile cache, repaints the appearance summary and
                # rebuilds the detector poller around what is left.
                main.update_config(self.app_config)
                main.sidebar.refresh_services(self.app_config)
            self.notify(
                "service presets saved" if result.services is not None else "appearance updated",
                timeout=4,
            )
        finally:
            # A no-op when the propagation above already restarted it.
            if main is not None:
                main.resume_detectors()

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
