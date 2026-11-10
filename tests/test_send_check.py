import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fuelcore.edi.simulation import SimulationResult
from fuelcore.schema import init_sqlite
from fuelcore.send_check import prepare
from fuelcore.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
FIRST = "TEST00A10E0001"
SECOND = "TEST00A10E0002"


class SendCheckTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.config = self.folder / "fuelcore.ini"
        example = (ROOT / "fuelcore.ini.example").read_text(encoding="utf-8")
        self.config.write_text(example + "\n[edi]\nedi_database = test_edi\nxmlset_database = test_xml\n"
                               "company_code = 00000000\nuser = TEST\n", encoding="utf-8")
        self.settings = Settings.load(self.config)
        self.today = date.today()
        init_sqlite(self.settings)
        self.add_declaration(FIRST)

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.settings.path)) as db, db:
            db.execute(sql, params)

    def add_declaration(self, sersno, *, day=None, call="TEST001", status="Y", quantity="2", product="TEST FUEL"):
        self.execute(
            "INSERT INTO cusdec (MSGCODE,SERSNO,SERSDATE,BANAME,BAID,BYNAME,BYBAN,STATUS,PKGTOTAL,PKGUNIT,GWEIGHT) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("5203S", sersno, (day or self.today).isoformat() + " 00:00:00", "TEST SHIP",
             call, "TEST BUYER", "TTBR", status, 2, "TNE", "2000"),
        )
        self.execute(
            "INSERT INTO invitem (MSGCODE,SERSNO,PINVORDER,`項次號碼`,`報單品名`,`貨品名稱`,`數量`,`數量單位`,"
            "`報單淨重`,UNITAMT,FOBPRICE) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("5203S", sersno, 1, 7, product, "TEST FALLBACK", quantity, "TNE", "2000", "1000", "2000"),
        )

    def result(self):
        return prepare([FIRST], self.settings)[0]

    def test_valid_declaration_is_read_only_and_returns_item_fields(self):
        before = self.settings.path.read_bytes()
        entry = self.result()
        self.assertEqual(self.settings.path.read_bytes(), before)
        self.assertTrue(entry.can_send)
        self.assertEqual(entry.warnings, [])
        self.assertEqual((entry.ship_name, entry.call_sign, entry.buyer, entry.status),
                         ("TEST SHIP", "TEST001", "TEST BUYER", "Y"))
        item, = entry.items
        self.assertEqual((item.number, item.name, item.quantity, item.unit, item.net_weight,
                          item.unitamt, item.fobprice),
                         ("7", "TEST FUEL", Decimal(2), "TNE", Decimal(2000), Decimal(1000), Decimal(2000)))
        self.execute("UPDATE invitem SET `報單品名` = '  '")
        self.assertEqual(self.result().items[0].name, "TEST FALLBACK")

    def test_blank_zero_and_negative_quantities_block(self):
        for quantity, blocked in [(None, True), ("  ", True), ("0", True), ("-1", True), ("2", False)]:
            with self.subTest(quantity=quantity):
                self.execute("UPDATE invitem SET `數量` = ?", (quantity,))
                entry = self.result()
                self.assertEqual(bool(entry.blocks), blocked)
                self.assertEqual(entry.can_send, not blocked)

    def test_future_date_blocks_and_today_does_not(self):
        for offset in (0, 1):
            self.execute("UPDATE cusdec SET SERSDATE = ?", ((self.today + timedelta(days=offset)).isoformat(),))
            self.assertEqual(self.result().blocks, ["報關日期在今天之後。"] if offset else [])

    def test_date_older_than_seven_days_warns(self):
        for age in (7, 8):
            self.execute("UPDATE cusdec SET SERSDATE = ?", ((self.today - timedelta(days=age)).isoformat(),))
            self.assertEqual(bool(self.result().warnings), age > 7)

    def test_history_counts_only_matching_number_and_message(self):
        self.execute("CREATE TABLE sendmsgrcds (`報單號碼` TEXT, `訊息簡稱` TEXT, `傳送時間` TEXT)")
        for sersno, msg, stamp in [(FIRST, "5204S", "2026-11-09"), (SECOND, "5203S", "2026-11-09")]:
            self.execute("INSERT INTO sendmsgrcds VALUES (?,?,?)", (sersno, msg, stamp))
        self.assertFalse(self.result().warnings)
        for stamp in ("2026-11-04 10:00:00", "2026-11-03 09:00:00"):
            self.execute("INSERT INTO sendmsgrcds VALUES (?,?,?)", (FIRST, "5203S", stamp))
        self.assertEqual(self.result().warnings, ["以前送過 2 次（最後一次 2026-11-04 10:00:00）。"])

    def test_item_weight_tne_relative_tolerance(self):
        for weight, warned in [(2002, False), (2003, True)]:
            self.execute("UPDATE invitem SET `報單淨重` = ?", (weight,))
            self.assertEqual(any("第 7 項報單淨重" in text for text in self.result().warnings), warned)

    def test_item_weight_kgm_minimum_tolerance_and_other_unit(self):
        for unit, weight, warned in [("KGM", 3, False), ("KGM", 4, True), ("LTR", 4, False)]:
            self.execute("UPDATE invitem SET `數量單位` = ?, `報單淨重` = ?", (unit, weight))
            self.assertEqual(any("第 7 項報單淨重" in text for text in self.result().warnings), warned)

    def test_amount_tolerances(self):
        for price, amount, warned in [(1000, 2002, False), (1000, 2003, True), (10, 21, False), (10, 22, True)]:
            self.execute("UPDATE invitem SET UNITAMT = ?, FOBPRICE = ?", (price, amount))
            self.assertEqual(any("FOBPRICE" in text for text in self.result().warnings), warned)

    def test_package_total_half_unit_and_matching_units(self):
        self.execute("UPDATE invitem SET `數量` = '2.5'")
        for total, unit, warned in [(3, "TNE", False), (4, "TNE", True), (4, "KGM", False)]:
            self.execute("UPDATE cusdec SET PKGTOTAL = ?, PKGUNIT = ?", (total, unit))
            self.assertEqual(any("PKGTOTAL" in text for text in self.result().warnings), warned)

    def test_gross_weight_total_tolerance(self):
        for weight, warned in [(2002, False), (2003, True)]:
            self.execute("UPDATE cusdec SET GWEIGHT = ?", (weight,))
            self.assertEqual(any("GWEIGHT" in text for text in self.result().warnings), warned)

    def test_buyer_code(self):
        self.assertFalse(self.result().warnings)
        self.execute("UPDATE cusdec SET BYBAN = 'TESTX'")
        self.assertEqual(len(self.result().warnings), 1)
        self.assertIn("BYBAN", self.result().warnings[0])

    def test_blank_call_sign(self):
        self.assertFalse(self.result().warnings)
        self.execute("UPDATE cusdec SET BAID = '  '")
        self.assertEqual(self.result().warnings, ["呼號空白。"])

    def test_ship_name_compares_case_and_spaces(self):
        self.execute("INSERT INTO myshipname (SN002, SN003) VALUES (?,?)", ("TEST001", " test   ship "))
        self.assertFalse(self.result().warnings)
        self.execute("UPDATE myshipname SET SN003 = 'TEST OTHER'")
        self.assertEqual(self.result().warnings, ["船舶代碼表的船名與報單船名不同。"])

    def test_batch_duplicates_need_complete_matching_items_and_nearby_date(self):
        self.add_declaration(SECOND, day=self.today - timedelta(days=3), call="TEST002", quantity="2.0000")
        self.assertFalse(self.result().warnings)
        batch = prepare([FIRST, SECOND], self.settings)
        self.assertEqual(batch[0].warnings, [f"可能重複報單：{SECOND}。"])
        self.assertEqual(batch[1].warnings, [f"可能重複報單：{FIRST}。"])
        self.execute("UPDATE cusdec SET SERSDATE = ? WHERE SERSNO = ?",
                     ((self.today - timedelta(days=4)).isoformat(), SECOND))
        self.assertFalse(prepare([FIRST, SECOND], self.settings)[0].warnings)
        self.execute("UPDATE cusdec SET SERSDATE = ? WHERE SERSNO = ?", (self.today.isoformat(), SECOND))
        self.execute("UPDATE invitem SET `報單品名` = ? WHERE SERSNO = ?", ("TEST OTHER", SECOND))
        self.assertFalse(prepare([FIRST, SECOND], self.settings)[0].warnings)

    def test_historical_duplicates_only_check_latest_ten_for_call_sign(self):
        self.add_declaration(SECOND, day=self.today - timedelta(days=3))
        self.assertEqual(self.result().warnings, [f"可能重複報單：{SECOND}。"])
        for index in range(3, 13):
            self.add_declaration(f"TEST00A10E{index:04d}", product=f"TEST DIFFERENT {index}")
        self.assertFalse(self.result().warnings)

    def test_duplicates_compare_all_items_independently_of_order(self):
        self.add_declaration(SECOND, product="TEST OTHER")
        self.execute(
            "INSERT INTO invitem (MSGCODE,SERSNO,PINVORDER,`項次號碼`,`報單品名`,`數量`,`數量單位`) "
            "VALUES ('5203S',?,2,8,?,2,'TNE')", (FIRST, "TEST OTHER"),
        )
        self.assertFalse(any("可能重複" in warning for warning in self.result().warnings))
        self.execute(
            "INSERT INTO invitem (MSGCODE,SERSNO,PINVORDER,`項次號碼`,`報單品名`,`數量`,`數量單位`) "
            "VALUES ('5203S',?,2,8,?,2,'TNE')", (SECOND, "TEST FUEL"),
        )
        self.assertTrue(any("可能重複" in warning for warning in self.result().warnings))

    def test_missing_optional_tables_and_bad_values_leave_other_checks_active(self):
        self.execute("DROP TABLE myshipname")
        self.execute("UPDATE invitem SET `報單淨重` = 'unknown', UNITAMT = '1e99999999'")
        self.execute("UPDATE cusdec SET PKGTOTAL = 0")
        entry = self.result()
        self.assertTrue(entry.can_send)
        self.assertEqual(entry.warnings, ["主檔 PKGTOTAL 與項次數量合計不符。"])
        self.execute("UPDATE cusdec SET BYBAN = 'TESTX'")
        self.execute("DROP TABLE invitem")
        entry = self.result()
        self.assertFalse(entry.can_send)
        self.assertEqual(entry.blocks, [])
        self.assertTrue(any("BYBAN" in warning for warning in entry.warnings))

    def test_simulation_eligibility_requires_y_and_items(self):
        self.assertTrue(self.result().can_send)
        self.execute("UPDATE cusdec SET STATUS = 'S'")
        self.assertFalse(self.result().can_send)
        self.execute("UPDATE cusdec SET STATUS = 'Y'")
        self.execute("DELETE FROM invitem")
        self.assertFalse(self.result().can_send)
        self.assertFalse(prepare([SECOND], self.settings)[0].can_send)

    def test_cli_exit_codes_for_warning_and_block(self):
        self.execute("UPDATE cusdec SET BAID = ''")
        command = [sys.executable, "-X", "utf8", "-m", "fuelcore", "send-check", FIRST, "--config", str(self.config)]
        warned = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(warned.returncode, 0, warned.stderr)
        self.assertIn("需確認", warned.stdout)
        self.execute("UPDATE invitem SET `數量` = '0'")
        blocked = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(blocked.returncode, 1, blocked.stderr)
        self.assertIn("不能送", blocked.stdout)

    def test_workbench_simulates_only_eligible_confirmed_rows(self):
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
        workbench = Workbench(root, self.config)
        self.add_declaration(SECOND, call="TEST002", quantity="0")
        self.execute("UPDATE cusdec SET BAID = '' WHERE SERSNO = ?", (FIRST,))

        def wait():
            deadline = time.monotonic() + 8
            while workbench.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(workbench.busy)

        root.update()
        wait()
        workbench.send_numbers.insert(0, f"{FIRST}, {SECOND}")
        workbench.check_before_send()
        self.assertTrue(workbench.busy)
        wait()
        rows = workbench.send_table.get_children()
        self.assertEqual([workbench.send_table.set(row, "result") for row in rows], ["需確認", "不能送"])
        workbench.send_table.selection_set(*rows)
        workbench.show_send_details()
        self.assertIn("呼號空白", workbench.send_details.get("1.0", "end"))
        self.assertIn("數量空白或不大於 0", workbench.send_details.get("1.0", "end"))
        with patch("fuelcore.gui.messagebox.askyesno", side_effect=[False, True, True]) as ask, \
                patch("fuelcore.gui.filedialog.askdirectory", return_value=str(self.folder)), \
                patch("fuelcore.gui.messagebox.showinfo") as info, \
                patch("fuelcore.gui.simulate_send", return_value=SimulationResult(self.folder, 1, 0)) as simulate:
            workbench.simulate_selected_send()
            simulate.assert_not_called()
            workbench.simulate_selected_send()
            wait()
            self.execute("UPDATE cusdec SET STATUS = 'S' WHERE SERSNO = ?", (FIRST,))
            workbench.simulate_selected_send()
            wait()
        self.assertEqual(ask.call_count, 3)
        self.assertEqual(simulate.call_count, 1)
        self.assertEqual(simulate.call_args.args[1:], ([FIRST], self.folder))
        self.assertIn("狀態或提醒已變更", info.call_args.args[1])
