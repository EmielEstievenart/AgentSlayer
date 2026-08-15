"""A number you set by dragging, for the one setting that is a feel rather than a fact.

Textual 8.2 ships no slider, and almost every number in this app is right not to
want one: a paste budget, a stale window, a context size are all values a user
KNOWS and types. Pixel tolerance is the exception. Nobody knows that their
browser's sub-pixel anti-aliasing needs 31 rather than 24 - they know their
button is not being found, and they want to push a value up until it is. An
Input asks them to guess and then to guess again; a track with a handle on it
says how much room is left in both directions and lets them walk it.

Deliberately small. There is no drag-tracking (a click on the track jumps
there, which is the same gesture in one press), no tick marks, no orientation
option, and no formatting hook: this widget exists for one setting, and every
one of those would be code with no second caller to keep it honest.

Two things about it are load-bearing rather than cosmetic:

* **The number is drawn beside the track.** A slider whose value cannot be read
  off is a slider whose value cannot be reported in a bug report, typed into a
  config file, or compared against the default. The track is the gesture; the
  number is the answer.
* **``Changed`` is posted only when the value really moved.** Clamping at the
  ends means a held arrow key produces a run of no-op sets, and a widget that
  announced every one of them would have the screen writing an unchanged
  preset back into its working copy dozens of times a second.

Keyboard: left/right (and up/down) by ``step``, shift+left/right by
``big_step``, home/end to the ends. Mouse: a click anywhere on the track sets
the value that position stands for.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.segment import Segment
from textual import events
from textual.geometry import Size
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget

# The glyphs the track is drawn from. Box-drawing rather than block characters:
# the handle has to be distinguishable from the filled part of the track at a
# glance, and a solid block on a solid block is not.
TRACK_CHAR = "━"
HANDLE_CHAR = "●"
# Cells reserved for the number at the right-hand end (" 100" at its widest,
# plus a space). Fixed rather than measured, so the handle does not shift by a
# cell when the value crosses from 9 to 10.
VALUE_WIDTH = 4
# The smallest track worth drawing. Below this the widget renders the number
# alone rather than a track that cannot express its own range.
MIN_TRACK = 4


@dataclass(frozen=True, slots=True)
class SliderRange:
    """The span a slider covers, and how far one press moves inside it."""

    minimum: int = 0
    maximum: int = 100
    step: int = 1
    big_step: int = 10

    def clamp(self, value: int) -> int:
        return max(self.minimum, min(self.maximum, value))

    @property
    def span(self) -> int:
        return self.maximum - self.minimum


class Slider(Widget, can_focus=True):
    """A focusable integer slider: a track, a handle, and the number itself."""

    DEFAULT_CSS = """
    Slider {
        height: 1;
        width: 1fr;
    }
    Slider:focus {
        color: $accent;
        text-style: bold;
    }
    """

    BINDINGS = [
        ("left,down", "nudge(-1)", "less"),
        ("right,up", "nudge(1)", "more"),
        ("shift+left,shift+down", "leap(-1)", "much less"),
        ("shift+right,shift+up", "leap(1)", "much more"),
        ("home", "to_end(-1)", "minimum"),
        ("end", "to_end(1)", "maximum"),
    ]

    value: reactive[int] = reactive(0, init=False)

    @dataclass
    class Changed(Message):
        """Posted when the value actually moved. ``slider`` follows Textual's
        convention of naming the widget the message is about, so a screen with
        two of them can tell which one spoke."""

        slider: Slider
        value: int

        @property
        def control(self) -> Slider:
            return self.slider

    def __init__(
        self,
        value: int = 0,
        *,
        minimum: int = 0,
        maximum: int = 100,
        step: int = 1,
        big_step: int = 10,
        id: str | None = None,  # noqa: A002 - Textual's widget kwarg is named `id`
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(id=id, classes=classes, disabled=disabled)
        self.range = SliderRange(minimum, maximum, max(1, step), max(1, big_step))
        # set_reactive rather than assignment: this is construction, not a user
        # gesture, and a Changed posted from __init__ would reach a screen that
        # has not composed yet.
        self.set_reactive(Slider.value, self.range.clamp(value))

    # -- geometry -------------------------------------------------------------

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return container.width

    @property
    def _track_width(self) -> int:
        return max(0, self.content_size.width - VALUE_WIDTH)

    def _handle_column(self, track: int) -> int:
        """Which cell of a ``track``-wide track the current value sits on.

        The ends are exact: the minimum is always the first cell and the maximum
        always the last, whatever the rounding in between does, because "is it
        at the end?" is the question a slider is worst at answering by eye.
        """
        if track <= 1 or self.range.span <= 0:
            return 0
        offset = (self.value - self.range.minimum) * (track - 1)
        return round(offset / self.range.span)

    def _value_at(self, column: int, track: int) -> int:
        if track <= 1 or self.range.span <= 0:
            return self.range.minimum
        position = max(0, min(track - 1, column))
        return self.range.clamp(
            self.range.minimum + round(position * self.range.span / (track - 1))
        )

    # -- painting -------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        """One cell row: the track with the handle on it, then the number.

        Painted as explicit segments carrying this widget's own resolved style
        rather than handed to Rich as markup - a Strip whose segments have no
        style is what Textual's opacity blending crashes on, and a slider in a
        modal is always inside something that blends.
        """
        style = self.rich_style
        if y != 0:
            return Strip.blank(self.content_size.width, style)
        track = self._track_width
        number = f"{self.value:>{VALUE_WIDTH}}"
        if track < MIN_TRACK:
            return Strip([Segment(number, style)])
        column = self._handle_column(track)
        bar = TRACK_CHAR * column + HANDLE_CHAR + TRACK_CHAR * (track - column - 1)
        return Strip([Segment(bar + number, style)])

    def watch_value(self, old: int, new: int) -> None:
        self.refresh()
        if old != new:
            self.post_message(self.Changed(self, new))

    def set_value(self, value: int, *, notify: bool = True) -> None:
        """Move the handle, clamped. ``notify=False`` writes the value WITHOUT
        posting Changed - what a screen loading a form needs, so that filling
        the control in does not read back as the user having set it."""
        clamped = self.range.clamp(value)
        if notify:
            self.value = clamped
        else:
            self.set_reactive(Slider.value, clamped)
            self.refresh()

    # -- gestures --------------------------------------------------------------

    def action_nudge(self, direction: int) -> None:
        self.value = self.range.clamp(self.value + direction * self.range.step)

    def action_leap(self, direction: int) -> None:
        self.value = self.range.clamp(self.value + direction * self.range.big_step)

    def action_to_end(self, direction: int) -> None:
        self.value = self.range.maximum if direction > 0 else self.range.minimum

    def on_click(self, event: events.Click) -> None:
        """A click on the track jumps the handle there.

        Clicks past the track - on the number at the right-hand end - are
        ignored rather than read as "maximum": the number is a readout, and a
        user reaching for it to read it should not thereby set it.
        """
        track = self._track_width
        if track < MIN_TRACK or event.x >= track:
            return
        event.stop()
        self.focus()
        self.value = self._value_at(event.x, track)
