"""Windows clipboard sequence-number shim (research digest: the free fast path)."""

from __future__ import annotations

import sys


def get_clipboard_sequence_number() -> int | None:
    """Current Win32 clipboard sequence number, or None on other platforms.

    A single user32 call: no OpenClipboard, no races, costs nanoseconds. The
    watcher polls it and only performs a real read when the value changes.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return None
