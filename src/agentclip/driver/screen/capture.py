"""Capture the pixels of a screen region (Windows GDI via ctypes, pure stdlib;
X11 via ``screen.x11`` on Linux).

Feeds the busy detector (screen.busy): the calibration snapshot and every later
probe both come from here. Same physical-pixel coordinate space as the overlay
and the focus click, so ``make_dpi_aware`` runs first.

:func:`crop` is here for the same reason :class:`RegionImage` is: it is the one
operation every consumer of a captured frame performs on it - cutting a matched
rectangle back out - and it belongs beside the buffer rather than inside
whichever UI happens to draw the result (both shells do now).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

from agentclip.driver.screen.focus import make_dpi_aware
from agentclip.driver.screen.region import ScreenRegion

_SRCCOPY = 0x00CC0020
# CAPTUREBLT: include layered/transparent windows. The chat runs in a browser
# whose stop button can sit in a layered surface, so without this the region
# would capture as whatever is behind it.
_CAPTUREBLT = 0x40000000
_BI_RGB = 0
_DIB_RGB_COLORS = 0


@dataclass(frozen=True, slots=True)
class RegionImage:
    """One captured frame: BGRX bytes, top-down rows, ``width * height * 4`` long."""

    width: int
    height: int
    pixels: bytes


class CaptureError(Exception):
    """The region could not be captured (unsupported platform or GDI failure)."""


# GDI bindings built exactly once (struct classes, DLL handles, argtypes) and
# shared by every capture thread. Rebuilding them per call raced: capture runs
# concurrently (busy-probe worker + hover scan), each call minted a fresh
# BitmapInfo class and rewrote argtypes on the process-global windll cache, so
# one thread's byref(info) hit another thread's POINTER(BitmapInfo) and ctypes
# rejected it as a different class ("expected LP_BitmapInfo instance").
_gdi_state = None
_gdi_lock = threading.Lock()


def _gdi():
    """Return (user32, gdi32, BitmapInfoHeader, BitmapInfo), initialised once.

    Private WinDLL instances, not ctypes.windll: the global cache shares one
    function object (and its argtypes) with every other module in the process.
    """
    global _gdi_state
    state = _gdi_state
    if state is not None:
        return state
    with _gdi_lock:
        if _gdi_state is not None:
            return _gdi_state

        import ctypes  # lazy, like screen.focus: nothing Windows-only at import time
        from ctypes import wintypes

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BitmapInfo(ctypes.Structure):
            # BI_RGB at 32bpp uses no palette, but GetDIBits still writes through
            # bmiColors, so the trailing entries must exist.
            _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            # Handles are pointer-sized; without explicit restypes ctypes truncates
            # them to a C int on 64-bit and every later call fails.
            user32.GetDC.restype = wintypes.HDC
            user32.GetDC.argtypes = [wintypes.HWND]
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            gdi32.CreateCompatibleDC.restype = wintypes.HDC
            gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
            gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
            gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
            gdi32.SelectObject.restype = wintypes.HGDIOBJ
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
            gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            gdi32.DeleteDC.argtypes = [wintypes.HDC]
            gdi32.BitBlt.argtypes = [
                wintypes.HDC,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HDC,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.DWORD,
            ]
            gdi32.GetDIBits.argtypes = [
                wintypes.HDC,
                wintypes.HBITMAP,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.c_void_p,
                ctypes.POINTER(BitmapInfo),
                wintypes.UINT,
            ]
        except (AttributeError, OSError) as exc:
            raise CaptureError(f"GDI is unavailable: {exc}") from exc

        _gdi_state = (user32, gdi32, BitmapInfoHeader, BitmapInfo)
        return _gdi_state


def capture_region(region: ScreenRegion) -> RegionImage:
    """Grab the region's current pixels from the virtual screen.

    Raises CaptureError on an unsupported platform and on any GDI failure -
    callers treat both the same way (the detector reports ERROR instead of a
    match verdict).

    Linux is served by ``screen.x11`` (XGetImage), which produces the same
    RegionImage layout; every other platform still refuses outright.
    """
    if sys.platform != "win32":
        if sys.platform.startswith("linux"):
            from agentclip.driver.screen import x11  # lazy: it imports THIS module

            return x11.capture_region(region)
        raise CaptureError("screen capture needs Windows or a Linux X11 desktop")
    width, height = int(region.width), int(region.height)
    if width <= 0 or height <= 0:
        raise CaptureError("region has no area")

    import ctypes

    make_dpi_aware()  # capture in the same physical pixels the overlay measured
    user32, gdi32, BitmapInfoHeader, BitmapInfo = _gdi()

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise CaptureError("could not open a screen device context")
    try:
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not mem_dc:
            raise CaptureError("could not create a memory device context")
        try:
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not bitmap:
                raise CaptureError("could not allocate a capture bitmap")
            try:
                previous = gdi32.SelectObject(mem_dc, bitmap)
                try:
                    # region.left/top are virtual-screen coordinates and may be
                    # negative on a multi-monitor desktop; GetDC(None) spans it.
                    copied = gdi32.BitBlt(
                        mem_dc,
                        0,
                        0,
                        width,
                        height,
                        screen_dc,
                        int(region.left),
                        int(region.top),
                        _SRCCOPY | _CAPTUREBLT,
                    )
                    if not copied:
                        raise CaptureError("BitBlt failed to copy the region")
                finally:
                    # GetDIBits requires the bitmap not be selected into a DC.
                    if previous:
                        gdi32.SelectObject(mem_dc, previous)

                info = BitmapInfo()
                info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
                info.bmiHeader.biWidth = width
                info.bmiHeader.biHeight = -height  # negative: top-down rows
                info.bmiHeader.biPlanes = 1
                info.bmiHeader.biBitCount = 32
                info.bmiHeader.biCompression = _BI_RGB
                buffer = ctypes.create_string_buffer(width * height * 4)
                rows = gdi32.GetDIBits(
                    mem_dc,
                    bitmap,
                    0,
                    height,
                    buffer,
                    ctypes.byref(info),
                    _DIB_RGB_COLORS,
                )
                if int(rows) != height:
                    raise CaptureError("GetDIBits returned no pixels for the region")
                return RegionImage(width, height, buffer.raw)
            finally:
                gdi32.DeleteObject(bitmap)
        finally:
            gdi32.DeleteDC(mem_dc)
    except (OSError, ctypes.ArgumentError) as exc:  # a ctypes-level failure anywhere in the GDI chain
        raise CaptureError(f"screen capture failed: {exc}") from exc
    finally:
        user32.ReleaseDC(None, screen_dc)


def crop(image: RegionImage, x: int, y: int, width: int, height: int) -> RegionImage:
    """The ``width x height`` rectangle of ``image`` at ``(x, y)``, clamped to it.

    The cutter both ELEMENTS panels are built on: a template search answers
    *where* the appearance is (``screen.template.RegionMatch``, scene-local),
    and this turns that answer back into the handful of pixels that actually
    matched, so a UI can show the user the thing the detector recognised instead
    of a coordinate. It lives here, beside the type it cuts, because it is a
    pure function over a captured buffer with nothing terminal (and nothing
    graphical) about it - it was ``shell.tui.pixels.crop`` while the TUI was the only
    shell, and the two shells may not import each other.

    Clamped rather than checked, and empty rather than raised, for the same
    reason ``tui.pixels.downsample`` is: this runs on a poll timer over a
    rectangle a moving browser produced, and the caller's answer to every
    degenerate case - a match reported off the edge of a frame, a zero-size
    request, a truncated buffer - is the same one it already has for "there was
    no match". The returned image is whatever part of the request the source
    really holds, so its ``width``/``height`` may be smaller than asked for and
    zero when the rectangle and the image do not overlap at all.
    """
    src_w, src_h = image.width, image.height
    if src_w <= 0 or src_h <= 0 or len(image.pixels) < src_w * src_h * 4:
        return RegionImage(0, 0, b"")
    left, top = max(0, x), max(0, y)
    right, bottom = min(src_w, x + width), min(src_h, y + height)
    out_w, out_h = right - left, bottom - top
    if out_w <= 0 or out_h <= 0:
        return RegionImage(0, 0, b"")
    src = image.pixels
    out = bytearray(out_w * out_h * 4)
    for row in range(out_h):
        start = ((top + row) * src_w + left) * 4
        offset = row * out_w * 4
        out[offset : offset + out_w * 4] = src[start : start + out_w * 4]
    return RegionImage(out_w, out_h, bytes(out))
