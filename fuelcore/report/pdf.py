import hashlib
import io
import math
import os
import re
import zlib
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageFilter
from reportlab.graphics.barcode.code39 import Standard39
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont, TTFError
from reportlab.pdfgen.canvas import Canvas

from .layout import field_lines, font_height, units
from .rpx import RpxError


FONT_ALIASES = {"華康儷粗黑": "DFLiHei-Bd", "華康儷中黑": "DFLiHei-Md", "華康粗黑體": "DFHei-Bd",
                "華康中黑體": "DFHei-Md", "細明體": "MingLiU", "新細明體": "MingLiU", "標楷體": "DFKai-SB"}
CM = 72 / 2.54


def font_inventory(assets):
    fonts = {path.stem.casefold(): path for path in assets.root.iterdir()
             if path.suffix.lower() in {".ttf", ".ttc"} and path.is_file()
             and path.resolve().parent == assets.root}
    if os.name == "nt":
        import winreg

        directory = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as registry:
            for index in range(winreg.QueryInfoKey(registry)[1]):
                label, filename, _ = winreg.EnumValue(registry, index)
                if isinstance(filename, str) and Path(filename).suffix.lower() in {".ttf", ".ttc"}:
                    for family in label.split(" (")[0].split(" & "):
                        fonts.setdefault(family.casefold(), directory / filename)
    return fonts


class FontMap:
    def __init__(self, assets, warnings):
        self.files = font_inventory(assets)
        self.warnings = warnings

    def get(self, family, styles, text):
        alias = FONT_ALIASES.get(family, family)
        cjk = any(ord(char) > 127 for char in text)
        bold = "fsBold" in styles or alias.endswith("-Bd")
        italic = "fsItalic" in styles
        path = self.files.get((alias + " Bold").casefold()) if bold else None
        path = path or self.files.get(alias.casefold())
        if not path and (cjk or alias in FONT_ALIASES.values()):
            substitute = "Microsoft JhengHei" + (" Bold" if bold else "") if "黑" in family else "MingLiU"
            path = self.files.get(substitute.casefold())
            self.warn(alias, substitute if path else "MSung-Light")
        if path:
            key = "FC" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
            if key not in pdfmetrics.getRegisteredFontNames():
                try:
                    pdfmetrics.registerFont(TTFont(key, str(path)))
                except (OSError, TTFError):
                    self.warn(alias, "MSung-Light" if cjk else "Courier")
                else:
                    return key
            else:
                return key
        if cjk:
            pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))
            return "MSung-Light"
        if bold:
            return "Courier-BoldOblique" if italic else "Courier-Bold"
        return "Courier-Oblique" if italic else "Courier"

    def warn(self, family, substitute):
        warning = f"本機字型 {family or '（未指定）'} 無法使用，PDF 採用 {substitute}"
        if warning not in self.warnings:
            self.warnings.append(warning)


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


def background(canvas, bitmap, width, height):
    # Embed the one-bit pixels directly. ImageReader otherwise promotes mode 1 to RGB.
    data = zlib.compress(bitmap.tobytes()).hex()
    canvas.addLiteral(f"q {width} 0 0 {height} 0 0 cm\n"
                      f"BI /W {bitmap.width} /H {bitmap.height} /BPC 1 /CS /G /F [/AHx /Fl]\n"
                      f"ID\n{data}>\nEI\nQ")


