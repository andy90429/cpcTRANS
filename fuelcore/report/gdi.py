import ctypes as ct
import os
import struct
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path

from .layout import FONT_ALIASES, units
from .rpx import RpxError


GRID = 16  # Subpixel logical coordinates, independent of device DPI.
PDF_PRINTER = "Microsoft Print to PDF"


def face_key(name):
    # GetTextFaceW can return the localized family even when an English alias was requested.
    localized = {"微軟正黑體": "Microsoft JhengHei", "微軟正黑體 UI": "Microsoft JhengHei UI"}
    return FONT_ALIASES.get(name, localized.get(name, name)).casefold()


class DocInfo(ct.Structure):
    _fields_ = [("size", ct.c_int), ("name", w.LPCWSTR), ("output", w.LPCWSTR),
                ("datatype", w.LPCWSTR), ("flags", w.DWORD)]


class PrinterInfo4(ct.Structure):
    _fields_ = [("name", w.LPWSTR), ("server", w.LPWSTR), ("attributes", w.DWORD)]


class PrinterInfo5(ct.Structure):
    _fields_ = [("name", w.LPWSTR), ("port", w.LPWSTR), ("attributes", w.DWORD),
                ("not_selected_timeout", w.DWORD), ("retry_timeout", w.DWORD)]


class Transform(ct.Structure):
    _fields_ = [(key, ct.c_float) for key in ("xx", "xy", "yx", "yy", "dx", "dy")]


