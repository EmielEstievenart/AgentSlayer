"""The service editor's MODEL, with no window, no page and no toolkit in it.

The GUI's answer to ``shell/tui/screens/service_editor.py`` (1302 lines of Textual
widgets wrapped around ~300 lines of decisions). Everything in that screen that
is a DECISION - the working copy, what validates, what applies live and what
waits for a discrete press, which of the three footer buttons is showing, what a
capture writes and when, and what one visit hands back - lives here, in one
object a test can drive with no DOM and no subprocess. What is left for
``gui/view.py`` and ``app.js`` is the two things a model cannot do: schedule the
capture coroutine onto the GUI's loop, and draw.

**The commit model is the TUI's, exactly** (``ui-briefs/service-editor.md`` §3.2):

* editing an EXISTING preset applies live - every change revalidates the whole
  candidate (``max <= total`` is a cross-field rule, so there is no such thing
  as validating one field) and, only while it is fully valid, writes
  ``replace(...)`` into the working copy. An invalid candidate is never
  committed; the working copy keeps its last-valid values and :attr:`error`
  says why;
* creating a NEW preset commits nothing until :meth:`add` is pressed, because a
  key is immutable once created and a half-typed one would file a service under
  it;
* the toggles, radios and the tolerance slider write straight through with no
  validation gate at all - none of them can express an illegal value - except
  in "+ add new" mode, where there is no key to file anything under and the
  page disables them while still SHOWING what the press would create.

**Captures are the exception to "applies on close"**: the profile store IS the
working copy for appearances, so a capture writes its PNG and a clear/forget
deletes immediately, and :attr:`profiles_changed` is how the caller is told
(it cannot diff for something that already happened on disk).

The three OS-facing functions (:func:`pick_region`, :func:`capture_region`,
:func:`save_template` and friends) are imported at module scope and called by
name, so ``tests/shell/gui`` monkeypatches this module and never opens an overlay.
"""

from __future__ import annotations

import asyncio
import base64
import re
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentclip.config import (
    BUILTIN_SERVICE_KEYS,
    DEFAULT_STABLE_SECONDS,
    DELIVERY_PASTE,
    DELIVERY_STREAM,
    FINISH_SIGNALS,
    MATCHER_ANCHORS,
    MATCHER_OPENCV,
    MATCHERS,
    SCROLL_ACTIONS,
    SCROLL_END,
    SCROLL_PAGE_DOWN,
    SCROLL_WHEEL,
    TOLERANCE_MAX,
    TOLERANCE_MIN,
    Config,
    ServicePreset,
    default_services,
    normalize_finish_signals,
)
from agentclip.driver.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.driver.screen.matchers import opencv_available
from agentclip.driver.screen.picker import ScreenPickError, pick_region
from agentclip.driver.screen.png import PngError, encode_png
from agentclip.driver.screen.profile import (
    DEFAULT_CLICK_PERCENT,
    ServiceProfile,
    TemplateKind,
    clamp_percent,
)
from agentclip.driver.screen.profile_store import (
    ProfileStoreError,
    delete_profile,
    drop_variant,
    load_profile,
    save_click_point,
    save_template,
)

# The picker's "+ Add new service..." row. Not a legal slug (it contains '+'),
# so it can never collide with a real key - the TUI's sentinel, kept verbatim
# because it is now a WIRE value: the page's <select> carries it back.
NEW_SENTINEL = "+add-new+"

# What "+ Add new" is going to create for every field the form does not ask
# about. The toggles read from THIS rather than from literals, so a form that
# shows "screen stops changing" unticked over a preset born with it ticked is
# not a shape this can take (``service_editor.py:135-144``).
_NEW_PRESET_DEFAULTS = ServicePreset(key="", label="", max_paste_chars=1, total_context_chars=1)
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# The stale-detector's stillness bounds, which are ``config.py``'s loader's
# (``_take_float(..., 0.5, 60.0)``): a value this editor accepts must never be
# silently rewritten the next time the app starts.
STABLE_MIN = 0.5
STABLE_MAX = 60.0

# == the words ================================================================
# ``shell/tui/screens/service_editor.py``'s display strings, spelled again here for
# the reason every literal in ``gui/view.py`` is: the two shells may not import
# each other (tests/test_layering.py). Where the TUI's text names a Textual
# affordance it is re-worded for this shell and said so at the constant.

TEMPLATES_NONE = "appearance: nothing captured yet"
TEMPLATE_UNSET = "not captured"

# The finish-signal checklist, in user words rather than detector names: the
# TOML keys describe how the detector works, these describe what the user will
# SEE when it fires, which is the only thing they can check against their own
# chat window.
SIGNAL_LABELS: dict[str, str] = {
    "busy": "reasoning icon disappears",
    "idle": "send icon appears",
    "stale": "screen stops changing",
}
# Which appearance a ticked signal additionally needs. "stale" needs none - a
# drawn chat region is a finish detector all by itself.
SIGNAL_TEMPLATE: dict[str, TemplateKind] = {
    "busy": TemplateKind.BUSY,
    "idle": TemplateKind.IDLE,
}
SIGNAL_UNCAPTURED = "ticked but not captured — it will be skipped"

