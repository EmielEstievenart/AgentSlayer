"""The stdlib PNG codec that persists appearance templates (screen/png.py).

Only the B/G/R channels are asserted on a round-trip: the X byte of a BGRX
capture is undefined, so the encoder writes opaque alpha for it and the decoder
hands back 0 - neither value means anything to a comparison in this layer.
"""

from __future__ import annotations

import struct
import tracemalloc
import zlib

import pytest

from agentclip.screen.capture import RegionImage
from agentclip.screen.png import PngError, decode_png, encode_png

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def gradient(width: int, height: int) -> RegionImage:
    """A frame whose every pixel differs, with a deliberately junk X byte."""
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 7) % 256, (y * 13) % 256, (x + y) % 256, 0xAB))
    return RegionImage(width, height, bytes(pixels))


def bgr(image: RegionImage) -> list[tuple[int, int, int]]:
    """The defined channels only, pixel by pixel."""
    return [
        (image.pixels[i], image.pixels[i + 1], image.pixels[i + 2])
        for i in range(0, image.width * image.height * 4, 4)
    ]


def make_chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def handmade(
    width: int,
    height: int,
    raw: bytes,
    *,
    colour: int = 6,
    depth: int = 8,
    interlace: int = 0,
) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)
    return b"".join(
        (
            SIGNATURE,
            make_chunk(b"IHDR", header),
            make_chunk(b"IDAT", zlib.compress(raw)),
            make_chunk(b"IEND", b""),
        )
    )


def test_round_trip_preserves_size_and_colour() -> None:
    image = gradient(5, 4)
    back = decode_png(encode_png(image))
    assert (back.width, back.height) == (5, 4)
    assert bgr(back) == bgr(image)


def test_the_undefined_x_byte_comes_back_as_zero() -> None:
    back = decode_png(encode_png(gradient(3, 3)))
    assert set(back.pixels[3::4]) == {0}


def test_a_single_pixel_round_trips() -> None:
    image = RegionImage(1, 1, bytes((9, 99, 199, 0)))
    back = decode_png(encode_png(image))
    assert (back.width, back.height, bgr(back)) == (1, 1, [(9, 99, 199)])


def test_a_wide_frame_round_trips() -> None:
    """Row handling is where a stride bug hides, so use a very non-square frame."""
    image = gradient(300, 2)
    back = decode_png(encode_png(image))
    assert (back.width, back.height) == (300, 2)
    assert bgr(back) == bgr(image)


def test_a_tall_frame_round_trips() -> None:
    image = gradient(2, 300)
    assert bgr(decode_png(encode_png(image))) == bgr(image)


def test_rgb_without_alpha_decodes_too() -> None:
    """A user may drop a hand-saved icon into a profile folder; RGB is what
    most tools write when there is no transparency."""
    raw = b"\x00" + bytes((10, 20, 30, 40, 50, 60))  # one row, two RGB pixels
    image = decode_png(handmade(2, 1, raw, colour=2))
    assert bgr(image) == [(30, 20, 10), (60, 50, 40)]


def test_image_data_split_across_several_idat_chunks() -> None:
    """Real encoders split large images; concatenation is the only correct read."""
    image = gradient(20, 20)
    data = encode_png(image)
    head, rest = data[:8], data[8:]
    (ihdr_len,) = struct.unpack_from(">I", rest, 0)
    ihdr = rest[: 12 + ihdr_len]
    (idat_len,) = struct.unpack_from(">I", rest, 12 + ihdr_len)
    payload = rest[12 + ihdr_len + 8 : 12 + ihdr_len + 8 + idat_len]
    assert len(payload) > 4
    cut = len(payload) // 2
    split = b"".join(
        (
            head,
            ihdr,
            make_chunk(b"IDAT", payload[:cut]),
            make_chunk(b"IDAT", payload[cut:]),
            make_chunk(b"IEND", b""),
        )
    )
    assert bgr(decode_png(split)) == bgr(image)


def test_non_png_bytes_raise() -> None:
    with pytest.raises(PngError):
        decode_png(b"this is not a png at all")
    with pytest.raises(PngError):
        decode_png(b"")