class GdiBackend:
    def __init__(self, *, pdf_output=None):
        if os.name != "nt":
            raise RpxError("GDI 直接列印僅支援 Windows")
        self.pdf_output = Path(pdf_output).resolve() if pdf_output is not None else None
        self.spool = ct.WinDLL("winspool.drv", use_last_error=True)
        self.gdi = ct.WinDLL("gdi32", use_last_error=True)
        self.bind()

    def bind(self):
        pointer = ct.c_void_p
        dword_ptr = ct.POINTER(w.DWORD)
        # Every handle-bearing function has an explicit signature for 64-bit Windows.
        spool = {
            "EnumPrintersW": (w.BOOL, w.DWORD, w.LPWSTR, w.DWORD, pointer, w.DWORD, dword_ptr, dword_ptr),
            "OpenPrinterW": (w.BOOL, w.LPWSTR, ct.POINTER(w.HANDLE), pointer),
            "ClosePrinter": (w.BOOL, w.HANDLE),
            "GetPrinterW": (w.BOOL, w.HANDLE, w.DWORD, pointer, w.DWORD, dword_ptr),
            "DeviceCapabilitiesW": (ct.c_int, w.LPCWSTR, w.LPCWSTR, w.WORD, pointer, pointer),
            "DocumentPropertiesW": (ct.c_long, w.HWND, w.HANDLE, w.LPWSTR, pointer, pointer, w.DWORD),
        }
        gdi = {
            "CreateDCW": (w.HDC, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, pointer),
            "DeleteDC": (w.BOOL, w.HDC), "GetDeviceCaps": (ct.c_int, w.HDC, ct.c_int),
            "StartDocW": (ct.c_int, w.HDC, ct.POINTER(DocInfo)),
            "StartPage": (ct.c_int, w.HDC), "EndPage": (ct.c_int, w.HDC),
            "EndDoc": (ct.c_int, w.HDC), "AbortDoc": (ct.c_int, w.HDC),
            "SetGraphicsMode": (ct.c_int, w.HDC, ct.c_int),
            "SetWorldTransform": (w.BOOL, w.HDC, ct.POINTER(Transform)),
            "SaveDC": (ct.c_int, w.HDC), "RestoreDC": (w.BOOL, w.HDC, ct.c_int),
            "SetBkMode": (ct.c_int, w.HDC, ct.c_int),
            "SetTextColor": (w.COLORREF, w.HDC, w.COLORREF),
            "SetTextAlign": (w.UINT, w.HDC, w.UINT),
            "CreateFontW": (w.HFONT, *(ct.c_int,) * 5, *(w.DWORD,) * 8, w.LPCWSTR),
            "GetTextFaceW": (ct.c_int, w.HDC, ct.c_int, w.LPWSTR),
            "GetTextMetricsW": (w.BOOL, w.HDC, pointer),
            "GetTextExtentPoint32W": (w.BOOL, w.HDC, w.LPCWSTR, ct.c_int, ct.POINTER(w.SIZE)),
            "ExtTextOutW": (w.BOOL, w.HDC, ct.c_int, ct.c_int, w.UINT, pointer, w.LPCWSTR, w.UINT, pointer),
            "SelectObject": (w.HGDIOBJ, w.HDC, w.HGDIOBJ),
            "DeleteObject": (w.BOOL, w.HGDIOBJ),
            "GetStockObject": (w.HGDIOBJ, ct.c_int),
            "Rectangle": (w.BOOL, w.HDC, *(ct.c_int,) * 4),
            "IntersectClipRect": (ct.c_int, w.HDC, *(ct.c_int,) * 4),
            "SetStretchBltMode": (ct.c_int, w.HDC, ct.c_int),
            "StretchDIBits": (ct.c_int, w.HDC, *(ct.c_int,) * 8, pointer, pointer, w.UINT, w.DWORD),
        }
        for library, signatures in ((self.spool, spool), (self.gdi, gdi)):
            for name, (result, *args) in signatures.items():
                method = getattr(library, name)
                method.restype, method.argtypes = result, args

    def printers(self):
        need, count = w.DWORD(), w.DWORD()
        flags = 2 | 4  # Local printers and installed connections.
        self.spool.EnumPrintersW(flags, None, 4, None, 0, ct.byref(need), ct.byref(count))
        if not need.value:
            return []
        data = ct.create_string_buffer(need.value)
        if not self.spool.EnumPrintersW(flags, None, 4, data, len(data), ct.byref(need), ct.byref(count)):
            raise RpxError(f"無法列出印表機（Windows 錯誤 {ct.get_last_error()}）")
        records = ct.cast(data, ct.POINTER(PrinterInfo4))
        return sorted({records[index].name for index in range(count.value) if records[index].name}, key=str.casefold)

    @contextmanager
    def printer_handle(self, name):
        handle = w.HANDLE()
        if not self.spool.OpenPrinterW(name, ct.byref(handle), None):
            raise RpxError(f"無法開啟印表機 {name}（Windows 錯誤 {ct.get_last_error()}）")
        try:
            yield handle
        finally:
            self.spool.ClosePrinter(handle)

    def duplex(self, name):
        with self.printer_handle(name) as handle:
            need = w.DWORD()
            self.spool.GetPrinterW(handle, 5, None, 0, ct.byref(need))
            if not need.value:
                return False
            data = ct.create_string_buffer(need.value)
            if not self.spool.GetPrinterW(handle, 5, data, len(data), ct.byref(need)):
                return False
            port = ct.cast(data, ct.POINTER(PrinterInfo5)).contents.port
            return self.spool.DeviceCapabilitiesW(name, port, 7, None, None) == 1  # DC_DUPLEX

    def devmode(self, name, duplex, points):
        with self.printer_handle(name) as handle:
            setup = self.spool.DocumentPropertiesW
            size = setup(None, handle, name, None, None, 0)
            if size < 96:
                raise RpxError("無法取得印表機的 DEVMODE")
            mode = ct.create_string_buffer(size)
            if setup(None, handle, name, mode, None, 2) != 1:
                raise RpxError("無法讀取印表機的 DEVMODE")
            if struct.unpack_from("<H", mode, 68)[0] < 96:
                raise RpxError("印表機的 DEVMODE 版本不支援紙張與雙面設定")
            # Public DEVMODEW fields have fixed offsets before the driver's private data.
            fields = struct.unpack_from("<I", mode, 72)[0] | 0x0001
            width, height = (round(value * 254 / 72) for value in points)
            if sorted((width, height)) == [2100, 2970]:
                fields = (fields | 0x0002) & ~0x000C
                struct.pack_into("<hh", mode, 76, 1 if width < height else 2, 9)
            else:
                fields = (fields | 0x000C) & ~0x0002
                struct.pack_into("<hhhh", mode, 76, 1, 0, height, width)
            if duplex or fields & 0x1000:
                fields |= 0x1000
                struct.pack_into("<h", mode, 94, 2 if duplex else 1)
            struct.pack_into("<I", mode, 72, fields)
            if setup(None, handle, name, mode, mode, 2 | 8) != 1:
                raise RpxError("印表機拒絕紙張或雙面設定")
            if duplex and (not struct.unpack_from("<I", mode, 72)[0] & 0x1000 or struct.unpack_from("<h", mode, 94)[0] != 2):
                raise RpxError("印表機未接受長邊雙面設定，列印已取消")
        return mode

    def open(self, name, duplex, points):
        if self.pdf_output is not None and name.casefold() != PDF_PRINTER.casefold():
            raise RpxError("指定 PDF 檔案只允許使用 Microsoft Print to PDF")
        mode = self.devmode(name, duplex, points)
        hdc = self.gdi.CreateDCW("WINSPOOL", name, None, mode)
        if not hdc:
            raise RpxError(f"無法建立印表機裝置內容（Windows 錯誤 {ct.get_last_error()}）")
        dots = [self.gdi.GetDeviceCaps(hdc, index) for index in (110, 111)]
        dpi = [self.gdi.GetDeviceCaps(hdc, index) for index in (88, 90)]
        if any(d <= 0 or n <= 0 or abs(n * 72 / d - point) > 1 for n, d, point in zip(dots, dpi, points)):
            self.gdi.DeleteDC(hdc)
            raise RpxError("印表機實際紙張尺寸與報表不符，列印已取消")
        return GdiJob(self.gdi, hdc, self.pdf_output)


