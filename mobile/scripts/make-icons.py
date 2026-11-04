"""Draws the Android launcher icons (a laptop with a signal dot) with only the standard library.

Run from the mobile folder:  python scripts/make-icons.py
Writes the mipmap PNGs and the adaptive-icon background colour under android/app/src/main/res.
"""
import struct
import zlib
from pathlib import Path

RES = Path(__file__).resolve().parent.parent / "android" / "app" / "src" / "main" / "res"
BACKGROUND = (15, 18, 23)
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
FOREGROUND = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}


def png_rgba(size, pixel, samples=3):
    """pixel(u, v) -> (r, g, b, a) with u, v in 0..1; supersampled for smooth edges."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(samples):
                for sx in range(samples):
                    r, g, b, a = pixel((x + (sx + 0.5) / samples) / size, (y + (sy + 0.5) / samples) / size)
                    acc[0] += r * a; acc[1] += g * a; acc[2] += b * a; acc[3] += a
            n = samples * samples
            alpha = acc[3] / n
            if acc[3] > 0:
                row += bytes([round(acc[0] / acc[3]), round(acc[1] / acc[3]), round(acc[2] / acc[3]), round(alpha * 255)])
            else:
                row += bytes([0, 0, 0, 0])
        rows.append(bytes(row))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def in_round_rect(u, v, x0, y0, x1, y1, r):
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    cx, cy = min(max(u, x0 + r), x1 - r), min(max(v, y0 + r), y1 - r)
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def glyph(u, v):
    """The laptop, or None where it is transparent."""
    if in_round_rect(u, v, 0.18, 0.24, 0.82, 0.64, 0.05):
        if in_round_rect(u, v, 0.23, 0.29, 0.77, 0.59, 0.025):
            return (79, 179, 255) if (u - 0.5) ** 2 + (v - 0.44) ** 2 < 0.012 else (23, 31, 44)
        return (232, 237, 244)
    if in_round_rect(u, v, 0.10, 0.67, 0.90, 0.72, 0.025):
        return (147, 160, 178)
    return None


def scaled(scale, background):
    def pixel(u, v):
        g = glyph((u - 0.5) / scale + 0.5, (v - 0.5) / scale + 0.5)
        if g:
            return (*g, 1.0)
        return (*background, 1.0) if background else (0, 0, 0, 0.0)
    return pixel


def circle(inner):
    def pixel(u, v):
        return inner(u, v) if (u - 0.5) ** 2 + (v - 0.5) ** 2 <= 0.25 else (0, 0, 0, 0.0)
    return pixel


for density, size in LEGACY.items():
    folder = RES / f"mipmap-{density}"
    (folder / "ic_launcher.png").write_bytes(png_rgba(size, scaled(0.9, BACKGROUND)))
    (folder / "ic_launcher_round.png").write_bytes(png_rgba(size, circle(scaled(0.8, BACKGROUND))))
for density, size in FOREGROUND.items():
    # The adaptive icon's visible area is the middle 66 of 108; keep the artwork inside it.
    (RES / f"mipmap-{density}" / "ic_launcher_foreground.png").write_bytes(png_rgba(size, scaled(0.62, None)))
(RES / "values" / "ic_launcher_background.xml").write_text(
    '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <color name="ic_launcher_background">#0F1217</color>\n</resources>\n',
    encoding="utf-8")
print("Android icons written under", RES)
