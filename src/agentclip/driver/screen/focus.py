"""Synthetic input and window-focus nudges for the auto-copy dance.

Region-aimed input, both "point at the region and act":

* ``click_region`` - the "done processing, hand the user back to the browser"
  focus click. After every outbound clipboard copy the TUI clicks the center of
  the user-drawn chat region, so the chat's input field is focused and a paste
  lands immediately. Its ``settle_s`` lets the cursor hover for a beat before
  the button goes down: web chat UIs render their copy button on hover, and a
  click that arrives in the same instant as the cursor can land on nothing.
* ``scroll_region`` - wheel the chat back to the bottom before hunting for the
  newest copy button (see screen.template), since the copy-button band only
  shows the latest response when the transcript is scrolled down.

Point-aimed input:

* ``move_cursor`` - park the real pointer on a virtual-screen point. Claude's
  chat only *renders* a response's copy button while the pointer hovers that
  response, so the copy hunt has to drag the cursor up the transcript and
  re-look after every stop (screen.hover picks the stops). It is also what
  parks the pointer over the transcript before the scroll below, for the pages
  that scroll only the pane under the cursor. This has to be a synthetic mouse
  MOVE, not ``SetCursorPos``: a teleported pointer does not reliably make the
  browser fire its ``:hover``.

Keyboard input, aimed at whatever has focus rather than at a region:

* ``send_paste`` - a synthetic Ctrl+V, so the TUI can drop the outbound payload
  into the chat's input field itself right after ``click_region`` focused it.
* ``send_enter`` - a synthetic Enter, the opt-in "auto submit" tap that sends
  what ``send_paste`` just dropped in (``ServicePreset.auto_submit``).
* ``send_scroll_key`` - Page Down / End taps, the opt-in keyboard alternative
  to ``scroll_region``'s wheel flick (``ServicePreset.scroll_action``) for
  pages the wheel does not reach.

Window focus, for snapping back to AgentClip after the auto-copy click:

* ``foreground_window`` - the handle of whatever window has focus right now;
  the TUI records its own terminal with this while the user is typing in it.
* ``focus_window`` - bring a recorded window back to the foreground. Windows
  refuses ``SetForegroundWindow`` from a background process, so a zero-effect
  ALT tap is sent first - the documented input-recency loophole.
* ``focus_window_verified`` - the same ask, repeated until the window really
  holds the foreground. Every caller here snaps back right after clicking in a
  browser, and the browser is still activating itself from that click: one
  request loses the race often enough that "back to AgentClip" silently did not
  happen. This is the form the TUI uses.

Windows first, pure stdlib: ``SetCursorPos`` + ``SendInput`` via ctypes. On
Linux every one of these hands off to ``screen.x11`` (XTest + EWMH, the same
primitives spelled in X11) so the monitor works on the VM it was designed to
run on; any OTHER platform still returns False/None so the caller can tell the
user once instead of failing on every copy. DPI awareness is forced first (see
``make_dpi_aware``) so these coordinates live in the same physical-pixel space
the overlay measured in - a Windows-only concern, and a no-op elsewhere.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from agentclip.driver.screen.region import ScreenRegion

_dpi_aware = False

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_WHEEL = 0x0800
# ABSOLUTE makes dx/dy a 0..65535 normalized point instead of a delta;
# VIRTUALDESK spreads that range over the WHOLE multi-monitor desktop rather
# than the primary screen, which is the only way to aim at a monitor sitting
# left of / above the primary (negative coordinates).
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_ABSOLUTE_RANGE = 65535
_KEYEVENTF_KEYUP = 0x0002
_VK_MENU = 0x12  # the ALT key
_VK_CONTROL = 0x11
_VK_V = 0x56
_VK_RETURN = 0x0D
_VK_NEXT = 0x22  # Page Down
_VK_END = 0x23

# The keyboard alternatives to the wheel flick, by the names config.py's
# SCROLL_ACTIONS uses for them (config is a stdlib-only leaf that may not
# import this layer, so the names are spelled twice and a test asserts they
# agree - the same arrangement as MATCHERS).
SCROLL_KEYS: dict[str, int] = {"page_down": _VK_NEXT, "end": _VK_END}
# One wheel detent, as Windows defines it. Negative scrolls down.
WHEEL_DELTA = 120


def _x11() -> Any | None:
    """The Linux X11 backend, or None where this layer has no implementation.

    The single platform switch every function below shares: on Linux it hands
    the call to ``screen.x11``, on macOS (and anything else) it answers None and
    the caller keeps its documented False/None. Imported inside the call, like
    every ctypes import here, so nothing platform-specific happens at import
    time - and so ``screen.x11`` may import ``screen.capture``, which imports
    this module.
    """
    if not sys.platform.startswith("linux"):
        return None
    from agentclip.driver.screen import x11

    return x11


def make_dpi_aware() -> None:
    """Opt this process out of DPI virtualization (Windows; no-op elsewhere).

    Without it, a scaled monitor makes Windows silently rescale cursor APIs and
    tkinter geometry, so the overlay and the click would disagree about where a
    pixel is. Harmless for the TUI itself - the console window belongs to the
    terminal process, not us. Safe to call repeatedly (a failed/duplicate set
    just leaves whatever awareness is already in effect).

    Nothing to do on Linux: X11 reports one pixel grid to every client and does
    no per-process scaling, so the X11 backend inherits this no-op rather than
    implementing anything.
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


