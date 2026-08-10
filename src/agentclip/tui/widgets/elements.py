"""ELEMENTS: the third column, showing the pixels the detectors recognised.

Its predecessor showed the whole chat region as one thumbnail, which answered
*is the box over my browser?* and nothing else. The question underneath it is
sharper - **is the tool recognising my send button, my stop icon, my copy
button?** - and a 28x8 picture of a whole window cannot answer it: the icon
that matters is four cells of it. So this column drops the wide shot and shows
the CLOSE-UPS: whenever a template search verifies a match, the matched
rectangle is cut out of that very frame and drawn here at readable size, beside
how well it matched.

One row per appearance with something to say at runtime - the send button, the
busy icon, the idle icon, the copy button - which is deliberately the same four
kinds the sidebar's DETECTION block gives a verdict line to (the two chat boxes
and the new-chat button are found on demand and report by toast). The panel is
the *picture* of that block and the block is the *words*, so the two describe
the same thing and the same window.

**A third column, not more sidebar.** The sidebar is narrow static chrome that
already overflows most terminals (tui.md 1.3), and hanging four pictures off
the bottom of it would push the verdict lines it exists for below the fold. So
this is a sibling of it in ``#body``, with its own toggle (``F7``, mirroring
``F3``) - a whole-column show/hide rather than per-row collapsibles, because
the thing a user wants back is the horizontal room, not one row of it.

**It describes the LIVE window**, like DETECTION and for the same reason - the
automation drives one window while the user may be reading the other's
transcript for the whole of a delegation - so the heading names that window and
only the detector machinery writes here (tui.md 3.4e). Every row is a fixed
height whether it holds a picture or a resting line, so a match landing cannot
make the column dance.

**Two renderers, chosen once.** "Readable size" means SIXEL where the terminal
can do it: the crop drawn as the bitmap it is, at the size the screenshot has
it. Half blocks - the same crop averaged down to a 16x6 cell grid - are what
every other terminal gets, and they are the reason the column survived being
built at all, so both paths are live. Which one is in use is decided at startup
(``tui.graphics``) and written at the bottom of the column, because a sixel
nobody can see is indistinguishable from a detector that never matches.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.tui.graphics import (
    TerminalGraphics,
    crop_picture,
    crop_rows,
    sixel_image_class,
    terminal_graphics,
)
from agentclip.tui.messages import ElementCrop
from agentclip.tui.pixels import half_block_text, thumbnail

if TYPE_CHECKING:  # the sixel widget is imported lazily - see tui.graphics
    from textual_image.widget.sixel import Image as SixelImage

ELEMENTS_TITLE = "ELEMENTS"
ELEMENTS_HINT = "F7 hides this column"

# The four appearances the detector searches for on every tick it is calibrated
# for them, in the order they matter in the loop: the send button holds the
# gate, the busy and idle icons decide when generating stopped, the copy button
# harvests. Same set as the sidebar's DETECTOR_LABEL, and deliberately so - one
# block says what was decided, this one shows what it was decided from.
ELEMENT_ORDER: tuple[TemplateKind, ...] = (
    TemplateKind.SEND_READY,
    TemplateKind.BUSY,
    TemplateKind.IDLE,
    TemplateKind.COPY,
)
ELEMENT_LABEL: dict[TemplateKind, str] = {
    TemplateKind.SEND_READY: "send button",
    TemplateKind.BUSY: "busy icon",
    TemplateKind.IDLE: "idle icon",
    TemplateKind.COPY: "copy button",
}

# The cell budget one crop is drawn in. The column is 20 wide, 17 of content,
# and 16 once a scrollbar shows - so 16 columns is what a crop can count on.
#
# The ROW budget is the half-block one, and it is the fallback's: six rows is
# twelve pixels of height, which for a ~24px icon is a halving, enough to tell
# an arrow from a clipboard from a slab of background and no more. Sixel does
# not use it - a sixel row is sized from the terminal's real cell height
# (``graphics.crop_rows``), because the budget that matters there is in pixels.
ELEMENT_CROP_COLS = 16
ELEMENT_CROP_ROWS = 6

# What a row says instead of a picture. Three states, because "nothing has been
# looked for yet" and "we looked and it is not there" are opposite readings of
# the same blank space - and the second is the one that explains a send gate
# that will not release or an auto-copy that never fires. A row that STAYS at
# "no match yet" while the tool runs now says something precise: this service
# has no capture of that appearance, because everything captured is searched
# for twice a second whatever the automation is doing.
ELEMENT_RESTING = "no match yet"
ELEMENT_MISSING = "not on screen"

# The column says which of the two renderers it is using. Not decoration: the
# difference between "the detector is finding the wrong thing" and "your
# terminal cannot draw pictures" is invisible otherwise, and the second one has
# a fix (a terminal with sixel) that the user can only reach if they are told.
ELEMENT_MODE_PREFIX = "crops · "


def element_mode_line(graphics: TerminalGraphics) -> str:
    return f"{ELEMENT_MODE_PREFIX}{graphics.mode}"


def element_crop_image(cut: RegionImage) -> RegionImage | None:
    """Size a freshly cut match for whichever renderer is live. Worker-side.

    Half blocks need the exact cell grid they will draw and nothing more, so the
    averaging happens here, in the thread that captured the frame (tui.pixels).
    Sixel needs the OPPOSITE - every pixel that was matched, because the whole
    point is drawing them at their real size - so the cut is passed through
    untouched and the fitting happens in the panel. That costs the message queue
    nothing: a Textual message carries the object, not a copy of it, and an
    appearance is icon-sized by construction.
    """
    if cut.width <= 0 or cut.height <= 0:
        return None
    if terminal_graphics().sixel:
        return cut
    return thumbnail(cut, ELEMENT_CROP_COLS, ELEMENT_CROP_ROWS)


def elements_title(window_name: str) -> str:
    return f"{ELEMENTS_TITLE} · {window_name}" if window_name else ELEMENTS_TITLE


def element_label_id(kind: TemplateKind) -> str:
    return f"el-label-{kind}"


def element_crop_id(kind: TemplateKind) -> str:
    return f"el-crop-{kind}"


def element_line(kind: TemplateKind, text: str) -> str:
    """``copy button`` over ``found · 1.2%`` - named as it is painted, because
    four unlabelled pictures stacked on each other are not a readout.

    Two LINES rather than one, and a fixed two cells tall, because the column
    is 17 cells of content: the name and the verdict do not fit side by side,
    and a wrapped label would push every picture below it down the column each
    time a match landed - on a 0.5 s timer.
    """
    return f"{ELEMENT_LABEL[kind]}\n{text}"


def found_line(diff: float) -> str:
    """What a matched row says: the same number the sidebar's verdict line
    reports, next to the picture it is a number about."""
    return f"found · {diff:.1%}"


class ElementsPanel(Vertical):
    """One row per recognised appearance: a named line and the matched crop."""

    DEFAULT_CSS = """
    ElementsPanel .el-label {
        color: $text-muted;
        /* The kind on one line, its verdict on the next - see element_line. */
        height: 2;
    }
    ElementsPanel .el-crop {
        /* ELEMENT_CROP_ROWS, reserved whether or not anything matched: a row
           that grows when its element appears would make every row below it
           jump on a 0.5 s timer. A sixel row overrides both dimensions inline
           (_crop_widget), because its height is a pixel budget divided by this
           terminal's cell height rather than a constant. */
        height: 6;
        width: 16;
    }
    ElementsPanel .el-hint {
        color: $text-muted;
    }
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # Read ONCE, per panel, rather than per repaint: the verdict cannot
        # change while the process runs (tui.graphics), and a panel that
        # composed half-block widgets must keep painting half blocks into them.
        self._graphics = terminal_graphics()
        self._rows = crop_rows(self._graphics.cell_height)
        # What each row is currently showing, so an unchanged crop can be left
        # alone - see _paint_crop.
        self._painted: dict[TemplateKind, RegionImage | None] = {}

    def compose(self) -> ComposeResult:
        yield Static(Text(elements_title("")), id="elements-title", classes="side-title")
        for kind in ELEMENT_ORDER:
            yield Static(
                Text(element_line(kind, ELEMENT_RESTING)),
                id=element_label_id(kind),
                classes="el-label",
            )
            yield self._crop_widget(kind)
        yield Static(Text(ELEMENTS_HINT), classes="el-hint")
        yield Static(Text(element_mode_line(self._graphics)), id="elements-mode", classes="el-hint")

    def _crop_widget(self, kind: TemplateKind) -> Widget:
        """The widget one crop is drawn in: a sixel image, or a block of text.

        Chosen at COMPOSE time from the startup probe, and never revisited. The
        half-block ``Static`` is not a stub - it is the renderer for every
        terminal that cannot do sixel, which includes every headless test run,
        so both branches are live code.
        """
        if self._graphics.sixel:
            sixel_image = sixel_image_class()
            if sixel_image is not None:
                widget = cast(Widget, sixel_image(id=element_crop_id(kind), classes="el-crop"))
                # Inline, because the height is this terminal's and the class
                # rule above is the fallback's. Both are pinned rather than left
                # to auto: textual-image scales the image to whatever cell box
                # it is given, and crop_picture has already padded the crop to
                # exactly this one.
                widget.styles.width = ELEMENT_CROP_COLS
                widget.styles.height = self._rows
                return widget
        return Static(Text(""), id=element_crop_id(kind), classes="el-crop")

    def _paint_crop(self, kind: TemplateKind, image: RegionImage | None) -> None:
        """Draw (or blank) one row's picture, in whichever mode this panel composed.

        A row showing the same pixels it was already showing is LEFT ALONE. The
        poller re-cuts the same still icon out of frame after frame, so most
        ticks would otherwise re-encode a picture nobody can tell from the one
        already on screen - and the sixel widget rebuilds its child to do it,
        which on a real terminal is a clear and a redraw twice a second. The
        comparison is a bytes equality over an icon, i.e. free.
        """
        if kind in self._painted and self._painted[kind] == image:
            return
        self._painted[kind] = image
        widget = self.query_one(f"#{element_crop_id(kind)}", Widget)
        if isinstance(widget, Static):
            widget.update(Text("") if image is None else half_block_text(image))
            return
        cast("SixelImage", widget).image = (
            None
            if image is None
            else crop_picture(image, ELEMENT_CROP_COLS, self._rows, self._graphics)
        )

    def show_window(self, window_name: str) -> None:
        """Name the window every crop below is from.

        The LIVE window, written by the detector machinery whenever it rebuilds
        (tui.md 3.4e). Without it a sub-agent's send button reads as the master
        tab's, which is exactly the confusion a picture is supposed to end.
        """
        self.query_one("#elements-title", Static).update(Text(elements_title(window_name)))

    def show_matches(self, crops: Mapping[TemplateKind, ElementCrop | None]) -> None:
        """Repaint the rows this tick has something to say about, and no others.

        A kind ABSENT from ``crops`` is a kind the live window's service is not
        calibrated for, which is the only reason a tick says nothing about one
        (screen/detector.py searches every calibrated kind on every frame), and
        its row keeps its last picture rather than being blanked by a tick that
        never looked. Present-and-``None`` is the opposite claim, that the
        search ran and found nothing, and it clears the row.

        ``ElementCrop.image`` arrives sized for whichever renderer is live
        (:func:`element_crop_image`, run in the worker that captured the frame):
        the exact cell grid for half blocks, the untouched pixels for sixel.
        What happens here is the last hop into the widget - the glyph pass, or
        the BGRX->Pillow->sixel fit.
        """
        for kind, crop in crops.items():
            if kind not in ELEMENT_LABEL:
                continue
            label = self.query_one(f"#{element_label_id(kind)}", Static)
            if crop is None or crop.image.width <= 0 or crop.image.height <= 0:
                label.update(Text(element_line(kind, ELEMENT_MISSING)))
                self._paint_crop(kind, None)
                continue
            label.update(Text(element_line(kind, found_line(crop.diff))))
            self._paint_crop(kind, crop.image)

    def clear(self) -> None:
        """Back to "nothing has matched yet", every row.

        Called from ``_paint_detection`` on every detector rebuild: the heading
        may have just been repointed at the other window, and a crop cut from
        the old one under the new one's name is a straightforward lie. The rows
        refill themselves on the new run's first tick.
        """
        for kind in ELEMENT_ORDER:
            self.query_one(f"#{element_label_id(kind)}", Static).update(
                Text(element_line(kind, ELEMENT_RESTING))
            )
            self._paint_crop(kind, None)
