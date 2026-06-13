"""ChatComposer: the persistent chat input docked at the bottom of MainScreen.

A small ``TextArea`` subclass that *sends on Enter* (chat convention) instead of
inserting a newline, so the steady-state loop reads like any chat app: type a
message, press Enter. Multi-line input still works two ways - pasting preserves
newlines (paste is a Paste event, not a stream of Enter keypresses) and ``ctrl+j``
inserts a literal newline. ``escape`` blurs the box so the main screen's
single-key shortcuts (u/c/i/w/e/x) become reachable again ("command mode").

The MainScreen owns every bit of routing; this widget only emits ``Submitted``.
"""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea


class ChatComposer(TextArea):
    """A chat-style input: Enter sends, ctrl+j newline, esc blurs to the screen."""

    class Submitted(Message):
        """Posted when the user presses Enter in the composer."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        # Enter sends (TextArea's default would insert "\n"); ctrl+j keeps the
        # literal-newline escape hatch. Everything else falls through to the
        # normal TextArea editing keys.
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if event.key == "ctrl+j":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.screen.set_focus(None)  # drop to single-key "command mode"
            return
        await super()._on_key(event)

    def reset(self) -> None:
        """Clear the box after a message is sent."""
        self.load_text("")
