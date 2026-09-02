"""Rebuild desktop/app_icon.ico from the WiseTech wordmark.

The source PNG is a black mark on a transparent background. Used bare, a
Windows taskbar icon like that blends into a dark-themed taskbar (or any
dark surface) -- so instead of the mark floating on transparency, it is
composited onto its own opaque white rounded-square plate. A white plate
reads clearly on both a light and a dark taskbar, which a CSS-only
"invert in dark mode" trick cannot do for a static .ico (Windows does not
re-render an app's resource icon based on OS theme).

Run with:  python desktop/build_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
SOURCE = HERE.parent / "sqm45_web" / "static" / "img" / "wisetech-logo.png"
OUT = HERE / "app_icon.ico"

CANVAS = 256
CORNER_RADIUS = 56  # ~22% of canvas -- rounded-square "badge", not a circle
PLATE_MARGIN = 6  # keeps the plate's edge from touching the icon bounds
LOGO_PADDING = 16  # empty space around the mark inside the plate

PLATE_FILL = (255, 255, 255, 255)
PLATE_BORDER = (228, 231, 236, 255)  # matches the app's --border, subtle on white bg


def build() -> None:
    logo = Image.open(SOURCE).convert("RGBA")

    plate = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(plate)
    bounds = [PLATE_MARGIN, PLATE_MARGIN, CANVAS - PLATE_MARGIN - 1, CANVAS - PLATE_MARGIN - 1]
    draw.rounded_rectangle(bounds, radius=CORNER_RADIUS, fill=PLATE_FILL, outline=PLATE_BORDER, width=3)

    max_w = CANVAS - 2 * PLATE_MARGIN - 2 * LOGO_PADDING
    max_h = CANVAS - 2 * PLATE_MARGIN - 2 * LOGO_PADDING
    scale = min(max_w / logo.width, max_h / logo.height)
    logo_resized = logo.resize((round(logo.width * scale), round(logo.height * scale)), Image.LANCZOS)

    paste_x = (CANVAS - logo_resized.width) // 2
    paste_y = (CANVAS - logo_resized.height) // 2
    plate.alpha_composite(logo_resized, (paste_x, paste_y))

    sizes = [16, 24, 32, 48, 64, 128, 256]
    plate.save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes={sizes})")


if __name__ == "__main__":
    build()
