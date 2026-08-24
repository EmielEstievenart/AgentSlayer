"""The X11 backend for the screen layer: capture and synthetic input on Linux.

``screen.capture`` and ``screen.focus`` are written Windows-first (GDI +
``SendInput`` through ctypes) and answered "unsupported platform" everywhere
else, which made ``agentclip-monitor`` - the half that runs where the *pixels*
are (``docs/design/ui-monitor.md`` S1) - inert on the Linux VM it was meant for.
This module is the other implementation of exactly those primitives, and the
two Windows-first modules dispatch here when ``sys.platform`` says linux. The
public names mirror theirs one for one, so the dispatch is a rename-free
hand-off:

* :func:`capture_region` - ``XGetImage`` on the root window, converted into the
  BGRX byte layout ``capture.RegionImage`` is defined in (see
  :func:`zpixmap_to_bgrx`; X and GDI agree on little-endian BGRX, which is why
  the conversion is usually a copy).
* :func:`virtual_screen_bounds` - the root window's geometry, which under both
  Xinerama and RandR is already the union of every attached monitor, so there
  is nothing cheaper to ask and no extension to query.
* :func:`move_cursor`, :func:`click_region`, :func:`scroll_region` - XTest
  ``fake_input``. Motion is a real ``MotionNotify``, not a warp, for focus.py's
  reason: the hover scan exists to make a browser render its hover-only copy
  button, and only genuine motion drives the ``:hover`` chain reliably. Wheel
  detents are button 4 (up) and 5 (down), X11's spelling of ``WHEEL_DELTA``.
* :func:`send_paste`, :func:`send_enter`, :func:`send_scroll_key` - XTest key
  events, aimed at whatever holds focus, with keysym names resolved to keycodes
  against the running server's map (a Dvorak or AZERTY layout moves ``v``).
* :func:`foreground_window`, :func:`focus_window`,
  :func:`focus_window_verified` - EWMH's ``_NET_ACTIVE_WINDOW``: read as a root
  property, written as a client message to the root window (plus an
  ``XRaiseWindow``), and verified by reading it back - the same "focus is a
  race, not a call" shape focus.py documents.

**X11 only, deliberately.** Under native Wayland there is no protocol for one
client to screenshot another's surface or to synthesise input into it, and a
compositor that offers portals for it asks the user per session; a browser
running under **XWayland** is a normal X client and works. See
``docs/configuration.md``, "Running the monitor on Linux".

python-xlib is imported lazily inside the functions here, exactly like
capture.py's ctypes: importing this module costs nothing and works on any
platform, which is what lets the Windows test suite exercise the pure parts
with a fake ``Xlib`` in ``sys.modules``. A missing ``DISPLAY``, a missing
python-xlib and a refused connection all end the same way - one logged line and
then a :class:`~agentclip.driver.screen.capture.CaptureError` (capture) or a
plain ``False`` (input) - never a traceback out of a poll tick.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.region import ScreenRegion

_log = logging.getLogger(__name__)

# X11's wheel buttons. Positive clicks scroll UP, matching ``focus.scroll_region``
# (and Windows' own sign convention) rather than X's button numbering, so
# callers keep one sign for both platforms.
BUTTON_WHEEL_UP = 4
BUTTON_WHEEL_DOWN = 5
BUTTON_LEFT = 1

# Keysym NAMES (not keycodes: those are per-server and looked up at call time).
# The scroll keys are spelled by the same names ``focus.SCROLL_KEYS`` uses -
# config is the source of those names and a test asserts the two maps agree, the
# same arrangement focus.py describes for MATCHERS.
SCROLL_KEYSYMS: dict[str, str] = {"page_down": "Page_Down", "end": "End"}
PASTE_KEYSYMS = ("Control_L", "v")
ENTER_KEYSYM = "Return"

# _NET_ACTIVE_WINDOW's source indication. 2 = "pager": window managers treat a
# pager's request as a user-driven switch and honour it, where an application's
# (1) is what focus-stealing prevention exists to refuse. AgentClip is asking on
# the user's behalf - they clicked in the browser and want their window back.
_SOURCE_PAGER = 2

# ``focus_window_verified``'s budget, kept beside focus.py's own numbers rather
# than imported: the reason for each is the same (outlast a browser's own
# activation, give up before the flow that asked hangs), and this module may not
# import focus.py - focus.py imports THIS one.
REFOCUS_ATTEMPTS = 4
REFOCUS_SETTLE_S = 0.05

# One connection, shared by every caller, behind a reentrant lock: capture runs
# concurrently here (busy-probe worker + hover scan, capture.py's ``_gdi`` note),
# and python-xlib's Display multiplexes one socket that is not safe to
# interleave. RLock because the accessors below take it around calls that open
# the connection themselves.
_display_lock = threading.RLock()
_display_state: Any | None = None
_display_failure: str | None = None


def _unavailable(reason: str) -> None:
    """Record why X11 is unreachable and say so exactly once.

    Once is the point: every one of these callers sits on a 0.5 s poll tick, and
    a monitor started outside a graphical session would otherwise write the same
    line a hundred times a minute.
    """
    global _display_failure
    if _display_failure is None:
        _display_failure = reason
        _log.warning("X11 is unavailable: %s", reason)


def _display() -> Any | None:
    """The shared X display, opened on first use. None when there is none.

    Failure is sticky: a monitor launched without a desktop does not get one
    later in the same process, and retrying the connect per tick would just be a
    slower way to answer False.
    """
    global _display_state
    if _display_state is not None:
        return _display_state
    with _display_lock:
        if _display_state is not None:
            return _display_state
        if _display_failure is not None:
            return None
        if not os.environ.get("DISPLAY"):
            _unavailable("DISPLAY is not set - the monitor needs an X11 desktop")
            return None
        try:
            from Xlib import display as xdisplay
        except ImportError as exc:
            _unavailable(f"python-xlib is not installed ({exc})")
            return None
        try:
            _display_state = xdisplay.Display()
        except Exception as exc:  # DisplayConnectionError, DisplayNameError, OSError...
            _unavailable(f"cannot open the display {os.environ.get('DISPLAY')!r} ({exc})")
            return None
        return _display_state


def _why_unavailable() -> str:
    return _display_failure or "no X display"


# == capture ==================================================================


def row_stride(width: int, bits_per_pixel: int, scanline_pad: int) -> int:
    """Bytes per scanline in a ZPixmap.

    X pads every row up to ``scanline_pad`` BITS (32 on every mainstream
    server), so a 24-bit-deep row is not simply ``width * 3``.
    """
    bits = width * bits_per_pixel
    units = (bits + scanline_pad - 1) // scanline_pad
    return units * scanline_pad // 8


def zpixmap_to_bgrx(
    data: bytes,
    width: int,
    height: int,
    *,
    bits_per_pixel: int = 32,
    scanline_pad: int = 32,
    msb_first: bool = False,
) -> bytes:
    """A ZPixmap buffer as ``RegionImage`` bytes: BGRX, top-down, no row padding.

    The one place the two backends have to be made to agree. GDI hands
    capture.py a tightly packed, top-down, little-endian BGRX buffer; X hands
    back rows padded to ``scanline_pad`` bits, in whichever channel order and
    byte order the server uses. Both are top-down already, so the rows never
    move - only their padding is cut and, where the server disagrees, the
    channels are re-ordered.

    The fourth byte is written as zero rather than kept as the server's. It is
    undefined on the Windows side too (capture.py asks for BI_RGB at 32bpp) and
    every consumer treats it that way: ``png.encode`` substitutes opaque alpha,
    the matchers read three channels. Zeroing it just makes two captures of the
    same pixels compare equal, which the busy detector depends on.
    """
    if width <= 0 or height <= 0:
        raise CaptureError("region has no area")
    if bits_per_pixel not in (24, 32):
        raise CaptureError(f"unsupported X11 pixel format ({bits_per_pixel} bits per pixel)")
    stride = row_stride(width, bits_per_pixel, scanline_pad)
    if len(data) < stride * height:
        raise CaptureError("XGetImage returned a truncated buffer")

    step = bits_per_pixel // 8
    if step == 4 and not msb_first and stride == width * 4:
        # The common case on every little-endian server: the buffer already IS
        # what RegionImage wants, so this is a copy plus one strided fill.
        packed = bytearray(data[: width * height * 4])
        packed[3::4] = bytes(width * height)
        return bytes(packed)

    # Byte position of B, G, R inside one source pixel. LSBFirst puts the low
    # byte (blue) first for both depths; MSBFirst reverses the pixel, which
    # leaves blue last at 32bpp (X,R,G,B) and third at 24bpp (R,G,B).
    if msb_first:
        offsets = (3, 2, 1) if step == 4 else (2, 1, 0)
    else:
        offsets = (0, 1, 2)

    out = bytearray(width * height * 4)
    span = width * step
    for row in range(height):
        line = data[row * stride : row * stride + span]
        dest = row * width * 4
        for channel, source in enumerate(offsets):
            out[dest + channel : dest + width * 4 : 4] = line[source::step]
    return bytes(out)


def _pixmap_format(display: Any, depth: int) -> tuple[int, int]:
    """``(bits_per_pixel, scanline_pad)`` the server uses for images of ``depth``.

    Falls back to the universal 32/32 rather than failing: every server this
    runs on advertises a 24-deep format at 32bpp, and a missing entry is not
    worth turning into a capture error.
    """
    for entry in getattr(display.info, "pixmap_formats", ()):
        if int(entry.depth) == int(depth):
            return int(entry.bits_per_pixel), int(entry.scanline_pad)
    return 32, 32


def capture_region(region: ScreenRegion) -> RegionImage:
    """Grab the region's current pixels off the root window.

    Raises CaptureError when X11 is unreachable or the grab fails - the same
    contract capture.py's GDI path has, and the detector already reports both as
    ERROR rather than a match verdict.
    """
    width, height = int(region.width), int(region.height)
    if width <= 0 or height <= 0:
        raise CaptureError("region has no area")
    display = _display()
    if display is None:
        raise CaptureError(f"screen capture needs an X11 display: {_why_unavailable()}")

    from Xlib import X

    try:
        with _display_lock:
            root = display.screen().root
            # Root coordinates: X has no negative virtual-screen origin the way
            # Windows does - the root window IS the whole desktop and starts at
            # (0, 0) - so region.left/top go straight through.
            raw = root.get_image(
                int(region.left), int(region.top), width, height, X.ZPixmap, 0xFFFFFFFF
            )
            bits_per_pixel, scanline_pad = _pixmap_format(display, raw.depth)
            msb_first = int(getattr(display.info, "image_byte_order", 0)) == 1
    except Exception as exc:
        raise CaptureError(f"X11 screen capture failed: {exc}") from exc

    data = raw.data
    if isinstance(data, str):  # python-xlib < 0.15 handed back a byte string
        data = data.encode("latin-1")
    pixels = zpixmap_to_bgrx(
        data,
        width,
        height,
        bits_per_pixel=bits_per_pixel,
        scanline_pad=scanline_pad,
        msb_first=msb_first,
    )
    return RegionImage(width, height, pixels)


def virtual_screen_bounds() -> tuple[int, int, int, int] | None:
    """``(left, top, width, height)`` of the whole desktop, or None with no X11.

    The root window's geometry, which is the union of every monitor under both
    Xinerama and RandR - so unlike Windows there is no separate "virtual screen"
    metric to ask for, and ``left``/``top`` are 0 rather than negative.
    """
    display = _display()
    if display is None:
        return None
    try:
        with _display_lock:
            geometry = display.screen().root.get_geometry()
    except Exception as exc:
        _log.debug("X11 root geometry failed: %s", exc)
        return None
    return (int(geometry.x), int(geometry.y), int(geometry.width), int(geometry.height))


# == synthetic input ==========================================================


def _fake_input(display: Any, event_type: int, detail: int = 0, x: int = 0, y: int = 0) -> bool:
    """Push one XTest event and flush it. False = it did not go out.

    THE injection choke point for this backend, and deliberately the only one:
    the suite-wide OS gate (``tests/conftest.py``) neuters this single function
    on Linux the way it neuters ``windll.user32.SendInput`` on Windows, and
    every click, wheel detent, cursor move and keystroke below then reports the
    plain False every caller already handles.
    """
    from Xlib.ext import xtest

    try:
        xtest.fake_input(display, event_type, detail, x=x, y=y)
        display.sync()
    except Exception as exc:
        _log.debug("XTest fake_input failed: %s", exc)
        return False
    return True


def _keycode(display: Any, keysym_name: str) -> int | None:
    """The running server's keycode for a keysym NAME, or None if unmapped.

    Resolved per call against the live map rather than tabulated: the keycode
    for ``v`` is a property of the user's layout, not of X11.
    """
    from Xlib import XK

    try:
        keysym = XK.string_to_keysym(keysym_name)
        if not keysym:
            return None
        keycode = int(display.keysym_to_keycode(keysym))
    except Exception as exc:
        _log.debug("no keycode for %s: %s", keysym_name, exc)
        return None
    return keycode or None


def _send_keys(names: list[str]) -> bool:
    """Press every named key in order, then release them in reverse.

    The nesting is what makes ``["Control_L", "v"]`` a Ctrl+V rather than two
    unrelated taps, and it is why this takes a list of names instead of the
    (virtual-key, flags) pairs focus.py's Windows twin sends: XTest has no
    burst, so the ordering has to be expressed here.
    """
    display = _display()
    if display is None or not names:
        return False
    from Xlib import X

    keycodes = [_keycode(display, name) for name in names]
    if any(code is None for code in keycodes):
        return False
    codes = [int(code or 0) for code in keycodes]
    with _display_lock:
        for code in codes:
            if not _fake_input(display, X.KeyPress, code):
                return False
        for code in reversed(codes):
            if not _fake_input(display, X.KeyRelease, code):
                return False
    return True


def move_cursor(x: int, y: int) -> bool:
    """Park the real pointer on ``(x, y)``. False = nothing moved.

    A ``MotionNotify`` through XTest rather than ``XWarpPointer``: focus.py
    chose SendInput over SetCursorPos for the same reason, and it is the same
    reason here - a warped pointer does not reliably make a browser fire its
    ``:hover``, and the hover scan exists precisely to do that.
    """
    display = _display()
    if display is None:
        return False
    from Xlib import X

    return _fake_input(display, X.MotionNotify, 0, x=int(x), y=int(y))


def _click_at_center(
    region: ScreenRegion, button: int, repeats: int = 1, *, settle_s: float = 0.0
) -> bool:
    """Move to the region's centre, wait ``settle_s``, then tap ``button``.

    ``settle_s`` blocks the calling thread, which is fine for focus.py's reason:
    callers do this off the UI thread, and a hover-rendered target only exists
    once the page has processed the move.
    """
    display = _display()
    if display is None or repeats < 1:
        return False
    from Xlib import X

    x, y = region.center
    if not _fake_input(display, X.MotionNotify, 0, x=int(x), y=int(y)):
        return False
    if settle_s > 0:
        time.sleep(settle_s)
    with _display_lock:
        for _ in range(repeats):
            if not _fake_input(display, X.ButtonPress, button):
                return False
            if not _fake_input(display, X.ButtonRelease, button):
                return False
    return True


def click_region(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
    """Move the cursor to the region's centre and left-click."""
    return _click_at_center(region, BUTTON_LEFT, settle_s=settle_s)


