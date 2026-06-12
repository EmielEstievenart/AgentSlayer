"""Textual messages bridging the clipboard watcher thread to the UI.

``ClipboardCaptured`` is the documented injectable path for tests: posting it
to the MainScreen is equivalent to the watcher thread capturing protocol text
from the OS clipboard.
"""

from __future__ import annotations

from textual.message import Message


class ClipboardCaptured(Message):
    """Protocol-looking text captured from the clipboard (or injected by tests)."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()
