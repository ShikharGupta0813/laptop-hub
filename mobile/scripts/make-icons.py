"""Draws the REDLINE icons (a crimson R monogram with a cursor block, on #05060A) with only the standard library.

Run from the mobile folder:  python scripts/make-icons.py          (Android launcher icons)
                             python scripts/make-icons.py --web    (also the PWA PNGs in hub/static/icons)
Writes the mipmap PNGs, the adaptive-icon background colour and the splash screens (#05060A with the
monogram, replacing Capacitor's white default) under android/app/src/main/res.
The geometry matches hub/static/icons/icon.svg (512 x 512 units), so the web and Android icons agree.
"""
import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "android" / "app" / "src" / "main" / "res"
WEB = ROOT.parent / "hub" / "static" / "icons"
BACKGROUND = (5, 6, 10)       # #05060A
FRAME = (42, 10, 20)          # #2A0A14
CRIMSON = (255, 83, 104)      # #FF5368
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


def in_round_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx, cy = min(max(x, x0 + r), x1 - r), min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


# ---- the monogram, in the SVG's 512-unit space ----------------------------------------------------
HALF = 22  # half the stroke width (44)
LEG_A, LEG_B = (229.0, 266.0), (308.0, 384.0)


def glyph(x, y):
    """True where the R monogram or its cursor block is drawn."""
    if 123 <= x <= 167 and 128 <= y <= 384:                     # stem
        return True
    if 145 <= x <= 260 and (abs(y - 150) <= HALF or abs(y - 266) <= HALF):   # bowl top and bottom
        return True
    if x >= 260 and abs(math.hypot(x - 260, y - 208) - 58) <= HALF:          # bowl curve
        return True
    if 244 <= y <= 384:                                         # leg
        ax, ay = LEG_A
        dx, dy = LEG_B[0] - ax, LEG_B[1] - ay
        length = math.hypot(dx, dy)
        t = ((x - ax) * dx + (y - ay) * dy) / (length * length)
        if 0 <= t <= 1.2 and abs((x - ax) * dy - (y - ay) * dx) / length <= HALF:
            return True
    if 350 <= x <= 390 and 340 <= y <= 384:                     # cursor block
        return True
    return False


def frame(x, y):
    """The hairline window frame and the accent tick on its top edge."""
    if 96 <= x <= 160 and 25 <= y <= 31:
        return CRIMSON
    outer = in_round_rect(x, y, 25, 25, 487, 487, 89)
    inner = in_round_rect(x, y, 31, 31, 481, 481, 83)
    return FRAME if outer and not inner else None


def icon(scale=1.0, shape="rounded", with_frame=True, background=True):
    """pixel(u, v) for the icon. shape: rounded | square | circle | none (transparent background)."""
    def pixel(u, v):
        if shape == "circle" and (u - 0.5) ** 2 + (v - 0.5) ** 2 > 0.25:
            return (0, 0, 0, 0.0)
        if shape == "rounded" and not in_round_rect(u, v, 0, 0, 1, 1, 112 / 512):
            return (0, 0, 0, 0.0)
        x, y = ((u - 0.5) / scale + 0.5) * 512, ((v - 0.5) / scale + 0.5) * 512
        if glyph(x, y):
            return (*CRIMSON, 1.0)
        if with_frame:
            f = frame(x, y)
            if f:
                return (*f, 1.0)
        return (*BACKGROUND, 1.0) if background else (0, 0, 0, 0.0)
    return pixel


def splash_png(width, height):
    """A #05060A splash with the monogram centred; only the glyph's box is supersampled, so it stays quick."""
    side = min(width, height) * 0.26           # on-screen size of the 512-unit icon space
    x0, y0 = (width - side) / 2, (height - side) / 2
    bx0, bx1 = int(x0 + side * 115 / 512), int(x0 + side * 398 / 512) + 1
    by0, by1 = int(y0 + side * 120 / 512), int(y0 + side * 392 / 512) + 1
    bg = bytes(BACKGROUND) + b"\xff"
    rows = []
    for y in range(height):
        if not by0 <= y <= by1:
            rows.append(b"\x00" + bg * width)
            continue
        row = bytearray(b"\x00" + bg * bx0)
        for x in range(bx0, min(bx1, width)):
            hits = sum(glyph((x + sx) / side * 512 - x0 / side * 512, (y + sy) / side * 512 - y0 / side * 512)
                       for sx in (0.25, 0.75) for sy in (0.25, 0.75))
            k = hits / 4
            row += bytes(round(BACKGROUND[i] * (1 - k) + CRIMSON[i] * k) for i in range(3)) + b"\xff"
        row += bg * (width - min(bx1, width))
        rows.append(bytes(row))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def write_splashes():
    for folder in sorted(RES.glob("drawable*")):
        old = folder / "splash.png"
        if old.exists():
            width, height = struct.unpack(">II", old.read_bytes()[16:24])
            old.write_bytes(splash_png(width, height))


def main():
    for density, size in LEGACY.items():
        folder = RES / f"mipmap-{density}"
        (folder / "ic_launcher.png").write_bytes(png_rgba(size, icon(1.0, "rounded")))
        (folder / "ic_launcher_round.png").write_bytes(png_rgba(size, icon(0.92, "circle", with_frame=False)))
    for density, size in FOREGROUND.items():
        # The adaptive icon's visible area is the middle 66 of 108; the glyph spans about half the width.
        (RES / f"mipmap-{density}" / "ic_launcher_foreground.png").write_bytes(
            png_rgba(size, icon(0.9, "none", with_frame=False, background=False)))
    (RES / "values" / "ic_launcher_background.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <color name="ic_launcher_background">#05060A</color>\n</resources>\n',
        encoding="utf-8")
    write_splashes()
    print("Android icons and splash screens written under", RES)
    if "--web" in sys.argv:
        (WEB / "icon-192.png").write_bytes(png_rgba(192, icon(1.0, "rounded")))
        (WEB / "icon-512.png").write_bytes(png_rgba(512, icon(1.0, "rounded"), samples=2))
        (WEB / "icon-maskable-512.png").write_bytes(png_rgba(512, icon(0.72, "square", with_frame=False), samples=2))
        print("Web icons written under", WEB)


if __name__ == "__main__":
    main()
