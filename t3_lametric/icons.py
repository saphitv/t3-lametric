"""Draw counts in TIME's 8x8 color section; its text section is white-only."""

from __future__ import annotations

import base64
import struct
import zlib
from functools import lru_cache
from math import atan2, hypot, tau

BLUE = (0, 128, 255)
GREEN = (40, 220, 100)
AMBER = (255, 160, 0)
RED = (255, 60, 80)
DIGITS = (
    ("111", "101", "101", "101", "101", "101", "111"),
    ("010", "110", "010", "010", "010", "010", "111"),
    ("111", "001", "001", "111", "100", "100", "111"),
    ("111", "001", "001", "111", "001", "001", "111"),
    ("101", "101", "101", "111", "001", "001", "001"),
    ("111", "100", "100", "111", "001", "001", "111"),
    ("111", "100", "100", "111", "101", "101", "111"),
    ("111", "001", "001", "010", "010", "010", "010"),
    ("111", "101", "101", "111", "101", "101", "111"),
    ("111", "101", "101", "111", "001", "001", "111"),
)


@lru_cache(maxsize=1)
def token_icon() -> str:
    # Three two-column bars, one left column, and single-column gaps. The
    # ninth white column extends into the spacer before the text area.
    heights = (0, 4, 4, 0, 8, 8, 0, 6, 6)
    pixels = [[int(y >= 8 - height) for height in heights] for y in range(8)]
    data = _png(pixels, (255, 255, 255))
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _png(pixels: list[list[int]], color: tuple[int, int, int] = BLUE) -> bytes:
    height, width = len(pixels), len(pixels[0])
    if not height or any(len(row) != width for row in pixels):
        raise ValueError("PNG rows must have equal widths")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"".join(
        b"\x00" + bytes(channel for pixel in row for channel in (color if pixel else (0, 0, 0)))
        for row in pixels
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _gif(
    images: list[list[int]], delays: list[int], palette: tuple[tuple[int, int, int], ...]
) -> bytes:
    if len(palette) not in (2, 4, 8, 128):
        raise ValueError("Icons support two, four, eight, or 128 colors")
    output = bytearray(
        b"GIF89a" + struct.pack("<HHBBB", 8, 8, 0x80 | (len(palette).bit_length() - 2), 0, 0)
    )
    output.extend(bytes(channel for color in palette for channel in color))
    output.extend(b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")
    for pixels, delay in zip(images, delays, strict=True):
        output.extend(b"\x21\xf9\x04\x00" + struct.pack("<H", delay) + b"\x00\x00")
        output.extend(b"\x2c" + struct.pack("<HHHHB", 0, 0, 8, 8, 0))
        # A clear before each literal keeps LZW codes at a fixed width,
        # without needing a compression dependency for these tiny images.
        minimum = max(2, (len(palette) - 1).bit_length())
        clear = 1 << minimum
        bits = minimum + 1
        codes = [code for pixel in pixels for code in (clear, pixel)] + [clear + 1]
        packed = sum(code << (bits * i) for i, code in enumerate(codes))
        data = packed.to_bytes((bits * len(codes) + 7) // 8, "little")
        output.extend(bytes((minimum, len(data))) + data + b"\x00")
    return bytes(output + b";")


def _scrolling_gif(rows: list[str]) -> bytes:
    last = len(rows[0]) - 8
    images = [
        [int(pixel) for row in rows for pixel in row[offset : offset + 8]] + [0] * 8
        for offset in range(last + 1)
    ]
    return _gif(images, [50 if i in (0, last) else 10 for i in range(last + 1)], ((0, 0, 0), BLUE))


@lru_cache(maxsize=8)
def symbol(name: str) -> str:
    patterns = {
        "done": (
            GREEN,
            (
                "00000000",
                "00000001",
                "00000011",
                "01000110",
                "01101100",
                "00111000",
                "00010000",
                "00000000",
            ),
        ),
        "input": (
            (160, 180, 255),
            (
                "00111100",
                "01100110",
                "00000110",
                "00001100",
                "00011000",
                "00000000",
                "00011000",
                "00000000",
            ),
        ),
        "failed": (
            RED,
            (
                "01000010",
                "01100110",
                "00111100",
                "00011000",
                "00111100",
                "01100110",
                "01000010",
                "00000000",
            ),
        ),
    }
    color, rows = patterns[name]
    return "data:image/png;base64," + base64.b64encode(
        _png([[int(p) for p in row] for row in rows], color)
    ).decode("ascii")


@lru_cache(maxsize=1)
def idle_dots() -> str:
    """A padded, colorful little dot that glances around and blinks."""
    body = ("0011100", "0111110", "1111111", "1111111", "1111111", "0111110", "0011100")
    # The eyes themselves move: two small white marks, without pupils.
    poses = (
        (0, 0, False, 100),
        (-1, 0, False, 75),
        (0, 0, False, 45),
        (0, -1, False, 70),
        (0, 0, True, 12),
        (0, 0, False, 58),
        (1, 0, False, 85),
        (1, 1, False, 65),
        (0, 0, False, 90),
        (0, 0, True, 12),
        (0, 0, False, 138),
    )
    anchors = (BLUE, (168, 85, 247), (244, 114, 182))
    average = tuple(sum(c[channel] for c in anchors) // 3 for channel in range(3))
    colors = [(0, 0, 0), (255, 255, 255)]
    # A continuous color wheel with softer colors at the center avoids hard
    # boundaries. Its orientation changes throughout the locally played GIF.
    for softness in (0, 0.25, 0.5):
        for hue in range(42):
            segment, amount = divmod(hue / 14, 1)
            first, second = anchors[int(segment)], anchors[(int(segment) + 1) % 3]
            colors.append(
                tuple(
                    round((a + (b - a) * amount) * (1 - softness) + m * softness)
                    for a, b, m in zip(first, second, average, strict=True)
                )
            )
    palette = tuple(colors)
    images, delays = [], []
    for step in range(75):
        elapsed = step * 10
        index = 0
        while elapsed >= poses[index][3]:
            elapsed -= poses[index][3]
            index += 1
        dx, dy, blink, _ = poses[index]
        bob = 1 if index in (3, 4, 5, 6, 7) else 0
        pixels = [0] * 64
        for y, row in enumerate(body):
            for x, pixel in enumerate(row, 1):
                if pixel == "1":
                    radius = hypot(x - 4, y - 3)
                    softness = 2 if radius < 1.5 else 1 if radius < 2.5 else 0
                    angle = atan2(y - 3, x - 4) / tau + step / 75 + radius * 0.06
                    hue = round(angle * 42) % 42
                    pixels[(y + bob) * 8 + x] = 2 + softness * 42 + hue
        for x in (3 + dx, 5 + dx):
            for y in (3,) if blink else (2, 3):
                pixels[(y + dy + bob) * 8 + x] = 1
        images.append(pixels)
        delays.append(10)
    data = _gif(images, delays, palette)
    return "data:image/gif;base64," + base64.b64encode(data).decode("ascii")


@lru_cache(maxsize=102)
def quota_battery(remaining: int | None) -> str:
    """A tiny sleepy battery: its fill and color show weekly energy left."""
    color = (
        (95, 100, 120)
        if remaining is None
        else GREEN
        if remaining >= 30
        else AMBER
        if remaining >= 10
        else RED
    )
    pixels = [0] * 64
    for x in (3, 4):
        pixels[x] = 1
    for y in range(1, 8):
        for x in range(1, 7):
            if x in (1, 6) or y in (1, 7):
                pixels[y * 8 + x] = 1
    rows = 0 if remaining is None else (remaining * 5 + 99) // 100
    for y in range(7 - rows, 7):
        for x in range(2, 6):
            pixels[y * 8 + x] = 2
    # A little face blinks once during its brief quota peek.
    pixels[3 * 8 + 2] = pixels[3 * 8 + 5] = 3
    pixels[5 * 8 + 3] = pixels[5 * 8 + 4] = 3
    blink = pixels.copy()
    blink[3 * 8 + 2] = blink[3 * 8 + 5] = 1
    data = _gif([pixels, blink], [210, 30], ((0, 0, 0), (60, 65, 80), color, (210, 210, 220)))
    return "data:image/gif;base64," + base64.b64encode(data).decode("ascii")


@lru_cache(maxsize=256)
def blue_count(count: int) -> str:
    if type(count) is not int or count <= 0:
        raise ValueError("A running count must be a positive integer")
    glyphs = [DIGITS[int(digit)] for digit in str(count)]
    rows = ["0".join(glyph[y] for glyph in glyphs) for y in range(7)]
    if len(glyphs) > 2:
        data, kind = _scrolling_gif(rows), "gif"
    else:
        if len(glyphs) == 1:
            rows = ["".join(pixel * 2 for pixel in row) for row in rows]
        left = (8 - len(rows[0])) // 2
        pixels = [[0] * 8 for _ in range(8)]
        for y, row in enumerate(rows):
            pixels[y][left : left + len(row)] = [int(pixel) for pixel in row]
        data, kind = _png(pixels), "png"
    return f"data:image/{kind};base64," + base64.b64encode(data).decode("ascii")


@lru_cache(maxsize=1)
def working_spinner() -> str:
    # Large ring with a blank left column, a bright head, and a fading trail.
    # The count uses the text area; TIME plays the animation locally.
    ring = ((3, 0), (5, 1), (6, 3), (5, 5), (3, 6), (2, 5), (1, 3), (2, 1))
    images = []
    for step in range(len(ring)):
        pixels = [0] * 64
        for index, (x, y) in enumerate(ring):
            age = (step - index) % len(ring)
            color = 1 if age == 0 else 2 if age <= 2 else 3
            for dy in (0, 1):
                for dx in (0, 1):
                    pixels[(y + dy) * 8 + x + dx] = color
        images.append(pixels)
    data = _gif(images, [10] * len(images), ((0, 0, 0), BLUE, (0, 64, 128), (0, 24, 48)))
    return "data:image/gif;base64," + base64.b64encode(data).decode("ascii")
