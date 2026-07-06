"""Regenerate the app icons from the brand artwork (assets/logo-alone.png).

Both outputs are committed so the release build (LapScope.spec / release.yml) and
the frontend need no rasterizer at build time; run this only when the brand
artwork changes:

    pip install pillow
    python tools/make_icon.py

The source logo is a raster (gradients + glow), so it stays a PNG — vector
tracing would only blur it. The mark is used AS-IS (no cropping, no rounded
corners, no distortion): the portrait is centered on a transparent square canvas
and only downscaled. Writes:

  * assets/lapscope.ico       — packed multi-resolution Windows exe icon
  * app/static/img/logo.png   — square favicon / header mark (transparent pad)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "logo-alone.png"
ICO = ROOT / "assets" / "lapscope.ico"
WEB = ROOT / "app" / "static" / "img" / "logo.png"

WEB_SIZE = 512
ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]


def square_master() -> Image.Image:
    """Center the untouched portrait on a transparent square canvas."""
    img = Image.open(SRC).convert("RGBA")
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas


def main() -> None:
    master = square_master()

    master.save(ICO, format="ICO", sizes=ICO_SIZES)
    print(f"Wrote {ICO} ({', '.join(f'{w}x{h}' for w, h in ICO_SIZES)})")

    WEB.parent.mkdir(parents=True, exist_ok=True)
    master.resize((WEB_SIZE, WEB_SIZE), Image.LANCZOS).save(WEB, format="PNG")
    print(f"Wrote {WEB} ({WEB_SIZE}x{WEB_SIZE})")


if __name__ == "__main__":
    main()
