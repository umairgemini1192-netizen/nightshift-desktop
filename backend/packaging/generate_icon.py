"""Deterministic pure-Python icon generator for Night-Shift Desktop.

Generates a multi-size Windows .ico file (16, 32, 48, 256 px PNG-encoded frames)
and a PNG icon byte stream for the system tray using only the Python standard
library (zlib + struct), requiring zero native build tools.

Palette follows design/tokens.json dark theme:
- Canvas: #080d1a
- Primary accent (cyan): #38bdf8
- Secondary accent (violet): #a78bfa
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

_BG_RGB = (8, 13, 26)
_PANEL_RGB = (15, 23, 42)
_CYAN_RGB = (56, 189, 248)
_VIOLET_RGB = (167, 139, 250)
_WHITE_RGB = (241, 245, 249)


def _blend(
    base: tuple[int, int, int, int],
    over: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    oa = over[3] / 255.0
    if oa <= 0.0:
        return base
    if oa >= 1.0:
        return over
    ba = base[3] / 255.0
    out_a = oa + ba * (1.0 - oa)
    if out_a <= 0.0:
        return (0, 0, 0, 0)
    r = int((over[0] * oa + base[0] * ba * (1.0 - oa)) / out_a)
    g = int((over[1] * oa + base[1] * ba * (1.0 - oa)) / out_a)
    b = int((over[2] * oa + base[2] * ba * (1.0 - oa)) / out_a)
    return (r, g, b, int(out_a * 255))


def _render_rgba(size: int) -> bytes:
    """Render a crisp radar/crescent Night-Shift icon into RGBA bytes."""
    buf = bytearray(size * size * 4)
    half = (size - 1) / 2.0
    radius = size * 0.46
    corner_r = size * 0.22

    for y in range(size):
        dy = y - half
        for x in range(size):
            dx = x - half
            # Rounded-square mask
            qx = max(abs(dx) - (half - corner_r), 0.0)
            qy = max(abs(dy) - (half - corner_r), 0.0)
            corner_dist = math.hypot(qx, qy) - corner_r
            if corner_dist > 0.75:
                continue
            mask_alpha = int(max(0.0, min(1.0, 0.75 - corner_dist)) * 255)

            # Deep-space background with subtle vertical gradient
            t = y / max(size - 1, 1)
            bg = (
                int(_BG_RGB[0] * (1.0 - 0.35 * t) + _PANEL_RGB[0] * (0.35 * t)),
                int(_BG_RGB[1] * (1.0 - 0.35 * t) + _PANEL_RGB[1] * (0.35 * t)),
                int(_BG_RGB[2] * (1.0 - 0.35 * t) + _PANEL_RGB[2] * (0.35 * t)),
                mask_alpha,
            )
            pixel = bg

            dist = math.hypot(dx, dy)
            angle = math.atan2(dy, dx)

            # Soft violet aurora glow in upper-right quadrant
            glow_dist = math.hypot(dx - size * 0.16, dy + size * 0.16)
            if glow_dist < size * 0.45:
                glow_a = int((1.0 - glow_dist / (size * 0.45)) ** 2 * 90)
                pixel = _blend(pixel, (*_VIOLET_RGB, glow_a))

            # Outer radar ring (cyan)
            ring_outer = radius * 0.78
            ring_w = max(1.0, size * 0.065)
            d_outer = abs(dist - ring_outer)
            if d_outer < ring_w:
                a = int((1.0 - d_outer / ring_w) * 235)
                pixel = _blend(pixel, (*_CYAN_RGB, a))

            # Inner radar ring (violet-tinted)
            ring_inner = radius * 0.44
            d_inner = abs(dist - ring_inner)
            if d_inner < ring_w * 0.8:
                a = int((1.0 - d_inner / (ring_w * 0.8)) * 165)
                pixel = _blend(pixel, (*_VIOLET_RGB, a))

            # Radar sweep beam (upper-right arc)
            if dist <= ring_outer and -math.pi * 0.48 <= angle <= -math.pi * 0.05:
                sweep_t = (angle + math.pi * 0.48) / (math.pi * 0.43)
                sweep_a = int(sweep_t * 140)
                pixel = _blend(pixel, (*_CYAN_RGB, sweep_a))

            # Center beacon dot
            center_r = max(1.2, size * 0.095)
            if dist < center_r:
                a = int(min(1.0, (center_r - dist) / max(1.0, center_r * 0.35)) * 255)
                pixel = _blend(pixel, (*_WHITE_RGB, a))

            # Apply rounded-square alpha clip
            final_a = (pixel[3] * mask_alpha) // 255
            idx = (y * size + x) * 4
            buf[idx] = pixel[0]
            buf[idx + 1] = pixel[1]
            buf[idx + 2] = pixel[2]
            buf[idx + 3] = final_a

    return bytes(buf)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def generate_png_bytes(size: int = 64) -> bytes:
    """Encode an RGBA icon of ``size`` x ``size`` pixels as a valid PNG byte string."""
    rgba = _render_rgba(size)
    raw_rows = bytearray()
    stride = size * 4
    for y in range(size):
        raw_rows.append(0)  # Filter type 0 (None)
        start = y * stride
        raw_rows.extend(rgba[start : start + stride])

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def generate_icon_ico(
    output_path: Path,
    sizes: tuple[int, ...] = (16, 32, 48, 256),
) -> Path:
    """Write a multi-resolution Windows .ico file containing PNG frames."""
    frames = [(s, generate_png_bytes(s)) for s in sizes]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ICONDIR header: reserved=0, type=1 (ICO), count=len(frames)
    header = struct.pack("<HHH", 0, 1, len(frames))
    dir_entries = bytearray()
    offset = 6 + 16 * len(frames)

    for size, png_data in frames:
        w_byte = 0 if size >= 256 else size
        h_byte = 0 if size >= 256 else size
        dir_entries.extend(
            struct.pack(
                "<BBBBHHII",
                w_byte,
                h_byte,
                0,  # palette colors
                0,  # reserved
                1,  # color planes
                32,  # bits per pixel
                len(png_data),
                offset,
            )
        )
        offset += len(png_data)

    with output_path.open("wb") as fh:
        fh.write(header)
        fh.write(dir_entries)
        for _, png_data in frames:
            fh.write(png_data)

    return output_path


if __name__ == "__main__":
    default_out = Path(__file__).resolve().parent / "nightshift.ico"
    generated = generate_icon_ico(default_out)
    print(f"Generated {generated}")