def virtual_screen_bounds() -> tuple[int, int, int, int] | None:
    """``(left, top, width, height)`` of the whole multi-monitor desktop
    (Windows), or None where the caller has to fall back to primary-screen
    metrics of its own.

    ``left``/``top`` are negative when a monitor sits left of / above the
    primary one, which is exactly the case ``move_cursor`` and the overlay both
    have to normalize against. On Linux the X11 backend answers with the root
    window's geometry, whose origin is always (0, 0).
    """
    if sys.platform != "win32":
        backend = _x11()
        return backend.virtual_screen_bounds() if backend else None
    import ctypes

    make_dpi_aware()  # metrics must come back in the same physical pixels
    metrics = ctypes.windll.user32.GetSystemMetrics
    # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
    return (metrics(76), metrics(77), metrics(78), metrics(79))


def move_cursor(x: int, y: int) -> bool:
    """Park the real pointer on the virtual-screen point ``(x, y)``. False =
    nothing moved (unsupported platform, no desktop metrics, or SendInput
    swallowed the event).

    Deliberately a SendInput MOVE rather than ``SetCursorPos``: the hover scan
    exists to make a browser render its hover-only copy button, and only a real
    input event reliably drives the WM_MOUSEMOVE -> ``:hover`` chain. Absolute
    coordinates are normalized 0..``_ABSOLUTE_RANGE`` across the virtual
    desktop, so monitors at negative coordinates work like any other.
    """
    if sys.platform != "win32":
        backend = _x11()
        return backend.move_cursor(x, y) if backend else False
    bounds = virtual_screen_bounds()
    if bounds is None:
        return False
    left, top, width, height = bounds
    if width <= 1 or height <= 1:
        return False
    import ctypes

    # Windows maps the normalized range onto (metric - 1) pixels, and clamping
    # keeps a point just outside the desktop on the nearest edge instead of
    # wrapping to the far one.
    def _normalize(value: int, origin: int, span: int) -> int:
        scaled = round((int(value) - origin) * _ABSOLUTE_RANGE / (span - 1))
        return max(0, min(_ABSOLUTE_RANGE, scaled))

    input_struct = _input_struct()
    burst = (input_struct * 1)()
    burst[0].type = _INPUT_MOUSE
    burst[0].union.mi.dx = _normalize(x, left, width)
    burst[0].union.mi.dy = _normalize(y, top, height)
    burst[0].union.mi.dwFlags = _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
    sent = ctypes.windll.user32.SendInput(1, burst, ctypes.sizeof(input_struct))
    return int(sent) == 1


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


