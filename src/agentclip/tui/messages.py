"""Textual messages bridging background threads to the UI.

``ClipboardCaptured`` is the documented injectable path for tests: posting it
to the MainScreen is equivalent to the watcher thread capturing protocol text
from the OS clipboard.

``BusyProbed`` is the same shape for the busy-region poller: posting it is
equivalent to a poll of ``agentclip.screen.busy.probe_busy`` completing.
"""

from __future__ import annotations

from textual.message import Message

from agentclip.screen.busy import BusyProbe


class ClipboardCaptured(Message):
    """Protocol-looking text captured from the clipboard (or injected by tests)."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class BusyProbed(Message):
    """One poll's verdict from the busy-region detector (or injected by tests)."""

    def __init__(self, probe: BusyProbe) -> None:
        self.probe = probe
        super().__init__()
