"""ServiceEditorScreen: the whole per-service PROFILE editor (F2 / sidebar button).

Replaces the never-built ConfigScreen sketch in tui.md section 1.4 - the scope
is everything that is a property of *one chat service*: its name, its two size
budgets, the stale detector's stillness window, what it LOOKS like (the seven
captured appearances), which finish signals its poller may run, and how an
outbound payload is delivered into its chat box (one paste or a chunked stream).

Model: the screen works on an in-memory *working copy* of ``config.services``
(``self._services``). Editing an existing preset's label/sizes applies live -
every keystroke revalidates the whole candidate (key + label + both sizes
together, since "max <= total" is a cross-field rule) and, only while valid,
writes it straight into the working copy; the detection checkboxes write the
same way. Adding a new preset is the one discrete action: fill in a unique
lowercase-hyphen key plus the other fields, then press "Add service" (enabled
only once the candidate validates) - keys are one-time (immutable after
creation), so committing them by an explicit action rather than continuously
avoids collisions/half-typed keys. Until that press there is no key to file
anything under, so the capture buttons and the checkboxes are disabled - but the
boxes still *show* what the press will create (``_NEW_PRESET_DEFAULTS``), because
a blank checklist over a preset that is born with "screen stops changing" ticked
is a lie about the only setting on that form the user cannot see anywhere else.

Captures are the exception to "applies on close": pressing "Capture <thing>..."
runs the same full-screen draw-a-box overlay the chat region uses and writes the
PNG to the profile store *immediately*, exactly as "Forget appearance" deletes
immediately. Only one overlay may be up at a time (cancelling a worker cannot
kill the blocking child process it spawned), so a second capture press - and
escape - are refused while one is in flight.

A capture ADDS an image to its kind rather than replacing one (screen.profile:
a kind is a stack, all of it ORed at match time), so the block also carries a
per-kind "Clear" that empties one stack - instantly, no confirm, because a
cleared kind is one press away from being back. "Forget appearance" is the
other thing entirely and keeps its dialog: it deletes the whole service's
calibration.

Escape closes with a :class:`ServiceEdits` answer - or ``None`` when nothing
changed at all, so the caller has nothing to do. Two independent things can
change behind one escape and the caller has to act on either: the presets table
(which it persists) and a service's captured appearances on disk (which
capture/"Forget appearance"/"Delete" have already written or removed, and which
the main screen caches, paints and hunts for). A bare services dict could not
say the second happened. If the currently displayed field values
are invalid, nothing was ever applied to the working copy (invalid values are
never committed), so there is no real "pending edit" to lose - but the visible
text would vanish, which is surprising - so escape instead asks via the shared
``ConfirmScreen`` whether to discard that (never-applied) text and close.

Deletion is only offered for non-built-in keys (the 12 shipped presets can be
edited and reset, never removed - config.py's ``save_services`` needs the
built-in set to know what NOT to write to disk). Deleting a preset takes its
captured appearances with it: the key is gone from every picker, so the folder
of PNGs behind it is unreachable from anywhere in the app. "Reset to default"
restores a built-in preset's shipped values (available for any built-in,
whether or not it currently differs - a no-op if it's already default) and
touches no captures at all.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from agentclip.config import (
    BUILTIN_SERVICE_KEYS,
    DEFAULT_STABLE_SECONDS,
    DELIVERY_PASTE,
    DELIVERY_STREAM,
    FINISH_SIGNALS,
    Config,
    ServicePreset,
    default_services,
    normalize_finish_signals,
)
from agentclip.screen.capture import CaptureError, capture_region
from agentclip.screen.picker import ScreenPickError, pick_region
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.profile_store import (
    ProfileStoreError,
    delete_profile,
    drop_template,
    load_profile,
    save_template,
)
from agentclip.tui.pixels import half_block_text, thumbnail
from agentclip.tui.screens.confirm import ConfirmScreen

_NEW_SENTINEL = "+add-new+"  # not a legal slug (contains '+'), so it can't collide with a key
# What "+ Add new" is going to create for every field the form does not ask
# about. The detection checkboxes load from THIS rather than from literals (and
# _revalidate builds its candidate from the same dataclass defaults), so the
# form and the preset it produces cannot disagree - an all-unticked form that
# quietly created a stale-ticked preset was the drift this closes. The three
# required fields are placeholders; only the defaults below them are read.
_NEW_PRESET_DEFAULTS = ServicePreset(
    key="", label="", max_paste_chars=1, total_context_chars=1
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# The appearance line for a service with nothing captured yet.
TEMPLATES_NONE = "appearance: nothing captured yet"
# What an appearance the service profile does not hold yet reads as. One line
# for all of them: they are captured the same way and lost the same way, and
# the per-kind advice belongs on the picker prompt (TemplateKind.prompt), where
# the user is actually being asked to draw the box.
TEMPLATE_UNSET = "not captured"

# The APPEARANCE block is generated per TemplateKind, so its widget ids are a
# naming convention rather than a table: the kind is parsed back out of a
# pressed button's id, which is what lets one handler serve all seven - twice
# over now that each kind has a Clear beside its Capture.
CAPTURE_CLASS = "svc-capture-btn"
CLEAR_CLASS = "svc-clear-btn"
_CAPTURE_PREFIX = "svc-capture-"
_CLEAR_PREFIX = "svc-clear-"
_BUTTON_SUFFIX = "-btn"
CLEAR_LABEL = "Clear"

# The finish-signal checklist, in user words rather than detector names. The
# TOML keys ("busy"/"idle"/"stale") describe how the detector works; these
# describe what the user will SEE when it fires, which is the only thing they
# can check against their own chat window.
SIGNAL_LABELS = {
    "busy": "reasoning icon disappears",
    "idle": "send icon appears",
    "stale": "screen stops changing",
}
# Which appearance a ticked signal additionally needs. "stale" needs none - a
# drawn chat region is a finish detector all by itself.
SIGNAL_TEMPLATE = {
    "busy": TemplateKind.BUSY,
    "idle": TemplateKind.IDLE,
}
HOVER_SCAN_LABEL = "hover-scan for copy icon"
# The delivery mode, as a tick rather than a picker: there are exactly two modes
# and one of them is the default, so "off" says "paste" without a second row of
# form to read. Worded as what the user will SEE, like the signal labels.
STREAM_DELIVERY_LABEL = "paste in chunks (big messages)"
# A ticked busy/idle entry whose appearance was never captured runs nothing at
# all (config.py's checklist and the profile are ANDed). Silent dead weight is
# exactly the failure that shows up as an auto-copy that never fires, so it is
# said here, next to the tick that caused it.
SIGNAL_UNCAPTURED = "ticked but not captured — it will be skipped"


def capture_button_id(kind: TemplateKind) -> str:
    return f"{_CAPTURE_PREFIX}{kind}{_BUTTON_SUFFIX}"


def clear_button_id(kind: TemplateKind) -> str:
    return f"{_CLEAR_PREFIX}{kind}{_BUTTON_SUFFIX}"


# The picture beside each appearance's status line. "40×40 · captured" says a
# capture happened; it cannot say WHAT was captured, and a drag that caught the
# background beside the stop button reads exactly the same. So the first image
# of each kind is drawn here in half-blocks (tui.pixels), at the only size the
# 34-cell column can spare: 12 cells by 2 rows, which is 12x4 pixels. That is
# far too coarse to read a glyph and quite enough to tell an orange icon from a
# slab of white page - which is the mistake this catches.
TEMPLATE_PREVIEW_COLS = 12
TEMPLATE_PREVIEW_ROWS = 2


def template_status_id(kind: TemplateKind) -> str:
    return f"svc-tpl-{kind}"


def template_preview_id(kind: TemplateKind) -> str:
    return f"svc-tpl-preview-{kind}"


def signal_checkbox_id(signal: str) -> str:
    return f"svc-signal-{signal}"


def _kind_from_id(button_id: str | None, prefix: str) -> TemplateKind | None:
    if (
        button_id is None
        or not button_id.startswith(prefix)
        or not button_id.endswith(_BUTTON_SUFFIX)
    ):
        return None
    try:
        return TemplateKind(button_id[len(prefix) : -len(_BUTTON_SUFFIX)])
    except ValueError:
        return None


def kind_from_button_id(button_id: str | None) -> TemplateKind | None:
    """The appearance a capture-button press is about, or None if it is not one."""
    return _kind_from_id(button_id, _CAPTURE_PREFIX)


def kind_from_clear_button_id(button_id: str | None) -> TemplateKind | None:
    """The appearance a Clear press is about, or None if it is not one."""
    return _kind_from_id(button_id, _CLEAR_PREFIX)


def template_status(profile: ServiceProfile | None, kind: TemplateKind) -> str:
    """One appearance's status line: what is captured for it, or the not-set default.

    A kind holds a stack of images, all of them searched for (screen.profile),
    so the count belongs in the readout: a second capture ADDS, and a line that
    kept saying "captured" would leave the user believing it had replaced.
    """
    templates = () if profile is None else profile.variants(kind)
    if not templates:
        return TEMPLATE_UNSET
    first = templates[0]
    if len(templates) == 1:
        return f"{first.width}×{first.height} · captured"
    return f"{first.width}×{first.height} · {len(templates)} images"


def template_preview(profile: ServiceProfile | None, kind: TemplateKind) -> Text:
    """The first captured image of ``kind``, drawn small - or empty text.

    The FIRST of the stack, not all of them: a kind's variants are pictures of
    the same control (a send button greyed out and not), the column has room
    for one, and the count is already on the status line beside it.
    """
    templates = () if profile is None else profile.variants(kind)
    if not templates:
        return Text("")
    small = thumbnail(templates[0].image, TEMPLATE_PREVIEW_COLS, TEMPLATE_PREVIEW_ROWS)
    return Text("") if small is None else half_block_text(small)


def _templates_line(profile: ServiceProfile | None) -> str:
    """The one-line summary of what this service LOOKS like."""
    if profile is None or not profile.captured:
        return TEMPLATES_NONE
    names = ", ".join(kind.label for kind in profile.captured)
    return f"appearance: {profile.describe()} ({names})"


def _signal_warning(preset: ServicePreset | None, profile: ServiceProfile | None) -> str:
    """The inline "you ticked a detector that cannot run" line, or ""."""
    if preset is None:
        return ""
    gaps = [
        SIGNAL_TEMPLATE[signal].label
        for signal in preset.finish_signals
        if signal in SIGNAL_TEMPLATE
        and (profile is None or not profile.has(SIGNAL_TEMPLATE[signal]))
    ]
    if not gaps:
        return ""
    return f"{', '.join(gaps)}: {SIGNAL_UNCAPTURED}"


@dataclass(frozen=True, slots=True)
class ServiceEdits:
    """What one visit to the editor changed, as one answer.

    ``services`` is the edited presets table, or None when the table came out
    exactly as it went in - the caller then has nothing to persist.
    ``profiles_changed`` is separate because captured appearances are written or
    deleted on disk the moment the user acts, not on close: the caller cannot
    diff for it, and must reload/repaint/re-arm around it either way.
    """

    services: dict[str, ServicePreset] | None
    profiles_changed: bool


def _select_options(services: dict[str, ServicePreset]) -> list[tuple[str, str]]:
    opts = [
        (f"{key} ({'builtin' if key in BUILTIN_SERVICE_KEYS else 'custom'})", key)
        for key in sorted(services)
    ]
    opts.append(("+ Add new service...", _NEW_SENTINEL))
    return opts


class ServiceEditorScreen(ModalScreen["ServiceEdits | None"]):
    """Dismisses with a :class:`ServiceEdits`, or ``None`` if nothing changed."""

    BINDINGS = [Binding("escape", "close", "close")]

    def __init__(
        self, config: Config, profile_root: Path, initial_key: str | None = None
    ) -> None:
        super().__init__()
        self._profile_root = profile_root
        self._services: dict[str, ServicePreset] = dict(config.services)
        self._initial_services: dict[str, ServicePreset] = dict(config.services)
        # Set the moment a profile folder is actually written or removed on
        # disk. Not derivable on close (it already happened), and the caller has
        # to hear about it: it caches profiles, paints them, and hunts for the
        # templates it thinks are there.
        self._profiles_changed = False
        # One overlay at a time: cancelling the exclusive worker cannot kill the
        # blocking child overlay process it spawned, so a second capture press -
        # and a close - are refused while one is up.
        self._capturing = False
        # Preselect the service behind the tab the user had open when they
        # pressed F2/"Edit services..." (``initial_key``, resolved by the
        # caller from the selected window tab) - falling back to the
        # configured default, then alphabetically first, exactly as before
        # when there is no such tab (a caller that doesn't pass one, or a key
        # that named a service since deleted).
        default_key = (
            initial_key
            if initial_key in self._services
            else config.general.service
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
                    yield Static(Text("DETECTION · finished when"), classes="side-title")
                    for signal in FINISH_SIGNALS:
                        yield Checkbox(
                            SIGNAL_LABELS[signal], id=signal_checkbox_id(signal), compact=True
                        )
                    yield Checkbox(HOVER_SCAN_LABEL, id="svc-hover-scan", compact=True)
                    yield Static("", id="svc-signal-warning")
                    yield Static(Text("DELIVERY · how the payload goes in"), classes="side-title")
                    yield Checkbox(
                        STREAM_DELIVERY_LABEL, id="svc-stream-delivery", compact=True
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
                    yield Static("", id="svc-error")
                    with Horizontal(id="svc-actions"):
                        yield Button("Add service", id="svc-add-btn", variant="primary")
                        yield Button("Reset to default", id="svc-reset-btn")
                        yield Button("Delete", id="svc-delete-btn", variant="error")
                with Vertical(id="svc-appearance-col"):
                    yield Static(Text("APPEARANCE"), classes="side-title")
                    for kind in TemplateKind:
                        yield Button(
                            f"Capture {kind.label}...",
                            id=capture_button_id(kind),
                            classes=CAPTURE_CLASS,
                            compact=True,
                        )
                        # The preview and Clear both ride on the status line
                        # rather than beside the capture button: the column is
                        # 34 wide and the longest capture label already fills
                        # it. The row is two cells tall - the fewest that draw
                        # a picture at all in half-blocks - and that is the
                        # whole of the height budget: a third row, times seven
                        # kinds, pushes the modal off a 45-row terminal.
                        with Horizontal(classes="svc-appearance-row"):
                            yield Static(
                                Text(""),
                                id=template_preview_id(kind),
                                classes="svc-tpl-preview",
                            )
                            yield Static(
                                Text(TEMPLATE_UNSET),
                                id=template_status_id(kind),
                                classes="side-status",
                            )
                            yield Button(
                                CLEAR_LABEL,
                                id=clear_button_id(kind),
                                classes=CLEAR_CLASS,
                                compact=True,
                            )
                    yield Static(Text(TEMPLATES_NONE), id="svc-templates")
                    yield Button("Forget appearance", id="svc-forget-templates-btn", compact=True)
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

    def _profile(self, key: str | None) -> ServiceProfile | None:
        """``key``'s captured appearances, read from disk. None for "+ Add new".

        Read once per selection and passed along: several readouts here are
        views of the same folder, and reading it twice per click is two decodes
        of every PNG a service has.
        """
        return None if key is None else load_profile(self._profile_root, key)

    def _load_service(self, key: str | None) -> None:
        self._selected_key = key
        key_input = self.query_one("#svc-key", Input)
        label_input = self.query_one("#svc-label", Input)
        max_input = self.query_one("#svc-max", Input)
        total_input = self.query_one("#svc-total", Input)
        stable_input = self.query_one("#svc-stable", Input)
        preset: ServicePreset | None = None
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
        # For "+ Add new", the boxes show what pressing "Add service" is
        # actually going to create - i.e. the ServicePreset dataclass defaults,
        # stale ticked, hover off, one-paste delivery - rather than an
        # all-unticked form that reads as "no finish detection" and then silently
        # produces the opposite. They stay disabled until the key exists.
        shown = preset if preset is not None else _NEW_PRESET_DEFAULTS
        signals, hover = shown.finish_signals, shown.hover_scan
        for signal in FINISH_SIGNALS:
            box = self.query_one(f"#{signal_checkbox_id(signal)}", Checkbox)
            box.value = signal in signals
        self.query_one("#svc-hover-scan", Checkbox).value = hover
        self.query_one("#svc-stream-delivery", Checkbox).value = shown.delivery == DELIVERY_STREAM
        self._show_appearance(self._profile(key))
        self._revalidate()

    def _show_appearance(self, profile: ServiceProfile | None) -> None:
        """Repaint everything derived from what this service looks like.

        One place, because five readouts are views of the same folder: the
        per-kind pictures, the per-kind status lines, the summary, whether
        there is anything to forget, and whether a ticked finish signal has an
        appearance to run against.
        """
        for kind in TemplateKind:
            self.query_one(f"#{template_preview_id(kind)}", Static).update(
                template_preview(profile, kind)
            )
            self.query_one(f"#{template_status_id(kind)}", Static).update(
                Text(template_status(profile, kind))
            )
        self.query_one("#svc-templates", Static).update(Text(_templates_line(profile)))
        self._update_buttons(profile)
        self._paint_signal_warning(profile)

    def _paint_signal_warning(self, profile: ServiceProfile | None) -> None:
        key = self._selected_key
        preset = self._services.get(key) if key is not None else None
        self.query_one("#svc-signal-warning", Static).update(
            Text(_signal_warning(preset, profile))
        )

    def _update_buttons(self, profile: ServiceProfile | None) -> None:
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
            profile is not None and bool(profile.captured)
        )
        # Nothing to file a capture or a checklist under until "Add service" has
        # actually created the key. Disabled rather than hidden so the column
        # does not reflow as the user fills the form in.
        for button in self.query(f".{CAPTURE_CLASS}").results(Button):
            button.disabled = is_new
        # Clear is dead for a kind holding nothing, which is also the whole
        # readout of whether pressing it would do anything at all - there is no
        # confirmation step to find that out from.
        for kind in TemplateKind:
            self.query_one(f"#{clear_button_id(kind)}", Button).disabled = is_new or not (
                profile is not None and profile.has(kind)
            )
        for box in self.query(Checkbox):
            box.disabled = is_new

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

    # -- the detection checklist -------------------------------------------------

    @on(Checkbox.Changed)
    def _on_detection_changed(self, event: Checkbox.Changed) -> None:
        """Fold every toggle on this column back into the selected preset, live.

        Read as a set rather than per-box: ``finish_signals`` is a checklist in
        one canonical order (config.FINISH_SIGNALS), so building it from all
        three boxes is both simpler and immune to the echo Textual fires when
        ``_load_service`` writes the values in - that echo writes the freshly
        loaded service's own values straight back, which is a no-op. The
        hover-scan and delivery ticks ride the same handler for the same reason,
        even though neither is a finish signal: one read of every box is what
        makes the echo harmless.
        """
        event.stop()
        key = self._selected_key
        if key is None or key not in self._services:
            return
        signals = normalize_finish_signals(
            signal
            for signal in FINISH_SIGNALS
            if self.query_one(f"#{signal_checkbox_id(signal)}", Checkbox).value
        )
        hover = self.query_one("#svc-hover-scan", Checkbox).value
        streaming = self.query_one("#svc-stream-delivery", Checkbox).value
        self._services[key] = replace(
            self._services[key],
            finish_signals=signals,
            hover_scan=hover,
            delivery=DELIVERY_STREAM if streaming else DELIVERY_PASTE,
        )
        self._paint_signal_warning(self._profile(key))

    # -- capturing what a service LOOKS like -------------------------------------

    @on(Button.Pressed, f".{CAPTURE_CLASS}")
    def _on_capture_pressed(self, event: Button.Pressed) -> None:
        """The one route into a capture, for all seven appearances.

        The block is generated per ``TemplateKind`` with the kind encoded in
        each button's id, so the kind is parsed back out here rather than
        duplicated in seven near-identical handlers - adding an eighth appearance
        means adding an enum member and nothing else.
        """
        event.stop()
        kind = kind_from_button_id(event.button.id)
        if kind is None or self._selected_key is None:
            return
        if self._capturing:
            self.notify("a region picker is already open - finish it or press Esc first")
            return
        self._capturing = True
        self.run_worker(self._capture_template(kind), group="capture", exclusive=True)

    @on(Button.Pressed, f".{CLEAR_CLASS}")
    def _on_clear_pressed(self, event: Button.Pressed) -> None:
        """Wipe one appearance's whole stack, immediately and without a confirm.

        The mirror of the capture buttons - same generated block, same
        parsed-out kind - and deliberately NOT the shape of "Forget
        appearance", which loses a whole service's calibration and therefore
        asks first. One kind is one capture button away from being back, and a
        Clear that opened a dialog would cost more attention than the mistake
        it guards against. It writes to disk on the press for the same reason a
        capture does: the store is the working copy.
        """
        event.stop()
        kind = kind_from_clear_button_id(event.button.id)
        key = self._selected_key
        if kind is None or key is None or self._capturing:
            return
        try:
            drop_template(self._profile_root, key, kind)
        except ProfileStoreError as exc:
            self.notify(f"could not clear the {kind.label}: {exc}", severity="error")
            return
        self._profiles_changed = True
        self._show_appearance(self._profile(key))
        self.notify(f"{kind.label} cleared for {key}")

    async def _capture_template(self, kind: TemplateKind) -> None:
        """Draw a box around ``kind`` and ADD the pixels to it, under the SERVICE.

        Added rather than substituted: a control can be drawn several ways (the
        send button greys out while a file uploads) and all of a kind's images
        are searched for, so a second capture is a second way to recognise the
        same thing. "Clear" is the only thing that takes images away.

        The same overlay the chat region uses, run as a child process; the
        prompt comes from the kind (screen.profile) rather than from here,
        because what makes a good capture is a fact about the appearance and
        must read identically wherever it is asked for.

        The write is immediate, like "Forget appearance"'s delete: the editor
        holds no profile of its own to hand back on close, so the store IS the
        working copy and ``_profiles_changed`` is what tells the caller to drop
        its cache. A save failure therefore files nothing at all - unlike the
        old main-screen path there is no in-memory template left to be useful.

        The overlay guard is held for the WHOLE method, not just the pick: the
        bookkeeping after it runs in an exclusive worker, so a second capture
        press mid-save would cancel this one halfway. Held, that press is
        refused instead - and so is escape.
        """
        try:
            key = self._selected_key
            if key is None:
                return
            try:
                region = await asyncio.to_thread(pick_region, prompt=kind.prompt)
            except ScreenPickError as exc:
                self.notify(str(exc), severity="error")
                return
            if region is None:
                self.notify(f"{kind.label} unchanged (selection cancelled)")
                return
            try:
                image = await asyncio.to_thread(capture_region, region)
            except CaptureError as exc:
                self.notify(f"could not capture the {kind.label}: {exc}", severity="error")
                return
            # Anchoring it here is the searchability check: a box narrower than
            # one anchor can never be matched back, so filing it would only
            # produce a template that finds nothing.
            probe = ServiceProfile(key)
            try:
                probe.put(kind, image)
            except ValueError as exc:
                self.notify(f"that {kind.label} cannot be searched for: {exc}", severity="error")
                return
            try:
                await asyncio.to_thread(save_template, self._profile_root, key, kind, image)
            except ProfileStoreError as exc:
                self.notify(f"could not save the {kind.label}: {exc}", severity="error")
                return
            self._profiles_changed = True
            self._show_appearance(self._profile(key))
            self.notify(f"{kind.label} captured for {key} ({region.describe()})")
        finally:
            self._capturing = False

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
        if key is None or self._capturing:
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(
                f"Forget the {key} appearance?",
                "The captured images of this service's buttons and chat box will be "
                "deleted from disk. Its size settings are untouched, but every one "
                "of those images has to be captured again.",
            )
        )
        if not confirmed:
            return
        # A profile we cannot delete simply reads as one that is still there:
        # the readout is re-derived from disk either way, and only a deletion
        # that really happened is worth telling the caller about.
        try:
            delete_profile(self._profile_root, key)
        except ProfileStoreError:
            pass
        else:
            self._profiles_changed = True
        self._show_appearance(self._profile(key))

    @on(Button.Pressed, "#svc-delete-btn")
    def _on_delete(self, event: Button.Pressed) -> None:
        event.stop()
        key = self._selected_key
        if key is None or key in BUILTIN_SERVICE_KEYS or key not in self._services:
            return
        del self._services[key]
        # The captures go with it. Nothing in the app can reach a profile whose
        # service key is gone - it is in no picker, so it can neither be
        # selected, searched for, nor forgotten - so leaving the folder behind
        # is leaving a pile of PNGs no user can ever act on again.
        try:
            delete_profile(self._profile_root, key)
        except ProfileStoreError:
            pass
        else:
            self._profiles_changed = True
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
        if self._capturing:
            # Escape belongs to the overlay right now, and closing the editor
            # out from under an in-flight capture would strand the worker that
            # still has to write the PNG.
            self.notify("a region picker is open - finish it or cancel it first")
            return
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
        if not changed and not self._profiles_changed:
            self.dismiss(None)  # nothing happened here at all
            return
        self.dismiss(ServiceEdits(self._services if changed else None, self._profiles_changed))
