"""Windows clipboard sequence-number shim (research digest: the free fast path).

Windows-only by nature, not by neglect - see the docstring below for why Linux
has no counterpart even now that the rest of the screen layer has an X11 backend.
"""

from __future__ import annotations

import sys


def get_clipboard_sequence_number() -> int | None:
    """Current Win32 clipboard sequence number, or None on other platforms.

    A single user32 call: no OpenClipboard, no races, costs nanoseconds. The
    watcher polls it and only performs a real read when the value changes.

    None on Linux, and that stays true with the X11 backend in place: X11 has no
    clipboard sequence counter at all - selection ownership is announced by
    XFIXES events to a client with an event loop, which the watcher is not - so
    there is no cheap "did it change?" to shortcut with. The watcher already
    handles None by reading the clipboard every poll, which is the whole
    behaviour off Windows.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return None