# The two click-point boxes on every appearance card. Titles rather than
# visible labels - the line beside them has room for "click %" and no more - so
# the sentence has room to say what the number means and what 50 does. Two
# identical-looking number boxes side by side are told apart by nothing but
# their order, so each names its AXIS first and the edge it counts from second:
# "across" alone still leaves the reader guessing which box they are hovering.
CLICK_X_LABEL = "horizontal click point: % of the image width, from the left (50 = the middle)"
CLICK_Y_LABEL = "vertical click point: % of the image height, from the top (50 = the middle)"

HOVER_SCAN_LABEL = "hover-scan for copy icon"
REQUIRE_FENCED_LABEL = "require fenced replies"
STREAM_DELIVERY_LABEL = "paste the payload in chunks"
AUTO_SUBMIT_LABEL = "press Enter after auto-paste"
# The ranged-edit mode. Worded as what the model gets, not as the tool's name,
# which means nothing to anyone who has not turned it on yet.
EDIT_BY_LINES_LABEL = "edit files by line number"

MATCHER_LABELS: dict[str, str] = {
    MATCHER_ANCHORS: "Anchors (built-in)",
    MATCHER_OPENCV: "OpenCV (exhaustive)",
}
SCROLL_LABELS: dict[str, str] = {
    SCROLL_WHEEL: "mouse wheel flick",
    SCROLL_PAGE_DOWN: "Page Down taps",
    SCROLL_END: "End key",
}
TOLERANCE_LABEL = "Pixel tolerance"

# Two messages, because the two audiences can do two different things about it:
# from source OpenCV is one `pip install` away and naming the extra IS the fix;
# inside the frozen exe there is no environment to install into, so the pip line
# would send somebody to a command that cannot help them.
OPENCV_MISSING_SOURCE = (
    "OpenCV is not installed — anchors will be used. Install it with "
    "pip install agentclip[cv]"
)
OPENCV_MISSING_FROZEN = (
    "This build does not include OpenCV — anchors will be used. "
    "Nothing to install: it has to be built in."
)

# The footer hint. The TUI says "escape closes"; so does this shell (Esc is the
# editor's close everywhere), so the sentence carries over whole.
FOOTER_HINT = "escape closes (applies valid edits) · built-ins: edit or reset, never delete"

FORGET_TITLE = "Forget the {key} appearance?"
FORGET_BODY = (
    "The captured images of this service's buttons and chat box will be deleted from "
    "disk. Its size settings are untouched, but every one of those images has to be "
    "captured again."
)
DISCARD_TITLE = "Discard the pending edit?"
DISCARD_BODY = (
    "The current field values are invalid ({error}) and were never applied. "
    "Close the service editor anyway?"
)

CAPTURE_BUSY = "a region picker is already open - finish it or press Esc first"
CLOSE_BUSY = "a region picker is open - finish it or cancel it first"


def opencv_missing_note(*, frozen: bool | None = None) -> str:
    """Which "you will not get OpenCV" line this build should show.

    ``frozen`` is the injection seam for tests; left None it asks the
    interpreter, which is the only thing that actually knows - PyInstaller sets
    ``sys.frozen`` on the bootloaded interpreter and nothing else does.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    return OPENCV_MISSING_FROZEN if frozen else OPENCV_MISSING_SOURCE


def png_data_uri(image: RegionImage) -> str:
    """One captured region as a ``data:`` URI an ``<img>`` can be pointed at.

    ``driver.screen.png.encode_png`` is the whole conversion, and it is the reason this
    shell needs no Pillow: it reads a capture as BGRX and writes the undefined
    fourth byte as OPAQUE alpha rather than as transparency - read as alpha that
    byte is zero and every crop encodes as an invisible rectangle. ``""`` for
    anything that cannot be encoded, so a thumbnail that failed is a blank box
    and never a broken row.
    """
    try:
        return "data:image/png;base64," + base64.b64encode(encode_png(image)).decode("ascii")
    except PngError:
        return ""


def template_status(profile: ServiceProfile | None, kind: TemplateKind, shown: int = 0) -> str:
    """One appearance's status line: what is captured for it, or the unset default.

    A kind holds a STACK of images, all of them searched for, so the count
    belongs in the readout: a second capture ADDS, and a line that kept saying
    "captured" would leave the user believing it had replaced. Above one image
    the count becomes a POSITION ("2/3"), because the row's thumbnail is one
    variant of several and a line that named only how many there were would
    leave the user unable to say which one they are looking at - or that the
    arrows beside it had moved anything.

    The dimensions are the SHOWN variant's, for the same reason: they describe
    the picture on screen, and variants of one control are routinely different
    sizes (a send button with a file chip beside it is not the bare one).
    """
    templates = () if profile is None else profile.variants(kind)
    if not templates:
        return TEMPLATE_UNSET
    index = min(max(shown, 0), len(templates) - 1)
    template = templates[index]
    if len(templates) == 1:
        return f"{template.width}×{template.height} · captured"
    return f"{template.width}×{template.height} · {index + 1}/{len(templates)}"


def templates_line(profile: ServiceProfile | None) -> str:
    """The one-line summary of what this service LOOKS like - the count only."""
    if profile is None or not profile.captured:
        return TEMPLATES_NONE
    return f"appearance: {profile.describe()}"


def signal_warning(preset: ServicePreset | None, profile: ServiceProfile | None) -> str:
    """The inline "you ticked a detector that cannot run" line, or "".

    A ticked busy/idle whose appearance was never captured runs NOTHING - the
    checklist and the profile are ANDed - and silent dead weight is exactly the
    failure that shows up later as an auto-copy that never fires.
    """
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


def select_options(services: dict[str, ServicePreset]) -> list[dict[str, Any]]:
    """The picker's rows: every key alphabetically, then the "+ add new" row."""
    rows: list[dict[str, Any]] = [
        {
            "key": key,
            "label": f"{key} ({'builtin' if key in BUILTIN_SERVICE_KEYS else 'custom'})",
            "builtin": key in BUILTIN_SERVICE_KEYS,
        }
        for key in sorted(services)
    ]
    rows.append({"key": NEW_SENTINEL, "label": "+ Add new service...", "builtin": False})
    return rows


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


