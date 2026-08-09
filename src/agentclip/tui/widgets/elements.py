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
"""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from agentclip.screen.profile import TemplateKind
from agentclip.tui.messages import ElementCrop
from agentclip.tui.pixels import half_block_text

ELEMENTS_TITLE = "ELEMENTS"
ELEMENTS_HINT = "F7 hides this column"

# The four appearances that are searched for while the automation runs, in the
# order they matter in the loop: the send button holds the gate, the busy and
# idle icons decide when generating stopped, the copy button harvests. Same set
# as the sidebar's DETECTOR_LABEL, and deliberately so - one block says what was
# decided, this one shows what it was decided from.
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

# The cell budget one crop is drawn in. The column is 20 wide (17 usable), and
# six rows is twelve pixels of height - which for a ~24px icon is a halving
# rather than the near-total loss the old whole-region thumbnail inflicted on
# it, and is enough to tell an arrow from a clipboard from a slab of
# background. That is the whole question this column answers; reading a glyph
# is not. Rows bind before columns for anything squarish, so the column budget
# only really decides how a WIDE appearance (a send bar, a composer) is drawn.
ELEMENT_CROP_COLS = 16
ELEMENT_CROP_ROWS = 6

# What a row says instead of a picture. Three states, because "nothing has been
# looked for yet" and "we looked and it is not there" are opposite readings of
# the same blank space - and the second is the one that explains a send gate
# that will not release or an auto-copy that never fires.
ELEMENT_RESTING = "no match yet"
ELEMENT_MISSING = "not on screen"


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
           jump on a 0.5 s timer. */
        height: 6;
    }
    ElementsPanel .el-hint {
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(elements_title("")), id="elements-title", classes="side-title")
        for kind in ELEMENT_ORDER:
            yield Static(
                Text(element_line(kind, ELEMENT_RESTING)),
                id=element_label_id(kind),
                classes="el-label",
            )
            yield Static(Text(""), id=element_crop_id(kind), classes="el-crop")
        yield Static(Text(ELEMENTS_HINT), classes="el-hint")

    def show_window(self, window_name: str) -> None:
        """Name the window every crop below is from.

        The LIVE window, written by the detector machinery whenever it rebuilds
        (tui.md 3.4e). Without it a sub-agent's send button reads as the master
        tab's, which is exactly the confusion a picture is supposed to end.
        """
        self.query_one("#elements-title", Static).update(Text(elements_title(window_name)))

    def show_matches(self, crops: Mapping[TemplateKind, ElementCrop | None]) -> None:
        """Repaint the rows this tick has something to say about, and no others.

        A kind ABSENT from ``crops`` is a kind that was not searched - the send
        button outside the gate, the copy button on any tick at all - and its
        row keeps its last picture rather than being blanked by a tick that
        never looked. Present-and-``None`` is the opposite claim, that the
        search ran and found nothing, and it clears the row.

        ``ElementCrop.image`` is already sized (``tui.pixels``, run in the
        worker that captured the frame); all that happens here is the channel
        swap and the half-block glyphs.
        """
        for kind, crop in crops.items():
            if kind not in ELEMENT_LABEL:
                continue
            label = self.query_one(f"#{element_label_id(kind)}", Static)
            picture = self.query_one(f"#{element_crop_id(kind)}", Static)
            if crop is None or crop.image.width <= 0 or crop.image.height <= 0:
                label.update(Text(element_line(kind, ELEMENT_MISSING)))
                picture.update(Text(""))
                continue
            label.update(Text(element_line(kind, found_line(crop.diff))))
            picture.update(half_block_text(crop.image))

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
            self.query_one(f"#{element_crop_id(kind)}", Static).update(Text(""))