def _send_keys(events: Sequence[tuple[int, int]]) -> bool:
    """Send ``(virtual-key, dwFlags)`` keyboard events in one SendInput burst,
    at whatever window currently has focus. False = nothing was sent
    (unsupported platform, empty burst, or SendInput swallowed events - a
    lower-integrity process cannot type into an elevated one).
    """
    if sys.platform != "win32" or not events:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    input_struct = _input_struct()
    burst = (input_struct * len(events))()
    for event, (key, flags) in zip(burst, events, strict=True):
        event.type = _INPUT_KEYBOARD
        event.union.ki.wVk = key
        event.union.ki.dwFlags = flags
    sent = user32.SendInput(len(events), burst, ctypes.sizeof(input_struct))
    return int(sent) == len(events)


def _send_at_center(
    region: ScreenRegion, events: Sequence[tuple[int, int]], *, settle_s: float = 0.0
) -> bool:
    """Put the cursor at the region's center, wait ``settle_s`` seconds, then
    send ``(dwFlags, mouseData)`` events in one SendInput burst. False = nothing
    was sent (unsupported platform, empty burst, the cursor could not be placed,
    or SendInput swallowed events - a lower-integrity process cannot drive an
    elevated one).

    The settle pause exists for hover-rendered targets: the button under the
    cursor may only exist once the page has processed the mouse-move. It blocks
    the calling thread, which is fine - callers do this off the UI thread.
    """
    if sys.platform != "win32" or not events:
        return False
    import ctypes

    make_dpi_aware()
    user32 = ctypes.windll.user32
    x, y = region.center
    if not user32.SetCursorPos(int(x), int(y)):
        return False
    if settle_s > 0:
        import time

        time.sleep(settle_s)

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


