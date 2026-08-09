"""StatusBar: one docked row of six segments (tui.md section 3.3, BMP glyphs only)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

# ``armed`` is the DISARMED badge and it is empty almost always - the segment
# hides itself rather than reserving its padding, so the armed bar looks exactly
# like the six-segment one it has always been. It sits second, right of the
# "what does the app want from you" segment and left of everything describing
# the session, because it qualifies the first and overrides none of the rest.
# Its own slot, NOT the ``edits``/YOLO one: both can be true at once, and a
# disarmed YOLO session is precisely the pair a user must be able to see.
_SEGMENTS = ("watch", "armed", "service", "out", "turn", "edits", "root")


class StatusBar(Horizontal):
    def compose(self) -> ComposeResult:
        for name in _SEGMENTS:
            yield Static("", classes="seg", id=f"seg-{name}")

    def update_segments(
        self,
        *,
        watch: str,
        watch_class: str,
        service: str,
        out: str,
        turn: str,
        edits: str,
        edits_class: str = "",
        root: str,
        armed: str = "",
    ) -> None:
        seg = self.query_one("#seg-watch", Static)
        seg.update(Text(watch))
        seg.set_classes(f"seg {watch_class}")
        # Empty means armed, and an armed app says nothing at all here: the
        # badge is an alarm, so it may not become furniture the eye stops
        # reading. Hidden rather than blanked so it does not leave two cells of
        # padding behind either.
        armed_seg = self.query_one("#seg-armed", Static)
        armed_seg.update(Text(armed))
        armed_seg.display = bool(armed)
        self.query_one("#seg-service", Static).update(Text(service))
        self.query_one("#seg-out", Static).update(Text(out))
        self.query_one("#seg-turn", Static).update(Text(turn))
        edits_seg = self.query_one("#seg-edits", Static)
        edits_seg.update(Text(edits))
        edits_seg.set_classes(f"seg {edits_class}".rstrip())
        self.query_one("#seg-root", Static).update(Text(root))
