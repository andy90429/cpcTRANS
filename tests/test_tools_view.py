import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fuelcore.cert_check import Certificate, check_pfx
from fuelcore.schema import init_sqlite
from fuelcore.settings import Settings
from fuelcore.tools_view import ToolsPage


ROOT = Path(__file__).resolve().parents[1]


class ToolsViewTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.config = self.folder / "fuelcore.ini"
        self.config.write_bytes((ROOT / "fuelcore.ini.example").read_bytes())
        self.settings = Settings.load(self.config)
        init_sqlite(self.settings)
        self.current = self.folder / "TEST-current.json"
        self.current.write_text(json.dumps({
            "start": "20261101", "end": "20261110", "items": [
                {"code": "USD", "buyValue": "32.455", "sellValue": "32.555"},
                {"code": "JPY", "buyValue": "0.22", "sellValue": "0.24"},
            ],
        }), encoding="utf-8")
        self.history = self.folder / "TEST-history.txt"
        self.history.write_bytes(b"USD1151030325550003245500\r\n")
        self.tools = ToolsPage.__new__(ToolsPage)
        self.tools.app = SimpleNamespace(
            config_path=self.config, report_page=SimpleNamespace(output=self.folder / "列印輸出"),
            busy=False, run_task=self.run_task,
        )
        self.tools.page = object()
        self.tools.open_report = Mock()

    @staticmethod
    def run_task(_, work, finish, on_error=None):
        finish(work())

    def seed(self, *rates):
        with closing(sqlite3.connect(self.settings.path)) as db, db:
            db.executemany("INSERT INTO mydeclarerate (DR002,DR003,DR004,DR005,DR006) VALUES (?,?,?,?,?)", rates)

    def rows(self):
        with closing(sqlite3.connect(self.settings.path)) as db:
            return db.execute("SELECT DR002,DR003,DR004,DR005,DR006 FROM mydeclarerate ORDER BY DR002,DR003").fetchall()

    def simulate(self):
        with patch("fuelcore.declare_rate.download_rate", side_effect=AssertionError("network")) as network:
            self.tools.simulate_rates((self.current, self.history))
            network.assert_not_called()
        path, text = self.tools.open_report.call_args.args
        self.assertEqual(path.parent, self.folder / "診斷")
        self.assertRegex(path.name, r"^每旬匯率模擬_\d{8}_\d{6}_.*\.txt$")
        self.assertEqual(path.read_text(encoding="utf-8-sig"), text)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        return text

    def test_no_current_difference_does_not_prompt_or_change_history(self):
        self.seed(("USD", "115/11", 1, "32.45500", "32.55500"),
                  ("JPY", "115/11", 1, "0.22000", "0.24000"),
                  ("USD", "115/10", 3, "31.00", "32.555"))
        before = self.settings.path.read_bytes()
        with patch("fuelcore.tools_view.messagebox.askyesno") as ask, \
                patch("fuelcore.tools_view.messagebox.showinfo") as info:
            report = self.simulate()
        ask.assert_not_called()
        self.assertIn("無須更新", info.call_args.args[1])
        self.assertIn("最近 9 旬歷史比對不一致 1 筆", report)
        self.assertEqual(self.settings.path.read_bytes(), before)

    def test_decline_after_preview_leaves_test_database_unchanged(self):
        self.seed(("USD", "115/11", 1, "32", "33"))
        before = self.settings.path.read_bytes()

        def decline(*args, **kwargs):
            self.tools.open_report.assert_called_once()
            self.assertEqual(self.settings.path.read_bytes(), before)
            return False

        with patch("fuelcore.tools_view.messagebox.askyesno", side_effect=decline) as ask, \
                patch("fuelcore.tools_view.save_rates") as write:
            self.simulate()
        self.assertIn("新增 1、更新 1", ask.call_args.args[1])
        write.assert_not_called()
        self.assertEqual(self.settings.path.read_bytes(), before)

    def test_approve_writes_and_verifies_current_rates_only(self):
        self.seed(("USD", "115/11", 1, "32", "33"), ("USD", "115/10", 3, "31.00", "32.555"))
        with patch("fuelcore.tools_view.messagebox.askyesno", return_value=True) as ask, \
                patch("fuelcore.tools_view.messagebox.showinfo") as info:
            report = self.simulate()
        self.assertIn("新增 1、更新 1", ask.call_args.args[1])
        self.assertIn("讀回核對 2 筆", info.call_args.args[1])
        self.assertIn("最近 9 旬歷史比對不一致 1 筆", report)
        self.assertEqual(self.rows(), [
            ("JPY", "115/11", 1, "0.22000", "0.24000"),
            ("USD", "115/10", 3, "31.00", "32.555"),
            ("USD", "115/11", 1, "32.45500", "32.55500"),
        ])

    def test_certificate_with_fake_reader_masks_input_and_compares_expiry(self):
        path = self.folder / "TEST.pfx"
        path.write_bytes(b"fake data")
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        expiry = datetime(2026, 11, 30, tzinfo=timezone.utc)
        reader = Mock(return_value=(Certificate("TEST SUBJECT", "TEST ISSUER", now, expiry, True, False),))
        secret = "TEST INPUT"
        with patch("fuelcore.tools_view.filedialog.askopenfilename", return_value=str(path)), \
                patch("fuelcore.tools_view.simpledialog.askstring", side_effect=["2026-12-01", secret]) as prompt, \
                patch("fuelcore.tools_view.check_pfx", partial(check_pfx, reader=reader, now=now)), \
                patch("fuelcore.tools_view.messagebox.showinfo") as result:
            self.tools.inspect_certificate()
        self.assertTrue(prompt.call_args.kwargs["show"])
        reader.assert_called_once_with(b"fake data", secret)
        text = result.call_args.args[1]
        for fragment in ("TEST.pfx", "TEST SUBJECT", "2026-11-30", "剩餘天數：20", "30 天內到期",
                         "2026-12-01（不一致）"):
            self.assertIn(fragment, text)
        self.assertNotIn(secret, text)
        self.assertNotIn(secret.encode(), self.config.read_bytes())

    def test_unexpected_certificate_reader_error_does_not_leak_input(self):
        path = self.folder / "TEST.pfx"
        path.write_bytes(b"fake data")
        secret = "TEST INPUT"
        with patch("fuelcore.tools_view.filedialog.askopenfilename", return_value=str(path)), \
                patch("fuelcore.tools_view.simpledialog.askstring", side_effect=["", secret]), \
                patch("fuelcore.tools_view.check_pfx", side_effect=RuntimeError(secret)), \
                patch("fuelcore.tools_view.messagebox.showerror") as error:
            self.tools.inspect_certificate()
        self.assertEqual(error.call_args.args[1], "無法檢查 PFX 憑證。")

    def test_windows_workbench_opens_offline_report_from_tools_tab(self):
        import tkinter as tk

        from fuelcore.gui import Workbench

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))

        def destroy():
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()

        self.addCleanup(destroy)
        root.withdraw()
        app = Workbench(root, self.config)

        def wait():
            deadline = time.monotonic() + 8
            while app.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)

        with patch("fuelcore.tools_view.check_pfx") as cert:
            root.update()
            wait()
            app.show("工具")
            cert.assert_not_called()
        with patch("fuelcore.tools_view.filedialog.askopenfilename",
                   side_effect=[str(self.current), str(self.history)]), \
                patch("fuelcore.tools_view.messagebox.askyesno", return_value=False) as ask, \
                patch("fuelcore.declare_rate.download_rate", side_effect=AssertionError("network")):
            app.tools_page.buttons[1].invoke()
            self.assertTrue(app.busy)
            wait()
        ask.assert_called_once()
        window, = (child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
        self.assertEqual(window.title(), "每旬匯率模擬報告")
        self.assertEqual(len(list((self.folder / "診斷").glob("每旬匯率模擬_*.txt"))), 1)
        self.assertEqual(self.rows(), [])