def scroll_button(clicks: int) -> int | None:
    """The X button for ``clicks`` wheel detents, or None for zero.

    Positive is UP, matching ``focus.scroll_region``'s documented sign, so the
    two backends read the same from the caller's side.
    """
    if clicks > 0:
        return BUTTON_WHEEL_UP
    if clicks < 0:
        return BUTTON_WHEEL_DOWN
    return None


def scroll_region(region: ScreenRegion, clicks: int) -> bool:
    """Move to the region's centre and turn the wheel ``clicks`` detents -
    negative scrolls DOWN, positive UP. ``clicks=0`` is a no-op (False)."""
    button = scroll_button(clicks)
    if button is None:
        return False
    return _click_at_center(region, button, abs(clicks))


def send_paste() -> bool:
    """Type Ctrl+V into whatever window has focus right now."""
    return _send_keys(list(PASTE_KEYSYMS))


def send_enter() -> bool:
    """Type Enter into whatever window has focus right now."""
    return _send_keys([ENTER_KEYSYM])


def send_scroll_key(key: str, taps: int = 1) -> bool:
    """Tap a scroll key (``"page_down"`` / ``"end"``) ``taps`` times."""
    name = SCROLL_KEYSYMS.get(key)
    if name is None or taps < 1:
        return False
    sent = True
    for _ in range(taps):
        sent = _send_keys([name]) and sent
    return sent