def logical(value):
    return round(value * GRID)


def utf16_length(value):
    return len(value.encode("utf-16le")) // 2


class GdiJob:
    def __init__(self, gdi, hdc, output):
        self.gdi, self.hdc, self.output = gdi, hdc, output
        self.active = False
        self.fonts = {}
        self.warnings = []
        self.dibs = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.active:
            self.gdi.AbortDoc(self.hdc)
        self.gdi.DeleteDC(self.hdc)
        for font in self.fonts.values():
            self.gdi.DeleteObject(font)

    def check(self, success, action):
        if not success:
            raise RpxError(f"{action}失敗（Windows 錯誤 {ct.get_last_error()}）")

    def start(self, title):
        info = DocInfo(ct.sizeof(DocInfo), title, str(self.output) if self.output is not None else None, None, 0)
        self.check(self.gdi.StartDocW(self.hdc, ct.byref(info)) > 0, "開始列印工作")
        self.active = True

    def finish(self):
        self.check(self.gdi.EndDoc(self.hdc) > 0, "完成列印工作")
        self.active = False

    def start_page(self, page):
        self.check(self.gdi.StartPage(self.hdc) > 0, "開始列印頁")
        self.page = page
        width, height = page.pixels
        dpi_x, dpi_y, off_x, off_y = (self.gdi.GetDeviceCaps(self.hdc, index) for index in (88, 90, 112, 113))
        self.check(dpi_x > 0 and dpi_y > 0, "取得列印解析度")
        self.device_width, self.device_height = page.points[0] * dpi_x / 72, page.points[1] * dpi_y / 72
        self.offset = off_x, off_y
        sign = -1 if page.reverse else 1
        self.xx, self.yy = sign * self.device_width / width / GRID, sign * self.device_height / height / GRID
        self.dx = (self.device_width if page.reverse else 0) - off_x
        self.dy = (self.device_height if page.reverse else 0) - off_y
        self.check(self.gdi.SetGraphicsMode(self.hdc, 2), "設定向量座標")
        if page.bitmap is not None:
            self.background(page.bitmap, page.reverse)
        self.transform()
        self.gdi.SetBkMode(self.hdc, 1)
        self.gdi.SetTextColor(self.hdc, 0)
        self.gdi.SelectObject(self.hdc, self.gdi.GetStockObject(4))  # BLACK_BRUSH
        self.gdi.SelectObject(self.hdc, self.gdi.GetStockObject(8))  # NULL_PEN

    def end_page(self):
        self.check(self.gdi.EndPage(self.hdc) > 0, "完成列印頁")

    def set_transform(self, matrix):
        self.check(self.gdi.SetWorldTransform(self.hdc, ct.byref(matrix)), "套用向量座標")

    def transform(self, scale=1, anchor=0):
        self.set_transform(Transform(self.xx * scale, 0, 0, self.yy,
                                     self.dx + self.xx * logical(anchor) * (1 - scale), self.dy))

    def background(self, bitmap, reverse):
        from PIL import Image

        key = id(bitmap), reverse
        if key not in self.dibs:
            source = bitmap.transpose(Image.Transpose.ROTATE_180) if reverse else bitmap
            width, height = source.size
            stride = (width + 31) // 32 * 4
            data = source.tobytes("raw", "1", stride, 1)
            header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 1, 0, len(data), 23622, 23622, 2, 2)
            palette = struct.pack("<II", 0, 0xFFFFFF)
            self.dibs[key] = (ct.create_string_buffer(data), ct.create_string_buffer(header + palette), width, height)
        pixels, info, width, height = self.dibs[key]
        self.set_transform(Transform(1, 0, 0, 1, 0, 0))
        self.gdi.SetStretchBltMode(self.hdc, 3)
        result = self.gdi.StretchDIBits(self.hdc, -self.offset[0], -self.offset[1],
                                        round(self.device_width), round(self.device_height),
                                        0, 0, width, height, pixels, info, 0, 0x00CC0020)
        self.check(result not in (0, -1), "繪製一位元底圖")

    def font(self, family, styles, text, size):
        alias = FONT_ALIASES.get(family, family)
        bold = "fsBold" in styles or alias.endswith("-Bd")
        candidates = tuple(dict.fromkeys(filter(None, (alias, "Microsoft JhengHei" if "黑" in family else "",
                                                        "MingLiU" if any(ord(c) > 127 for c in text) else "Courier New",
                                                        "Microsoft JhengHei"))))
        key = candidates, size, bold, "fsItalic" in styles
        if key not in self.fonts:
            for face in candidates:
                font = self.gdi.CreateFontW(-logical(size), 0, 0, 0, 700 if bold else 400,
                                            int(key[3]), 0, 0, 1, 4, 0, 2, 0, face)
                if not font:
                    continue
                old = self.gdi.SelectObject(self.hdc, font)
                actual = ct.create_unicode_buffer(64)
                self.gdi.GetTextFaceW(self.hdc, len(actual), actual)
                self.gdi.SelectObject(self.hdc, old)
                if face_key(actual.value) == face_key(face) or face == candidates[-1]:
                    self.fonts[key] = font
                    if alias and face_key(actual.value) != face_key(alias):
                        self.warnings.append(f"GDI 字型 {alias} 改用 {actual.value}")
                    break
                self.gdi.DeleteObject(font)
        self.check(key in self.fonts, "建立報表字型")
        return self.fonts[key]

    def measure(self, font, text):
        if not text:
            return 0
        self.gdi.SelectObject(self.hdc, font)
        size = w.SIZE()
        self.check(self.gdi.GetTextExtentPoint32W(self.hdc, text, utf16_length(text), ct.byref(size)), "測量文字")
        return size.cx / GRID

    def baseline(self, font, size):
        self.gdi.SelectObject(self.hdc, font)
        metrics = ct.create_string_buffer(128)
        self.check(self.gdi.GetTextMetricsW(self.hdc, metrics), "測量文字基線")
        ascent, descent = (value / GRID for value in struct.unpack_from("<ll", metrics, 4))
        return (size + ascent - descent) / 2

    @contextmanager
    def clipped(self, clip):
        saved = self.gdi.SaveDC(self.hdc)
        self.check(saved, "儲存繪製狀態")
        try:
            if clip:
                x, y, width, height = clip
                result = self.gdi.IntersectClipRect(self.hdc, logical(x), logical(y), logical(x + width), logical(y + height))
                self.check(result != 0, "套用欄位裁切")
            yield
        finally:
            self.gdi.RestoreDC(self.hdc, saved)

    def text(self, font, value, x, y, scale, cell, clip):
        with self.clipped(clip):
            if scale != 1:
                self.transform(scale, x)
            self.gdi.SelectObject(self.hdc, font)
            self.gdi.SetTextAlign(self.hdc, 24 | (6 if cell else 0))  # TA_BASELINE, optionally TA_CENTER
            if cell:
                for char in value:
                    advance = units(char) * cell
                    if not char.isspace():
                        self.glyph(char, x + advance / 2, y)
                    x += advance
            else:
                self.glyph(value, x, y)

    def glyph(self, value, x, y):
        self.check(self.gdi.ExtTextOutW(self.hdc, logical(x), logical(y), 0, None,
                                       value, utf16_length(value), None), "繪製文字")

    def rect(self, x, y, width, height, *, clip=None):
        if clip:
            with self.clipped(clip):
                self.rect(x, y, width, height)
            return
        x0, y0 = logical(x), logical(y)
        x1, y1 = max(x0 + 1, logical(x + width)), max(y0 + 1, logical(y + height))
        self.check(self.gdi.Rectangle(self.hdc, x0, y0, x1, y1), "繪製黑色矩形")
