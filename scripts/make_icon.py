"""Draw the app icon (ZIP -> Markdown) and write an .iconset folder for iconutil."""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def draw(size: int) -> Image.Image:
    s = 1024
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # rounded square with a vertical gradient (macOS icon grid: 824px body)
    body = Image.new("RGBA", (824, 824))
    g = ImageDraw.Draw(body)
    for y in range(824):
        t = y / 823
        g.line([(0, y), (824, y)], fill=(int(32 + 20 * t), int(99 + 30 * t), int(235 - 40 * t), 255))
    mask = Image.new("L", (824, 824), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 823, 823], radius=185, fill=255)
    img.paste(body, (100, 100), mask)
    # white document sheet
    d.rounded_rectangle([290, 210, 734, 790], radius=40, fill=(255, 255, 255, 255))
    d.polygon([(614, 210), (734, 330), (614, 330)], fill=(210, 222, 245, 255))
    # zipper teeth down the left of the sheet
    for i, y in enumerate(range(250, 520, 34)):
        x = 330 if i % 2 == 0 else 350
        d.rectangle([x, y, x + 30, y + 18], fill=(120, 140, 170, 255))
    # "MD"
    f = ImageFont.truetype(BOLD, 190)
    d.text((512, 650), "MD", font=f, fill=(32, 99, 235, 255), anchor="mm")
    d.text((512, 520), "↓", font=ImageFont.truetype(BOLD, 120), fill=(32, 99, 235, 255), anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        draw(base).save(out / f"icon_{base}x{base}.png")
        draw(base * 2).save(out / f"icon_{base}x{base}@2x.png")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
