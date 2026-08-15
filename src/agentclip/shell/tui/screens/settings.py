"""SettingsScreen: appearance/theme picker (F4 - F3 is MainScreen's sidebar toggle).

Deliberately structured around a single-tab ``TabbedContent`` ("Appearance")
even though there's only one tab today - more settings tabs land later and
this keeps the shape stable for them rather than reworking the screen when
they show up.

Model: the screen remembers the theme that was active when it opened
(``self._initial_theme``). Selecting a radio button applies the theme
immediately via ``self.app.theme = ...`` - a live preview, not a staged
edit - so Save has nothing to do but hand back whatever is currently applied,
and Cancel/Escape restores ``_initial_theme`` before dismissing with ``None``
so the caller knows there's nothing to persist.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RadioButton, RadioSet, Static, TabbedContent, TabPane

# (theme name, friendly label) - order matches how they're offered in the UI.
THEME_CHOICES: tuple[tuple[str, str], ...] = (
    ("textual-light", "Light"),
    ("textual-dark", "Dark"),
    ("claude-warm", "Claude Warm"),
    ("claude-dark", "Claude Dark"),
)

_RADIO_ID_PREFIX = "theme-"


class SettingsScreen(ModalScreen[str | None]):
    """Dismisses with the theme name to persist, or ``None`` if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, initial_theme: str) -> None:
        super().__init__()
        self._initial_theme = initial_theme

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box", id="settings-box"):
            yield Static(Text("SETTINGS"), classes="title")
            with TabbedContent(id="settings-tabs"), TabPane("Appearance", id="tab-appearance"):
                yield RadioSet(
                    *[
                        RadioButton(
                            label,
                            value=(name == self._initial_theme),
                            id=f"{_RADIO_ID_PREFIX}{name}",
                        )
                        for name, label in THEME_CHOICES
                    ],
                    id="theme-radio-set",
                )
            with Horizontal(id="settings-actions"):
                yield Button("Save", id="settings-save-btn", variant="primary")
                yield Button("Cancel", id="settings-cancel-btn")
            yield Static(
                "selecting a theme previews it live · escape cancels (reverts preview)",
                classes="hint",
            )

    # -- live preview -------------------------------------------------------

    @on(RadioSet.Changed, "#theme-radio-set")
    def _on_theme_changed(self, event: RadioSet.Changed) -> None:
        event.stop()
        radio_id = event.pressed.id
        if radio_id is None:
            return
        self.app.theme = radio_id.removeprefix(_RADIO_ID_PREFIX)

    # -- close ----------------------------------------------------------------

    @on(Button.Pressed, "#settings-save-btn")
    def _on_save(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(self.app.theme)

    @on(Button.Pressed, "#settings-cancel-btn")
    def _on_cancel_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def action_cancel(self) -> None:
        self.app.theme = self._initial_theme
        self.dismiss(None)
