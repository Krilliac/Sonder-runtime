"""Minimal PNG encoding for captured windows, with no imaging dependency."""
from __future__ import annotations

import struct
import zlib


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def encode_bgra(width: int, height: int, pixels: bytes, *, level: int = 6) -> bytes:
    """Encode top-down 32-bit BGRA rows (GDI's layout) as an RGB PNG."""
    if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
        raise ValueError("pixel buffer does not match the stated size")
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        line = pixels[row * stride:(row + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = line[2::4]
        rgb[1::3] = line[1::4]
        rgb[2::3] = line[0::4]
        raw.append(0)
        raw += rgb
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), level)) + _chunk(b"IEND", b""))


def decode_rgb(png: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG written by ``encode_bgra`` (for tests and cropping)."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, width, height, idat = 8, 0, 0, b""
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
            if data[8:10] != b"\x08\x02":
                raise ValueError("only 8-bit RGB is supported")
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3
    out = bytearray()
    for row in range(height):
        start = row * (stride + 1)
        if raw[start] != 0:
            raise ValueError("only unfiltered rows are supported")
        out += raw[start + 1:start + 1 + stride]
    return width, height, bytes(out)


def crop_rgb_png(png: bytes, left: int, top: int, right: int, bottom: int) -> bytes:
    """Crop an ``encode_bgra`` PNG to the box, clamped to the image."""
    width, height, rgb = decode_rgb(png)
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right <= left or bottom <= top:
        raise ValueError("empty crop")
    w, h = right - left, bottom - top
    bgra = bytearray(w * h * 4)
    for row in range(h):
        src = rgb[((top + row) * width + left) * 3:((top + row) * width + right) * 3]
        dst = row * w * 4
        bgra[dst + 2:dst + w * 4:4] = src[0::3]
        bgra[dst + 1:dst + w * 4:4] = src[1::3]
        bgra[dst + 0:dst + w * 4:4] = src[2::3]
    return encode_bgra(w, h, bytes(bgra))
