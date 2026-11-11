import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fuelcore.diagnostics import Diagnostics, collect_diagnostics
from fuelcore.report.rpx import Component, RpxDocument
from fuelcore.schema import init_sqlite
from fuelcore.settings import Settings
from fuelcore.tools_view import ToolsPage
from fuelcore.workbench import save_options


ROOT = Path(__file__).resolve().parents[1]


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.config = self.folder / "fuelcore.ini"
        self.config.write_bytes((ROOT / "fuelcore.ini.example").read_bytes())

    def test_comparison_uses_sqlite_read_only_and_redacts_shared_values(self):
        number = "TEST00A10E0001"
        settings = Settings.load(self.config)
        init_sqlite(settings)
        with closing(sqlite3.connect(settings.path)) as db, db:
            db.execute("INSERT INTO cusdec (MSGCODE,SERSNO,BANAME) VALUES (?,?,?)",
                       ("5203S", number, "TEST DATABASE SHIP"))
        path = self.folder / "TEST legacy.Rpx"
        front = Component("TGPage")
        front.set_text("PageTitle", number)
        field = Component("TGTxt")
        field.set_text("Field", "[CUSDEC.BANAME]")
        field.set_text("Caption", "TEST LEGACY SHIP")
        front.children.append(field)
        path.write_bytes(RpxDocument.create("TEST", [front]).to_bytes())
        before = {item: item.read_bytes() for item in (settings.path, path, self.config)}
        with patch.dict("os.environ", {"FUELCORE_CBS_REPORTS": ""}):
            result = collect_diagnostics(self.config, path)
        self.assertIn("CUSDEC.BANAME", result.summary)
        for value in (number, "TEST DATABASE SHIP", "TEST LEGACY SHIP", str(self.folder)):
            self.assertNotIn(value, result.summary)
        self.assertIn("TEST DATABASE SHIP", result.details)
        self.assertIn("TEST LEGACY SHIP", result.details)
        self.assertEqual(before, {item: item.read_bytes() for item in before})

    def test_rejects_database_without_test_marker_before_opening(self):
        text = self.config.read_text(encoding="utf-8").replace("fuelcore_test.sqlite3", "actual.sqlite3")
        self.config.write_text(text, encoding="utf-8")
        with patch("fuelcore.diagnostics.Database") as database:
            result = collect_diagnostics(self.config)
        database.assert_not_called()
        self.assertIn("未通過測試庫檢查", result.summary)
        self.assertNotIn("actual.sqlite3", result.summary)

    def test_report_diagnostic_checks_configured_templates_and_bitmap(self):
        reports = self.folder / "TEST 報表"
        reports.mkdir()
        page = Component("TGPage")
        page.children.append(Component("TGTxt"))
        page.set_text("PictureFile", "TEST.bmp")
        data = RpxDocument.create("TEST", [page]).to_bytes()
        for name in ("N5203.Rpt", "N5203CUS.Rpt"):
            (reports / name).write_bytes(data)
        save_options(self.config, {"print": {"reports_dir": reports.name}})
        with patch.dict("os.environ", {"FUELCORE_CBS_REPORTS": ""}):
            incomplete = collect_diagnostics(self.config)
            self.assertIn("缺少可讀取的範本及底圖", incomplete.summary)
            (reports / "TEST.bmp").touch()
            ready = collect_diagnostics(self.config)
            self.assertIn("CBS 報表資源目錄：可讀取", ready.summary)
        self.assertNotIn(str(reports), ready.summary)

    def test_diagnostic_button_passes_only_redacted_summary_to_copy(self):
        page = ToolsPage.__new__(ToolsPage)
        page.page = object()
        page.open_report = Mock()
        page.app = SimpleNamespace(busy=False, config_path=self.config,
                                   run_task=lambda _, work, finish: finish(work()))
        with patch("fuelcore.tools_view.messagebox.askyesnocancel", return_value=True), \
                patch("fuelcore.tools_view.filedialog.askopenfilename", return_value="TEST.Rpx"), \
                patch("fuelcore.tools_view.collect_diagnostics",
                      return_value=Diagnostics("TEST SUMMARY", "TEST PRIVATE")) as collect:
            page.diagnose()
        collect.assert_called_once_with(self.config, "TEST.Rpx")
        self.assertIn("TEST PRIVATE", page.open_report.call_args.args[1])
        self.assertEqual(page.open_report.call_args.kwargs["summary"], "TEST SUMMARY")
