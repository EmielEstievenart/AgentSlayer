"""Synthetic input and window-focus nudges for the auto-copy dance.

Region-aimed input, both "point at the region and act":

* ``click_region`` - the "done processing, hand the user back to the browser"
  focus click. After every outbound clipboard copy the TUI clicks the center of
  the user-drawn chat region, so the chat's input field is focused and a paste
  lands immediately.
* ``scroll_region`` - wheel the chat back to the bottom before hunting for the
  newest copy button (see screen.template), since the copy-button band only
  shows the latest response when the transcript is scrolled down.

Window focus, for snapping back to AgentClip after the auto-copy click:

* ``foreground_window`` - the handle of whatever window has focus right now;
  the TUI records its own terminal with this while the user is typing in it.
* ``focus_window`` - bring a recorded window back to the foreground. Windows
  refuses ``SetForegroundWindow`` from a background process, so a zero-effect
  ALT tap is sent first - the documented input-recency loophole.

Windows-only, pure stdlib: ``SetCursorPos`` + ``SendInput`` via ctypes. Other
platforms return False/None so the caller can tell the user once instead of
failing on every copy. DPI awareness is forced first (see ``make_dpi_aware``)
so these coordinates live in the same physical-pixel space the overlay
measured in.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from agentclip.screen.region import ScreenRegion

_dpi_aware = False

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_KEYUP = 0x0002
_VK_MENU = 0x12  # the ALT key
# One wheel detent, as Windows defines it. Negative scrolls down.
WHEEL_DELTA = 120


def make_dpi_aware() -> None:
    """Opt this process out of DPI virtualization (Windows; no-op elsewhere).

    Without it, a scaled monitor makes Windows silently rescale cursor APIs and
    tkinter geometry, so the overlay and the click would disagree about where a
    pixel is. Harmless for the TUI itself - the console window belongs to the
    terminal process, not us. Safe to call repeatedly (a failed/duplicate set
    just leaves whatever awareness is already in effect).
    """
    global _dpi_aware
    if _dpi_aware or sys.platform != "win32":
        return
    import contextlib
    import ctypes

    try:  # per-monitor awareness (Win 8.1+); returns an HRESULT we can ignore
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    _dpi_aware = True


def _input_struct() -> Any:
    """The ``INPUT`` struct ``SendInput`` expects, with mouse and keyboard
    union members.

    Built inside a call because the ``wintypes`` it is made of only exist on
    Windows - the same lazy-ctypes rule the rest of this layer follows.
    """
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t  # ULONG_PTR: pointer-sized, missing from wintypes

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class KeybdInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class InputUnion(ctypes.Union):
        # MOUSEINPUT is the largest member of the real INPUT union, so this
        # declaration still has the exact size SendInput expects.
        _fields_ = [("mi", MouseInput), ("ki", KeybdInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", InputUnion)]

    return Input


def _send_at_center(region: ScreenRegion, events: Sequence[tuple[int, int]]) -> bool:
    """Put the cursor at the region's center, then send ``(dwFlags, mouseData)``
    events in one SendInput burst. False = nothing was sent (unsupported
    platform, empty burst, the cursor could not be placed, or SendInput
    swallowed events - a lower-integrity process cannot drive an elevated one).
    """
    if sys.platform != "win32" or not events:
        return False
    import ctypes

    make_dpi_aware()
    user32 = ctypes.windll.user32
    x, y = region.center
    if not user32.SetCursorPos(int(x), int(y)):
        return False

    input_struct = _input_struct()
    burst = (input_struct * len(events))()
    for event, (flags, mouse_data) in zip(burst, events, strict=True):
        event.type = _INPUT_MOUSE
        event.union.mi.dwFlags = flags
        # mouseData is a DWORD; a scroll-down delta is negative, so wrap it into
        # the unsigned range ourselves instead of relying on ctypes' coercion.
        event.union.mi.mouseData = mouse_data & 0xFFFFFFFF
    sent = user32.SendInput(len(events), burst, ctypes.sizeof(input_struct))
    return int(sent) == len(events)


def click_region(region: ScreenRegion) -> bool:
    """Move the cursor to the region's center and left-click. False = no click
    happened (unsupported platform or the cursor could not be placed)."""
    return _send_at_center(region, [(_MOUSEEVENTF_LEFTDOWN, 0), (_MOUSEEVENTF_LEFTUP, 0)])


def scroll_region(region: ScreenRegion, clicks: int) -> bool:
    """Move the cursor to the region's center and turn the wheel ``clicks``
    detents - negative scrolls DOWN, positive UP (the OS sign convention).

    All detents go out in a single SendInput burst with no sleeps: the caller
    wants a fast "snap the transcript to the bottom" flick, not a smooth scroll.
    ``clicks=0`` is a no-op and returns False, as does any failure.
    """
    if clicks == 0:
        return False
    delta = WHEEL_DELTA if clicks > 0 else -WHEEL_DELTA
    return _send_at_center(region, [(_MOUSEEVENTF_WHEEL, delta)] * abs(clicks))


def foreground_window() -> int | None:
    """The handle of the window that has focus right now, or None (unsupported
    platform, or Windows says no window has focus - e.g. mid focus switch).

    The TUI calls this while the user is demonstrably typing in AgentClip, so
    the returned handle is the user's own terminal window - the thing
    ``focus_window`` later snaps back to.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND  # default c_int truncates 64-bit handles
    handle = user32.GetForegroundWindow()
    return int(handle) if handle else None


def focus_window(handle: int) -> bool:
    """Bring a previously recorded window (see ``foreground_window``) back to
    the foreground. False = the window is gone, the platform is unsupported,
    or Windows kept focus where it was.

    ``SetForegroundWindow`` is refused for processes that have not received
    input recently, so a zero-effect ALT press+release goes out through
    SendInput first - the long-documented loophole that marks this process as
    "recently interacted with" without typing anything anywhere.
    """
    if sys.platform != "win32" or not handle:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    if not user32.IsWindow(handle):
        return False

    input_struct = _input_struct()
    tap = (input_struct * 2)()
    for event, flags in zip(tap, (0, _KEYEVENTF_KEYUP), strict=True):
        event.type = _INPUT_KEYBOARD
        event.union.ki.wVk = _VK_MENU
        event.union.ki.dwFlags = flags
    user32.SendInput(2, tap, ctypes.sizeof(input_struct))

    user32.SetForegroundWindow(handle)
    current = user32.GetForegroundWindow()
    return bool(current) and int(current) == handle
