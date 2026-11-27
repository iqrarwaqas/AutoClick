"""Draw packaging/icon.ico (the app and installer icon), matching static/icon.svg.

Only needed when the icon design changes:  python packaging/make_icon.py  (needs Pillow)
"""

from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # draw large, then scale down for each icon size
k = S / 64  # static/icon.svg uses a 64x64 grid

# Diagonal sky-blue -> indigo gradient.
top, bottom = (0x0E, 0xA5, 0xE9), (0x63, 0x66, 0xF1)
grad = Image.new("RGBA", (S, S))
px = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S - 2)
        px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=round(16 * k), fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, mask=mask)

# Click ripple rings around tip (20, 18)
for radius, width, alpha in ((8.0, 2.8, 178), (13.5, 2.4, 90)):
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cx, cy, rr = 20 * k, 18 * k, radius * k
    ImageDraw.Draw(layer).ellipse(
        (cx - rr, cy - rr, cx + rr, cy + rr),
        outline=(255, 255, 255, alpha),
        width=round(width * k),
    )
    img = Image.alpha_composite(img, layer)

# Cursor arrow with rounded vertices
d = ImageDraw.Draw(img)
pts = [(21 * k, 19 * k), (27.5 * k, 48 * k), (33.7 * k, 37.8 * k), (45.5 * k, 34 * k)]
d.polygon(pts, fill="white")
d.line(pts + pts[:1], fill="white", width=round(2.5 * k), joint="curve")
r = 1.25 * k
for cx, cy in pts:
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")

out = Path(__file__).with_name("icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"Wrote {out}")
