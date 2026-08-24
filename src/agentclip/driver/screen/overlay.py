"""The fullscreen overlays: translucent windows drawn over the whole desktop.

Two of them, and both run in THEIR OWN PROCESS, spawned by screen.picker via a
hidden CLI flag. They cannot share a process with the Textual app: tkinter
insists on the main thread and runs its own event loop.

* :func:`run_overlay` - the draw-a-box picker (``--pick-region``). The CLI
  prints the result in the region wire format; this module only returns it.
* :func:`run_identify_overlay` - the read-only `/identify` answer
  (``--show-identify``): every element screen.identify found, boxed where it
  sits, tagged with a short badge, and spelled out once in a legend drawn on the
  same canvas. It takes no input beyond dismissing it, and returns nothing.

tkinter is imported lazily so a Linux install without the Tk bindings can run
everything else - the ImportError surfaces as a per-use CLI error instead of
killing the app at startup.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentclip.driver.screen.focus import make_dpi_aware, virtual_screen_bounds
from agentclip.driver.screen.identify import CHAT_REGION_LABEL, IdentifiedElement
from agentclip.driver.screen.region import ScreenRegion

if TYPE_CHECKING:  # type-only: the real import stays lazy, inside the two entry points
    import tkinter as tk

_MIN_DRAG_PX = 8  # anything smaller is a stray click, not a drawn box
# Callers that pick a different kind of region (e.g. the busy-detector's
# stop-button square) pass their own instruction instead.
DEFAULT_PROMPT = "Drag a box around the AI chat's input area · Esc cancels"

IDENTIFY_PROMPT = "What AgentClip sees in the chat window · click or press any key to dismiss"
# Lighter than the picker's 0.3: the picker dims the desktop so a dragged
# rectangle stands out against it, while this one is read AGAINST the browser
# underneath - the boxes only mean anything next to the buttons they claim to be.
IDENTIFY_ALPHA = 0.45
# One colour per kind, assigned in first-appearance order so a scene with three
# copy buttons and one send button reads as two families rather than four boxes.
# The drawn chat region gets its own, off the cycle: it is the container, not a
# find, and it is the one box drawn around everything else.
IDENTIFY_REGION_COLOUR = "#ffd166"
IDENTIFY_COLOURS = ("#00ff88", "#4cc9f0", "#ff6b6b", "#c77dff", "#ffe066", "#80ffdb")

# The drawn chat region's badge. Not a number: the finds are numbered 1..n to
# match the toast's count, which excludes the container the user drew.
IDENTIFY_REGION_BADGE = "#R"
# The UI face, per platform. Tk resolves an unknown family by silently falling
# back to a default one, so a hard-coded "Segoe UI" is not an error on Linux -
# it is a legend drawn in whatever Tk picked, at a width nothing here measured.
# DejaVu Sans is what a desktop with tkinter installed effectively always has
# (it rides along with the fontconfig package set every distro pulls); the
# TkDefaultFont behind it is Tk's own answer for anywhere else.
if sys.platform == "win32":
    UI_FONT_FAMILY = "Segoe UI"
elif sys.platform.startswith("linux"):
    UI_FONT_FAMILY = "DejaVu Sans"
else:
    UI_FONT_FAMILY = "TkDefaultFont"


def ui_font(size: int, *modifiers: str) -> tuple[str, int, *tuple[str, ...]]:
    """A Tk font tuple in the platform's UI face - ``("Segoe UI", 11)`` and its
    equivalents. One helper because all four fonts drawn here are the same face
    at three sizes, and the only thing that varies between platforms is which
    family that is."""
    return (UI_FONT_FAMILY, size, *modifiers)


_BADGE_FONT = ui_font(10, "bold")
_LEGEND_FONT = ui_font(11)
# Roughly one badge wide at _BADGE_FONT: how far a badge steps sideways when it
# would land on top of one already placed, which is what several matches of one
# appearance on one physical element look like.
_BADGE_NUDGE_PX = 26
# Badges further apart than this vertically are on different lines and cannot
# collide however close their columns are.
_BADGE_LINE_PX = 7
_LEGEND_MARGIN_PX = 16  # from the right edge of the screen
_LEGEND_TOP_PX = 72  # clear of IDENTIFY_PROMPT's line (centred at y=40) on any width
_LEGEND_LINE_PX = 18
_LEGEND_PAD_X, _LEGEND_PAD_Y = 10, 7
_LEGEND_FILL = "#0b0b12"
_LEGEND_BORDER = "#5a5a6e"


@dataclass(frozen=True, slots=True)
class IdentifyLabel:
    """One element's annotation, in the two places it appears.

    ``badge`` is what sits on the box - two or three characters, because the
    boxes routinely overlap and a full description on each was unreadable the
    moment one appearance matched twice on one button. ``legend`` is the same
    element spelled out (badge, label, diff) in the legend block, which has the
    room the box does not. ``x``/``y`` are window-relative pixels for the badge.
    """

    badge: str
    legend: str
    colour: str
    x: int
    y: int


def run_overlay(prompt: str | None = None) -> ScreenRegion | None:
    """Show the overlay; block until a box is dragged (region) or Esc (None).

    ``prompt`` is the instruction drawn across the top (default: DEFAULT_PROMPT).
    """
    import tkinter as tk

    make_dpi_aware()  # before Tk() so its geometry is physical pixels
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.3)
    bounds = virtual_screen_bounds()  # None off-Windows: tkinter's primary screen is all we get
    if bounds is not None:
        left, top, width, height = bounds
    else:
        left, top = 0, 0
        width, height = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{width}x{height}+{left}+{top}")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        width // 2,
        40,
        fill="white",
        font=ui_font(14),
        text=prompt or DEFAULT_PROMPT,
    )

    picked: list[ScreenRegion] = []
    start: dict[str, int] = {}
    rect: list[int] = []

    def on_press(event: tk.Event) -> None:
        start["x"], start["y"] = event.x, event.y
        rect[:] = [
            canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00ff88", width=2)
        ]

    def on_motion(event: tk.Event) -> None:
        if rect:
            canvas.coords(rect[0], start["x"], start["y"], event.x, event.y)

    def on_release(event: tk.Event) -> None:
        if not rect:
            return
        box_w, box_h = abs(event.x - start["x"]), abs(event.y - start["y"])
        if box_w < _MIN_DRAG_PX or box_h < _MIN_DRAG_PX:
            canvas.delete(rect[0])  # stray click: stay up for another try
            rect.clear()
            return
        # Window-relative -> virtual-screen coordinates (window sits at left/top).
        picked.append(
            ScreenRegion(
                left + min(start["x"], event.x), top + min(start["y"], event.y), box_w, box_h
            )
        )
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_motion)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", lambda _event: root.destroy())
    root.focus_force()
    root.mainloop()
    return picked[0] if picked else None


def _identify_colours(elements: Sequence[IdentifiedElement]) -> dict[str, str]:
    """A stable colour per label, in first-appearance order."""
    colours: dict[str, str] = {}
    for element in elements:
        if element.label in colours:
            continue
        if element.label == CHAT_REGION_LABEL:
            colours[element.label] = IDENTIFY_REGION_COLOUR
        else:
            colours[element.label] = IDENTIFY_COLOURS[len(colours) % len(IDENTIFY_COLOURS)]
    return colours


def _identify_labels(
    elements: Sequence[IdentifiedElement], left: int = 0, top: int = 0
) -> list[IdentifyLabel]:
    """Badge, legend row, colour and badge position for each element, in order.

    ``left``/``top`` are the virtual screen's origin, subtracted to turn absolute
    element rectangles into the window-relative coordinates the canvas draws in.

    Pure, and kept apart from the drawing for that reason: the numbering, the
    shared colour and the anti-collision nudge are the parts that can be wrong in
    a way the picture would not obviously show.
    """
    colours = _identify_colours(elements)
    labels: list[IdentifyLabel] = []
    placed: list[tuple[int, int]] = []
    found = 0
    for element in elements:
        if element.label == CHAT_REGION_LABEL:
            badge = IDENTIFY_REGION_BADGE
        else:
            found += 1
            badge = f"#{found}"
        # Window-relative: the window sits at the virtual screen's own origin.
        x, y = element.rect.left - left + 2, element.rect.top - top
        # Above the box where there is room, inside its top edge where there is
        # not (an element flush against the top of the screen).
        y = y - 9 if y >= 14 else y + 9
        # Several matches of one appearance on one physical element are boxes a
        # few pixels apart, and their badges would print over each other: step
        # the later one sideways until it clears every badge already placed.
        while any(
            abs(x - other_x) < _BADGE_NUDGE_PX and abs(y - other_y) < _BADGE_LINE_PX
            for other_x, other_y in placed
        ):
            x += _BADGE_NUDGE_PX
        placed.append((x, y))
        labels.append(
            IdentifyLabel(badge, f"{badge}  {element.describe()}", colours[element.label], x, y)
        )
    return labels


def _draw_legend(canvas: tk.Canvas, labels: Sequence[IdentifyLabel], width: int) -> None:
    """The key to the badges, as a block in the top-right corner of the canvas.

    One row per element, in each element's own colour, so the badge on a box and
    the row that explains it are matched by two things rather than one. The rows
    are laid out at the left edge and then shifted as a group, which right-aligns
    the block without measuring the font: the text stays left-aligned inside it.

    The rows are drawn first and the backdrop is slid underneath them, because
    the block's size is whatever the longest label came out as. The backdrop is
    the point of the exercise - the window is translucent, and unbacked text over
    a bright browser is exactly as unreadable as the overlapping labels this
    legend replaced.
    """
    if not labels:
        return
    rows = [
        canvas.create_text(
            0,
            _LEGEND_TOP_PX + index * _LEGEND_LINE_PX,
            anchor="w",
            fill=label.colour,
            font=_LEGEND_FONT,
            text=label.legend,
        )
        for index, label in enumerate(labels)
    ]
    block = canvas.bbox(*rows)
    if block is None:  # no font metrics yet: leave the rows where they are
        return
    shift = width - _LEGEND_MARGIN_PX - block[2]
    for row in rows:
        canvas.move(row, shift, 0)
    x1, y1, x2, y2 = block[0] + shift, block[1], block[2] + shift, block[3]
    backdrop = canvas.create_rectangle(
        x1 - _LEGEND_PAD_X,
        y1 - _LEGEND_PAD_Y,
        x2 + _LEGEND_PAD_X,
        y2 + _LEGEND_PAD_Y,
        fill=_LEGEND_FILL,
        outline=_LEGEND_BORDER,
    )
    canvas.tag_lower(backdrop, rows[0])


def run_identify_overlay(elements: Sequence[IdentifiedElement]) -> None:
    """Box every identified element and key the boxes; block until dismissed.

    Each element gets an outlined rectangle where it sits and a short badge at
    its top-left corner - ``#1``, ``#2``, ``#R`` for the drawn chat region - and
    the legend in the top-right corner spells each badge out with its label and
    diff. The description used to be printed on every box, which was legible
    until the interesting case: several matches of one appearance land on one
    physical element, and their labels then overlapped into a smear exactly where
    the user was looking. A badge is short enough to survive that (and steps
    sideways when it would not), and the text that no longer fits on the box has
    a corner of the screen to itself.

    Read-only: no drag, no result, nothing to cancel. It is a picture the user
    asked for, so it stays up until they are done reading it - there is no timer.
    Any key and any click take it down, two exits rather than one because an
    ``overrideredirect`` window that lost keyboard focus to a stray click
    elsewhere would otherwise be unclosable with the keyboard alone.

    Coordinates are absolute virtual-screen pixels, the same space the capture
    and the click use, so a box lands exactly on the pixels the matcher scored.
    """
    import tkinter as tk

    make_dpi_aware()  # before Tk() so its geometry is physical pixels
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", IDENTIFY_ALPHA)
    bounds = virtual_screen_bounds()  # None off-Windows: tkinter's primary screen is all we get
    if bounds is not None:
        left, top, width, height = bounds
    else:
        left, top = 0, 0
        width, height = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{width}x{height}+{left}+{top}")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="arrow")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        width // 2,
        40,
        fill="white",
        font=ui_font(14),
        text=IDENTIFY_PROMPT,
    )

    labels = _identify_labels(elements, left, top)
    for element, label in zip(elements, labels, strict=True):
        rect = element.rect
        x1, y1 = rect.left - left, rect.top - top
        canvas.create_rectangle(
            x1, y1, x1 + rect.width, y1 + rect.height, outline=label.colour, width=2
        )
        canvas.create_text(
            label.x, label.y, anchor="w", fill=label.colour, font=_BADGE_FONT, text=label.badge
        )
    _draw_legend(canvas, labels, width)

    root.bind("<Escape>", lambda _event: root.destroy())
    root.bind("<Key>", lambda _event: root.destroy())  # any key, not just Esc
    canvas.bind("<Button>", lambda _event: root.destroy())
    root.focus_force()
    root.mainloop()
