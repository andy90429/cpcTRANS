import tempfile
import time
import unittest
from codecs import BOM_UTF8
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fuelcore.convert import read_txt
from fuelcore.report.calc import density_api
from fuelcore.txt_edit import BACKUP_FOLDER, TxtEditor
from fuelcore.workbench import InputFile, find_inputs


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "資料" / "txt樣本"
SAMPLE = SAMPLES / "D4122_TEST_0001.txt"


class TxtEditTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.path = self.folder / "TEST.txt"
        self.header, (self.item,) = read_txt(SAMPLE)
        for column, value in {
            13: "50000", 15: "50.000", 17: "58932", 25: "15326", 26: "502.50",
            28: "30.50000", 31: "6",
        }.items():
            self.header[column] = value
        for column, value in {11: "58932", 12: "15326", 13: "502.50", 14: "10.05"}.items():
            self.item[column] = value

    def sample(self, *, bom=False, newline="\n", final=True, second=False):
        header = self.header.copy()
        items = [self.item]
        if second:
            items.append(self.item.copy())
            for column, value in {13: "100000", 15: "100.000", 17: "117864", 25: "30652",
                                  26: "1005.00", 31: "12"}.items():
                header[column] = value
        text = newline.join("|".join(row) for row in (header, *items))
        data = (BOM_UTF8 if bom else b"") + (text + (newline if final else "")).encode("utf-8")
        self.path.write_bytes(data)
        return data

    def test_fifty_samples_open_and_roundtrip_without_rewriting(self):
        paths = sorted(SAMPLES.glob("*.txt"))
        self.assertEqual(len(paths), 50)
        for path in paths:
            with self.subTest(path=path.name):
                editor = TxtEditor(path)
                self.assertFalse(editor.dirty)
                self.assertEqual(editor.to_bytes(), path.read_bytes())
                self.assertIsInstance(editor.warnings(), list)

    def test_density_inference_range_and_density_edit_change_only_liters(self):
        self.sample()
        editor = TxtEditor(self.path)
        density, low, high = editor.inferred[0]
        self.assertEqual(editor.calculation(0), (Decimal("34.9"), Decimal("0.13489"), Decimal("58932")))
        self.assertTrue(low <= density <= high)
        self.assertEqual((density_api(low), density_api(high)), (Decimal("34.9"),) * 2)
        self.assertNotEqual(density_api(low - Decimal(".0001")), Decimal("34.9"))
        self.assertNotEqual(density_api(high + Decimal(".0001")), Decimal("34.9"))
        editor.edit_density(0, ".9000")
        self.assertEqual(editor.items[0][11], "55633")
        self.assertEqual(editor.header[17], "55633")
        self.assertEqual([i for i, (a, b) in enumerate(zip(self.item, editor.items[0])) if a != b], [11])
        self.assertEqual([i for i, (a, b) in enumerate(zip(self.header, editor.header)) if a != b], [17])
        editor.edit_item(0, 11, "55630")
        self.assertIn("第 1 項 LTR 應為 55633", "\n".join(editor.warnings()))
        self.assertIn("H17", "\n".join(editor.warnings()))

    def test_quantity_price_and_exchange_recalculate_at_original_scale(self):
        self.sample()
        editor = TxtEditor(self.path)
        editor.edit_density(0, ".9000")
        editor.edit_item(0, 9, "60.5")
        self.assertEqual([editor.items[0][i] for i in (7, 9, 11, 12, 13, 14)],
                         ["60500", "60.500", "67316", "18545", "608.03", "10.05"])
        self.assertEqual([editor.header[i] for i in (13, 15, 17, 25, 26, 28, 31)],
                         ["60500", "60.500", "67316", "18545", "608.03", "30.50000", "7"])
        editor.edit_header(28, "31.5")
        self.assertEqual([editor.header[i] for i in (13, 15, 17, 25, 26, 28, 31)],
                         ["60500", "60.500", "67316", "19153", "608.03", "31.50000", "8"])
        editor.edit_item(0, 14, "11.5")
        self.assertEqual([editor.items[0][i] for i in (12, 13, 14)], ["21916", "695.75", "11.50"])
        self.assertEqual([editor.header[i] for i in (25, 26, 31)], ["21916", "695.75", "9"])
        self.assertEqual(editor.warnings(), [])

    def test_header_tons_only_updates_items_when_there_is_one(self):
        for second in (False, True):
            with self.subTest(second=second):
                self.sample(second=second)
                editor = TxtEditor(self.path)
                editor.edit_header(15, "60.5")
                self.assertEqual(editor.header[15], "60.500")
                self.assertEqual(editor.items[0][9], "50.000" if second else "60.500")
                self.assertEqual(editor.items[0][7], "50000" if second else "60500")
                if second:
                    self.assertIn("H15", "\n".join(editor.warnings()))

    def test_exchange_recalculates_each_item_but_keeps_weight_and_liters(self):
        self.sample(second=True)
        editor = TxtEditor(self.path)
        editor.edit_header(28, "31")
        self.assertEqual([item[12] for item in editor.items], ["15578", "15578"])
        self.assertEqual(editor.header[25], "31156")
        self.assertEqual(editor.header[17], "117864")
        self.assertEqual(editor.header[13], "100000")

    def test_missing_density_leaves_liters_and_shows_nonblocking_warnings(self):
        self.item[11] = self.header[17] = "2"
        self.sample()
        editor = TxtEditor(self.path)
        self.assertIsNone(editor.densities[0])
        editor.edit_item(0, 9, "52")
        self.assertEqual(editor.items[0][11], "2")
        editor.edit_item(0, 12, "1")
        editor.edit_item(0, 13, "1")
        editor.edit_header(31, "999")
        for fragment in ("無法核對 LTR", "美元金額與噸數", "H25", "H26", "H31"):
            self.assertIn(fragment, "\n".join(editor.warnings()))
        self.assertIsNotNone(editor.save())

    def test_edit_save_preserves_bom_newline_final_blank_lines_and_unmodified_fields(self):
        for bom, newline, final in ((True, "\r\n", True), (False, "\n", False), (True, "\n", False),
                                    (False, "\r\n", True)):
            with self.subTest(bom=bom, newline=repr(newline), final=final):
                data = self.sample(bom=bom, newline=newline, final=final)
                sep = newline.encode()
                data = data.replace(sep, sep + b"   " + sep, 1)
                self.path.write_bytes(data)
                editor = TxtEditor(self.path)
                self.assertIsNone(editor.save())
                self.assertEqual(self.path.read_bytes(), data)
                editor.edit_header(20, "TEST SHIP")
                with patch("fuelcore.txt_edit.datetime") as clock:
                    clock.now.return_value = datetime(2026, 11, 10, 12)
                    backup = editor.save()
                self.assertEqual(backup.parent, self.folder / BACKUP_FOLDER)
                self.assertRegex(backup.name, r"TEST\.\d{8}_\d{6}_\d{6}(?:_\d+)?\.txt")
                self.assertEqual(backup.read_bytes(), data)
                expected = data.replace(self.header[20].encode("utf-8"), b"TEST SHIP", 1)
                self.assertEqual(self.path.read_bytes(), expected)
                self.assertEqual([entry.path for entry in find_inputs(self.folder, (backup,), show_all=True)],
                                 [self.path])
        self.assertEqual(len(list((self.folder / BACKUP_FOLDER).glob("*.txt"))), 4)

    def test_backup_uses_configured_source_for_files_in_subdirectories(self):
        data = self.sample()
        dated = self.folder / "20261110"
        dated.mkdir()
        path = self.path.rename(dated / self.path.name)
        editor = TxtEditor(path, self.folder)
        editor.edit_header(20, "TEST SHIP")
        backup = editor.save()
        self.assertEqual(backup.parent, self.folder / BACKUP_FOLDER)
        self.assertEqual(backup.read_bytes(), data)

    def test_restore_validation_and_external_edit_protection(self):
        data = self.sample()
        editor = TxtEditor(self.path)
        with self.assertRaisesRegex(ValueError, "分隔符號"):
            editor.edit_item(0, 4, "split|field")
        with self.assertRaisesRegex(ValueError, "I0"):
            editor.edit_item(0, 0, "BAD")
        with self.assertRaisesRegex(ValueError, "密度"):
            editor.edit_density(0, "2")
        editor.edit_density(0, "0.9")
        editor.restore()
        self.assertEqual(editor.to_bytes(), data)
        self.assertIsNone(editor.save())
        editor.edit_header(20, "TEST SHIP")
        self.path.write_bytes(data + b"\n")
        with self.assertRaisesRegex(ValueError, "其他程式修改"):
            editor.save()
        self.assertEqual(self.path.read_bytes(), data + b"\n")
        self.assertFalse((self.folder / BACKUP_FOLDER).exists())

    def test_workbench_pending_double_click_and_density_cell_save(self):
        import tkinter as tk
        from tkinter import ttk

        from fuelcore.gui import Workbench
        from fuelcore.txt_view import TxtEditWindow

        original = self.sample()
        config = self.folder / "fuelcore.ini"
        config.write_bytes((ROOT / "fuelcore.ini.example").read_bytes())
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
        app = Workbench(root, config)

        def wait():
            deadline = time.monotonic() + 8
            while app.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)

        root.update()
        wait()
        app.show_files((self.folder, [InputFile(self.path, "", self.header[1])], []))
        wait()
        row, = app.files.get_children()
        event = SimpleNamespace(x=1, y=1)
        dialogs = []

        def open_dialog(*args):
            dialogs.append(TxtEditWindow(*args))

        with patch.object(app.files, "identify_region", return_value="cell"), \
                patch.object(app.files, "identify_row", return_value=row), \
                patch("fuelcore.gui.TxtEditWindow", side_effect=open_dialog):
            app.open_file_detail(event)
            wait()
        view, = dialogs
        window = view.window

        def widgets(widget):
            for child in widget.winfo_children():
                yield child
                yield from widgets(child)

        controls = list(widgets(window))
        oils, headers, items = (child for child in controls if isinstance(child, ttk.Treeview))
        self.assertEqual(oils.set("0", "txt_ltr"), "58932")
        self.assertEqual(headers.set("17", "original"), "58932")
        self.assertEqual(items.set("0", "i11"), "58932")
        with patch.object(oils, "identify_region", return_value="cell"), \
                patch.object(oils, "identify_row", return_value="0"), \
                patch.object(oils, "identify_column", return_value="#3"), \
                patch("fuelcore.txt_view.simpledialog.askstring", return_value=".9000"):
            view.edit_density(event)
        self.assertEqual(oils.set("0", "txt_ltr"), "55633")
        self.assertEqual(oils.item("0", "tags"), ("changed",))
        self.assertEqual(self.path.read_bytes(), original)
        save, = (child for child in controls if isinstance(child, ttk.Button) and child.cget("text") == "存檔")
        save.invoke()
        wait()
        self.assertIn(b"|55633|", self.path.read_bytes())
        backup, = (self.folder / BACKUP_FOLDER).glob("*.txt")
        self.assertEqual(backup.read_bytes(), original)
