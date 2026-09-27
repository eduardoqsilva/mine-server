"""Gera assets/server-icon.svg e assets/server-icon.png a partir da mesma geometria.

Bandeira revolutionary on a dark field, 256x256. The same control points feed the
SVG (cubic beziers) and the PNG (Pillow, 4x supersampling), so both stay in sync.

Regenerate:  python -m pip install Pillow && python assets/generate_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
SS = 4  # supersampling factor
OUT = Path(__file__).resolve().parent

BG_TOP = (34, 38, 46)
BG_BOTTOM = (12, 14, 18)
BORDER = (7, 8, 10)
GLOW = (214, 32, 42, 38)  # RGBA
POLE = (47, 53, 64)
FINIAL = (69, 77, 92)
FLAG_FROM = (239, 43, 52)
FLAG_TO = (168, 15, 25)
STAR = (247, 249, 251)

BG_RECT = (6.0, 6.0, 250.0, 250.0)
BG_RADIUS = 44.0

POLE_RECT = (42.0, 48.0, 56.0, 214.0)
FINIAL_CENTER = (49.0, 46.0)
FINIAL_RADIUS = 9.0

# Bandeira em S: topo e base ondulam em fase, espessura constante, borda livre tremula.
FLAG_START = (56.0, 52.0)
FLAG_TOP = ((110.0, 64.0), (170.0, 40.0), (224.0, 56.0))
FLAG_RIGHT = ((232.0, 92.0), (232.0, 134.0), (224.0, 170.0))
FLAG_BOTTOM = ((170.0, 186.0), (110.0, 146.0), (56.0, 168.0))

STAR_CENTER = (140.0, 110.0)
STAR_OUTER = 36.0
STAR_INNER = 15.0


def cubic(p0, p1, p2, p3, steps=48):
    points = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def flag_polygon():
    start = FLAG_START
    points = [start]
    for segment in (FLAG_TOP, FLAG_RIGHT, FLAG_BOTTOM):
        points.extend(cubic(start, *segment)[1:])
        start = segment[2]
    return points



def star_polygon(center, outer, inner, points=5, rotation=-90.0):
    cx, cy = center
    coords = []
    for i in range(points * 2):
        radius = outer if i % 2 == 0 else inner
        angle = math.radians(rotation + i * 180.0 / points)
        coords.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return coords


def vertical_gradient(size, top, bottom):
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((size, size))


def horizontal_gradient(size, start, end):
    img = Image.new("RGB", (size, 1))
    px = img.load()
    for x in range(size):
        t = x / max(size - 1, 1)
        px[x, 0] = tuple(round(start[i] + (end[i] - start[i]) * t) for i in range(3))
    return img.resize((size, size))


def render_png(path: Path, size: int = SIZE) -> None:
    big = size * SS

    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    field = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    field.paste(vertical_gradient(big, BG_TOP, BG_BOTTOM), (0, 0))

    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [int(60 * SS), int(4 * SS), int(268 * SS), int(196 * SS)], fill=GLOW
    )
    field = Image.alpha_composite(field, glow)

    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [int(v * SS) for v in BG_RECT], radius=int(BG_RADIUS * SS), fill=255
    )
    canvas.paste(field, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    def rect(box, radius, fill):
        draw.rounded_rectangle([int(v * SS) for v in box], radius=int(radius * SS), fill=fill)

    def poly(points, fill):
        draw.polygon([(x * SS, y * SS) for x, y in points], fill=fill)

    rect(POLE_RECT, 7, POLE)
    draw.ellipse(
        [
            int((FINIAL_CENTER[0] - FINIAL_RADIUS) * SS),
            int((FINIAL_CENTER[1] - FINIAL_RADIUS) * SS),
            int((FINIAL_CENTER[0] + FINIAL_RADIUS) * SS),
            int((FINIAL_CENTER[1] + FINIAL_RADIUS) * SS),
        ],
        fill=FINIAL,
    )

    flag = flag_polygon()
    flag_layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    flag_mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(flag_mask).polygon([(x * SS, y * SS) for x, y in flag], fill=255)
    flag_layer.paste(horizontal_gradient(big, FLAG_FROM, FLAG_TO), (0, 0), flag_mask)
    canvas = Image.alpha_composite(canvas, flag_layer)
    draw = ImageDraw.Draw(canvas)

    poly(star_polygon(STAR_CENTER, STAR_OUTER, STAR_INNER), STAR)

    stroke = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(stroke).rounded_rectangle(
        [int(v * SS) for v in BG_RECT],
        radius=int(BG_RADIUS * SS),
        outline=BORDER + (255,),
        width=max(2, int(4 * SS)),
    )
    canvas = Image.alpha_composite(canvas, stroke)

    canvas.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)


def render_svg(path: Path) -> None:
    def fmt(pt):
        return f"{pt[0]:g},{pt[1]:g}"

    start = FLAG_START
    d = [f"M {fmt(start)}"]
    for c1, c2, end in (FLAG_TOP, FLAG_RIGHT, FLAG_BOTTOM):
        d.append(f"C {fmt(c1)} {fmt(c2)} {fmt(end)}")
        start = end
    path_data = " ".join(d) + " Z"
    star = " ".join(f"{x:.1f},{y:.1f}" for x, y in star_polygon(STAR_CENTER, STAR_OUTER, STAR_INNER))
    content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}" role="img" aria-label="Revolucao">
  <defs>
    <linearGradient id="field" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="rgb{BG_TOP}"/>
      <stop offset="1" stop-color="rgb{BG_BOTTOM}"/>
    </linearGradient>
    <linearGradient id="flag" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="rgb{FLAG_FROM}"/>
      <stop offset="1" stop-color="rgb{FLAG_TO}"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.62" cy="0.38" r="0.6">
      <stop offset="0" stop-color="rgb({GLOW[0]},{GLOW[1]},{GLOW[2]})" stop-opacity="0.18"/>
      <stop offset="1" stop-color="rgb({GLOW[0]},{GLOW[1]},{GLOW[2]})" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect x="6" y="6" width="244" height="244" rx="44" fill="url(#field)"/>
  <rect x="6" y="6" width="244" height="244" rx="44" fill="url(#glow)"/>
  <rect x="44" y="44" width="14" height="168" rx="7" fill="rgb{POLE}"/>
  <circle cx="{FINIAL_CENTER[0]:g}" cy="{FINIAL_CENTER[1]:g}" r="{FINIAL_RADIUS:g}" fill="rgb{FINIAL}"/>
  <path d="{path_data}" fill="url(#flag)"/>
  <polygon points="{star}" fill="rgb{STAR}"/>
  <rect x="6" y="6" width="244" height="244" rx="44" fill="none" stroke="rgb{BORDER}" stroke-width="4"/>
</svg>
"""
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    render_svg(OUT / "server-icon.svg")
    render_png(OUT / "server-icon.png")
    print("gerado:", OUT / "server-icon.svg", OUT / "server-icon.png")
