"""Sidebar: the right-hand settings column on MainScreen (tui.md section 1.3).

Replaces the old new-session modal. The service ("profile") picker lives here
permanently instead of behind a launch dialog: at launch the user sees an empty
chat with the composer focused, and the sidebar tells them *which* service the
first message will start a session against. The Select is locked while a session
runs (a session's preset is fixed - its budget is baked into the engine) and
unlocks again whenever the app is waiting for a new session's first message.

The widget is dumb on purpose: it holds no session state, exposes ``service``
(the chosen preset key), ``set_locked``, ``refresh_services``, ``update_region``,
``update_click``, ``update_busy`` and ``update_copy``; MainScreen owns every bit
of routing, including the "Edit services..." button, the "Set busy region..."
polling loop and the "Set copy button..." picker + auto-copy-click flow.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Select, Static

from agentclip.config import Config
from agentclip.screen.region import ScreenRegion

_HINT = "F3 hides this column · F2 settings · F1 help"
_REGION_UNSET = "not set - alt-tab to the chat yourself"
_CLICK_UNSET = "not set - clicks fall back to the chat region"
BUSY_UNSET = "not calibrated - set while the model is generating"  # MainScreen's teardown default
COPY_UNSET = "not set - auto-copy-click disabled"  # MainScreen's teardown default


def _short_root(project_root: Path) -> str:
    try:
        return str(Path("~") / project_root.relative_to(Path.home()))
    except ValueError:
        return str(project_root)


def _budget(chars: int) -> str:
    return f"{chars // 1000}k" if chars >= 1000 else str(chars)


def _service_options(config: Config) -> list[tuple[str, str]]:
    """``key · 12k`` per row - the column is 30 cells wide, so the preset's human
    label goes on its own line under the Select instead of wrapping inside it."""
    presets = sorted(config.services.values(), key=lambda p: p.key)
    return [(f"{p.key} · {_budget(p.max_paste_chars)}", p.key) for p in presets]


class Sidebar(Vertical):
    """Project root + service picker + the door to the service editor."""

    def __init__(self, config: Config, project_root: Path, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._config = config
        self._project_root = project_root

    def compose(self) -> ComposeResult:
        yield Static(Text("PROJECT"), classes="side-title")
        yield Static(Text(_short_root(self._project_root)), id="side-root")
        yield Static(Text("SERVICE"), classes="side-title")
        yield Select(
            _service_options(self._config),
            value=self._default_service(),
            allow_blank=False,
            id="service-select",
        )
        yield Static(Text(self._preset_caption()), id="side-service-label")
        yield Button("Edit services...", id="edit-services-btn", variant="primary")
        yield Static(Text("CHAT WINDOW"), classes="side-title")
        yield Button("Set chat region...", id="set-region-btn")
        yield Static(Text(_REGION_UNSET), id="side-region")
        yield Button("Set click region...", id="set-click-btn")
        yield Static(Text(_CLICK_UNSET), id="side-click")
        yield Static(Text("REASONING"), classes="side-title")
        yield Button("Set busy region...", id="set-busy-btn")
        yield Static(Text(BUSY_UNSET), id="side-busy")
        yield Static(Text("COPY BUTTON"), classes="side-title")
        yield Button("Set copy button...", id="set-copy-btn")
        yield Static(Text(COPY_UNSET), id="side-copy")
        yield Static(Text(_HINT), classes="side-hint")

    def _preset_caption(self, key: str | None = None) -> str:
        preset = self._config.services.get(key or self._default_service())
        if not preset:
            return ""
        return (
            f"{preset.label} · {preset.max_paste_chars:,} chars per paste "
            f"· {preset.total_context_chars:,} chars context"
        )

    @on(Select.Changed, "#service-select")
    def _on_service_changed(self, event: Select.Changed) -> None:
        # The caption is display only; MainScreen reads `service` when it starts a
        # session, so the message keeps bubbling (nothing else listens today).
        value = event.value
        key = None if value is Select.NULL else str(value)
        self.query_one("#side-service-label", Static).update(Text(self._preset_caption(key)))

    def _default_service(self) -> str:
        """The configured default, or the first preset if the config is odd."""
        configured = self._config.general.service
        if configured in self._config.services:
            return configured
        return next(iter(sorted(self._config.services)))

    # -- the service picker ---------------------------------------------------

    @property
    def service_select(self) -> Select[str]:
        return self.query_one("#service-select", Select)

    @property
    def service(self) -> str:
        """The selected preset key (falls back to the configured default)."""
        value = self.service_select.value
        return self._default_service() if value is Select.NULL else str(value)

    def set_locked(self, locked: bool) -> None:
        """Lock the picker while a session owns the service; unlock between sessions."""
        self.service_select.disabled = locked

    # -- the chat region ------------------------------------------------------

    def update_region(self, region: ScreenRegion | None) -> None:
        """Show the session's drawn chat region - the window that hosts the
        chatbot, and the fallback the post-response click uses when no click
        region is drawn (display only; MainScreen owns it)."""
        text = f"{region.describe()} · chatbot window" if region is not None else _REGION_UNSET
        self.query_one("#side-region", Static).update(Text(text))

    # -- the click region -----------------------------------------------------

    def update_click(self, region: ScreenRegion | None) -> None:
        """Show the session's drawn click region - where AgentClip clicks once a
        model response is fully handled (display only; MainScreen owns it)."""
        text = (
            f"{region.describe()} · outbound copies click it"
            if region is not None
            else _CLICK_UNSET
        )
        self.query_one("#side-click", Static).update(Text(text))

    # -- the busy region --------------------------------------------------------

    def update_busy(self, text: str) -> None:
        """Repaint the live busy-probe readout (display only; MainScreen owns the
        detector loop and formats the text - MATCH/CHANGED/ERROR, or the
        not-calibrated default)."""
        self.query_one("#side-busy", Static).update(Text(text))

    # -- the copy-button region -------------------------------------------------

    def update_copy(self, text: str) -> None:
        """Repaint the copy-button readout (display only; MainScreen owns the
        region/template state and the auto-copy-click flow - formats the text
        as "set", "clicked", "not found", or the not-set default)."""
        self.query_one("#side-copy", Static).update(Text(text))

    def refresh_services(self, config: Config | None = None) -> None:
        """Rebuild the options after the services table changed (service editor hook).

        Keeps the current selection when that preset survived the edit.
        """
        if config is not None:
            self._config = config
        current = self.service
        select = self.service_select
        select.set_options(_service_options(self._config))
        select.value = current if current in self._config.services else self._default_service()
        self.query_one("#side-service-label", Static).update(
            Text(self._preset_caption(self.service))
        )