def test_truncated_data_raises() -> None:
    data = encode_png(gradient(8, 8))
    with pytest.raises(PngError):
        decode_png(data[: len(data) // 2])


def test_a_nonzero_scanline_filter_raises() -> None:
    """Only filter 0 is supported - anything else would decode to garbage."""
    raw = b"\x01" + bytes((1, 2, 3, 255))
    with pytest.raises(PngError):
        decode_png(handmade(1, 1, raw))


def test_an_interlaced_png_raises() -> None:
    raw = b"\x00" + bytes((1, 2, 3, 255))
    with pytest.raises(PngError):
        decode_png(handmade(1, 1, raw, interlace=1))


def test_an_unsupported_bit_depth_raises() -> None:
    raw = b"\x00" + bytes((1, 2, 3, 255))
    with pytest.raises(PngError):
        decode_png(handmade(1, 1, raw, depth=16))


def test_an_unsupported_colour_type_raises() -> None:
    raw = b"\x00" + bytes((1,))
    with pytest.raises(PngError):
        decode_png(handmade(1, 1, raw, colour=0))  # greyscale


def test_a_wrong_pixel_count_raises() -> None:
    raw = b"\x00" + bytes((1, 2, 3, 255))  # one pixel, header claims four
    with pytest.raises(PngError):
        decode_png(handmade(2, 2, raw))


def test_corrupt_compressed_data_raises() -> None:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    data = b"".join(
        (
            SIGNATURE,
            make_chunk(b"IHDR", header),
            make_chunk(b"IDAT", b"not zlib"),
            make_chunk(b"IEND", b""),
        )
    )
    with pytest.raises(PngError):
        decode_png(data)


def test_a_multi_row_rgb_image_decodes_row_by_row() -> None:
    """Three rows of RGB: the stride a hand-saved icon actually arrives with,
    and where an off-by-one in the filter byte would show up as a skew."""
    rows = [
        bytes((10, 20, 30, 40, 50, 60)),
        bytes((70, 80, 90, 100, 110, 120)),
        bytes((130, 140, 150, 160, 170, 180)),
    ]
    image = decode_png(handmade(2, 3, b"".join(b"\x00" + row for row in rows), colour=2))
    assert (image.width, image.height) == (2, 3)
    assert bgr(image) == [
        (30, 20, 10),
        (60, 50, 40),
        (90, 80, 70),
        (120, 110, 100),
        (150, 140, 130),
        (180, 170, 160),
    ]


def test_a_decompression_bomb_is_just_another_unreadable_png() -> None:
    """A megabyte of zeros compresses to nothing and expands to a gigabyte. The
    decoder must never allocate what a header asks for on faith: this is a
    PngError like any other corrupt file, not a MemoryError that would blow
    straight through load_profile's never-raises contract."""
    bomb = zlib.compress(b"\x00" * (64 << 20))
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)  # claims a 1x1 image
    data = b"".join(
        (
            SIGNATURE,
            make_chunk(b"IHDR", header),
            make_chunk(b"IDAT", bomb),
            make_chunk(b"IEND", b""),
        )
    )
    tracemalloc.start()
    try:
        with pytest.raises(PngError):
            decode_png(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 << 20, "the decoder expanded the whole bomb before rejecting it"


def test_a_header_claiming_more_pixels_than_it_has_raises() -> None:
    """The bomb the other way round: an enormous header over a tiny IDAT, which
    is what a crafted file uses to make the decoder reserve the gigabyte."""
    tiny = zlib.compress(b"\x00" * 16)
    header = struct.pack(">IIBBBBB", 40_000, 40_000, 8, 6, 0, 0, 0)
    data = b"".join(
        (
            SIGNATURE,
            make_chunk(b"IHDR", header),
            make_chunk(b"IDAT", tiny),
            make_chunk(b"IEND", b""),
        )
    )
    with pytest.raises(PngError):
        decode_png(data)


def test_encoding_an_empty_or_truncated_image_raises() -> None:
    with pytest.raises(PngError):
        encode_png(RegionImage(0, 0, b""))
    with pytest.raises(PngError):
        encode_png(RegionImage(4, 4, b"\x00" * 8))
