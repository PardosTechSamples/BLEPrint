"""
"Print as QR": a QR code of the label text at full label height, with the text beside it.

The QR fills the whole label height (88 dots = 11 mm on the Supvan E10). Short text gives a
21-module code at 4-5 dots per module, which phone cameras read easily; longer text still prints,
but the modules get too small to scan reliably.
"""
from __future__ import annotations

import math

import segno
from PIL import Image, ImageDraw, ImageFont

FONT_PATHS = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def qr_image(text: str, height: int, border: int = 0) -> Image.Image:
    """
    Square QR image exactly `height` dots tall (black on white).

    Modules are spread over the full height, so some are one dot wider than others (e.g. 21
    modules in 88 dots -> 4 and 5 dot modules); scanners don't mind. With border=0 the blank tape
    around the print area serves as the quiet zone.
    """
    qr = segno.make(text or " ", error="l", micro=False, boost_error=True)
    rows = list(qr.matrix_iter(scale=1, border=border))
    n = len(rows)
    edge = [round(i * height / n) for i in range(n + 1)]
    img = Image.new("L", (height, height), 255)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(rows):
        for x, dark in enumerate(row):
            if dark:
                d.rectangle((edge[x], edge[y], edge[x + 1] - 1, edge[y + 1] - 1), fill=0)
    return img


def _font_for(text, max_w, max_h, d):
    for path in FONT_PATHS:
        try:
            for size in range(max_h * 2, 6, -1):
                f = ImageFont.truetype(path, size)
                l, t, r, b = d.multiline_textbbox((0, 0), text, font=f)
                if r - l <= max_w and b - t <= max_h:
                    return f
            return None
        except OSError:
            continue
    return ImageFont.load_default()


def render_qr_label(text: str, height: int, width: int | None = None, margin: int = 8,
                    gap: int = 10, show_text: bool = True) -> Image.Image:
    """
    Landscape label image `height` dots tall: QR on the left, text on the right.
    width=None fits the label to its content (edge to edge); otherwise the content is laid out
    within `width` (the text shrinks to fit, or is left out if there is no room).
    """
    text = (text or "").strip("\n")
    qr = qr_image(text, height)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    txt_font, txt_box = None, None
    if show_text and text:
        room = 4000 if width is None else width - 2 * margin - qr.width - gap
        if room > 16:
            txt_font = _font_for(text, room, height - 4, probe)
            if txt_font:
                txt_box = probe.multiline_textbbox((0, 0), text, font=txt_font)
    content_w = qr.width + (gap + int(math.ceil(txt_box[2] - txt_box[0])) if txt_box else 0)
    w = content_w + 2 * margin if width is None else max(width, 8)
    img = Image.new("L", (w, height), 255)
    x0 = (w - content_w) // 2
    img.paste(qr, (x0, (height - qr.height) // 2))
    if txt_box:
        l, t, r, b = txt_box
        ImageDraw.Draw(img).multiline_text(
            (x0 + qr.width + gap - l, (height - (b - t)) // 2 - t), text, font=txt_font, fill=0)
    return img
