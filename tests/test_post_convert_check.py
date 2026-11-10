import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fuelcore import Settings
from fuelcore.logic_check import run_logic_check
from fuelcore.workbench import check_summaries, convert_batch, load_fields, save_fields


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "資料" / "txt樣本" / "D4122_TEST_0001.txt"
NUMBER = "TEST00A10E0001"


class PostConvertCheckTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.ini = folder / "fuelcore.ini"
        shutil.copyfile(ROOT / "fuelcore.ini.example", self.ini)
        shutil.copyfile(ROOT / "tests" / "fixtures" / "check_stub" / "run_check.py", folder / "run_check.py")
        self.marker = folder / "started.json"
        self.settings = Settings.load(self.ini)
        env = patch.dict(os.environ, {"FUELCORE_LOGIC_PLUGIN": str(folder)})
        env.start()
        self.addCleanup(env.stop)

    def stored_state(self):
        with closing(sqlite3.connect(self.settings.path)) as db:
            return db.execute("SELECT STATUS, `是否已邏輯` FROM cusdec WHERE SERSNO = ?", (NUMBER,)).fetchone()

    def disable_auto_check(self):
        fields = load_fields(self.ini)
        self.assertEqual(fields["check_after_convert"], "yes")
        fields["check_after_convert"] = "no"
        save_fields(self.ini, fields)
        self.settings = Settings.load(self.ini)
        self.assertFalse(self.settings.check_after_convert)

    def test_batch_checks_successful_declarations_and_writes_results(self):
        converted = []

        def notify(path, result):
            converted.append(result.ok)
            if result.ok:
                self.assertEqual(self.stored_state(), ("0", 0))
                self.assertFalse(self.marker.exists())

        ids, run = convert_batch([SAMPLE, self.ini.with_suffix(".txt")], self.settings, notify)
        self.assertEqual(converted, [True, False])
        self.assertEqual(ids, [NUMBER])
        self.assertTrue(run.results[0].passed)
        self.assertTrue(run.results[0].saved)
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(marker["ids"], [NUMBER])
        self.assertTrue(marker["save"])
        self.assertEqual(self.stored_state(), ("Y", 1))
        summaries, warning = check_summaries(ids, self.settings)
        self.assertFalse(warning)
        (ship, call, buyer), items = summaries[NUMBER]
        self.assertEqual(ship, "ATLAS TRADER")
        self.assertTrue(buyer)
        self.assertEqual(items[0][2:], ("50.0000", "TNE"))

    def test_disabled_auto_check_and_manual_read_only(self):
        self.disable_auto_check()
        ids, run = convert_batch([SAMPLE], self.settings, lambda *_: None)
        self.assertEqual(ids, [NUMBER])
        self.assertIsNone(run)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.stored_state(), ("0", 0))
        self.assertTrue(run_logic_check(ids, self.settings).results[0].passed)
        self.assertFalse(json.loads(self.marker.read_text(encoding="utf-8"))["save"])
        self.assertEqual(self.stored_state(), ("0", 0))

    def test_sent_declaration_is_not_passed_to_plugin_for_writeback(self):
        self.disable_auto_check()
        convert_batch([SAMPLE], self.settings, lambda *_: None)
        with closing(sqlite3.connect(self.settings.path)) as db, db:
            db.execute("UPDATE cusdec SET STATUS = ? WHERE SERSNO = ?", ("S", NUMBER))
        result, = run_logic_check([NUMBER], self.settings, save=True).results
        self.assertEqual(result.status, "error")
        self.assertIn("已拒絕寫回", result.messages[0].text)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.stored_state(), ("S", 0))

    def test_workbench_displays_failed_check_separately_from_successful_conversion(self):
        import tkinter as tk

        from fuelcore.gui import Workbench
        from fuelcore.workbench import IMPORTED, InputFile

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
        workbench = Workbench(root, self.ini)
        self.assertFalse(workbench.write_check.get())
        sample = SAMPLE.with_name("D4122_TEST_0002.txt")
        number = "TEST00A10E0002"
        workbench.extra.add(sample)

        def wait():
            limit = time.monotonic() + 8
            while workbench.busy and time.monotonic() < limit:
                root.update()
                time.sleep(0.01)
            self.assertFalse(workbench.busy)

        workbench.show_files((sample.parent, [InputFile(sample, "", number)], []))
        wait()
        workbench.start_button.invoke()
        self.assertTrue(workbench.busy)
        wait()
        row, = workbench.files.get_children()
        self.assertEqual(workbench.rows[row].status, IMPORTED)
        self.assertEqual(workbench.files.set(row, "logic"), "邏輯檢查（F6）未通過")
        window, = (child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
        self.assertEqual(window.title(), "檢查結果")
        contents = window.winfo_children()
        body, = (child for child in contents[0].winfo_children() if isinstance(child, tk.Text))
        self.assertIn("執行 87 條", body.get("1.0", "end"))
        self.assertIn("測試結果", body.get("1.0", "end"))
        self.assertIn("轉檔說明", body.get("1.0", "end"))
        self.assertEqual([button.cget("text") for button in contents[1].winfo_children()], ["重新檢查", "關閉"])
