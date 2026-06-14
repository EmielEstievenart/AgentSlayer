"""StatusBar: one docked row of six segments (tui.md section 3.3, BMP glyphs only)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

_SEGMENTS = ("watch", "service", "out", "turn", "edits", "root")


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
    ) -> None:
        seg = self.query_one("#seg-watch", Static)
        seg.update(Text(watch))
        seg.set_classes(f"seg {watch_class}")
        self.query_one("#seg-service", Static).update(Text(service))
        self.query_one("#seg-out", Static).update(Text(out))
        self.query_one("#seg-turn", Static).update(Text(turn))
        edits_seg = self.query_one("#seg-edits", Static)
        edits_seg.update(Text(edits))
        edits_seg.set_classes(f"seg {edits_class}".rstrip())
        self.query_one("#seg-root", Static).update(Text(root))
