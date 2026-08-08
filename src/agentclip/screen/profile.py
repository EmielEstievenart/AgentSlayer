"""What each service LOOKS like: the appearances a slot recognises on screen.

The stop button is a property of ChatGPT, not of the window someone happens to
have ChatGPT open in. Pinning appearances to a window (as calibrated regions
do) means recapturing them every time the browser moves, resizes, or a second
slot opens the same service in a second window - six drag-a-box ceremonies for
information that never actually changed. So they live here instead: captured
once per service, shared by every slot pointed at it, persisted to disk
(screen.profile_store) and reloaded on the next run.

A profile holds appearances only. Where to look for them - the chat region of
a particular window - stays with the slot, because that genuinely is per
window. The picker prompt for each kind lives on the enum rather than in the
TUI: what makes a good capture is a fact about the appearance ("catch the stop
button, not the spinner it animates next to"), and it must read identically
wherever the user is asked for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from agentclip.screen.capture import RegionImage
from agentclip.screen.template import Template

_LABELS = {
    "busy": "busy indicator",
    "idle": "idle indicator",
    "chatbox-initial": "start chat box",
    "chatbox-ongoing": "ongoing chat box",
    "copy": "copy button",
    "new-chat": "new-chat button",
}

_PROMPTS = {
    # An animated spinner is a different picture on every frame, so it can
    # never be matched back - the warning is the whole reason this text is
    # centralised rather than retyped at each call site.
    "busy": (
        "Drag a TIGHT box around something that is on screen ONLY while the model is "
        "generating (the stop button is the usual one) - avoid animated spinners "
        "· Esc cancels"
    ),
    "idle": (
        "Drag a TIGHT box around something that is on screen ONLY while the chat is "
        "idle (the send button is the usual one) · Esc cancels"
    ),
    # Captured EMPTY: the box is recognised in order to click into it, and it
    # is only ever clicked when there is nothing in it yet. A capture with text
    # in it would match nothing afterwards.
    "chatbox-initial": (
        "Drag a box around the chat input box AS IT SITS IN A FRESH CHAT (centred, no "
        "messages yet), while it is EMPTY · Esc cancels"
    ),
    "chatbox-ongoing": (
        "Drag a box around the chat input box AS IT SITS IN AN ONGOING CHAT (docked at "
        "the bottom), while it is EMPTY · Esc cancels"
    ),
    "copy": (
        "Drag a TIGHT box around ONE copy button icon (pick the one under the last "
        "response, while the page is idle) · Esc cancels"
    ),
    "new-chat": "Drag a TIGHT box around the browser's NEW CHAT button · Esc cancels",
}

# How much of a captured appearance may differ before it stops being that
# appearance. Buttons are small, crisp and always drawn the same way, so they
# get the strict threshold; the new-chat control is often a text label whose
# hover state shifts more pixels. The chat boxes are far looser on purpose:
# they are big rectangles of background whose placeholder text, focus ring and
# attachment chips all move, and mistaking one layout for the other is the only
# error that matters there.
_MAX_DIFFS = {
    "busy": 0.08,
    "idle": 0.08,
    "chatbox-initial": 0.20,
    "chatbox-ongoing": 0.20,
    "copy": 0.08,
    "new-chat": 0.10,
}


class TemplateKind(StrEnum):
    """The six appearances a service profile can hold."""

    BUSY = "busy"
    IDLE = "idle"
    CHATBOX_INITIAL = "chatbox-initial"
    CHATBOX_ONGOING = "chatbox-ongoing"
    COPY = "copy"
    NEW_CHAT = "new-chat"

    @property
    def label(self) -> str:
        """Short human name, for buttons and status lines."""
        return _LABELS[self.value]

    @property
    def prompt(self) -> str:
        """The overlay instruction shown while the user drags this box."""
        return _PROMPTS[self.value]

    @property
    def max_diff(self) -> float:
        """The fraction of sampled pixels that may differ and still match."""
        return _MAX_DIFFS[self.value]


@dataclass(slots=True)
class ServiceProfile:
    """One service's captured appearances, keyed by kind.

    Mutable (unlike most dataclasses here) because it is edited a capture at a
    time: the user calibrates the busy indicator today and the copy button next
    week, and the profile in memory is the same object the UI is showing.
    """

    key: str
    templates: dict[TemplateKind, Template] = field(default_factory=dict)

    def has(self, kind: TemplateKind) -> bool:
        return kind in self.templates

    def get(self, kind: TemplateKind) -> Template | None:
        return self.templates.get(kind)

    def put(self, kind: TemplateKind, image: RegionImage) -> None:
        """Store a freshly captured appearance, anchoring it for search.

        Raises ValueError (from :meth:`Template.build`) if the capture cannot
        be searched for - an empty drag, or a box narrower than one anchor.
        """
        self.templates[kind] = Template.build(image)

    def drop(self, kind: TemplateKind) -> None:
        self.templates.pop(kind, None)

    def clear(self) -> None:
        self.templates.clear()

    @property
    def captured(self) -> tuple[TemplateKind, ...]:
        """The kinds held, in declaration order - the order the UI lists them."""
        return tuple(kind for kind in TemplateKind if kind in self.templates)

    def describe(self) -> str:
        return f"{len(self.templates)}/{len(TemplateKind)} captured"
