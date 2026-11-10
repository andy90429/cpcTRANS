import io
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fuelcore import Settings, convert_file
from fuelcore.__main__ import main
from fuelcore.runtime import LOGGER, app_version, eligible_recent, record, runtime_logging, save_recent
from fuelcore.schema import init_sqlite
from fuelcore.settings import read_config


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "資料" / "txt樣本"


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.config = self.folder / "fuelcore.ini"
        ini = read_config(ROOT / "fuelcore.ini.example")
        ini["paths"] = {"txt_path": "TXT"}
        with self.config.open("w", encoding="utf-8") as stream:
            ini.write(stream)
        self.source = self.folder / "TXT"
        self.source.mkdir()
        self.settings = Settings.load(self.config)
        init_sqlite(self.settings)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["fuelcore", *args, "--config", str(self.config)]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main()
        return code, out.getvalue(), err.getvalue()

    def test_list_and_convert_all_skip_existing_and_include_older_folders(self):
        older = self.source / "20200101"
        older.mkdir()
        first, second = self.source / "TEST1.txt", older / "TEST2.TXT"
        first.write_bytes((SAMPLES / "D4122_TEST_0001.txt").read_bytes())
        second.write_bytes((SAMPLES / "D4122_TEST_0002.txt").read_bytes())
        self.assertTrue(convert_file(first, self.settings).ok)
        code, out, _ = self.cli("list")
        self.assertEqual((code, out.splitlines()), (0, [f"TEST00A10E0002\t{second}"]))
        code, out, _ = self.cli("convert", "--all")
        self.assertEqual(code, 0)
        self.assertIn("成功 1，失敗 0", out)
        self.assertEqual(self.cli("list")[:2], (0, ""))
        self.assertEqual(eligible_recent(self.config, self.settings), ["TEST00A10E0002"])
        self.assertIn("file='TEST2.TXT'", (self.folder / "fuelcore_run.log").read_text(encoding="utf-8"))

    def test_recent_filters_status_and_update_and_preserves_order(self):
        numbers = [f"TEST00A10E{i:04d}" for i in range(1, 7)]
        save_recent(self.config, [*reversed(numbers), numbers[-1], "invalid"])
        with closing(sqlite3.connect(self.settings.path)) as db, db:
            db.executemany("INSERT INTO cusdec (MSGCODE, SERSNO, STATUS, `UPDATE`) VALUES (?, ?, ?, ?)",
                           [("5203S", number, status, update) for number, (status, update) in zip(numbers, (
                               ("0", ""), ("S", ""), ("P", ""), ("Y", "*"), ("Y", None),
                           ))])
        before = self.settings.path.read_bytes()
        self.assertEqual(eligible_recent(self.config, self.settings), [numbers[4], numbers[0]])
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertEqual(list(self.folder.glob("fuelcore_ui.json.*.tmp")), [])

    def test_logging_rotates_and_never_logs_conversion_error_contents(self):
        filename = SAMPLES / "D4122_TEST_0001.txt"
        sensitive = "TEST password=TEST_VALUE mysql://example"
        with runtime_logging(self.config):
            handler = LOGGER.handlers[-1]
            self.assertEqual((handler.maxBytes, handler.backupCount), (2 * 1024 * 1024, 2))
            with patch("fuelcore.convert.transact", side_effect=ValueError(sensitive)):
                self.assertFalse(convert_file(filename, self.settings).ok)
            record("logic_check", "passed", count=2)
            record("print_report", "submitted", count=1)
            record("reply_watch", "failed")
        log = (self.folder / "fuelcore_run.log").read_text(encoding="utf-8")
        self.assertRegex(log, r"convert failed file='D4122_TEST_0001.txt' seconds=\d+\.\d+")
        for event in ("logic_check passed count=2", "print_report submitted count=1", "reply_watch failed"):
            self.assertIn(event, log)
        self.assertNotIn(sensitive, log)
        self.assertTrue((self.folder / "fuelcore_crash.log").is_file())

    def test_workbench_restores_numbers_and_does_not_replace_manual_input(self):
        import tkinter as tk

        from fuelcore.gui import Workbench

        result = convert_file(SAMPLES / "D4122_TEST_0001.txt", self.settings)
        self.assertTrue(result.ok, result.message)
        save_recent(self.config, [result.sersno])
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
        self.assertIn(app_version(), root.title())
        deadline = time.monotonic() + 8
        while not app.send_numbers.get() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        self.assertEqual(app.send_numbers.get(), result.sersno)
        app.send_numbers.delete(0, "end")
        app.send_numbers.insert(0, "TEST00A10E0099")
        with patch("fuelcore.gui.eligible_recent") as restore:
            app.restore_recent()
            root.update()
        restore.assert_not_called()
        self.assertEqual(app.send_numbers.get(), "TEST00A10E0099")
