import importlib.util
import os
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fuelcore.report import render_report
from fuelcore.report.formula import FormulaError, evaluate
from fuelcore.report.layout import wrap_lines
from fuelcore.report.rpx import Component, RpxDocument, RpxError, Value
from test_edi import REPORTS, load_fixture_tables, settings


PDF_AVAILABLE = all(importlib.util.find_spec(name) for name in ("PIL", "reportlab", "pypdfium2"))
LOCAL_REPORTS = os.environ.get("FUELCORE_CBS_REPORTS")


def short(data):
    return bytes([len(data)]) + data


def long(data, count=None):
    return struct.pack("<i", len(data) if count is None else count) + data


class RpxTest(unittest.TestCase):
    def test_all_delphi_types_roundtrip_and_read_values(self):
        chinese = "測試".encode("cp950")
        wide = "甲𠀀".encode("utf-16le")
        utf8 = "測試 Unicode".encode("utf-8")
        ext = struct.pack("<QH", 0xC000000000000000, 16383)
        collection = b"\x02\x03\x01\x04Name\x06\x01a\x00\x00"
        values = [b"", b"\x06\x01x\x02\xff\x00", b"\xff", struct.pack("<h", -32000), struct.pack("<i", -99999),
                  ext, short(chinese), short(b"taRightJustify"), b"", b"", long(b"\x00TPF0"),
                  short(b"fsBold") + short(b"fsItalic") + b"\x00", long(chinese), b"", collection,
                  struct.pack("<f", 1.25), struct.pack("<q", 12345), struct.pack("<d", 44999.25),
                  long(wide, len(wide) // 2), struct.pack("<q", 2**55), long(utf8), struct.pack("<d", 3.25)]
        fields = b"".join(short(f"P{i}".encode()) + bytes([i]) + value for i, value in enumerate(values))
        root = b"TPF0\xf2\x02\x01" + short(b"TGPage") + short(b"Pg") + fields + b"\x00\x00"
        header = b"EDI1.1" + long(chinese) + b"\x00\x00"
        data = header + root + b"\xff\x00" + root + b"end"
        document = RpxDocument.read(data)
        self.assertEqual(document.to_bytes(), data)
        self.assertEqual(document.title, "測試")
        self.assertEqual(len(document.roots), 2)
        page = document.roots[0]
        self.assertEqual(page.get("P2").number(), -1)
        self.assertEqual(page.get("P5").number(), 1.5)
        self.assertEqual(page.text("P6"), "測試")
        self.assertFalse(page.boolean("P8", True))
        self.assertTrue(page.boolean("P9"))
        self.assertEqual(page.get("P14").data[0].properties[0][1].text(), "a")
        self.assertEqual(page.text("P18"), "甲𠀀")
        self.assertEqual(page.text("P20"), "測試 Unicode")
        self.assertEqual(page.number("P21"), 3.25)
        page.set_text("P20", "更正")
        self.assertEqual(RpxDocument.read(document.to_bytes()).roots[0].text("P20"), "更正")
        self.assertEqual(document.roots[1].text("P20"), "測試 Unicode")

    def test_malformed_stream_and_formula_rejected(self):
        with self.assertRaises(RpxError):
            RpxDocument.read(b"EDI1.1\xff\xff\xff\xffTPF0")
        with self.assertRaises(RpxError):
            RpxDocument.read(b"TPF0\x06TGPage\x00\x01P\x12\xff\xff\xff\x7f")
        context = {"CUSDEC.SERSNO": "TEST00A10E0777", "CUSDEC.SATYPE": "1"}
        resolve = lambda name: context.get(name, "")
        self.assertEqual(evaluate('IIF([CUSDEC.SATYPE]="1", MID([CUSDEC.SERSNO], 1, 4), "")', resolve), "TEST")
        self.assertEqual(evaluate('FMT(1234.5, "#,##0.00")', resolve), "1,234.50")
        self.assertEqual(wrap_lines("中文 ABCDE\n第二行", 5), ["中文", "ABCDE", "第二", "行"])
        for invalid in ("().__class__", "open('x')", "[CUSDEC.SERSNO].upper()"):
            with self.subTest(invalid=invalid), self.assertRaises(FormulaError):
                evaluate(invalid, resolve)


def component(cls, name, left, top, width, height, **values):
    props = dict(Left=left, Top=top, Width=width, Height=height, **values)
    return Component(cls, name, [(key, Value(9 if value else 8) if isinstance(value, bool) else
                                  Value.integer(value) if isinstance(value, int) else Value.string(value))
                                 for key, value in props.items()])


@unittest.skipUnless(PDF_AVAILABLE, "請安裝 report extras")
class ReportPdfTest(unittest.TestCase):
    def test_synthetic_templates_formula_pdf_dimensions_and_select_only(self):
        import pypdfium2
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as temp, closing(sqlite3.connect(":memory:")) as connection:
            root = Path(temp)
            reports = root / "assets"
            reports.mkdir()
            front = component("TGPage", "Front", 0, 0, 400, 600, PageWidth=10, PageHeight=15, PictureFile="Front.bmp")
            front.children = [
                component("TGTxt", "Number", 20, 22, 220, 20, PrtConditions="[CUSDEC.SERSNO]", Caption="OLD"),
                component("TGTxt", "Route", 20, 60, 180, 20, Caption="OLD",
                          PrtConditions='IIF([CUSDEC.CUSISCODE]="C2", "REPLIED C2", "WAITING")'),
                component("TGTxt", "Dash", 20, 90, 250, 2, Caption="-----", **{"LineSet.Style": "psDot"}),
                component("TGBarcode", "Code", 20, 108, 200, 24, PrtConditions='"*"+[CUSDEC.SERSNO]+"*"'),
                component("TGTxt", "Secret", 20, 150, 180, 20, Caption="HIDDEN-FIELD", Visible=False),
            ]
            back = component("TGPage", "Back", 0, 0, 360, 520, PageWidth=9, PageHeight=13)
            back.children = [component("TGTxt", "Inspection", 20, 22, 200, 20, Caption="INSPECTION")]
            for filename, page in (("N5203.Rpt", front), ("N5203CUS.Rpt", back)):
                data = RpxDocument.create("測試", [page]).to_bytes()
                (reports / filename).write_bytes(data)
                self.assertEqual(RpxDocument.read(data).to_bytes(), data)
            with Image.new("RGB", (40, 60), "white") as background:
                ImageDraw.Draw(background).line((2, 5, 38, 5), fill="gray")
                background.save(reports / "Front.bmp")
            source_before = (reports / "Front.bmp").read_bytes()
            refs = reports / "參考輸出"
            refs.mkdir()
            front.set_text("PageTitle", f"報單編號: {REPORTS[0]}")
            front.children[0].set_text("PrtConditions", "SERSNO")
            front.children[0].set_text("Caption", REPORTS[0])
            (refs / "old.Rpx").write_bytes(RpxDocument.create("測試", [front]).to_bytes())
            connection.executescript("""
                CREATE TABLE cusdec (SERSNO, MSGCODE, CUSISCODE, CUSISDATE);
                CREATE TABLE invitem (SERSNO, MSGCODE, `項次號碼`);
                CREATE TABLE n5107 (`報單號碼`, `通關方式`, `接收日期時間`, `訊息參考號碼`);
                INSERT INTO cusdec VALUES ('TEST00A10E0777','5203S','',NULL);
                INSERT INTO invitem VALUES ('TEST00A10E0777','5203S',1);
                INSERT INTO n5107 VALUES ('TEST00A10E0777','C2','2026-11-09 12:00:00','202611090001');
            """)
            statements = []
            connection.set_trace_callback(statements.append)
            result = render_report(settings(), [REPORTS[0]], root / "output", reports=reports,
                                   cache=root / "cache", connection=connection)
            self.assertTrue(statements)
            self.assertTrue(all(sql.startswith("SELECT ") for sql in statements), statements)
            self.assertEqual((reports / "Front.bmp").read_bytes(), source_before)
            with Image.open(next((root / "cache").glob("*.png"))) as bitmap:
                self.assertEqual(bitmap.mode, "1")
                self.assertAlmostEqual(bitmap.info["dpi"][0], 600, delta=1)
            self.assertEqual(len(result.previews), 2)
            self.assertTrue(all(path.is_file() for path in result.previews))
            comparison = result.comparison.read_text(encoding="utf-8-sig")
            self.assertIn("REPLIED C2", comparison)
            self.assertNotIn("SERSNO |", comparison)
            rpx = RpxDocument.read(result.rpx.read_bytes())
            self.assertEqual(len(rpx.roots), 1)
            self.assertIn("REPLIED C2", [field.text("Caption") for field in rpx.roots[0].children])
            with closing(pypdfium2.PdfDocument(result.pdf)) as pdf:
                self.assertEqual(len(pdf), 2)
                for index, (width, height) in enumerate(((10, 15), (9, 13))):
                    with closing(pdf[index]) as page, closing(page.get_textpage()) as text:
                        self.assertAlmostEqual(page.get_width(), width * 72 / 2.54, places=3)
                        self.assertAlmostEqual(page.get_height(), height * 72 / 2.54, places=3)
                        value = text.get_text_bounded()
                        self.assertIn("REPLIED C2" if index == 0 else "INSPECTION", value)
                        self.assertNotIn("HIDDEN-FIELD", value)


@unittest.skipUnless(LOCAL_REPORTS, "請設定 FUELCORE_CBS_REPORTS")
class LocalReportTest(unittest.TestCase):
    def test_local_reports_roundtrip(self):
        files = [path for path in Path(LOCAL_REPORTS).rglob("*") if path.is_file() and path.suffix.lower() in {".rpx", ".rpt"}]
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                self.assertEqual(RpxDocument.read(original).to_bytes(), original)

    @unittest.skipUnless(PDF_AVAILABLE, "請安裝 report extras")
    def test_local_templates_generate_fixture_declarations(self):
        import pypdfium2

        with tempfile.TemporaryDirectory() as temp, closing(sqlite3.connect(":memory:")) as connection:
            load_fixture_tables(connection)
            connection.execute("UPDATE cusdec SET CUSISCODE = NULL, CUSISDATE = NULL WHERE SERSNO = ?", (REPORTS[1],))
            statements = []
            connection.set_trace_callback(statements.append)
            root = Path(temp)
            result = render_report(settings(), REPORTS, root / "output", cache=root / "cache", connection=connection)
            self.assertTrue(statements)
            self.assertTrue(all(sql.startswith("SELECT ") for sql in statements), statements)
            rpx = RpxDocument.read(result.rpx.read_bytes())
            self.assertEqual([page.text("PageTitle") for page in rpx.roots], [f"報單編號: {report}" for report in REPORTS])
            self.assertIn("C1", [field.text("Caption") for field in rpx.roots[1].children])
            with closing(pypdfium2.PdfDocument(result.pdf)) as pdf:
                self.assertEqual(len(pdf), 4)
                self.assertEqual(len(result.previews), 4)
