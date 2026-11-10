import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fuelcore import Settings, convert_file
from fuelcore.agent_lookup import NetworkError, ShipResult
from fuelcore.codes import ship_call
from fuelcore.convert import read_txt
from fuelcore.schema import init_sqlite
from fuelcore.ship_view import ShipDialog
from fuelcore.ships import CORRUPT_WARNING, SHIP_FILENAME, Ship, change_ship, load_ships, valid_imo


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "資料" / "txt樣本" / "D4122_TEST_0001.txt"
ALPHA = Ship("TEST ALPHA", "9292187", "TEST1", "TEST BUYER")
BRAVO = Ship("TEST BRAVO", "9680437", "TEST2")


class ShipTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.path = self.folder / SHIP_FILENAME

    def test_imo_checksum_and_ascii_length(self):
        self.assertTrue(valid_imo(ALPHA.imo))
        self.assertFalse(valid_imo("9292182"))
        self.assertFalse(valid_imo("929218"))
        self.assertFalse(valid_imo("９２９２１８７"))
        self.assertFalse(valid_imo(9292187))

    def test_storage_crud_and_duplicate_validation(self):
        self.assertEqual(load_ships(self.path), ((), ""))
        self.assertEqual(change_ship(self.path, ALPHA), (ALPHA,))
        old = self.path.read_bytes()
        for ship, message in ((Ship("test alpha", BRAVO.imo, "TEST"), "船名重複"),
                              (Ship(BRAVO.name, ALPHA.imo, "TEST"), "IMO 重複"),
                              (Ship(" ", BRAVO.imo, "TEST"), "船名不可空白")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                change_ship(self.path, ship)
        self.assertEqual(self.path.read_bytes(), old)
        edited = Ship(ALPHA.name, ALPHA.imo, ALPHA.call_sign, agent="測試代理行")
        self.assertEqual(change_ship(self.path, edited, original=ALPHA), (edited,))
        self.assertEqual(load_ships(self.path), ((edited,), ""))
        self.assertEqual(change_ship(self.path, original=edited), ())
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_all_four_lookup_priorities_with_overlapping_candidates(self):
        change_ship(self.path, ALPHA)
        change_ship(self.path, BRAVO)
        db = Mock()
        db.table.return_value = "myshipname"
        for imo, name, rows, expected, basis, calls in (
            (ALPHA.imo, BRAVO.name, [[("DB1",)], [("DB2",)]], "TEST1", "船舶資料 IMO", 0),
            ("1111111", BRAVO.name.lower(), [[("DB1",)], [("DB2",)]], "TEST2", "船舶資料 船名", 0),
            ("1111111", "TEST OTHER", [[("DB1",)], [("DB2",)]], "DB1", "船舶代碼表 IMO", 1),
            ("1111111", "TEST OTHER", [[], [("DB2",)]], "DB2", "船舶代碼表 船名", 2),
        ):
            with self.subTest(basis=basis):
                db.select.reset_mock()
                db.select.side_effect = rows
                notes = []
                self.assertEqual(ship_call(db, "", imo, name, notes, ships_path=self.path), expected)
                self.assertEqual(db.select.call_count, calls)
                self.assertEqual(len(notes), 1)
                self.assertIn(basis, notes[0])
        db.select.reset_mock()
        notes = []
        self.assertEqual(ship_call(db, "TXT", ALPHA.imo, ALPHA.name, notes, ships_path=self.path), "TXT")
        db.select.assert_not_called()

    def test_corrupt_file_is_empty_and_conversion_falls_back_to_database(self):
        self.path.write_bytes(b'{"bad":')
        self.assertEqual(load_ships(self.path), ((), CORRUPT_WARNING))
        with self.assertRaisesRegex(ValueError, "已損壞"):
            change_ship(self.path, ALPHA)
        self.assertEqual(self.path.read_bytes(), b'{"bad":')
        ini = self.folder / "fuelcore.ini"
        ini.write_bytes((ROOT / "fuelcore.ini.example").read_bytes())
        settings = Settings.load(ini)
        self.assertEqual(settings.ships_path, self.path)
        init_sqlite(settings)
        header, items = read_txt(SAMPLE)
        header[27] = ""
        with closing(sqlite3.connect(settings.path)) as db, db:
            db.execute("INSERT INTO myshipname (SN002, SN003, SN004) VALUES (?,?,?)",
                       ("TESTDB", header[20], header[22]))
        txt = self.folder / "TEST.txt"
        txt.write_text("\n".join("|".join(row) for row in (header, *items)), encoding="utf-8")
        result = convert_file(txt, settings)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.notes.count(CORRUPT_WARNING), 1)
        with closing(sqlite3.connect(settings.path)) as db:
            self.assertEqual(db.execute("SELECT BAID FROM cusdec").fetchone(), ("TESTDB",))

    def test_conversion_can_find_original_txt_ship_name_in_local_list(self):
        ini = self.folder / "fuelcore.ini"
        ini.write_bytes((ROOT / "fuelcore.ini.example").read_bytes())
        settings = Settings.load(ini)
        settings.write_codes = False
        init_sqlite(settings)
        change_ship(self.path, Ship("TEST & SHIP", ALPHA.imo, ALPHA.call_sign))
        header, items = read_txt(SAMPLE)
        header[20], header[27] = "test & ship", ""
        txt = self.folder / "TEST.txt"
        txt.write_text("\n".join("|".join(row) for row in (header, *items)), encoding="utf-8")
        result = convert_file(txt, settings)
        self.assertTrue(result.ok, result.message)
        self.assertIn("自動填入呼號 TEST1（依船舶資料 船名 test & ship）", result.notes)
        with closing(sqlite3.connect(settings.path)) as db:
            self.assertEqual(db.execute("SELECT BAID FROM cusdec").fetchone(), ("TEST1",))

    def test_query_button_only_updates_agent_and_reports_network_error(self):
        name, agent = Mock(), Mock()
        name.get.return_value = ALPHA.name
        dialog = SimpleNamespace(app=SimpleNamespace(busy=False), inputs={"name": name, "agent": agent},
                                 winfo_exists=lambda: True)

        def task(_, work, finish, failed):
            try:
                result = work()
            except NetworkError as error:
                failed(str(error))
            else:
                finish(result)

        dialog.app.run_task = task
        with patch("fuelcore.ship_view.lookup_ship", return_value=ShipResult("no_call_sign", agent_full="TEST AGENT")) as lookup:
            lookup.assert_not_called()
            ShipDialog.query_agent(dialog)
        lookup.assert_called_once_with(ALPHA.name)
        agent.insert.assert_called_once_with(0, "TEST AGENT")
        agent.reset_mock()
        with patch("fuelcore.ship_view.lookup_ship", side_effect=NetworkError("TEST OFFLINE")), \
                patch("fuelcore.ship_view.messagebox.showinfo") as show:
            ShipDialog.query_agent(dialog)
        self.assertIn("TEST OFFLINE", show.call_args.args[1])
        agent.insert.assert_not_called()

    def test_settings_and_txt_editor_share_ship_file_without_network(self):
        import tkinter as tk

        from fuelcore.gui import Workbench, make_table
        from fuelcore.txt_edit import TxtEditor
        from fuelcore.txt_view import TxtEditWindow

        ini = self.folder / "fuelcore.ini"
        ini.write_bytes((ROOT / "fuelcore.ini.example").read_bytes())
        path = self.folder / "TEST.txt"
        path.write_bytes(SAMPLE.read_bytes())
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
        app = Workbench(root, ini)

        def wait():
            deadline = time.monotonic() + 8
            while app.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)

        root.update()
        wait()
        panel = app.settings_pages.ship_panel
        with patch("fuelcore.ship_view.lookup_ship") as network:
            with patch("fuelcore.ship_view.ShipDialog", return_value=SimpleNamespace(result=ALPHA)):
                panel.edit()
                wait()
            self.assertEqual(load_ships(self.path), ((ALPHA,), ""))
            editor = TxtEditor(path)
            view = TxtEditWindow(app, editor, make_table)
            view.ship_combo.current(0)
            view.select_ship()
            self.assertEqual([editor.header[i] for i in (20, 22, 27, 23)],
                             [ALPHA.name, ALPHA.imo, ALPHA.call_sign, ALPHA.buyer_id])
            with patch("fuelcore.txt_view.ShipDialog", return_value=SimpleNamespace(result=BRAVO)) as dialog:
                view.add_ship()
                wait()
            self.assertEqual(dialog.call_args.args[-1], ALPHA)
            self.assertEqual(load_ships(self.path), ((ALPHA, BRAVO), ""))
            view.ship_combo.current(1)
            view.select_ship()
            self.assertEqual(editor.header[20], BRAVO.name)
            self.assertEqual(editor.header[23], ALPHA.buyer_id)
            network.assert_not_called()
        self.assertEqual(path.read_bytes(), SAMPLE.read_bytes())