def text_box(canvas, field, page_height, fonts):
    text = field.text("Caption")
    left, top = field.number("Left"), field.number("Top")
    width, height = max(1, field.number("Width", 1)), max(1, field.number("Height", 1))
    alignment, layout = field.text("Alignment"), field.text("Layout")
    auto = field.boolean("AutoSize", True)
    size = font_height(field)
    family = field.text("Font.Name")
    if "CODE39" in family.upper() or "BARCODE" in field.class_name.upper():
        label = text.strip().upper().strip("*")
        if not label or not re.fullmatch(r"[A-Z0-9 .\-$/+%]+", label):
            raise RpxError(f"{field.name} 的 Code 39 含無效字元")
        barcode = Standard39(label, barWidth=1, barHeight=height, ratio=2.5, checksum=0)
        canvas.saveState()
        canvas.translate(left, page_height - top - height)
        canvas.scale(width / barcode.width, 1)
        barcode.drawOn(canvas, 0, 0)
        canvas.restoreState()
        return
    if re.fullmatch(r"-{3,}", text.strip()) or "line" in field.class_name.lower():
        style = field.text("LineSet.Style")
        canvas.setLineCap(0)
        canvas.setLineWidth(1)
        canvas.setDash([] if style == "psSolid" else [6, 3] if style == "psDash" else [3, 2])
        y = page_height - top - height / 2
        canvas.line(left, y, left + width, y)
        return
    if not text:
        return
    style = field.get("Font.Style")
    styles = style.data if style and style.kind == 11 else ()
    font = fonts.get(family, styles, text)
    canvas.setFont(font, size)
    lines = field_lines(field)
    mono = family in {"細明體", "新細明體", "MingLiU", "PMingLiU"}
    unit = math.ceil(size / 2)
    # Preserve the fixed right edge of numeric form cells when a value is wider than the slot.
    if mono and alignment == "taRightJustify" and all(re.fullmatch(r"[ ()+\-\d.,]*", line) for line in lines):
        natural = max((units(line) * unit for line in lines), default=0)
        if natural > width:
            scale = width / natural
            canvas.translate(left, 0)
            canvas.scale(scale, 1)
            left, width = 0, natural

    def measure(line):
        return units(line) * unit if mono else pdfmetrics.stringWidth(line, font, size)

    text_height = len(lines) * size
    if auto and mono:
        height = text_height
        if not field.boolean("WordWrap"):
            fitted = max(map(measure, lines), default=0) or width
            if alignment == "taRightJustify":
                left += width - fitted
            width = fitted
    vertical = (height - text_height) / 2 if layout == "tlCenter" else height - text_height if layout == "tlBottom" else 0
    ascent, descent = pdfmetrics.getAscentDescent(font, size)
    offset = ascent + (size - ascent + descent) / 2
    if not auto:
        path = canvas.beginPath()
        path.rect(left, page_height - top - height, width, height)
        canvas.clipPath(path, stroke=0, fill=0)
    for index, line in enumerate(lines):
        line_width = measure(line)
        x = left + (width - line_width if alignment == "taRightJustify" else (width - line_width) / 2 if alignment == "taCenter" else 0)
        y = page_height - top - vertical - offset - index * size
        if mono:
            cursor = x
            for char in line:
                advance = units(char) * unit
                if not char.isspace():
                    canvas.drawCentredString(cursor + advance / 2, y, char)
                cursor += advance
        else:
            canvas.drawString(x, y, line)
        if "fsUnderline" in styles and line_width:
            canvas.setDash([])
            canvas.setLineWidth(0.6)
            canvas.line(x, y - 1, x + line_width, y - 1)


def render_pdf(pages, assets, cache, warnings):
    output = io.BytesIO()
    canvas = Canvas(output, pageCompression=1)
    canvas.setTitle("fuelcore 報單預覽")
    fonts = FontMap(assets, warnings)
    bitmaps = {}
    for page, reverse in pages:
        (width, height), points = page_dimensions(page)
        canvas.setPageSize(points)
        canvas.saveState()
        if reverse:
            canvas.translate(*points)
            canvas.rotate(180)
        canvas.scale(points[0] / width, points[1] / height)
        picture = page.text("PictureFile").strip()
        if picture:
            key = (picture.casefold(), points)
            if key not in bitmaps:
                bitmaps[key] = clear_background(assets.find(picture), points, cache)
            background(canvas, bitmaps[key], width, height)
        for field in page.children:
            if field.boolean("Visible", True) and field.boolean("isVisible", True):
                canvas.saveState()
                canvas.setFillColorRGB(0, 0, 0)
                canvas.setStrokeColorRGB(0, 0, 0)
                text_box(canvas, field, height, fonts)
                canvas.restoreState()
        canvas.restoreState()
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def preview_pngs(pdf, folder):
    import pypdfium2

    paths = []
    with closing(pypdfium2.PdfDocument(pdf)) as document:
        for index in range(len(document)):
            path = folder / f"page-{index + 1:03d}.png"
            with closing(document[index]) as page, closing(page.render(scale=150 / 72)) as bitmap:
                bitmap.to_pil().save(path, dpi=(150, 150))
            paths.append(path)
    return paths
