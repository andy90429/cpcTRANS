import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fuelcore import Settings, convert_file
from fuelcore.schema import init_sqlite
from fuelcore.settings_editor import lookup_note, save_countries, save_other, save_products, unsupported_cp950
from fuelcore.workbench import save_options


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "fuelcore.ini.example"
SAMPLE = ROOT / "資料" / "txt樣本" / "D4122_TEST_0001.txt"


class SettingsPagesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / "fuelcore.ini"
        self.config.write_bytes(EXAMPLE.read_bytes())

    def settings(self):
        return Settings.load(self.config)

    def test_saves_only_selected_options_preserving_comments_bom_and_newlines(self):
        text = EXAMPLE.read_text(encoding="utf-8").replace("printer =", "    Printer :")
        text += "\n[untouched]\n; A comment\nkey : one\n    two\n"
        self.config.write_bytes(text.replace("\n", "\r\n").encode("utf-8-sig"))
        before = self.config.read_bytes()
        save_options(self.config, {"print": {"printer": "TEST PRINTER"}})
        self.assertEqual(self.config.read_bytes(), before.replace(b"    Printer :\r\n",
                                                                  b"    printer = TEST PRINTER\r\n"))
        self.assertEqual(self.settings().printer, "TEST PRINTER")

    def test_invalid_config_does_not_replace_ini(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "poll_seconds"):
            save_options(self.config, {"notify": {"poll_seconds": "0"}})
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_product_roundtrip_and_invalid_code(self):
        products = self.settings().products
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "999-X99999-999"):
            save_products(self.config, [("113-161181-003", {})])
        self.assertEqual(self.config.read_bytes(), before)
        row = dict(products["113-F61181-003"], description="TEST FUEL\nTEST LINE")
        save_products(self.config, [("113-F61181-003", row)])
        self.assertEqual(self.settings().products, {"113-F61181-003": row})
        save_products(self.config, [])
        self.assertEqual(self.settings().products, {})

    def test_buyer_roundtrip_and_duplicate(self):
        before = self.config.read_bytes()
        row = ("TESTAA", "ZZ", "TEST COUNTRY")
        with self.assertRaisesRegex(ValueError, "重複"):
            save_countries(self.config, [row, row])
        self.assertEqual(self.config.read_bytes(), before)
        save_countries(self.config, [row])
        self.assertEqual(self.settings().countries, {"TESTAA": ("ZZ", "TEST COUNTRY")})

    def test_other_preserves_multiline_text_and_checks_cp950(self):
        text = " TEST\n\n#TEST\n;TEST\n🌊🛢🌊\n"
        self.assertEqual(unsupported_cp950(text), ("🌊", "🛢"))
        save_other(self.config, "yes", "EDIOTHER", text)
        settings = self.settings()
        self.assertEqual(settings.other, text)
        self.assertTrue(settings.other_enabled)
        self.assertEqual(settings.other_fields, ("EDIOTHER",))

    def test_other_fields_reach_conversion_and_disabled_clears_both(self):
        for enabled, expected in (("yes", (None, "TEST LINE TWO")), ("no", ("", ""))):
            with self.subTest(enabled=enabled):
                save_other(self.config, enabled, "EDIOTHER", "TEST LINE\nTWO")
                settings = self.settings()
                settings.path = self.config.parent / (enabled + "_test.sqlite3")
                init_sqlite(settings)
                result = convert_file(SAMPLE, settings)
                self.assertTrue(result.ok, result.message)
                with closing(sqlite3.connect(settings.path)) as db:
                    self.assertEqual(db.execute("SELECT OTHER, EDIOTHER FROM cusdec").fetchone(), expected)

    def test_note_lookup_is_parameterized_and_normalizes_cr(self):
        settings = self.settings()
        init_sqlite(settings)
        with self.assertRaisesRegex(ValueError, "沒有 mynoteabbr"):
            lookup_note(self.config, "T01")
        with closing(sqlite3.connect(settings.path)) as db, db:
            db.execute("CREATE TABLE mynoteabbr (NA002 TEXT, NA003 TEXT, NA004 TEXT)")
            db.execute("INSERT INTO mynoteabbr VALUES (?, ?, ?)", ("T01", "TEST NOTE", "a\r\nb\rc"))
        before = settings.path.read_bytes()
        self.assertEqual(lookup_note(self.config, "T01"), ("TEST NOTE", "a\nb\nc"))
        with self.assertRaisesRegex(ValueError, "找不到代碼"):
            lookup_note(self.config, "' OR 1=1 --")
        self.assertEqual(settings.path.read_bytes(), before)

    def test_workbench_notebook_each_tab_saves_its_values(self):
        import tkinter as tk
        from tkinter import ttk

        from fuelcore.gui import Workbench

        save_options(self.config, {"app": {"admin_password": "測試"}})
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
        pages = app.settings_pages
        notebook = app.settings_notebook
        tabs = {notebook.tab(tab, "text"): root.nametowidget(tab) for tab in notebook.tabs()}
        self.assertEqual(list(tabs), ["資料庫與路徑", "轉檔", "其他申報事項", "油品表", "買方國家", "列印", "海關回覆"])
        with patch("fuelcore.gui.simpledialog.askstring", side_effect=["錯誤", "測試"]) as ask, \
                patch("fuelcore.gui.messagebox.showerror") as error:
            app.show("設定")
            self.assertFalse(app.settings_unlocked)
            app.show("設定")
            self.assertTrue(app.settings_unlocked)
            app.show("設定")
        self.assertEqual(ask.call_count, 2)
        error.assert_called_once()

        def wait():
            deadline = time.monotonic() + 8
            while app.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)

        def controls(widget):
            for child in widget.winfo_children():
                yield child
                yield from controls(child)

        def click_save(tab):
            button, = (child for child in controls(tabs[tab]) if isinstance(child, ttk.Button)
                       and child.cget("text") in ("儲存", "儲存設定"))
            button.invoke()
            wait()
            self.assertIn("已儲存", app.feedback.get())

        def set_form(section, key, value):
            pages.forms[section, key][0].set(value)

        root.update()
        wait()
        app.values["txt_path"].set(str(self.config.parent))
        set_form("workflow", "check_after_convert", "no")
        click_save("資料庫與路徑")
        self.assertTrue(self.settings().check_after_convert)
        set_form("conversion", "user_code", "TEST01")
        set_form("conversion", "cusboxno", "TEST")
        set_form("conversion", "write_codes", "no")
        set_form("workflow", "archive_after_convert", "no")
        set_form("logic_check", "timeout_seconds", "25")
        click_save("轉檔")
        cfg = self.settings()
        self.assertEqual((cfg.user_code, cfg.cusboxno, cfg.write_codes, cfg.check_after_convert,
                          cfg.archive_after_convert, cfg.logic_check.timeout_seconds),
                         ("TEST01", "TEST", False, False, False, 25))

        pages.note_code.insert(0, "T01")
        with patch("fuelcore.settings_view.lookup_note", return_value=("TEST NOTE", "TEST TEXT\n🌊")), \
                patch("fuelcore.settings_view.messagebox.askyesno", side_effect=[False, True, False, True]) as confirm:
            old = pages.other_text.get("1.0", "end-1c")
            pages.insert_note()
            wait()
            self.assertEqual(pages.other_text.get("1.0", "end-1c"), old)
            pages.insert_note()
            wait()
            self.assertEqual(pages.other_text.get("1.0", "end-1c"), "TEST TEXT\n🌊")
            pages.save_other()
            self.assertEqual(self.settings().other, old)
            click_save("其他申報事項")
            self.assertIn("🌊", confirm.call_args.args[1])
        self.assertEqual(self.settings().other, "TEST TEXT\n🌊")

        product = dict(self.settings().products["113-F61181-003"], description="TEST FUEL\nLINE")
        pages.tables[0][1].insert("999-X99999-999", product)
        click_save("油品表")
        self.assertEqual(self.settings().products["999-X99999-999"], product)
        pages.tables[1][1].insert("TESTAA", {"country": "ZZ", "name": "TEST COUNTRY"})
        click_save("買方國家")
        self.assertEqual(self.settings().countries["TESTAA"], ("ZZ", "TEST COUNTRY"))

        with patch("fuelcore.settings_view.list_printers", return_value=("TEST PRINTER",)):
            pages.reload_printers()
            wait()
        self.assertEqual(tuple(pages.printer.cget("values")), ("TEST PRINTER",))
        pages.printer.set("TEST PRINTER")
        click_save("列印")
        self.assertEqual(self.settings().printer, "TEST PRINTER")

        set_form("notify", "poll_seconds", "20")
        pages.forms["notify", "c3_pdfs"][0].insert("1.0", "{SERSNO}.pdf\nTEST/*.pdf")
        for key, value in (("enabled", "yes"), ("edi_database", "test_edi"), ("xmlset_database", "test_xml"),
                           ("company_code", "00000000"), ("user", "TEST")):
            set_form("edi", key, value)
        click_save("海關回覆")
        cfg = self.settings()
        self.assertEqual(cfg.notify.poll_seconds, 20)
        self.assertEqual(cfg.notify.c3_pdfs, ("{SERSNO}.pdf", "TEST/*.pdf"))
        self.assertEqual(cfg.edi.edi_database, "test_edi")
