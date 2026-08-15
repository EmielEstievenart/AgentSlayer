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

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.template import Template

_LABELS = {
    "busy": "busy indicator",
    "idle": "idle indicator",
    "chatbox-initial": "start chat box",
    "chatbox-ongoing": "ongoing chat box",
    "copy": "copy button",
    "new-chat": "new-chat button",
    "send-ready": "ready-to-send button",
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
    # Captured with the box NON-empty, which is the opposite of the two chat-box
    # captures above and the whole point of this one: the control only exists
    # while there is something to send, and its DISAPPEARANCE is what proves the
    # user pressed Enter.
    "send-ready": (
        "Drag a TIGHT box around the button that SENDS the message, WITH SOMETHING TYPED "
        "in the chat box - it has to be the control that disappears once you send · "
        "Esc cancels"
    ),
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
    # Looser than the three crisp icons: this one is captured with text in the
    # composer beside it, and the button is usually the one control a service
    # tints, fades or animates as the input fills - the question it answers is
    # only "is it there at all", so a hover state must not read as absence.
    "send-ready": 0.10,
}


class TemplateKind(StrEnum):
    """The seven appearances a service profile can hold."""

    BUSY = "busy"
    IDLE = "idle"
    CHATBOX_INITIAL = "chatbox-initial"
    CHATBOX_ONGOING = "chatbox-ongoing"
    COPY = "copy"
    NEW_CHAT = "new-chat"
    SEND_READY = "send-ready"

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
    """One service's captured appearances: a STACK of images per kind.

    A kind is one *thing on screen*, not one picture of it. A send button is
    drawn one way with the composer full and another way, greyed out, while a
    file uploads; both are the send button, and a profile that could hold only
    the picture it was last shown would disarm the send gate for the whole of
    an upload. So every kind holds a stack of variants, and a search asks the
    plain OR question - is ANY of them on screen? - against that kind's own
    ``max_diff``. Capturing ADDS one; :meth:`drop` wipes the kind's whole stack.

    Mutable (unlike most dataclasses here) because it is edited a capture at a
    time: the user calibrates the busy indicator today and the copy button next
    week, and the profile in memory is the same object the UI is showing.
    """

    key: str
    templates: dict[TemplateKind, list[Template]] = field(default_factory=dict)

    def has(self, kind: TemplateKind) -> bool:
        return bool(self.templates.get(kind))

    def variants(self, kind: TemplateKind) -> tuple[Template, ...]:
        """Every image captured for ``kind``, in capture order. Empty if none.

        The only reader of a kind, deliberately: a single-template ``get`` is
        the shape in which a call site silently searches for the first variant
        and never learns the others exist.
        """
        return tuple(self.templates.get(kind, ()))

    def put(self, kind: TemplateKind, image: RegionImage) -> None:
        """Add a freshly captured appearance to ``kind``, anchoring it for search.

        Raises ValueError (from :meth:`Template.build`) if the capture cannot
        be searched for - an empty drag, or a box narrower than one anchor -
        and the stack is left exactly as it was.
        """
        template = Template.build(image)
        self.templates.setdefault(kind, []).append(template)

    def drop(self, kind: TemplateKind) -> None:
        """Forget every image of ``kind``. Dropping what isn't there is legal."""
        self.templates.pop(kind, None)

    def clear(self) -> None:
        self.templates.clear()

    @property
    def captured(self) -> tuple[TemplateKind, ...]:
        """The kinds held, in declaration order - the order the UI lists them.

        Kinds, not images: "is the copy button calibrated?" is a yes/no
        question however many pictures of it there are.
        """
        return tuple(kind for kind in TemplateKind if self.has(kind))

    def describe(self) -> str:
        return f"{len(self.captured)}/{len(TemplateKind)} captured"