def click_region(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
    """Move the cursor to the region's center and left-click. False = no click
    happened (unsupported platform or the cursor could not be placed).

    ``settle_s`` holds the cursor still for that many seconds before the button
    goes down. Pass it when the target only appears on hover (a chat's copy
    button); leave it at 0 for a plain focus click, which needs no pause.
    """
    if sys.platform != "win32":
        backend = _x11()
        return backend.click_region(region, settle_s=settle_s) if backend else False
    return _send_at_center(
        region, [(_MOUSEEVENTF_LEFTDOWN, 0), (_MOUSEEVENTF_LEFTUP, 0)], settle_s=settle_s
    )


def scroll_region(region: ScreenRegion, clicks: int) -> bool:
    """Move the cursor to the region's center and turn the wheel ``clicks``
    detents - negative scrolls DOWN, positive UP (the OS sign convention).

    All detents go out in a single SendInput burst with no sleeps: the caller
    wants a fast "snap the transcript to the bottom" flick, not a smooth scroll.
    ``clicks=0`` is a no-op and returns False, as does any failure.
    """
    if clicks == 0:
        return False
    if sys.platform != "win32":
        backend = _x11()
        return backend.scroll_region(region, clicks) if backend else False
    delta = WHEEL_DELTA if clicks > 0 else -WHEEL_DELTA
    return _send_at_center(region, [(_MOUSEEVENTF_WHEEL, delta)] * abs(clicks))


def send_paste() -> bool:
    """Type Ctrl+V into whatever window has focus right now. False = nothing was
    typed (unsupported platform, or SendInput refused).

    Deliberately un-aimed: the caller decides where the paste lands by focusing
    it first (``click_region`` on the chat's input field), the same way a user
    clicks before pressing Ctrl+V. All four events - CTRL down, V down, V up,
    CTRL up - go out in one burst so nothing can slip between the modifier and
    the key.
    """
    if sys.platform != "win32":
        backend = _x11()
        return backend.send_paste() if backend else False
    return _send_keys(
        [
            (_VK_CONTROL, 0),
            (_VK_V, 0),
            (_VK_V, _KEYEVENTF_KEYUP),
            (_VK_CONTROL, _KEYEVENTF_KEYUP),
        ]
    )


def send_enter() -> bool:
    """Type Enter into whatever window has focus right now. False = nothing was
    typed (unsupported platform, or SendInput refused).

    Un-aimed for the same reason as ``send_paste``: the caller has just focused
    the chat box and pasted into it, and this is the "auto submit" tap that
    sends the message the way the user's own Enter would. Both events go out in
    one burst.
    """
    if sys.platform != "win32":
        backend = _x11()
        return backend.send_enter() if backend else False
    return _send_keys([(_VK_RETURN, 0), (_VK_RETURN, _KEYEVENTF_KEYUP)])


def send_scroll_key(key: str, taps: int = 1) -> bool:
    """Tap a scroll key (``"page_down"`` / ``"end"``, see ``SCROLL_KEYS``)
    ``taps`` times into whatever window has focus. False = nothing was sent
    (unknown key, ``taps`` < 1, unsupported platform, or SendInput refused).

    The keyboard counterpart of ``scroll_region``'s wheel flick, for pages the
    wheel does not reach. All taps go out in one SendInput burst, matching the
    flick's no-smoothing shape: the caller wants the transcript at the bottom,
    not an animation. Un-aimed like every keyboard send here - the caller
    focuses the chat window first.
    """
    vk = SCROLL_KEYS.get(key)
    if vk is None or taps < 1:
        return False
    if sys.platform != "win32":
        backend = _x11()
        return backend.send_scroll_key(key, taps) if backend else False
    return _send_keys([(vk, 0), (vk, _KEYEVENTF_KEYUP)] * taps)


def foreground_window() -> int | None:
    """The handle of the window that has focus right now, or None (unsupported
    platform, or Windows says no window has focus - e.g. mid focus switch).

    The TUI calls this while the user is demonstrably typing in AgentClip, so
    the returned handle is the user's own terminal window - the thing
    ``focus_window`` later snaps back to.
    """
    if sys.platform != "win32":
        backend = _x11()
        return backend.foreground_window() if backend else None
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
    "recently interacted with" without typing anything anywhere. X11 has no
    such rule; the backend there asks the window manager as a pager instead.
    """
    if sys.platform != "win32":
        backend = _x11()
        return backend.focus_window(handle) if backend and handle else False
    if not handle:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    if not user32.IsWindow(handle):
        return False

    _send_keys([(_VK_MENU, 0), (_VK_MENU, _KEYEVENTF_KEYUP)])

    user32.SetForegroundWindow(handle)
    current = user32.GetForegroundWindow()
    return bool(current) and int(current) == handle


# How many times ``focus_window_verified`` asks, and the beat it waits before
# believing the answer. The beat GROWS with the attempt (0.05s, 0.10s, ...), so
# the whole budget is 0.5s at these defaults - long enough to outlast a
# browser's own activation, short enough that a window that will never come
# forward does not hang the flow that asked.
REFOCUS_ATTEMPTS = 4
REFOCUS_SETTLE_S = 0.05


def focus_window_verified(
    handle: int, *, attempts: int = REFOCUS_ATTEMPTS, settle_s: float = REFOCUS_SETTLE_S
) -> bool:
    """``focus_window`` until that window ACTUALLY holds the foreground. False =
    it still does not, after every attempt (or the platform/handle is refused
    outright).

    Focus is a race here, not a call. Callers ask right after clicking inside a
    browser window, and the browser's own activation from that click can arrive
    *after* our ``SetForegroundWindow`` - so a single request can be granted and
    then quietly taken back. Hence the settle before the check: it verifies the
    state the user ends up in, not the one that existed for an instant.

    Blocks the calling thread for up to ``attempts``/``settle_s`` worth of
    beats, which is fine - callers do this off the UI thread, and the budget is
    documented above.
    """
    if sys.platform != "win32":
        backend = _x11()
        if backend is None or not handle:
            return False
        return backend.focus_window_verified(handle, attempts=attempts, settle_s=settle_s)
    if not handle:
        return False
    import time

    for attempt in range(1, attempts + 1):
        focus_window(handle)
        time.sleep(settle_s * attempt)  # let a competing activation land first
        if foreground_window() == handle:
            return True
    return False
