"""Small PNG sample for image-input connectivity tests."""

from __future__ import annotations

import base64
import struct
import zlib


VISION_PROMPT = "This is an image input connectivity test. Reply only OK."


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack("!I", len(payload))
        + kind
        + payload
        + struct.pack("!I", zlib.crc32(kind + payload))
    )


def create_vision_sample() -> str:
    colors = ((230, 30, 30), (20, 170, 40), (30, 70, 230), (250, 220, 20))
    row = b"\xff" * 24 + b"".join(bytes(color) * 56 + b"\xff" * 24 for color in colors)
    width, height = 264, 112
    blank = b"\xff" * (width * 3)
    pixels = b"".join(
        b"\0" + (row if 8 <= y < height - 8 else blank) for y in range(height)
    )
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(pixels))
        + _png_chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