@dataclass(frozen=True, slots=True)
class CloseResult:
    """The answer to "may I close, and what do I owe the caller".

    Two independent noes are possible and they are not the same: a capture in
    flight refuses outright (``closed=False``, a toast already said why), and a
    declined discard-confirm also stays open - with the invalid text intact, so
    the user can fix it. ``closed=True`` with ``edits=None`` is the third state,
    "nothing happened here at all", and the caller does nothing.
    """

    closed: bool
    edits: ServiceEdits | None


Notify = Callable[[str, str], None]
Confirm = Callable[[str, str], Awaitable[bool]]


def _always_yes(title: str, body: str) -> Awaitable[bool]:
    """The confirm a model nobody wired a dialog into gets."""

    async def yes() -> bool:
        return True

    return yes()


def _no_notify(message: str, severity: str) -> None:
    """The toast sink a model nobody wired a page into gets."""


class ServiceEditor:
    """The F2 editor's whole state, as one object with no window behind it.

    Constructed per visit (like the TUI's ``ModalScreen``), driven by the
    ``svc_*`` ``js_api`` methods, and read back through :meth:`state`, which is
    the entire event the page renders from.
    """

    def __init__(
        self,
        config: Config,
        profile_root: Path,
        initial_key: str | None = None,
        *,
        notify: Notify | None = None,
        confirm: Confirm | None = None,
        opencv: bool | None = None,
        frozen: bool | None = None,
    ) -> None:
        self._profile_root = profile_root
        self._services: dict[str, ServicePreset] = dict(config.services)
        self._initial_services: dict[str, ServicePreset] = dict(config.services)
        self._notify = notify if notify is not None else _no_notify
        self._confirm = confirm if confirm is not None else _always_yes
        # Probed ONCE per visit, like the TUI's: the verdict cannot change while
        # the process runs, and asking costs a real import of a ~60MB wheel that
        # has no business happening on every keystroke in the form. Resolved
        # beside it, for the same reason, is which of the two "no OpenCV"
        # sentences this install should show.
        self._opencv = opencv_available() if opencv is None else opencv
        self._opencv_note = opencv_missing_note(frozen=frozen)
        # Set the moment a profile folder is written or removed on disk. Not
        # derivable on close (it already happened), and the caller has to hear
        # about it: it caches profiles, paints them, and hunts for the templates
        # it thinks are there.
        self._profiles_changed = False
        # One overlay at a time. Claimed synchronously by ``start_capture``
        # BEFORE the coroutine that does the picking is scheduled, because two
        # presses marshal onto the loop as two callbacks and the second must be
        # refused rather than raced (the external child process an in-app cancel
        # cannot kill - brief §3.3).
        self._capturing = False
        default_key = (
            initial_key
            if initial_key in self._services
            else config.general.service
            if config.general.service in self._services
            else next(iter(sorted(self._services)))
        )
        self._selected_key: str | None = default_key
        self._pending_new: ServicePreset | None = None
        self._error: str | None = None
        self._form: dict[str, str] = {}
        self._profile: ServiceProfile | None = None
        self._thumbs: dict[TemplateKind, str] = {}
        # Which variant of each kind the row is showing. A kind is a STACK and
        # the row has space for one picture, so WHICH one is a piece of state,
        # and it lives here rather than on the page: the page renders
        # :meth:`state` and owns nothing the model does not know. Reconciled
        # against the folder in ``_show_appearance``, so a stale index (a clear,
        # a re-read, a different service) clamps instead of pointing past the
        # end of a stack that shrank under it.
        self._shown: dict[TemplateKind, int] = {}
        # True on the state push that FOLLOWS a form reload (a selection change,
        # an add, a reset, a delete) and false on every push a live edit caused.
        # The page rewrites its inputs only on the first kind: repainting a text
        # box from the model on every keystroke would fight the caret.
        self._reload = True
        self._load(default_key)

    # == reading ===============================================================

    @property
    def selected_key(self) -> str | None:
        """The key being edited, or None while "+ Add new" is selected."""
        return self._selected_key

    @property
    def error(self) -> str:
        """Why the current candidate is not being applied, or ""."""
        return self._error or ""

    @property
    def capturing(self) -> bool:
        return self._capturing

    @property
    def profiles_changed(self) -> bool:
        return self._profiles_changed

    @property
    def services(self) -> dict[str, ServicePreset]:
        """The working copy. Read-only by convention; the caller gets its own
        dict out of :meth:`close`."""
        return self._services

    @property
    def dirty(self) -> bool:
        """Has the presets table moved at all since the editor opened?"""
        return self._services != self._initial_services

    def state(self) -> dict[str, Any]:
        """The whole editor as one event payload - what the page renders from.

        One event rather than a dozen partial writes for ``_push_sidebar``'s
        reason: these readouts are repainted by the same few moments (a
        selection, a keystroke, a capture landing) and a page that reassembles a
        modal out of partial writes has that many ways to be half-painted.
        """
        is_new = self._selected_key is None
        shown = self._shown_preset()
        matcher_warning = (
            self._opencv_note if shown.matcher == MATCHER_OPENCV and not self._opencv else ""
        )
        return {
            "open": True,
            "reload": self._reload,
            "services": select_options(self._services),
            "selected": self._selected_key if self._selected_key is not None else NEW_SENTINEL,
            "is_new": is_new,
            "form": dict(self._form),
            # The key is immutable once created (brief §6): the input is shown
            # either way, disabled for anything that already exists.
            "key_locked": not is_new,
            "error": self.error,
            "signals": [
                {"name": name, "label": SIGNAL_LABELS[name], "on": name in shown.finish_signals}
                for name in FINISH_SIGNALS
            ],
            # The five stand-alone ticks and the slider's caption, worded here
            # rather than in the page for the reason the checklist is: every one
            # of them says what the user will SEE happen in their own chat
            # window, and two shells with two wordings of that is drift.
            "labels": {
                "hover_scan": HOVER_SCAN_LABEL,
                "require_fenced": REQUIRE_FENCED_LABEL,
                "stream": STREAM_DELIVERY_LABEL,
                "auto_submit": AUTO_SUBMIT_LABEL,
                "edit_by_lines": EDIT_BY_LINES_LABEL,
                "tolerance": TOLERANCE_LABEL,
            },
            "hover_scan": shown.hover_scan,
            "require_fenced": shown.require_fenced_reply,
            "stream": shown.delivery == DELIVERY_STREAM,
            "auto_submit": shown.auto_submit,
            "edit_by_lines": shown.edit_by_lines,
            "scroll": shown.scroll_action,
            "scrolls": [{"value": name, "label": SCROLL_LABELS[name]} for name in SCROLL_ACTIONS],
            "matcher": shown.matcher,
            "matchers": [{"value": name, "label": MATCHER_LABELS[name]} for name in MATCHERS],
            "matcher_warning": matcher_warning,
            "tolerance": shown.tolerance,
            "tolerance_min": TOLERANCE_MIN,
            "tolerance_max": TOLERANCE_MAX,
            "signal_warning": signal_warning(
                self._services.get(self._selected_key) if self._selected_key else None,
                self._profile,
            ),
            # Not hidden - disabled - so the layout cannot reflow while the user
            # fills the form in (brief §3.5).
            "controls_disabled": is_new,
            "can_add": self._pending_new is not None,
            "show_add": is_new,
            "show_reset": not is_new and self._selected_key in BUILTIN_SERVICE_KEYS,
            "show_delete": not is_new and self._selected_key not in BUILTIN_SERVICE_KEYS,
            "show_forget": self._profile is not None and bool(self._profile.captured),
            "templates": templates_line(self._profile),
            # ``shown``/``count`` are the stack the row is looking into: which
            # variant its thumbnail and its dimensions are of, and how many
            # there are to walk. The page needs both - it disables the arrows
            # below two rather than hiding them, for the reason Clear is
            # disabled rather than hidden (brief §3.6).
            "kinds": [
                {
                    "kind": str(kind),
                    "label": kind.label.capitalize(),
                    "status": template_status(self._profile, kind, self._shown.get(kind, 0)),
                    "png": self._thumbs.get(kind, ""),
                    "shown": self._shown.get(kind, 0),
                    "count": len(self._profile.variants(kind)) if self._profile else 0,
                    "can_clear": not is_new
                    and self._profile is not None
                    and self._profile.has(kind),
                    # Where inside the matched picture this kind is clicked, as
                    # percentages: 50/50 - the middle - for every kind nobody
                    # has moved, which is where all seven were clicked before
                    # the point was adjustable at all.
                    "click_x": self._click_point(kind)[0],
                    "click_y": self._click_point(kind)[1],
                }
                for kind in TemplateKind
            ],
            "click_labels": {"x": CLICK_X_LABEL, "y": CLICK_Y_LABEL},
            "capturing": self._capturing,
            "hint": FOOTER_HINT,
        }

    # == selection =============================================================

    def select(self, key: str) -> None:
        """Show a different service - or the "+ Add new" row."""
        target = None if key == NEW_SENTINEL else key
        if target is not None and target not in self._services:
            return
        self._load(target)

    def _load(self, key: str | None) -> None:
        self._selected_key = key
        if key is None:
            self._form = {
                "key": "",
                "label": "",
                "max": "",
                "total": "",
                # Pre-filled, unlike the sizes: the stale window has a sensible
                # universal default, and "add a service" should stay a
                # four-field job for anyone who never touches the detector.
                "stable": str(DEFAULT_STABLE_SECONDS),
                "extra": "",
            }
        else:
            preset = self._services[key]
            self._form = {
                "key": preset.key,
                "label": preset.label,
                "max": str(preset.max_paste_chars),
                "total": str(preset.total_context_chars),
                "stable": str(preset.stable_seconds),
                "extra": preset.extra_instructions,
            }
        self._pending_new = None
        self._reload = True
        self._show_appearance()
        self._revalidate()

    def _shown_preset(self) -> ServicePreset:
        """What the toggles/radios/slider currently DISPLAY.

        For "+ Add new" that is the dataclass defaults, not blanks: an
        all-unticked form over a preset born with "screen stops changing" ticked
        is a lie about the only setting the user cannot see anywhere else
        (brief §3.5).
        """
        key = self._selected_key
        if key is None or key not in self._services:
            return _NEW_PRESET_DEFAULTS
        return self._services[key]

    def _show_appearance(self, *, newest: TemplateKind | None = None) -> None:
        """Re-derive everything that is a view of this service's profile folder.

        The TUI's ``_show_appearance``: read the folder ONCE and repaint the
        seven thumbnails, the seven status lines, the summary, whether there is
        anything to forget, and whether a ticked signal has an appearance to run
        against. The thumbnails are encoded HERE rather than in :meth:`state` so
        a keystroke in the form does not re-encode seven PNGs.

        This is also where the per-kind shown-variant index is reconciled, and
        the only place it can be: the folder is the truth and it moves under the
        editor (a clear, a capture, a forget, another service selected), so the
        index is clamped to whatever is really there rather than trusted. Kinds
        with nothing captured keep no index at all - swapping the whole dict is
        what stops a stack that emptied and refilled from resuming at a position
        that meant something about a picture that is gone. ``newest`` is the
        kind a capture has just landed in, whose LAST variant is the one the
        user drew and therefore the one to show.
        """
        key = self._selected_key
        self._profile = None if key is None else load_profile(self._profile_root, key)
        thumbs: dict[TemplateKind, str] = {}
        shown: dict[TemplateKind, int] = {}
        if self._profile is not None:
            for kind in TemplateKind:
                variants = self._profile.variants(kind)
                if not variants:
                    continue
                wanted = len(variants) - 1 if kind is newest else self._shown.get(kind, 0)
                index = min(max(wanted, 0), len(variants) - 1)
                shown[kind] = index
                # ONE of the stack, not all of them: a kind's variants are
                # pictures of the same control, the row has space for one, and
                # the arrows beside it are how the others are reached.
                thumbs[kind] = png_data_uri(variants[index].image)
        self._thumbs = thumbs
        self._shown = shown

    # == the form ==============================================================

    def set_form(self, fields: dict[str, str]) -> None:
        """One change in the form column: revalidate the WHOLE candidate.

        The page sends every field on any change, deliberately - the TUI's
        detection handler reads its whole group for the same reason (never trust
        which control fired), and here it is forced anyway: ``max <= total`` is
        a cross-field rule, so a per-field validator could not exist.
        """
        for name in ("key", "label", "max", "total", "stable", "extra"):
            if name in fields:
                self._form[name] = str(fields[name])
        self._reload = False
        self._revalidate()

    def _revalidate(self) -> None:  # noqa: PLR0912 - straight-line field validation
        """The TUI's ``_revalidate``, verbatim in behaviour and in wording.

        One function walking the fields in order, stopping at the first problem,
        and - only when there is none - writing the candidate through. Editing
        an existing preset applies live; the "+ Add new" candidate is held in
        ``_pending_new`` until the discrete press.
        """
        is_new = self._selected_key is None
        key_text = self._form.get("key", "").strip()
        label_text = self._form.get("label", "").strip()
        max_text = self._form.get("max", "").strip()
        total_text = self._form.get("total", "").strip()
        stable_text = self._form.get("stable", "").strip()
        # Stripped as the TUI strips it: the box holds newlines, and a trailing
        # one from a stray Enter is not guidance the model needs shipped.
        extra_text = self._form.get("extra", "").strip()

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
                if not (STABLE_MIN <= stable_val <= STABLE_MAX):
                    error = "stale seconds must be between 0.5 and 60"

        self._error = error
        if error is not None:
            self._pending_new = None
            return

        assert (
            key is not None
            and max_val is not None
            and total_val is not None
            and stable_val is not None
        )
        if is_new:
            self._pending_new = ServicePreset(
                key=key,
                label=label_text,
                max_paste_chars=max_val,
                total_context_chars=total_val,
                stable_seconds=stable_val,
                extra_instructions=extra_text,
            )
        else:
            self._services[key] = replace(
                self._services[key],
                label=label_text,
                max_paste_chars=max_val,
                total_context_chars=total_val,
                stable_seconds=stable_val,
                extra_instructions=extra_text,
            )

    # == the toggles, the radios and the slider ================================

    def set_detection(
        self,
        *,
        signals: Iterable[str],
        hover_scan: bool,
        require_fenced: bool,
        stream: bool,
        auto_submit: bool,
    ) -> None:
        """Fold every toggle on the left column back into the preset, live.

        Read as a SET rather than per-box, exactly as the TUI does: the shape
        the framework note there is about ("read the whole group on any single
        change, never trust which control fired") is worth carrying, and
        ``finish_signals`` is a checklist in one canonical order anyway.
        No validation gate - none of these can express an illegal value.
        """
        key = self._selected_key
        self._reload = False
        if key is None or key not in self._services:
            return
        self._services[key] = replace(
            self._services[key],
            finish_signals=normalize_finish_signals(signals),
            hover_scan=bool(hover_scan),
            delivery=DELIVERY_STREAM if stream else DELIVERY_PASTE,
            require_fenced_reply=bool(require_fenced),
            auto_submit=bool(auto_submit),
        )

    def set_edit_by_lines(self, on: bool) -> None:
        """Does this service get replace_lines and a numbered read_file?

        Its own setter rather than a member of ``set_detection``: that method
        folds the LEFT column's toggles back as one set because they describe
        one thing (how a finished reply is recognised and delivered), and this
        describes the model's tool catalog. Writes only that field.
        """
        key = self._selected_key
        self._reload = False
        if key is None or key not in self._services:
            return
        self._services[key] = replace(self._services[key], edit_by_lines=bool(on))

    def set_scroll(self, action: str) -> None:
        """How the auto-copy flow reaches the newest reply. Writes only that field."""
        key = self._selected_key
        self._reload = False
        if action not in SCROLL_ACTIONS or key is None or key not in self._services:
            return
        self._services[key] = replace(self._services[key], scroll_action=action)

    def set_matcher(self, matcher: str) -> None:
        """Which backend hunts this service's appearances.

        Saved even on a machine without OpenCV - the user may be configuring a
        machine they are about to install it on - which is precisely why the
        fallback warning has to be visible the moment it is chosen (brief §6).
        """
        key = self._selected_key
        self._reload = False
        if matcher not in MATCHERS:
            return
        if key is None or key not in self._services:
            return  # the warning is re-derived from ``_shown_preset`` either way
        self._services[key] = replace(self._services[key], matcher=matcher)

    def _click_point(self, kind: TemplateKind) -> tuple[int, int]:
        """Where ``kind``'s click lands, or the centre with no profile loaded."""
        if self._profile is None:
            return (DEFAULT_CLICK_PERCENT, DEFAULT_CLICK_PERCENT)
        return self._profile.click_point(kind)

    def set_click_point(self, kind: TemplateKind, x: object, y: object) -> None:
        """Aim one appearance's click at x%/y% of the picture that matches it.

        A CAPTURE-side setting, so it takes the capture side's commit model
        (module docstring): the profile folder is the working copy, this writes
        the manifest immediately, and :attr:`profiles_changed` is how the caller
        hears about it - there is nothing to hand back on close.

        Clamped rather than refused (``clamp_percent``): the page sends whatever
        is in a number box, an empty one included, and the boxes are repainted
        from the model on the next reload - so the worst a stray keystroke can
        do is aim at an edge, visibly.
        """
        key = self._selected_key
        self._reload = False
        if key is None or self._capturing:
            return
        point = (clamp_percent(x), clamp_percent(y))
        if self._profile is not None and self._profile.click_point(kind) == point:
            return  # the page echoing back what it was painted with
        try:
            save_click_point(self._profile_root, key, kind, *point)
        except ProfileStoreError as exc:
            self._notify(f"could not save the {kind.label} click point: {exc}", "error")
            return
        # Straight into the loaded profile rather than through a reload: the
        # only thing that moved is two numbers, and re-reading the folder would
        # re-encode seven thumbnails per keystroke.
        if self._profile is not None:
            self._profile.set_click_point(kind, *point)
        self._profiles_changed = True

    def set_tolerance(self, value: int) -> None:
        """Per-channel pixel slack, 0-64. No validation gate, by construction:
        the control cannot express a value outside the range ``config.py``
        itself enforces, so nothing it produces is ever invalid."""
        key = self._selected_key
        self._reload = False
        if key is None or key not in self._services:
            return
        try:
            clamped = int(value)
        except (TypeError, ValueError):
            return
        clamped = max(TOLERANCE_MIN, min(TOLERANCE_MAX, clamped))
        self._services[key] = replace(self._services[key], tolerance=clamped)

    # == add / reset / delete ==================================================

    def add(self) -> None:
        """Commit the pending "+ Add new" candidate under its new key.

        The one discrete action in the whole editor: keys are immutable once
        created, so committing them continuously would file services under
        half-typed slugs.
        """
        preset = self._pending_new
        if preset is None:
            return
        self._services[preset.key] = preset
        self._pending_new = None
        self._load(preset.key)
        self._notify(f"{preset.key} added", "information")

    def reset(self) -> None:
        """Restore a built-in's shipped values. Touches no captures at all."""
        key = self._selected_key
        if key is None or key not in BUILTIN_SERVICE_KEYS:
            return
        self._services[key] = default_services()[key]
        self._load(key)
        self._notify(f"{key} reset to its shipped defaults", "information")

    def delete(self) -> None:
        """Remove a custom key - and the captures behind it.

        Nothing in the app can reach a profile whose service key is gone (it is
        in no picker, so it can neither be selected, searched for nor
        forgotten), so leaving the folder behind is leaving a pile of PNGs no
        user can ever act on again. A delete failure is swallowed: the readout
        is re-derived from disk either way.
        """
        key = self._selected_key
        if key is None or key in BUILTIN_SERVICE_KEYS or key not in self._services:
            return
        del self._services[key]
        try:
            delete_profile(self._profile_root, key)
        except ProfileStoreError:
            pass
        else:
            self._profiles_changed = True
        self._load(next(iter(sorted(self._services))))
        self._notify(f"{key} deleted", "information")

    # == appearances ===========================================================

    def show_previous(self, kind: TemplateKind) -> None:
        """Step one row's thumbnail back through the stack, wrapping at the start."""
        self._step_shown(kind, -1)

    def show_next(self, kind: TemplateKind) -> None:
        """Step one row's thumbnail on through the stack, wrapping at the end."""
        self._step_shown(kind, 1)

    def _step_shown(self, kind: TemplateKind, step: int) -> None:
        """Move which variant of ``kind`` is on show. Wraps, and re-encodes one PNG.

        Wrapping rather than stopping at the ends because the stack is a ring of
        two or three pictures of one control, not a document: there is no "first"
        variant worth landing on, and an arrow that goes dead is a button the
        user has to look at to use.

        Nothing is re-read from disk - only this editor writes there, and it
        re-reads on every write - so one press costs one PNG encode rather than
        a folder walk and seven.
        """
        variants = self._profile.variants(kind) if self._profile is not None else ()
        if len(variants) < 2:
            return  # nothing to cycle: the page keeps the arrows disabled anyway
        index = (self._shown.get(kind, 0) + step) % len(variants)
        self._shown[kind] = index
        self._thumbs[kind] = png_data_uri(variants[index].image)
        self._reload = False

    def clear(self, kind: TemplateKind) -> None:
        """Drop the variant this row is SHOWING, immediately and with no confirm.

        One picture, not the kind: a stack is how a control drawn several ways
        is recognised in all of them, and a user who captured a bad third
        variant wants that one gone, not the two good ones with it. "Forget
        appearance" is still there for the whole-service case.

        No dialog, deliberately - it is one capture press away from being back,
        and a confirm here would cost more attention than the mistake it guards
        against. The row does not follow the picture it dropped: the index stays
        put, so what slid into the slot is what is on show, and
        ``_show_appearance``'s clamp is what turns clearing the LAST variant
        into showing the new last one instead of a hole.
        """
        key = self._selected_key
        if key is None or self._capturing:
            return
        try:
            drop_variant(self._profile_root, key, kind, self._shown.get(kind, 0))
        except ProfileStoreError as exc:
            self._notify(f"could not clear the {kind.label}: {exc}", "error")
            return
        self._profiles_changed = True
        self._reload = False
        self._show_appearance()
        self._notify(f"{kind.label} cleared for {key}", "information")

    async def forget(self) -> None:
        """Delete this service's whole profile folder, behind a confirmation.

        Separate from "Delete", which removes the PRESET: a user whose browser
        theme changed wants to recapture, not to lose their size settings.
        """
        key = self._selected_key
        if key is None or self._capturing:
            return
        if not await self._confirm(FORGET_TITLE.format(key=key), FORGET_BODY):
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
        self._reload = False
        self._show_appearance()

    def start_capture(self, kind: TemplateKind) -> bool:
        """Claim the one overlay slot. False (and a toast) when it is taken.

        Synchronous and separate from :meth:`run_capture` on purpose: the
        capture body is a coroutine the shell schedules, and two presses that
        both marshalled onto the loop before either task ran would both see
        ``capturing`` false. The claim has to happen in the press handler.
        """
        if self._selected_key is None:
            return False
        if self._capturing:
            self._notify(CAPTURE_BUSY, "warning")
            return False
        self._capturing = True
        self._reload = False
        return True

    async def run_capture(self, kind: TemplateKind) -> None:
        """Draw a box around ``kind`` and ADD the pixels to it, under the service.

        Added rather than substituted: a control can be drawn several ways (the
        send button greys out while a file uploads) and all of a kind's images
        are searched for, so a second capture is a second way to recognise the
        same thing.

        The write is immediate, like "Forget appearance"'s delete: the editor
        holds no profile of its own to hand back on close, so the store IS the
        working copy and :attr:`profiles_changed` is what tells the caller to
        drop its cache. The overlay guard is held for the WHOLE method, not just
        the pick, so a second press mid-save is refused rather than racing the
        write.
        """
        try:
            key = self._selected_key
            if key is None:
                return
            try:
                region = await asyncio.to_thread(pick_region, prompt=kind.prompt)
            except ScreenPickError as exc:
                self._notify(str(exc), "error")
                return
            if region is None:
                self._notify(f"{kind.label} unchanged (selection cancelled)", "information")
                return
            try:
                image = await asyncio.to_thread(capture_region, region)
            except CaptureError as exc:
                self._notify(f"could not capture the {kind.label}: {exc}", "error")
                return
            # Anchoring it here is the searchability check: a box narrower than
            # one anchor can never be matched back, so filing it would only
            # produce a template that finds nothing.
            probe = ServiceProfile(key)
            try:
                probe.put(kind, image)
            except ValueError as exc:
                self._notify(f"that {kind.label} cannot be searched for: {exc}", "error")
                return
            try:
                await asyncio.to_thread(save_template, self._profile_root, key, kind, image)
            except ProfileStoreError as exc:
                self._notify(f"could not save the {kind.label}: {exc}", "error")
                return
            self._profiles_changed = True
            # Onto the variant that was just drawn: the user is looking at the
            # box they dragged, and a row that went on showing the older picture
            # would read as a capture that did not land.
            self._show_appearance(newest=kind)
            self._notify(
                f"{kind.label} captured for {key} ({region.describe()})", "information"
            )
        finally:
            self._capturing = False
            self._reload = False

    # == closing ===============================================================

    async def close(self) -> CloseResult:
        """Esc: apply what validated, ask about what did not, hand back the diff.

        Nothing can be LOST here - an invalid candidate was never committed to
        the working copy in the first place - but the visible text would simply
        vanish, which is surprising, so it is confirmed rather than dropped
        (brief §3.4).
        """
        if self._capturing:
            # Escape belongs to the overlay right now, and closing out from
            # under an in-flight capture would strand the flow that still has to
            # write the PNG.
            self._notify(CLOSE_BUSY, "warning")
            return CloseResult(False, None)
        if self._error is not None and not await self._confirm(
            DISCARD_TITLE, DISCARD_BODY.format(error=self._error)
        ):
            return CloseResult(False, None)
        changed = self.dirty
        if not changed and not self._profiles_changed:
            return CloseResult(True, None)  # nothing happened here at all
        return CloseResult(
            True, ServiceEdits(dict(self._services) if changed else None, self._profiles_changed)
        )


