"""RunningBar: a one-line animated "working" indicator (tui.md section 8a).

Shown precisely while the engine is executing a turn's tool calls - the moment
``engine.execute()`` (or ``answer_user()``) is in flight on the worker thread -
so the user can see that a build/command is actually running rather than staring
at a frozen-looking screen. The braille spinner glyphs are all in the BMP
(U+28xx), honoring the Windows-only-BMP brief.

It is the HEADER of the run panel (widgets/run_panel.py), which grew the rest of
the region under it - one row per call, and the running command's live output.
This still owns the animation, the label and the cancel hint, and nothing else:
that split is what keeps "is anything happening?" (one line, always) apart from
"what exactly is happening?" (the rows, and the tail behind ctrl+o).
"""

from __future__ import annotations

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# The bar is the only moment the cancel key does anything, so it is also the
# only place that has to advertise it (MainScreen owns the binding itself).
CANCEL_HINT = "(ctrl+x to cancel)"


class RunningBar(Static):
    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__("", id=id)
        self._frame = 0
        self._label = ""
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self.display = False

    def start(self, label: str) -> None:
        self._label = label
        self._frame = 0
        self.display = True
        self._paint()
        if self._timer is None:
            self._timer = self.set_interval(0.1, self._tick)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.display = False

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_FRAMES)
        self._paint()

    def _paint(self) -> None:
        self.update(Text(f"{_FRAMES[self._frame]} {self._label}  {CANCEL_HINT}"))
