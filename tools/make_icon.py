"""Regenerate the Windows exe icon (assets/lapscope.ico) from assets/lapscope.svg.

The .ico is committed so the release build (LapScope.spec / release.yml) needs no
rasterizer; run this only when the brand artwork changes:

    pip install svglib reportlab pillow rlPyCairo pycairo
    python tools/make_icon.py

Uses svglib + reportlab's renderPM (rlPyCairo backend) to rasterize the SVG,
then Pillow to round the corners and pack the standard icon resolutions.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.graphics import renderPM
from svglib.svglib import svg2rlg

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "assets" / "lapscope.svg"
ICO = ROOT / "assets" / "lapscope.ico"
RENDER = 256
CORNER = 52  # rounded-corner radius at 256 px (~13/64 of the canvas)
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]


def main() -> None:
    # renderPM has no alpha channel, so the SVG paints a full opaque square and
    # we round the corners here with a Pillow alpha mask (transparent corners).
    drawing = svg2rlg(str(SVG))
    scale = RENDER / drawing.width
    drawing.width *= scale
    drawing.height *= scale
    drawing.scale(scale, scale)
    png_bytes = renderPM.drawToString(drawing, fmt="PNG")

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, RENDER - 1, RENDER - 1), CORNER, fill=255)
    img.putalpha(mask)

    img.save(ICO, format="ICO", sizes=SIZES)
    print(f"Wrote {ICO} ({', '.join(f'{w}x{h}' for w, h in SIZES)})")


if __name__ == "__main__":
    main()