def kind_of(name: str) -> TemplateKind | None:
    """The appearance a page-side id names, or None when it names none.

    The page's rows are generated per ``TemplateKind`` with the kind's own value
    as the id, so one handler serves all seven and an eighth appearance is an
    enum member and nothing else.
    """
    try:
        return TemplateKind(name)
    except ValueError:
        return None


__all__: Sequence[str] = [
    "AUTO_SUBMIT_LABEL",
    "CAPTURE_BUSY",
    "CLICK_X_LABEL",
    "CLICK_Y_LABEL",
    "CLOSE_BUSY",
    "DISCARD_BODY",
    "DISCARD_TITLE",
    "FOOTER_HINT",
    "FORGET_BODY",
    "FORGET_TITLE",
    "HOVER_SCAN_LABEL",
    "MATCHER_LABELS",
    "NEW_SENTINEL",
    "OPENCV_MISSING_FROZEN",
    "OPENCV_MISSING_SOURCE",
    "REQUIRE_FENCED_LABEL",
    "SCROLL_LABELS",
    "SIGNAL_LABELS",
    "SIGNAL_UNCAPTURED",
    "STABLE_MAX",
    "STABLE_MIN",
    "STREAM_DELIVERY_LABEL",
    "TEMPLATES_NONE",
    "TEMPLATE_UNSET",
    "TOLERANCE_LABEL",
    "CloseResult",
    "ServiceEditor",
    "ServiceEdits",
    "kind_of",
    "opencv_missing_note",
    "png_data_uri",
    "select_options",
    "signal_warning",
    "template_status",
    "templates_line",
]
