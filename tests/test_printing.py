import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fuelcore.report import render_report
from fuelcore.report.declaration import fit_total_slots
from fuelcore.report.layout import wrap_lines
from fuelcore.report.printing import print_report
from fuelcore.report.retention import MARKER, SEVEN_DAYS, cleanup_batches, mark_batch
from fuelcore.report.rpx import RpxDocument, RpxError
from test_edi import REPORTS, load_fixture_tables, settings
from test_report import PDF_AVAILABLE, component


class RecordingJob:
    def __init__(self):
        self.pages, self.warnings = [], []
        self.finished = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def start(self, title):
        self.title = title

    def start_page(self, page):
        self.pages.append((page.reverse, [], page.bitmap is not None))

    def end_page(self):
        pass

    def finish(self):
        self.finished = True

    def font(self, family, styles, caption, size):
        return size

    def measure(self, font, text):
        return font * len(text) / 2

    def baseline(self, font, size):
        return size * 0.8

    def text(self, font, value, x, y, scale, cell, clip):
        self.pages[-1][1].append((value, x, y, scale, cell, clip))

    def rect(self, x, y, width, height, *, clip=None):
        pass


class FakePrinter:
    def __init__(self, duplex=True):
        self.duplex_supported = duplex
        self.opens = []
        self.job = RecordingJob()

    def printers(self):
        return ["Fake Office", "Fake PDF"]

    def duplex(self, name):
        return self.duplex_supported

    def open(self, name, duplex, points):
        self.opens.append((name, duplex, points))
        return self.job


def make_templates(root):
    front = component("TGPage", "Front", 0, 0, 794, 1123, PageWidth=21)
    front.children = [
        component("TGTxt", "Number", 50, 70, 220, 20, PrtConditions="[CUSDEC.SERSNO]"),
        component("TGTxt", "Port", 128, 186, 70, 13, Caption="TAICHUNG ,TAIWAN", WordWrap=True,
                  **{"Font.Name": "細明體", "Font.Height": -13}),
        component("TGTxt", "Secret", 20, 120, 220, 20, Caption="HIDDEN", Visible=False),
    ]
    back = component("TGPage", "Back", 0, 0, 794, 1123, PageWidth=21)
    back.children = [component("TGTxt", "Check", 30, 40, 220, 20, Caption="INSPECTION")]
    for file, page in (("N5203.Rpt", front), ("N5203CUS.Rpt", back)):
        (root / file).write_bytes(RpxDocument.create("測試", [page]).to_bytes())


class PrintSafetyTest(unittest.TestCase):
    def test_invalid_printer_never_opens_job(self):
        for name in ("", "Nonexistent"):
            with self.subTest(name=name):
                cfg = settings()
                cfg.printer = name
                printer = FakePrinter()
                with self.assertRaisesRegex(RpxError, "Fake Office、Fake PDF"):
                    print_report(cfg, REPORTS, backend=printer, yes=True)
                self.assertEqual(printer.opens, [])

    def test_retention_removes_only_old_marked_owned_folders(self):
        now = 10 * SEVEN_DAYS
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def batch(suffix, age, owned=True, extra=False):
                folder = root / f"報單_{REPORTS[0]}_{suffix * 8}"
                folder.mkdir()
                (folder / "報單.pdf").write_bytes(b"fake")
                if owned:
                    mark_batch(folder, now=now - age)
                if extra:
                    (folder / "user.txt").write_text("keep", encoding="utf-8")
                return folder

            old = batch("a", SEVEN_DAYS + 1)
            recent = batch("b", SEVEN_DAYS - 1)
            boundary = batch("c", SEVEN_DAYS)
            unmarked = batch("d", SEVEN_DAYS + 1, owned=False)
            mixed = batch("e", SEVEN_DAYS + 1, extra=True)
            foreign = root / "other"
            foreign.mkdir()
            mark_batch(foreign, now=now - SEVEN_DAYS - 1)
            self.assertEqual(cleanup_batches(root, now=now), [old])
            self.assertFalse(old.exists())
            self.assertTrue(all(path.exists() for path in (recent, boundary, unmarked, mixed, foreign)))
            self.assertTrue((mixed / MARKER).is_file())

    def test_closing_punctuation_stays_on_preceding_line(self):
        self.assertEqual(wrap_lines("TAICHUNG ,TAIWAN", 10), ["TAICHUNG ,", "TAIWAN"])
        self.assertEqual(wrap_lines("測試）下一行。", 4), ["測試）", "下一", "行。"])

    def test_gdi_recognizes_localized_font_family_names(self):
        from fuelcore.report.gdi import face_key

        self.assertEqual(face_key("細明體"), face_key("MingLiU"))
        self.assertEqual(face_key("微軟正黑體"), face_key("Microsoft JhengHei"))
        self.assertNotEqual(face_key("細明體"), face_key("DFLiHei-Bd"))


