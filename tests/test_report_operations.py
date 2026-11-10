import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from fuelcore.report.comparison import comparable
from fuelcore.report.retention import MARKER, daily_cleanup, mark_diagnostic, mark_outputs
from fuelcore.report_view import MAX_PREVIEW_PIXELS, ReportPage, preview_scale


class ReportOperationsTest(unittest.TestCase):
    def test_preview_scale_uses_native_pixels_and_bounds_large_bitmaps(self):
        paper = (600, 800)
        self.assertAlmostEqual(preview_scale("符合寬度", (1824, 1000), paper, 96), 3.0)
        self.assertAlmostEqual(preview_scale("整頁", (1824, 824), paper, 144), 1.0)
        self.assertAlmostEqual(preview_scale("150%", (800, 600), paper, 144), 3.0)
        scale = preview_scale("符合寬度", (12000, 4000), paper, 144)
        self.assertLessEqual(scale * scale * paper[0] * paper[1], MAX_PREVIEW_PIXELS + 1)

    def test_daily_cleanup_prunes_only_old_date_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = base / ".fuelcore-cleanup"
            state.mkdir()
            for name in ("20261102", "20261103", "keep.txt"):
                (state / name).touch()
            daily_cleanup(base, {"print": 0, "diag": 0, "generated": 0, "correction": 0},
                          today=date(2026, 11, 10))
            self.assertEqual({path.name for path in state.iterdir()}, {"20261103", "20261110", "keep.txt"})

    def test_numeric_date_and_nil_comparison(self):
        self.assertTrue(comparable("1,234.50", "1234.4999", "#,##0.00###")[0])
        self.assertFalse(comparable("1,234.49", "1234.4999", "#,##0.00###")[0])
        self.assertTrue(comparable("民國 115/11/10", "2026-11-10 12:00:00", "民國 YY/mm/dd")[0])
        self.assertTrue(comparable("NIL", "0.00", '#,##0.00;(#,##0.00);"NIL"')[0])

    def test_daily_cleanup_categories_and_open_file_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "列印輸出"
            folder = root / "報單_TEST00A10E0001_aaaaaaaa"
            folder.mkdir(parents=True)
            files = {"報單.Rpx": "generated", "報單.pdf": "print", "TEST.xlsx": "correction"}
            for name in files:
                (folder / name).touch()
            mark_outputs(folder, files, now=time.time() - 40 * 86400)
            backup = root / "原檔備份"
            backup.mkdir()
            (backup / "TEST.Rpx").touch()
            (root / "TEST_COPY.Rpx").touch()
            diagnostic = base / "診斷" / "每旬匯率模擬_20261001_010101.txt"
            diagnostic.parent.mkdir()
            diagnostic.touch()
            mark_diagnostic(diagnostic, now=time.time() - 40 * 86400)
            days = {"print": 30, "diag": 30, "generated": 7, "correction": 0}
            unlink = Path.unlink

            def locked(path, *args, **kwargs):
                if path.name == "報單.pdf":
                    raise PermissionError("TEST IN USE")
                return unlink(path, *args, **kwargs)

            with patch.object(type(folder), "unlink", locked):
                daily_cleanup(base, days, today=date(2026, 11, 10))
            self.assertFalse((folder / "報單.Rpx").exists())
            self.assertFalse(diagnostic.exists())
            self.assertTrue((folder / MARKER).exists())
            daily_cleanup(base, days, today=date(2026, 11, 10))
            self.assertTrue((folder / "報單.pdf").exists())
            daily_cleanup(base, days, today=date(2026, 11, 11))
            self.assertFalse((folder / "報單.pdf").exists())
            self.assertTrue((folder / "TEST.xlsx").exists())
            self.assertTrue((backup / "TEST.Rpx").exists())
            self.assertTrue((root / "TEST_COPY.Rpx").exists())

    def test_correction_print_requires_confirmation_and_uses_shell_print(self):
        page = ReportPage.__new__(ReportPage)
        page.workbench = Mock(busy=False)
        page.page = Mock()
        page.status = Mock()
        page.correction_path = Path("TEST.xlsx")
        page.task = lambda title, work, finish: finish(work())
        with patch("fuelcore.report_view.messagebox.askokcancel", side_effect=(False, True)), \
                patch("fuelcore.report_view.windows_open") as launch:
            page.print_correction()
            launch.assert_not_called()
            page.print_correction()
        launch.assert_called_once_with(page.correction_path, "print")
