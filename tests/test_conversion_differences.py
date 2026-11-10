import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fuelcore import Settings, convert_file
from fuelcore.__main__ import run_conversion
from fuelcore.codes import F2_PREFIX, SHIP_BATCH_REMINDER, has_new_ship
from fuelcore.convert import read_txt
from fuelcore.schema import init_sqlite
from fuelcore.txt_edit import TxtEditor
from fuelcore.workbench import find_inputs


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "資料" / "txt樣本" / "D4122_TEST_0001.txt"


class ConversionDifferencesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.config = self.folder / "fuelcore.ini"
        self.config.write_bytes((ROOT / "fuelcore.ini.example").read_bytes())
        self.settings = Settings.load(self.config)
        init_sqlite(self.settings)

    def sample(self, index=1, *, encoding="utf-8", name="TEST SHIP", call="TEST1", imo="9292187",
               buyer="ALPHA BUNKER TRADING CO.,LTD."):
        header, items = read_txt(SAMPLE)
        header[1], header[20], header[22], header[23], header[24], header[27] = (
            f"TEST00A10E{index:04d}", name, imo, "TEST001", buyer, call,
        )
        path = self.folder / f"{index:04d}.txt"
        path.write_bytes(("\r\n".join("|".join(row) for row in (header, *items)) + "\r\n").encode(encoding))
        return path

    def sql(self, statement, parameters=()):
        with contextlib.closing(sqlite3.connect(self.settings.path)) as db, db:
            return db.execute(statement, parameters).fetchall()

    def convert(self, *args, **kwargs):
        result = convert_file(self.sample(*args, **kwargs), self.settings)
        self.assertTrue(result.ok, result.message)
        return result

    def f2(self, result):
        matches = [note for note in result.notes if note.startswith(F2_PREFIX)]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_new_buyer_copies_country_and_destination_without_overwriting_existing(self):
        self.convert()
        buyer = self.sql("SELECT BY012, BY013, BY017, BY018 FROM mybuyer")
        self.assertEqual(buyer, self.sql("SELECT BYCRTYCODE, BYCRTYNAME, DESTCODE, DESTNAME FROM cusdec"))
        self.assertEqual(buyer[0][:2], ("JP", "日本"))
        self.convert(2, buyer="OCEAN STAR MARINE SERVICES CO.,LTD.")
        self.assertEqual(self.sql("SELECT BY012, BY013, BY017, BY018 FROM mybuyer"), buyer)

    def test_short_buyer_code_keeps_value_and_adds_warning(self):
        result = self.convert(buyer="A1B")
        self.assertIn("買方代號只有 2 碼：AB（名稱 A1B）", result.notes)
        self.assertEqual(self.sql("SELECT BYBAN FROM cusdec"), [("AB",)])

    def test_f2_always_describes_new_existing_missing_and_supplemented_call(self):
        self.assertIn("新增船舶呼號", self.f2(self.convert()))
        self.assertIn("CBS 代碼檔已有，不用新增", self.f2(self.convert(2)))
        self.sql("INSERT INTO myshipname (SN002) VALUES (?)", ("TEST2",))
        supplemented = self.f2(self.convert(3, call="TEST2", name="TEST SECOND", imo="9680437"))
        self.assertIn("補 IMO 9680437", supplemented)
        self.assertIn("補船名 TEST SECOND", supplemented)
        self.assertIn("找不到呼號", self.f2(self.convert(4, call="", name="TEST UNKNOWN", imo="")))
        self.settings.write_codes = False
        self.assertIn("代碼檔寫回已停用", self.f2(self.convert(5, call="TEST3")))

    def test_same_call_with_other_ship_names_lists_existing_names_on_insert(self):
        self.sql("INSERT INTO myshipname (SN002, SN003) VALUES (?, ?), (?, ?)",
                 ("TEST1", "TEST B", "TEST1", "TEST A"))
        result = self.convert(name="TEST C")
        self.assertIn("代碼檔原有同呼號：TEST A、TEST B", self.f2(result))
        self.assertTrue(has_new_ship(result.notes))
        self.assertEqual(self.sql("SELECT SN003 FROM myshipname ORDER BY SN003"),
                         [("TEST A",), ("TEST B",), ("TEST C",)])

    def test_cli_summarizes_new_calls_once_and_excludes_rolled_back_ship(self):
        first = self.sample()
        second = self.sample(2, name="TEST SECOND", call="TEST2")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(run_conversion([first, second], self.settings), 0)
        self.assertEqual(err.getvalue().splitlines(), [SHIP_BATCH_REMINDER])
        lines = out.getvalue().splitlines()
        self.assertTrue(all(json.loads(line)["ok"] for line in lines[:-1]))
        self.assertEqual(lines[-1], "成功 2，失敗 0")
        self.sql("CREATE TRIGGER test_rollback BEFORE INSERT ON cusdec "
                 "BEGIN SELECT RAISE(ABORT, 'TEST'); END")
        third = self.sample(3, name="TEST THIRD", call="TEST3")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(run_conversion([third], self.settings), 1)
        self.assertEqual(err.getvalue(), "")
        result = json.loads(out.getvalue().splitlines()[0])
        self.assertIn(F2_PREFIX + "轉檔未完成，未確認代碼檔", result["notes"])
        self.assertFalse(has_new_ship(result["notes"]))
        self.assertEqual(self.sql("SELECT SN002 FROM myshipname ORDER BY SN002"), [("TEST1",), ("TEST2",)])

    def test_cp950_fallback_warns_and_editor_preserves_encoding(self):
        path = self.sample(encoding="cp950", name="TEST 測試船", buyer="TEST 十")
        path.write_bytes(path.read_bytes().replace(b"TEST \xa4\x51", b"TEST \xa2\xcc"))
        original = path.read_bytes()
        self.assertEqual(find_inputs(self.folder)[0].error, "")
        editor = TxtEditor(path)
        self.assertEqual(editor.encoding, "cp950")
        self.assertEqual(editor.to_bytes(), original)
        editor.edit_header(20, "TEST 測試船二")
        backup = editor.save()
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(path.read_bytes(),
                         original.replace("TEST 測試船".encode("cp950"), "TEST 測試船二".encode("cp950")))
        result = convert_file(path, self.settings)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sum("cp950" in note for note in result.notes), 1)
        self.assertEqual(self.sql("SELECT BANAME FROM cusdec"), [("TEST 測試船二",)])
        utf8 = self.convert(2, encoding="utf-8-sig", name="TEST 測試船三")
        self.assertFalse(any("cp950" in note for note in utf8.notes))

    def test_q6_does_not_change_existing_name_cleaning(self):
        self.convert(name=" TEST 船 ! , &amp; NAME ", buyer=" TEST BUYER ! , & NAME ")
        self.assertEqual(self.sql("SELECT BANAME, BYNAME FROM cusdec"),
                         [("TEST 船 ! , NAME", "TEST BUYER ! , NAME")])

    def test_workbench_shows_and_logs_one_batch_reminder(self):
        import tkinter as tk

        from fuelcore.gui import Workbench
        from fuelcore.workbench import InputFile, save_options

        paths = (self.sample(), self.sample(2, name="TEST SECOND", call="TEST2"))
        save_options(self.config, {"workflow": {"check_after_convert": "no", "archive_after_convert": "no"}})
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

        root.update()
        wait()
        app.extra.update(paths)
        app.show_files((None, [InputFile(path, "", f"TEST00A10E{int(path.stem):04d}") for path in paths], []))
        wait()
        with patch("fuelcore.gui.messagebox.showinfo") as info:
            app.start_button.invoke()
            wait()
        self.assertEqual(info.call_args.args[1].count(SHIP_BATCH_REMINDER), 1)
        logs = [app.logs.set(row, "message") for row in app.logs.get_children()]
        self.assertEqual(sum(SHIP_BATCH_REMINDER in message for message in logs), 1)
