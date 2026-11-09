import hashlib
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageFilter
from reportlab.graphics.barcode.code39 import Standard39

from .layout import field_lines, font_height, units
from .rpx import RpxError


MONO_FONTS = {"細明體", "新細明體", "MingLiU", "PMingLiU"}
CM = 72 / 2.54


@dataclass(frozen=True)
class Page:
    component: object
    reverse: bool
    pixels: tuple
    points: tuple
    bitmap: object = None


def page_dimensions(page):
    pixels = page.number("Width", 794), page.number("Height", 1123)
    points = (page.number("PageWidth", 0) * CM or pixels[0] * 0.75,
              page.number("PageHeight", 0) * CM or pixels[1] * 0.75)
    if not all(math.isfinite(n) and 0 < n <= 5000 for n in (*pixels, *points)):
        raise RpxError("報表頁面尺寸無效")
    return pixels, points


def clear_background(source, points, cache):
    data = source.read_bytes()
    size = tuple(round(value * 600 / 72) for value in points)
    if size[0] * size[1] > 100_000_000:
        raise RpxError("600 dpi 底圖尺寸過大")
    key = hashlib.sha256(data + f":{size}:600:unsharp-2-180-2:threshold-180:v1".encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{key}.png"
    if dest.is_file():
        with Image.open(dest) as cached:
            if cached.mode == "1" and cached.size == size:
                return cached.copy()
    with Image.open(io.BytesIO(data)) as original:
        grey = original.convert("L").resize(size, Image.Resampling.LANCZOS)
    crisp = grey.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=2))
    bitmap = crisp.point([0] * 180 + [255] * 76, "1")
    temp = None
    try:
        with NamedTemporaryFile(dir=cache, suffix=".png", delete=False) as handle:
            temp = Path(handle.name)
            bitmap.save(handle, format="PNG", dpi=(600, 600))
        temp.replace(dest)
    finally:
        if temp and temp.exists():
            temp.unlink()
    return bitmap


def prepare_pages(pages, assets, cache):
    bitmaps = {}
    prepared = []
    for page, reverse in pages:
        pixels, points = page_dimensions(page)
        picture = page.text("PictureFile").strip()
        bitmap = None
        if picture:
            key = picture.casefold(), points
            if key not in bitmaps:
                bitmaps[key] = clear_background(assets.find(picture), points, cache)
            bitmap = bitmaps[key]
        prepared.append(Page(page, reverse, pixels, points, bitmap))
    return prepared


def barcode_rectangles(painter, label, left, top, width, height):
    label = label.strip().upper().strip("*")
    if not label or not re.fullmatch(r"[A-Z0-9 .\-$/+%]+", label):
        raise RpxError("Code 39 含無效字元")
    barcode = Standard39(label, barWidth=1, barHeight=height, ratio=2.5, checksum=0)
    scale = width / barcode.width  # This also prepares the encoded bar pattern.
    pos = barcode.lquiet
    for code in barcode.decomposed:
        length = barcode.gap if code == "i" else barcode.barWidth * (barcode.ratio if code.isupper() else 1)
        if code.lower() == "b":
            painter.rect(left + pos * scale, top, length * scale, height)
        pos += length


def rule_rectangles(painter, style, left, top, width):
    if style == "psSolid":
        painter.rect(left, top, width, 1)
        return
    on, off = (6, 3) if style == "psDash" else (3, 2)
    for start in range(0, math.ceil(width), on + off):
        painter.rect(left + start, top, min(on, width - start), 1)


def draw_field(painter, field):
    caption = field.text("Caption")
    left, top = field.number("Left"), field.number("Top")
    width, height = max(1, field.number("Width", 1)), max(1, field.number("Height", 1))
    family = field.text("Font.Name")
    if "CODE39" in family.upper() or "BARCODE" in field.class_name.upper():
        barcode_rectangles(painter, caption, left, top, width, height)
        return
    if re.fullmatch(r"-{3,}", caption.strip()) or "line" in field.class_name.lower():
        rule_rectangles(painter, field.text("LineSet.Style"), left, top + height / 2 - 0.5, width)
        return
    if not caption:
        return
    size = font_height(field)
    alignment, layout = field.text("Alignment"), field.text("Layout")
    auto, wrap = field.boolean("AutoSize", True), field.boolean("WordWrap")
    style = field.get("Font.Style")
    styles = tuple(style.data) if style and style.kind == 11 else ()
    font = painter.font(family, styles, caption, size)
    cell = math.ceil(size / 2) if family in MONO_FONTS else 0
    lines = field_lines(field)
    widths = [units(line) * cell if cell else painter.measure(font, line) for line in lines]
    text_height = len(lines) * size
    if auto and cell:
        height = text_height
    vertical = (height - text_height) / 2 if layout == "tlCenter" else height - text_height if layout == "tlBottom" else 0
    baseline = top + vertical + painter.baseline(font, size)
    numeric = all(re.fullmatch(r"[ ()+\-\d.,]*(?:KGM|LTR|TNE)?[ )]*", line) for line in lines)
    contain = wrap or (alignment == "taRightJustify" and numeric)
    clip = (left, top, width, height) if not auto or contain else None
    for index, (line, natural) in enumerate(zip(lines, widths)):
        if not line:
            continue
        fitted = min(natural, width) if contain else natural
        scale = fitted / natural if natural else 1
        if alignment == "taRightJustify":
            x = left + width - fitted
        elif alignment == "taCenter" and not (auto and cell and not wrap and not contain):
            x = left + (width - fitted) / 2
        else:
            x = left
        y = baseline + index * size
        painter.text(font, line, x, y, scale, cell, clip)
        if "fsUnderline" in styles and fitted:
            painter.rect(x, y + 1, fitted, 0.6, clip=clip)


def draw_fields(painter, page):
    for field in page.component.children:
        if field.boolean("Visible", True) and field.boolean("isVisible", True):
            draw_field(painter, field)