# == window focus =============================================================


def foreground_window() -> int | None:
    """The X id of the window that has focus right now, or None.

    EWMH's ``_NET_ACTIVE_WINDOW`` on the root, not ``XGetInputFocus``: the
    latter answers with whatever child widget holds the focus inside a client,
    which is not a handle ``focus_window`` can hand back to the window manager.
    """
    display = _display()
    if display is None:
        return None
    from Xlib import X

    try:
        with _display_lock:
            root = display.screen().root
            atom = display.intern_atom("_NET_ACTIVE_WINDOW")
            prop = root.get_full_property(atom, X.AnyPropertyType)
    except Exception as exc:
        _log.debug("_NET_ACTIVE_WINDOW read failed: %s", exc)
        return None
    if prop is None or not getattr(prop, "value", None):
        return None
    return int(prop.value[0]) or None


def _activate(display: Any, handle: int) -> bool:
    """Ask the window manager to activate ``handle``, and raise it. False = the
    window is gone or the request could not be sent.

    The second injection seam, and the second thing the OS gate neuters on
    Linux: this is the call that can yank a window in front of whatever the user
    is looking at, exactly as ``SetForegroundWindow`` can on Windows.

    Both halves are needed. The client message is what an EWMH window manager
    obeys; ``XRaiseWindow`` (a configure with ``stack_mode=Above``) is what
    stacks it in front under a bare or non-conforming one.
    """
    from Xlib import X
    from Xlib.protocol import event as xevent

    try:
        with _display_lock:
            root = display.screen().root
            window = display.create_resource_object("window", handle)
            window.get_attributes()  # BadWindow here = the window is gone
            message = xevent.ClientMessage(
                window=window,
                client_type=display.intern_atom("_NET_ACTIVE_WINDOW"),
                data=(32, [_SOURCE_PAGER, X.CurrentTime, 0, 0, 0]),
            )
            root.send_event(
                message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask
            )
            window.configure(stack_mode=X.Above)
            display.sync()
    except Exception as exc:
        _log.debug("activating window %s failed: %s", handle, exc)
        return False
    return True


def focus_window(handle: int) -> bool:
    """Bring a recorded window back to the foreground. False = it did not come.

    No ALT-tap loophole to mirror here: X11 has no input-recency rule, only the
    window manager's own focus-stealing prevention, which ``_activate`` answers
    by asking as a pager.
    """
    display = _display()
    if display is None or not handle:
        return False
    if not _activate(display, handle):
        return False
    return foreground_window() == handle


def focus_window_verified(
    handle: int, *, attempts: int = REFOCUS_ATTEMPTS, settle_s: float = REFOCUS_SETTLE_S
) -> bool:
    """``focus_window`` until the window ACTUALLY holds the foreground.

    Same growing settle as focus.py's Windows twin, for the same race: the
    browser we just clicked in is still activating itself, and a window manager
    can grant the switch and then hand focus straight back.
    """
    if not handle or _display() is None:
        return False
    for attempt in range(1, attempts + 1):
        focus_window(handle)
        time.sleep(settle_s * attempt)  # let a competing activation land first
        if foreground_window() == handle:
            return True
    return False
