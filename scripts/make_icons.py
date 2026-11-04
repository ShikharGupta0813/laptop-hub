"""Draws the app icons (a laptop with a signal dot) with only the standard library."""
import struct
import zlib
from pathlib import Path


def png(size, pixel):
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            row += bytes(pixel(x / size, y / size))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def inside_round_rect(u, v, x0, y0, x1, y1, r):
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def make(scale):
    """scale < 1 shrinks the artwork toward the centre (the maskable icon needs a safe zone)."""
    def pixel(u, v):
        u = (u - 0.5) / scale + 0.5
        v = (v - 0.5) / scale + 0.5
        bg = (15, 18, 23)
        if inside_round_rect(u, v, 0.18, 0.24, 0.82, 0.64, 0.05):          # screen frame
            if inside_round_rect(u, v, 0.23, 0.29, 0.77, 0.59, 0.025):      # screen
                return (79, 179, 255) if (u - 0.5) ** 2 + (v - 0.44) ** 2 < 0.012 else (23, 31, 44)
            return (232, 237, 244)
        if inside_round_rect(u, v, 0.10, 0.67, 0.90, 0.72, 0.025):          # base
            return (147, 160, 178)
        return bg
    return pixel


out = Path(__file__).parent / "hub" / "static" / "icons"
out.mkdir(parents=True, exist_ok=True)
(out / "icon-192.png").write_bytes(png(192, make(1.0)))
(out / "icon-512.png").write_bytes(png(512, make(1.0)))
(out / "icon-maskable-512.png").write_bytes(png(512, make(0.72)))
print("icons written to", out)