@unittest.skipUnless(PDF_AVAILABLE, "請安裝 report extras")
class PrintDrawingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assets = self.root / "reports"
        self.assets.mkdir()
        make_templates(self.assets)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        load_fixture_tables(self.db)
        self.cfg = settings()
        self.cfg.printer = "fake office"

    def options(self):
        return dict(reports=self.assets, cache=self.root / "cache", connection=self.db,
                    retention_dir=self.root / "output")

    def test_total_quantity_fits_inside_ink_boundary(self):
        from fuelcore.report.drawing import draw_field

        for caption, formula in (("105,000.00KGM", "TOT_NETWT"), ("(117,150.00LTR)", "[SUMSTC.STCQUAN]")):
            field = component("TGField", "Total", 553, 666, 72, 13, Caption=caption, Field=formula,
                              Alignment="taRightJustify", **{"Font.Name": "細明體", "Font.Height": -13})
            fit_total_slots([field])
            job = RecordingJob()
            job.pages.append((False, [], False))
            draw_field(job, field)
            value, left, _, scale, cell, clip = job.pages[0][1][0]
            self.assertEqual(value, caption)
            self.assertGreaterEqual(left, 561)
            self.assertLessEqual(left + len(value) * cell * scale, 625.00001)
            self.assertEqual(clip[:3], (561, 666, 64))
            self.assertLess(scale, 1)

    def test_cancel_and_unsupported_duplex(self):
        printer = FakePrinter(duplex=False)
        notes = []
        self.assertFalse(print_report(self.cfg, REPORTS, backend=printer, confirm=lambda _: "yes",
                                      notify=notes.append, **self.options()))
        self.assertFalse(printer.opens)
        self.assertIn("單面", notes[0])
        self.assertTrue(print_report(self.cfg, REPORTS[:1], backend=printer, confirm=lambda _: "y",
                                     notify=notes.append, **self.options()))
        self.assertEqual(printer.opens[0][:2], ("Fake Office", False))

    def test_one_duplex_job_matches_pdf_content_and_selects_only(self):
        from fuelcore.report.pdf import PdfPainter

        statements = []
        self.db.set_trace_callback(statements.append)
        printer = FakePrinter()
        self.assertTrue(print_report(self.cfg, REPORTS, backend=printer, yes=True, notify=lambda _: None,
                                     **self.options()))
        self.assertTrue(statements and all(sql.startswith("SELECT ") for sql in statements), statements)
        self.assertEqual(len(printer.opens), 1)
        self.assertEqual(printer.opens[0][:2], ("Fake Office", True))
        self.assertTrue(printer.job.finished)
        self.assertEqual([page[0] for page in printer.job.pages], [False, True, False, True])
        captured = []
        original = PdfPainter.text

        def record(painter, font, value, *args):
            captured.append(value)
            return original(painter, font, value, *args)

        with patch.object(PdfPainter, "text", record):
            result = render_report(self.cfg, REPORTS, self.root / "output", reports=self.assets,
                                   cache=self.root / "cache", connection=self.db)
        self.assertEqual(captured, [text[0] for _, runs, _ in printer.job.pages for text in runs])
        self.assertEqual(captured, [REPORTS[0], "TAICHUNG ,", "TAIWAN", "INSPECTION",
                                    REPORTS[1], "TAICHUNG ,", "TAIWAN", "INSPECTION"])
        self.assertTrue((result.pdf.parent / MARKER).exists())

    @unittest.skipUnless(os.environ.get("FUELCORE_CBS_REPORTS"), "請設定本機 CBS 報表")
    def test_local_cbs_templates_use_one_fake_duplex_job(self):
        options = self.options()
        options["reports"] = None
        printer = FakePrinter()
        self.assertTrue(print_report(self.cfg, REPORTS, yes=True, backend=printer, notify=lambda _: None, **options))
        self.assertEqual(len(printer.opens), 1)
        self.assertEqual([(reverse, bitmap) for reverse, _, bitmap in printer.job.pages],
                         [(False, True), (True, True), (False, True), (True, True)])
        first = [text for text, *_ in printer.job.pages[0][1]]
        self.assertIn("TAICHUNG ,", first)
        self.assertIn("TAIWAN", first)
