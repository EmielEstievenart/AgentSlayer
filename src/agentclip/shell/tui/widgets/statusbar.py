"""StatusBar: one docked row of segments (tui.md section 3.3, BMP glyphs only)."""

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
# ``mode`` is the permission mode (tui.md 2.6a) and it is FIRST, left of even the
# watch segment: it is the one cell that says what the app is allowed to do to
# the project at all, and shift+tab changes it with no other feedback than this.
# Unlike ``armed`` it never hides - all three modes are worth seeing, and a
# segment that only appears in two of them is a segment the eye cannot use to
# answer "which mode am I in?" at a glance.
# ``instr`` is the extra-instructions re-inject (tui.md 3.4h) and hides itself
# when empty for ``armed``'s reason: it is true only between an `r` press and
# the payload that spends it, which is usually seconds, and a permanently
# present "not armed" cell would be six characters of furniture. It sits next to
# ``edits`` because both describe what the NEXT thing out of here will carry.
# ``mcp`` is the MCP runtime's one-cell readout ("mcp 2/3" =
# connected/enabled-total, docs/design/mcp.md section 6) and it hides itself
# whenever the app has no MCP servers configured - which is most installs, and
# an empty "mcp 0/0" cell would be furniture on every one of them. It sits at
# the far right, against ``root``: like the project root it is a fact about the
# APP RUN rather than about this session's turn, so it stays out of the block
# that changes as a turn progresses.
# ``unattended`` is the auto-deny toggle's badge and it sits right of ``edits``,
# because the two say the opposite halves of one thing - what happens when a call
# reaches a gate - and a user with both on has to see the pair, exactly as they
# do with DISARMED and YOLO. Its own slot for the same reason, then: neither may
# hide the other. It hides itself when off, ``armed``'s rule for ``armed``'s
# reason - the badge is an alarm, and an always-present "attended" cell would be
# furniture the eye stops reading.
_SEGMENTS = (
    "mode",
    "watch",
    "armed",
    "service",
    "out",
    "turn",
    "instr",
    "edits",
    "unattended",
    "mcp",
    "root",
)


class StatusBar(Horizontal):
    def compose(self) -> ComposeResult:
        for name in _SEGMENTS:
            yield Static("", classes="seg", id=f"seg-{name}")

    def update_segments(
        self,
        *,
        mode: str,
        mode_class: str,
        watch: str,
        watch_class: str,
        service: str,
        out: str,
        turn: str,
        edits: str,
        edits_class: str = "",
        root: str,
        armed: str = "",
        instr: str = "",
        unattended: str = "",
        mcp: str = "",
    ) -> None:
        mode_seg = self.query_one("#seg-mode", Static)
        mode_seg.update(Text(mode))
        mode_seg.set_classes(f"seg {mode_class}".rstrip())
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
        instr_seg = self.query_one("#seg-instr", Static)
        instr_seg.update(Text(instr))
        instr_seg.display = bool(instr)
        edits_seg = self.query_one("#seg-edits", Static)
        edits_seg.update(Text(edits))
        edits_seg.set_classes(f"seg {edits_class}".rstrip())
        unattended_seg = self.query_one("#seg-unattended", Static)
        unattended_seg.update(Text(unattended))
        unattended_seg.display = bool(unattended)
        # Hidden, not blanked, when empty - ``armed``'s rule for ``armed``'s
        # reason: most installs configure no MCP servers, and they must get
        # exactly the bar they always had, padding included.
        mcp_seg = self.query_one("#seg-mcp", Static)
        mcp_seg.update(Text(mcp))
        mcp_seg.display = bool(mcp)
        self.query_one("#seg-root", Static).update(Text(root))
