"""Read and write captured frames as PNG, with nothing but zlib and struct.

Appearance templates (the stop button, the copy icon) are captured once and
must survive a restart, so they have to land on disk as something a human can
open and eyeball - a raw BGRX blob would be unreviewable and unfixable. Pillow
would do it in three lines and cost the project a compiled dependency plus a
PyInstaller hook; the screen layer is deliberately stdlib-only, and PNG's
uncomplicated path (8-bit, non-interlaced, filter 0) is ~70 lines of zlib.

Not a general-purpose codec on purpose: the decoder accepts what this encoder
writes (RGBA) plus plain RGB, because those are the two things a user might
plausibly drop into a profile folder by hand. Anything else - 16-bit, palettes,
interlacing, a per-scanline filter - is a PngError rather than a silent
mis-decode. Chunk CRCs are not verified: a corrupt file fails at zlib or at the
pixel-count check anyway, and the caller's answer to both is the same (treat
the template as missing).
"""

from __future__ import annotations

import struct
import zlib

from agentclip.screen.capture import RegionImage

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COLOUR_RGB = 2
_COLOUR_RGBA = 6
# zlib level 6 is the default trade: a 40x40 icon compresses in microseconds and
# the file size difference against level 9 is noise at this scale.
_COMPRESS_LEVEL = 6


class PngError(Exception):
    """The bytes are not a PNG this module can read (or write)."""


def _chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def encode_png(image: RegionImage) -> bytes:
    """Serialise a captured frame as an 8-bit RGBA PNG.

    The capture's undefined X byte is written as opaque alpha, so viewers show
    the icon rather than an invisible rectangle.
    """
    width, height = image.width, image.height
    count = width * height
    if width <= 0 or height <= 0:
        raise PngError("image has no area")
    if len(image.pixels) < count * 4:
        raise PngError("pixel buffer is truncated")

    source = image.pixels[: count * 4]
    rgba = bytearray(count * 4)
    rgba[0::4] = source[2::4]  # R
    rgba[1::4] = source[1::4]  # G
    rgba[2::4] = source[0::4]  # B
    rgba[3::4] = b"\xff" * count  # A: the captured X byte carries no alpha

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) on every scanline
        raw += rgba[y * stride : (y + 1) * stride]

    header = struct.pack(">IIBBBBB", width, height, 8, _COLOUR_RGBA, 0, 0, 0)
    return b"".join(
        (
            _SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(raw), _COMPRESS_LEVEL)),
            _chunk(b"IEND", b""),
        )
    )


def decode_png(data: bytes) -> RegionImage:
    """Parse an 8-bit RGB/RGBA non-interlaced PNG back into a BGRX frame.

    The X byte comes back as 0 - it is undefined in a capture, so no comparison
    in this layer reads it.
    """
    if not data.startswith(_SIGNATURE):
        raise PngError("not a PNG (bad signature)")

    header: tuple[int, ...] | None = None
    idat = bytearray()
    pos = len(_SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4 : pos + 8]
        end = pos + 8 + length
        if end + 4 > len(data):
            raise PngError("truncated PNG chunk")
        payload = data[pos + 8 : end]
        if tag == b"IHDR":
            if len(payload) != 13:
                raise PngError("malformed IHDR")
            header = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            idat += payload  # one image may be split over any number of IDATs
        elif tag == b"IEND":
            break
        pos = end + 4  # skip the chunk CRC

    if header is None:
        raise PngError("PNG has no IHDR")
    width, height, depth, colour, compression, filter_method, interlace = header
    if width <= 0 or height <= 0:
        raise PngError("PNG has no area")
    if depth != 8:
        raise PngError(f"unsupported bit depth {depth} (only 8)")
    if colour not in (_COLOUR_RGB, _COLOUR_RGBA):
        raise PngError(f"unsupported colour type {colour} (only 2 and 6)")
    if compression != 0 or filter_method != 0:
        raise PngError("unsupported compression/filter method")
    if interlace != 0:
        raise PngError("interlaced PNGs are not supported")
    if not idat:
        raise PngError("PNG has no image data")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PngError(f"corrupt PNG image data: {exc}") from exc

    channels = 4 if colour == _COLOUR_RGBA else 3
    stride = width * channels
    if len(raw) != height * (stride + 1):
        raise PngError("PNG pixel count does not match its header")

    body = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        if raw[start] != 0:
            raise PngError(f"unsupported scanline filter {raw[start]} (only 0)")
        body += raw[start + 1 : start + 1 + stride]

    count = width * height
    pixels = bytearray(count * 4)  # byte 3 stays 0: undefined in a capture
    pixels[0::4] = body[2::channels]  # B
    pixels[1::4] = body[1::channels]  # G
    pixels[2::4] = body[0::channels]  # R
    return RegionImage(width, height, bytes(pixels))
