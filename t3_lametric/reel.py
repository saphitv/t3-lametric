"""A short vertical digit reel for TIME's full-width streaming canvas."""

from __future__ import annotations

import struct
import uuid

from .icons import DIGITS

FONT = {
    **{str(index): rows for index, rows in enumerate(DIGITS)},
    ".": ("0", "0", "0", "0", "0", "0", "1"),
    "k": ("100", "100", "101", "110", "110", "101", "101"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "B": ("110", "101", "101", "110", "101", "101", "110"),
    "T": ("111", "010", "010", "010", "010", "010", "010"),
}
ROLL_SECONDS = 0.5


def roll_images(previous: str, current: str) -> list[list[int]]:
    if (
        previous == current
        or len(previous) != len(current)
        or any(c not in FONT for c in previous + current)
        or any(
            a != b and not (a.isdigit() and b.isdigit())
            for a, b in zip(previous, current, strict=True)
        )
    ):
        return []
    images = []
    for offset in range(9):
        pixels = [0] * (37 * 8)
        for x, height in enumerate((0, 4, 4, 0, 8, 8, 0, 6, 6)):
            for y in range(8 - height, 8):
                pixels[y * 37 + x] = 1
        x = 10
        for old, new in zip(previous, current, strict=True):
            glyphs = ((new, 0),) if old == new else ((old, -offset), (new, 8 - offset))
            for character, top in glyphs:
                for y, row in enumerate(FONT[character], top):
                    for dx, pixel in enumerate(row):
                        if pixel == "1" and 0 <= y < 8 and x + dx < 37:
                            pixels[y * 37 + x + dx] = 1
            x += len(FONT[new][0]) + 1
        if x > 38:
            return []
        images.append(pixels)
    return images


def stream_packet(session_id: str, pixels: list[int]) -> bytes:
    content = bytes(
        channel for pixel in pixels for channel in ((255, 255, 255) if pixel else (0, 0, 0))
    )
    return (
        b"lmsp"
        + struct.pack("<H", 1)
        + uuid.UUID(session_id).bytes
        + bytes((0, 0, 1, 0))
        + struct.pack("<HHHHH", 0, 0, 37, 8, len(content))
        + content
    )
