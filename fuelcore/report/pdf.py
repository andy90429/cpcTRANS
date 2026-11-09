import hashlib
import io
import os
import zlib
from contextlib import closing
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont, TTFError
from reportlab.pdfgen.canvas import Canvas

from .drawing import draw_fields, prepare_pages
from .layout import FONT_ALIASES, units


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


class PdfPainter:
    def __init__(self, canvas, height, fonts):
        self.canvas = canvas
        self.height = height
        self.fonts = fonts

    def font(self, family, styles, text, size):
        return self.fonts.get(family, styles, text), size

    def measure(self, font, text):
        name, size = font
        return pdfmetrics.stringWidth(text, name, size)

    def baseline(self, font, size):
        ascent, descent = pdfmetrics.getAscentDescent(*font)
        return ascent + (size - ascent + descent) / 2

    def clip(self, box):
        if box is not None:
            x, y, width, height = box
            path = self.canvas.beginPath()
            path.rect(x, self.height - y - height, width, height)
            self.canvas.clipPath(path, stroke=0, fill=0)

    def text(self, font, value, x, y, scale, cell, clip):
        canvas = self.canvas
        canvas.saveState()
        self.clip(clip)
        canvas.translate(x, self.height - y)
        canvas.scale(scale, 1)
        canvas.setFont(*font)
        if cell:
            position = 0
            for char in value:
                advance = units(char) * cell
                if not char.isspace():
                    canvas.drawCentredString(position + advance / 2, 0, char)
                position += advance
        else:
            canvas.drawString(0, 0, value)
        canvas.restoreState()

    def rect(self, x, y, width, height, *, clip=None):
        if clip:
            self.canvas.saveState()
            self.clip(clip)
        self.canvas.rect(x, self.height - y - height, width, height, stroke=0, fill=1)
        if clip:
            self.canvas.restoreState()


def background(canvas, bitmap, width, height):
    # Embed the one-bit pixels directly. ImageReader otherwise promotes mode 1 to RGB.
    data = zlib.compress(bitmap.tobytes()).hex()
    canvas.addLiteral(f"q {width} 0 0 {height} 0 0 cm\n"
                      f"BI /W {bitmap.width} /H {bitmap.height} /BPC 1 /CS /G /F [/AHx /Fl]\n"
                      f"ID\n{data}>\nEI\nQ")


def render_pdf(pages, assets, cache, warnings):
    output = io.BytesIO()
    canvas = Canvas(output, pageCompression=1)
    canvas.setTitle("fuelcore 報單預覽")
    fonts = FontMap(assets, warnings)
    for page in prepare_pages(pages, assets, cache):
        width, height = page.pixels
        points = page.points
        canvas.setPageSize(points)
        canvas.saveState()
        if page.reverse:
            canvas.translate(*points)
            canvas.rotate(180)
        canvas.scale(points[0] / width, points[1] / height)
        canvas.setFillColorRGB(0, 0, 0)
        if page.bitmap is not None:
            background(canvas, page.bitmap, width, height)
        draw_fields(PdfPainter(canvas, height, fonts), page)
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
