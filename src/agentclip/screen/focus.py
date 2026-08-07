"""Synthetic mouse input aimed at the middle of a screen region.

Two nudges live here, both "point at the region and act":

* ``click_region`` - the "done processing, hand the user back to the browser"
  focus click. After every outbound clipboard copy the TUI clicks the center of
  the user-drawn chat region, so the chat's input field is focused and a paste
  lands immediately.
* ``scroll_region`` - wheel the chat back to the bottom before hunting for the
  newest copy button (see screen.template), since the copy-button band only
  shows the latest response when the transcript is scrolled down.

Windows-only, pure stdlib: ``SetCursorPos`` + ``SendInput`` via ctypes. Other
platforms return False so the caller can tell the user once instead of failing
on every copy. DPI awareness is forced first (see ``make_dpi_aware``) so these
coordinates live in the same physical-pixel space the overlay measured in.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from agentclip.screen.region import ScreenRegion

_dpi_aware = False

_INPUT_MOUSE = 0
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_WHEEL = 0x0800
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
    """The mouse-only ``INPUT`` struct ``SendInput`` expects.

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

    class InputUnion(ctypes.Union):
        # MOUSEINPUT is the largest member of the real INPUT union, so a
        # mouse-only declaration still has the exact size SendInput expects.
        _fields_ = [("mi", MouseInput)]

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
