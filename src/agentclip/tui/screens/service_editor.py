"""ServiceEditorScreen: add/edit/delete service presets (F2 / sidebar button).

Replaces the never-built ConfigScreen sketch in tui.md section 1.4 - scope is
narrowed to just the services table (name, max paste size, total context size),
which is the thing users actually need to tune per chat service.

Model: the screen works on an in-memory *working copy* of ``config.services``
(``self._services``). Editing an existing preset's label/sizes applies live -
every keystroke revalidates the whole candidate (key + label + both sizes
together, since "max <= total" is a cross-field rule) and, only while valid,
writes it straight into the working copy. Adding a new preset is the one
discrete action: fill in a unique lowercase-hyphen key plus the other fields,
then press "Add service" (enabled only once the candidate validates) - keys
are one-time (immutable after creation), so committing them by an explicit
action rather than continuously avoids collisions/half-typed keys.

Escape closes: if the two size fields and label are currently valid for
whatever is selected, the screen dismisses with the working ``services`` dict
if it differs from what it opened with (``None`` if nothing changed - the
caller then has nothing to persist). If the currently displayed field values
are invalid, nothing was ever applied to the working copy (invalid values are
never committed), so there is no real "pending edit" to lose - but the visible
text would vanish, which is surprising - so escape instead asks via the shared
``ConfirmScreen`` whether to discard that (never-applied) text and close.

Deletion is only offered for non-built-in keys (the 12 shipped presets can be
edited and reset, never removed - config.py's ``save_services`` needs the
built-in set to know what NOT to write to disk). "Reset to default" restores a
built-in preset's shipped values (available for any built-in, whether or not
it currently differs - a no-op if it's already default).
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from agentclip.config import (
    BUILTIN_SERVICE_KEYS,
    DEFAULT_STABLE_SECONDS,
    Config,
    ServicePreset,
    default_services,
)
from agentclip.screen.profile_store import ProfileStoreError, delete_profile, load_profile
from agentclip.tui.screens.confirm import ConfirmScreen

_NEW_SENTINEL = "+add-new+"  # not a legal slug (contains '+'), so it can't collide with a key
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# The appearance line for a service with nothing captured yet.
TEMPLATES_NONE = "appearance: nothing captured yet"


def _templates_line(root: Path, key: str | None) -> str:
    """One read-only line describing what this service LOOKS like.

    The editor never captures anything - drawing a box needs the browser on
    screen, which is a main-screen job - but it is where a user goes to reason
    about a service, so "does this one know what its copy button looks like?"
    has to be answerable here.
    """
    if key is None:
        return TEMPLATES_NONE
    captured = load_profile(root, key).captured
    if not captured:
        return TEMPLATES_NONE
    names = ", ".join(kind.label for kind in captured)
    return f"appearance: {len(captured)}/6 captured ({names})"


def _select_options(services: dict[str, ServicePreset]) -> list[tuple[str, str]]:
    opts = [
        (f"{key} ({'builtin' if key in BUILTIN_SERVICE_KEYS else 'custom'})", key)
        for key in sorted(services)
    ]
    opts.append(("+ Add new service...", _NEW_SENTINEL))
    return opts


class ServiceEditorScreen(ModalScreen["dict[str, ServicePreset] | None"]):
    """Dismisses with the edited services table, or ``None`` if nothing changed."""

    BINDINGS = [Binding("escape", "close", "close")]

    def __init__(self, config: Config, profile_root: Path) -> None:
        super().__init__()
        self._profile_root = profile_root
        self._services: dict[str, ServicePreset] = dict(config.services)
        self._initial_services: dict[str, ServicePreset] = dict(config.services)
        default_key = (
            config.general.service
            if config.general.service in self._services
            else next(iter(sorted(self._services)))
        )
        self._selected_key: str | None = default_key
        self._pending_new: ServicePreset | None = None
        self._current_error: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box", id="service-editor-box"):
            yield Static(Text("SERVICE EDITOR"), classes="title")
            with Horizontal(id="svc-body"):
                with Vertical(id="svc-list-col"):
                    yield Static(Text("Services"), classes="side-title")
                    yield Select(
                        _select_options(self._services),
                        value=self._selected_key,
                        allow_blank=False,
                        id="svc-select",
                    )
                with Vertical(id="svc-form-col"):
                    yield Static(Text("Key"), classes="side-title")
                    yield Input(id="svc-key", placeholder="lowercase-with-hyphens")
                    yield Static(Text("Name"), classes="side-title")
                    yield Input(id="svc-label", placeholder="display name")
                    yield Static(Text("Max input size (chars per paste)"), classes="side-title")
                    yield Input(id="svc-max", placeholder="e.g. 12000")
                    yield Static(Text("Total context size (chars)"), classes="side-title")
                    yield Input(id="svc-total", placeholder="e.g. 500000")
                    yield Static(Text("Stale after (seconds unchanged)"), classes="side-title")
                    yield Input(id="svc-stable", placeholder="e.g. 2.0")
                    yield Static(
                        Text(_templates_line(self._profile_root, self._selected_key)),
                        id="svc-templates",
                    )
                    yield Static("", id="svc-error")
                    with Horizontal(id="svc-actions"):
                        yield Button("Add service", id="svc-add-btn", variant="primary")
                        yield Button("Reset to default", id="svc-reset-btn")
                        yield Button("Forget appearance", id="svc-forget-templates-btn")
                        yield Button("Delete", id="svc-delete-btn", variant="error")
            yield Static(
                "escape closes (applies valid edits) · built-ins: edit or reset, never delete",
                classes="hint",
            )

    def on_mount(self) -> None:
        self._load_service(self._selected_key)

    # -- selecting a service ---------------------------------------------------

    @on(Select.Changed, "#svc-select")
    def _on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        value = str(event.value)
        self._load_service(None if value == _NEW_SENTINEL else value)

    def _load_service(self, key: str | None) -> None:
        self._selected_key = key
        key_input = self.query_one("#svc-key", Input)
        label_input = self.query_one("#svc-label", Input)
        max_input = self.query_one("#svc-max", Input)
        total_input = self.query_one("#svc-total", Input)
        stable_input = self.query_one("#svc-stable", Input)
        if key is None:
            key_input.disabled = False
            key_input.value = ""
            label_input.value = ""
            max_input.value = ""
            total_input.value = ""
            # Pre-filled, unlike the sizes: the stale window has a sensible
            # universal default, and "add a service" should stay a four-field
            # job for users who never touch the stale detector.
            stable_input.value = str(DEFAULT_STABLE_SECONDS)
        else:
            preset = self._services[key]
            key_input.disabled = True
            key_input.value = preset.key
            label_input.value = preset.label
            max_input.value = str(preset.max_paste_chars)
            total_input.value = str(preset.total_context_chars)
            stable_input.value = str(preset.stable_seconds)
        self._pending_new = None
        self.query_one("#svc-templates", Static).update(
            Text(_templates_line(self._profile_root, key))
        )
        self._update_buttons()
        self._revalidate()

    def _update_buttons(self) -> None:
        is_new = self._selected_key is None
        key = self._selected_key
        self.query_one("#svc-add-btn", Button).display = is_new
        self.query_one("#svc-reset-btn", Button).display = (
            not is_new and key in BUILTIN_SERVICE_KEYS
        )
        self.query_one("#svc-delete-btn", Button).display = (
            not is_new and key not in BUILTIN_SERVICE_KEYS
        )
        # Captures are per service, not per built-in-ness: any existing service
        # with something captured can have it forgotten.
        self.query_one("#svc-forget-templates-btn", Button).display = (
            not is_new
            and key is not None
            and bool(load_profile(self._profile_root, key).captured)
        )

    # -- live validation --------------------------------------------------------

    @on(Input.Changed)
    def _on_field_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._revalidate()

    def _revalidate(self) -> None:  # noqa: PLR0912 - straight-line field validation
        is_new = self._selected_key is None
        key_text = self.query_one("#svc-key", Input).value.strip()
        label_text = self.query_one("#svc-label", Input).value.strip()
        max_text = self.query_one("#svc-max", Input).value.strip()
        total_text = self.query_one("#svc-total", Input).value.strip()
        stable_text = self.query_one("#svc-stable", Input).value.strip()

        error: str | None = None
        key = self._selected_key
        if is_new:
            if not key_text:
                error = "key is required"
            elif not _SLUG_RE.match(key_text):
                error = "key must be lowercase letters, digits, and hyphens only"
            elif key_text in self._services:
                error = f"key {key_text!r} is already in use"
            else:
                key = key_text

        if error is None and not label_text:
            error = "name is required"

        max_val: int | None = None
        total_val: int | None = None
        if error is None:
            try:
                max_val = int(max_text)
            except ValueError:
                error = "max input size must be a whole number"
            else:
                if max_val <= 0:
                    error = "max input size must be positive"
        if error is None:
            try:
                total_val = int(total_text)
            except ValueError:
                error = "total context size must be a whole number"
            else:
                if total_val <= 0:
                    error = "total context size must be positive"
        if error is None and max_val is not None and total_val is not None and max_val > total_val:
            error = "max input size can't exceed total context size"

        stable_val: float | None = None
        if error is None:
            try:
                stable_val = float(stable_text)
            except ValueError:
                error = "stale seconds must be a number"
            else:
                # Same bounds config.py enforces on load, so a value the editor
                # accepts is never silently replaced on the next start.
                if not (0.5 <= stable_val <= 60.0):
                    error = "stale seconds must be between 0.5 and 60"

        self.query_one("#svc-error", Static).update(error or "")
        self._current_error = error

        if error is not None:
            self._pending_new = None
            if is_new:
                self.query_one("#svc-add-btn", Button).disabled = True
            return

        assert (
            key is not None and max_val is not None and total_val is not None
            and stable_val is not None
        )
        if is_new:
            self._pending_new = ServicePreset(
                key=key,
                label=label_text,
                max_paste_chars=max_val,
                total_context_chars=total_val,
                stable_seconds=stable_val,
            )
            self.query_one("#svc-add-btn", Button).disabled = False
        else:
            existing = self._services[key]
            self._services[key] = replace(
                existing,
                label=label_text,
                max_paste_chars=max_val,
                total_context_chars=total_val,
                stable_seconds=stable_val,
            )

    # -- add / reset / delete ----------------------------------------------------

    @on(Button.Pressed, "#svc-add-btn")
    def _on_add(self, event: Button.Pressed) -> None:
        event.stop()
        if self._pending_new is None:
            return
        preset = self._pending_new
        self._services[preset.key] = preset
        self._pending_new = None
        self._refresh_select_options(preset.key)

    @on(Button.Pressed, "#svc-reset-btn")
    def _on_reset(self, event: Button.Pressed) -> None:
        event.stop()
        key = self._selected_key
        if key is None or key not in BUILTIN_SERVICE_KEYS:
            return
        self._services[key] = default_services()[key]
        self._load_service(key)

    @on(Button.Pressed, "#svc-forget-templates-btn")
    def _on_forget_templates(self, event: Button.Pressed) -> None:
        # Same worker hand-off as action_close: push_screen_wait needs one.
        event.stop()
        self.run_worker(self._forget_templates_async(), group="forget", exclusive=True)

    async def _forget_templates_async(self) -> None:
        """Delete a service's captured appearances from disk, behind a confirm.

        Deliberately separate from "Delete", which removes the *preset*: a user
        whose browser theme changed wants to recapture, not to lose their size
        settings.
        """
        key = self._selected_key
        if key is None:
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(
                f"Forget the {key} appearance?",
                "The captured images of this service's buttons and chat box will be "
                "deleted from disk. Its size settings are untouched, but every one "
                "of those images has to be captured again from the main screen.",
            )
        )
        if not confirmed:
            return
        # A profile we cannot delete simply reads as one that is still there:
        # the readout is re-derived from disk either way.
        with suppress(ProfileStoreError):
            delete_profile(self._profile_root, key)
        self._load_service(key)

    @on(Button.Pressed, "#svc-delete-btn")
    def _on_delete(self, event: Button.Pressed) -> None:
        event.stop()
        key = self._selected_key
        if key is None or key in BUILTIN_SERVICE_KEYS or key not in self._services:
            return
        del self._services[key]
        next_key = next(iter(sorted(self._services)))
        self._refresh_select_options(next_key)

    def _refresh_select_options(self, value: str) -> None:
        select = self.query_one("#svc-select", Select)
        select.set_options(_select_options(self._services))
        select.value = value
        self._load_service(value)

    # -- close --------------------------------------------------------------

    def action_close(self) -> None:
        # Bound directly to the escape key, so Textual dispatches it OUTSIDE a
        # worker - push_screen_wait (for the discard confirm) needs one, hence
        # the hand-off (same pattern as AgentClipApp._confirm_quit).
        self.run_worker(self._close_async(), group="close", exclusive=True)

    async def _close_async(self) -> None:
        if self._current_error is not None:
            discard = await self.app.push_screen_wait(
                ConfirmScreen(
                    "Discard the pending edit?",
                    f"The current field values are invalid ({self._current_error}) and were "
                    "never applied. Close the service editor anyway?",
                )
            )
            if not discard:
                return
        changed = self._services != self._initial_services
        self.dismiss(self._services if changed else None)
