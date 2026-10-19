#!/usr/bin/env python3
"""Generate the password-mask font: U+2022 redrawn as the photonicat hexagon.

The login field's box-shaped bullets come from Titillium Web, whose U+2022 is a
filled rectangle rather than a round dot. This replaces that rectangle with the
cube logo's silhouette -- app/static/logo.svg outlines a pointy-top regular
hexagon, `M14.3301 0 L0 8.25 V24.75 L14.3301 33 L28.6602 24.75 V8.25 Z`, i.e.
vertical left and right sides with the side vertices a quarter of the height in
from top and bottom.

No CSS can substitute one glyph's shape, so the hexagon is baked into a
single-glyph font that sits in front of Titillium Web in the password field's
font stack: only U+2022 resolves from it, every other character (the
placeholder text) still falls through to Titillium Web.

    python3 tools/make_password_dot_font.py [--scale 1.96] [--radius 35]

Writes app/static/fonts/pcat-password-dots.woff2 (~1 KB).
"""

import argparse
import math
import pathlib

from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.subset import Subsetter
from fontTools.ttLib import TTFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "app/static/fonts/Titillium_Web/TitilliumWeb-Regular.ttf"
OUT = ROOT / "app/static/fonts/pcat-password-dots.woff2"
BULLET = 0x2022


def _round_corners(pen, pts, radius):
    """Trace pts with each corner cut back by `radius` and filleted.

    The corner point itself becomes the off-curve control of a quadratic, which
    is all a TrueType outline can express — and at 9 px on screen a quadratic
    fillet is indistinguishable from a true arc anyway.
    """
    n = len(pts)
    approach, depart = [], []
    for i, (vx, vy) in enumerate(pts):
        for nbr, dest in ((pts[i - 1], approach), (pts[(i + 1) % n], depart)):
            dx, dy = nbr[0] - vx, nbr[1] - vy
            edge = math.hypot(dx, dy)
            # Never eat more than half an edge, or opposite fillets would meet.
            r = min(radius, edge / 2)
            dest.append((vx + dx / edge * r, vy + dy / edge * r))

    pen.moveTo(depart[0])
    for i in range(1, n + 1):
        pen.lineTo(approach[i % n])
        pen.qCurveTo(pts[i % n], depart[i % n])
    pen.closePath()


def build(scale: float, radius: float, out: pathlib.Path = OUT) -> pathlib.Path:
    font = TTFont(SRC)
    name = font.getBestCmap()[BULLET]

    # Height comes from the source box scaled up (the shape covers less of its
    # bounding box than the rectangle did, so it needs the extra size to keep
    # the row looking as heavy); width then follows from the height so the
    # hexagon is regular, matching the logo's 28.6602:33 proportion. Centre and
    # advance width are kept, so spacing does not shift.
    box = font["glyf"][name]
    cx, cy = (box.xMin + box.xMax) / 2, (box.yMin + box.yMax) / 2
    hh = (box.yMax - box.yMin) / 2 * scale
    hw = hh * math.sqrt(3) / 2
    advance = font["hmtx"][name][0]

    pen = TTGlyphPen(None)
    corners = [
        (cx, cy + hh),
        (cx + hw, cy + hh / 2),
        (cx + hw, cy - hh / 2),
        (cx, cy - hh),
        (cx - hw, cy - hh / 2),
        (cx - hw, cy + hh / 2),
    ]
    if radius > 0:
        _round_corners(pen, corners, radius)
    else:
        pen.moveTo(corners[0])
        for corner in corners[1:]:
            pen.lineTo(corner)
        pen.closePath()
    font["glyf"][name] = pen.glyph()
    font["hmtx"][name] = (advance, int(cx - hw))

    # Ship only the bullet: the rest of Titillium Web is already served for the
    # page text, and a one-glyph font compresses to about a kilobyte.
    subsetter = Subsetter()
    subsetter.populate(unicodes=[BULLET])
    subsetter.subset(font)

    font["name"].setName("PcatPasswordDots", 1, 3, 1, 0x409)
    font["name"].setName("PcatPasswordDots-Regular", 4, 3, 1, 0x409)
    font.flavor = "woff2"
    font.save(out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.96)
    ap.add_argument("--radius", type=float, default=35,
                    help="corner fillet in font units (1000/em); 0 = sharp")
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    args = ap.parse_args()
    path = build(args.scale, args.radius, args.out)
    print(f"{path} ({path.stat().st_size} bytes, scale {args.scale}, radius {args.radius})")
