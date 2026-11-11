import ast
import importlib.util
import os
import re
import sqlite3
import tempfile
import time
import unittest
from copy import deepcopy
from decimal import Decimal as D, ROUND_DOWN, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from fuelcore.report import ReportBatch
from fuelcore.report.calc import ItemInput, OriginalItem, Trial
from fuelcore.report.editing import EditableReport, TrialFields, report_ids
from fuelcore.report.rpx import RpxDocument, RpxError

from test_edi import load_fixture_tables, settings
from test_printing import FakePrinter
from test_report import PDF_AVAILABLE, REPORTS, component


def sample_trial():
    items = tuple(OriginalItem(str(i), *map(D, values)) for i, values in enumerate(
        (("20", "902.70", "20000", "21757", "581609"), ("25", "1312.20", "25000", "30123", "1056813"),
         ("60", "902.70", "60000", "65270", "1744829")), 1))
    return Trial.from_original(items, D("32.215"), D(105000), D(105), D(1353), D(1353))


def changed_first(trial, quantity, price="902.70", density=None):
    return (ItemInput(D(quantity), D(price), D(density) if density else None), *trial.inputs()[1:])


class TrialTest(unittest.TestCase):
    def test_editing_item_quantity_recalculates_the_report_in_memory(self):
        trial = sample_trial()
        original = trial.calculate()
        page = component("TGPage", "Front", 0, 0, 400, 600)
        fields = [("FOREX", original.exchange), ("SHIPNAME", "ORIGINAL SHIP")]
        quantities, prices = [], []
        for item in trial.items:
            quantities.append(len(fields) + 1)
            prices.append(len(fields) + 2)
            fields.extend((("項次號碼", item.number), ("數量", item.quantity), ("UNITAMT", item.price),
                           ("報單淨重", item.net), ("DUTYAMT", item.twd), ("數量單位", item.unit),
                           ("統計數量", item.liters), ("統計單位", item.stat_unit)))
        fields.extend((("TOT_NETWT", original.net), ("TWDPRICE", original.twd), ("TOTALAMT", original.usd),
                       ("EXPQUAN", original.quantity), ("GWEIGHT", original.gross), ("PKGTOTAL", original.packages),
                       ("TAXAMT", original.fee), ("TAXTOTAL", original.tax_total)))
        page.children = [component("TGTxt", f"Field{top}", 0, top, 100, 12,
                                   PrtConditions=key, Caption=str(value))
                         for top, (key, value) in enumerate(fields)]
        document = RpxDocument.create("測試", [page])
        report = EditableReport(ReportBatch(["TEST00A10E0777"], None, None, [(page, False)], [page], []),
                                document, deepcopy([page]), {})
        original_rpx = document.to_bytes()

        with patch("fuelcore.report.editing.new_copy") as write, patch("fuelcore.report.editing.Database") as database:
            unchanged = report.edit(0, quantities[0], "20.00")
            self.assertEqual(unchanged.document.to_bytes(), original_rpx)
            caption_edits = report.edit(0, 1, "UPDATED SHIP").edit(0, prices[0], "PRICE NOTE").edit(0, 0, "RATE NOTE")
            with self.assertRaisesRegex(RpxError, "有效的非負數值"):
                caption_edits.edit(0, quantities[0], "21 TNE")
            updated = caption_edits.edit(0, quantities[0], "21")
            inputs = updated.inputs(0)
            with_density = updated.apply(0, (inputs[0], ItemInput(D("26"), inputs[1].price, D("0.923")), inputs[2]))
            updated_again = with_density.edit(0, quantities[0], "22")
            write.assert_not_called()
            database.assert_not_called()

        front = updated.batch.fronts[0]
        self.assertEqual(front.children[1].text("Caption"), "UPDATED SHIP")
        self.assertEqual(caption_edits.batch.fronts[0].children[prices[0]].text("Caption"), "PRICE NOTE")
        self.assertIs(updated.document.roots[0], front)
        self.assertIs(updated.batch.pages[0][0], front)
        self.assertEqual(updated.inputs(0)[0], ItemInput(D("21"), D("902.70")))
        self.assertEqual(updated.exchange(0), D("32.215"))
        _, current = updated.correction_values(0, None, None)
        self.assertEqual((current.quantity, current.usd, current.twd, current.net, current.fee),
                         (D("106"), D("105923.70"), D("3412332"), D("106000"), D("1364")))
        self.assertEqual((current.rows[0].liters, current.rows[0].twd), (D("22845"), D("610690")))
        self.assertEqual(updated_again.inputs(0)[1], ItemInput(D("26"), D("1312.20"), D("0.923")))
        _, with_density_values = updated_again.correction_values(0, None, None)
        expected = trial.calculate(updated_again.inputs(0))
        self.assertEqual(with_density_values.rows[1], expected.rows[1])
        self.assertEqual(report.document.to_bytes(), original_rpx)

    def test_correction_uses_original_and_current_report_captions(self):
        page = component("TGPage", "Front", 0, 0, 400, 600)
        fields = [
            ("FOREX", "32.215", 10), ("項次號碼", "1", 20), ("數量", "20", 21),
            ("UNITAMT", "902.70", 22), ("報單淨重", "20000", 23), ("DUTYAMT", "581609", 24),
            ("數量單位", "TNE", 25), ("統計數量", "(21757", 26), ("統計單位", "LTR", 27),
            ("TOT_NETWT", "20000", 40), ("TWDPRICE", "581610", 41),
        ]
        page.children = [component("TGTxt", f"Field{top}", 0, top, 100, 12,
                                   PrtConditions=key, Caption=value) for key, value, top in fields]
        document = RpxDocument.create("測試", [page])
        report = EditableReport(ReportBatch(["TEST00A10E0777"], None, None, [], [page], []),
                                document, deepcopy([page]), {})
        TrialFields(page).apply(report.trial(0).calculate(report.inputs(0), exchange=D("30")))
        trial, current = report.correction_values(0, None, None)
        self.assertEqual(trial.calculate().twd, 581610)
        self.assertEqual(trial.calculate().rows[0].twd, 581609)
        self.assertEqual(current.exchange, D("32.215"))
        self.assertEqual(current.rows[0].twd, 541620)

    def test_exchange_change_recomputes_all_items_but_keeps_filed_values(self):
        trial = sample_trial()
        result = trial.calculate(trial.inputs(), exchange=D("30"))
        self.assertEqual(result.exchange, D("30"))
        self.assertEqual([row.twd for row in result.rows], [541620, 984150, 1624860])
        self.assertEqual(trial.calculate().rows[0].twd, 581609)
        self.assertEqual(trial.calculate().exchange, D("32.215"))

    def test_original_and_legacy_modified_cases(self):
        trial = sample_trial()
        original = trial.calculate()
        self.assertEqual(trial.apis, (D("22.1"), D("38.6"), D("22.1")))
        self.assertEqual([(row.usd, row.twd, row.net, row.liters) for row in original.rows],
                         [(18054, 581609, 20000, 21757), (32805, 1056813, 25000, 30123),
                          (54162, 1744829, 60000, 65270)])
        self.assertEqual((original.quantity, original.usd, original.twd, original.net, original.liters,
                          original.gross, original.packages, original.fee, original.tax_total),
                         (105, 105021, 3383251, 105000, 117150, 105000, 105, 1353, 1353))
        # Values captured by running the old HTML's compute() against the same fixture inputs.
        cases = [
            (("21",), ("106", "105923.70", 3412332, 106000, 118238, 1364, 22845, 610690)),
            (("18.125",), ("103.13", "103328.44", 3328726, 103125, 115110, 1331, 19717, 527084)),
            (("22", "910.125", "0.923"), ("107", "106989.75", 3446675, 107000, 119263, 1378, 23870, 645033)),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                result = trial.calculate(changed_first(trial, *args))
                self.assertEqual((result.quantity, result.usd, result.twd, result.net, result.liters,
                                  result.fee, result.rows[0].liters, result.rows[0].twd), tuple(map(D, expected)))


def evaluate_cell(sheet, coordinate):
    """Independently evaluate the supported Excel formulas used by this form."""
    value = sheet[coordinate].value
    if not isinstance(value, str) or not value.startswith("="):
        return D(str(value))
    expression = value[1:].replace("$", "").replace("=", "==")
    tree = ast.parse(expression, mode="eval")

    def numeric(node):
        if isinstance(node, ast.Constant):
            return D(ast.get_source_segment(expression, node))
        if isinstance(node, ast.Name):
            return evaluate_cell(sheet, node.id)
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Eq):
            return numeric(node.left) == numeric(node.comparators[0])
        if isinstance(node, ast.BinOp):
            left, right = numeric(node.left), numeric(node.right)
            operator = type(node.op)
            return {ast.Add: D.__add__, ast.Sub: D.__sub__, ast.Mult: D.__mul__, ast.Div: D.__truediv__}[operator](left, right)
        if isinstance(node, ast.Call):
            name = node.func.id
            values = [numeric(arg) for arg in node.args]
            if name in ("ROUND", "ROUNDDOWN"):
                mode = ROUND_DOWN if name == "ROUNDDOWN" else ROUND_HALF_UP
                return values[0].quantize(D(1).scaleb(-int(values[1])), rounding=mode)
            if name == "IF":
                return values[1] if values[0] else values[2]
            return {"SUM": sum, "MAX": max, "AND": all}[name](values)
        raise AssertionError(ast.dump(node))

    return numeric(tree.body)


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "請安裝 report extras")
class CorrectionTest(unittest.TestCase):
    def test_excel_styles_formulas_and_recalculation(self):
        from io import BytesIO
        from openpyxl import load_workbook
        from fuelcore.report.correction import correction_bytes

        trial = sample_trial()
        result = trial.calculate(changed_first(trial, "21"))
        content = correction_bytes(trial, result, "DA/G5/15/A10/E0777", "115/10/23")
        sheet = load_workbook(BytesIO(content), rich_text=True).active
        stored = load_workbook(BytesIO(content), data_only=True).active
        self.assertEqual(str(sheet["A4"].value), "     報單號碼  DA / G5 / 15 / A10 / E0777")
        self.assertEqual([(part.text, part.font.u) for part in sheet["A4"].value][1:],
                         [("DA / G5 / 15 / A10 / E0777", "single")])
        self.assertNotIn("A4:C4", sheet.merged_cells)
        self.assertEqual(sheet.title, "修改報表")
        self.assertEqual([sheet.column_dimensions[c].width for c in "ABC"], [21.5, 37.5, 41.5])
        self.assertEqual(sheet["A12"].value, 38.1)
        self.assertIn("115年10月23日", sheet["A5"].value)
        self.assertEqual(sheet["B8"].border.bottom.style, "thin")
        self.assertEqual(sheet["B8"].alignment.horizontal, "center")
        self.assertTrue(sheet.print_options.horizontalCentered)
        self.assertTrue(sheet.print_options.verticalCentered)
        self.assertTrue(sheet.sheet_properties.pageSetUpPr.fitToPage)
        self.assertTrue(sheet.row_dimensions[15].hidden)
        self.assertTrue(sheet.row_dimensions[16].hidden)
        self.assertFalse(sheet.row_dimensions[12].hidden)
        self.assertIn("ROUNDDOWN", sheet["C30"].value)
        for col, expected in (("B", trial.calculate()), ("C", result)):
            for row, name in ((9, "usd"), (10, "twd"), (20, "net"), (21, "quantity"),
                              (22, "liters"), (27, "packages"), (28, "gross"), (30, "fee")):
                self.assertEqual(D(str(stored[f"{col}{row}"].value)), getattr(expected, name))
        self.assertEqual(evaluate_cell(sheet, "B10"), 3383252)
        self.assertEqual(evaluate_cell(sheet, "C10"), result.twd)
        sheet["C12"] = D("18.125")
        updated = trial.calculate(changed_first(trial, "18.125"))
        self.assertEqual(evaluate_cell(sheet, "C30"), updated.fee)
        self.assertEqual(evaluate_cell(sheet, "C13"), updated.rows[0].liters)

    def test_unchanged_form_shows_all_rows_and_distinct_rates_are_used(self):
        from fuelcore.report.correction import workbook

        trial = sample_trial()
        before = trial.calculate()
        unchanged = workbook(trial, before, "TEST00A10E0777", "").active
        self.assertFalse(any(unchanged.row_dimensions[row].hidden for row in range(9, 32)))
        after = trial.calculate(changed_first(trial, "18.125"), exchange=D("30"))
        changed = workbook(trial, after, "TEST00A10E0777", "").active
        self.assertIn("32.215", changed["B23"].value)
        self.assertIn("*30", changed["C23"].value)
        self.assertIn("#,##0.000", changed["C12"].number_format)


@unittest.skipUnless(os.environ.get("FUELCORE_CBS_REPORTS") and PDF_AVAILABLE, "請設定本機報表與 report extras")
class EditableReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name)
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.db.close)
        load_fixture_tables(self.db)
        self.sql = []
        self.db.set_trace_callback(self.sql.append)
        self.settings = settings()
        self.settings.printer = "Fake Office"

    def load(self):
        return EditableReport.from_database(self.settings, REPORTS, cache=self.output / "cache", connection=self.db)

    def test_copy_keeps_original_release_and_other_declaration(self):
        from contextlib import closing
        import pypdfium2

        report = self.load()
        path = report.save(self.output, 0)
        digest = sha256(path.read_bytes()).digest()
        reopened = EditableReport.open(path, cache=self.output / "cache", backup=self.output)
        backup, = (self.output / "原檔備份").glob("*.Rpx")
        self.assertEqual(sha256(backup.read_bytes()).digest(), digest)
        trial = reopened.trial(0)
        self.assertEqual(trial.calculate().rows, sample_trial().calculate().rows)
        self.assertEqual(report_ids(self.settings, "TEST", connection=self.db), list(reversed(REPORTS)))
        files = set(self.output.rglob("*"))
        edited = reopened.apply(0, changed_first(trial, "21"))
        self.assertEqual(edited.release(0), reopened.release(0))
        self.assertEqual(edited.batch.fronts[1], reopened.batch.fronts[1])
        self.assertEqual(TrialFields(edited.batch.fronts[0]).totals["fee"].text("Caption"), "1,364")
        self.assertEqual(sha256(path.read_bytes()).digest(), digest)
        position = edited.fields(1)[0][0]
        modified = edited.edit(1, position, "FIELD COPY")
        self.assertEqual(set(self.output.rglob("*")), files)
        edited_path = modified.save(self.output, 0)
        self.assertNotEqual(edited_path, path)
        self.assertEqual(sha256(path.read_bytes()).digest(), digest)
        self.assertEqual(modified.batch.fronts[0], edited.batch.fronts[0])
        self.assertEqual(modified.batch.fronts[1].children[position].text("Caption"), "FIELD COPY")
        with closing(pypdfium2.PdfDocument(edited.export_pdf(self.output))) as pdf:
            self.assertEqual(len(pdf), 4)
        self.assertTrue(self.sql and all(sql.startswith("SELECT ") for sql in self.sql))

    def test_opened_report_compares_visible_direct_fields_with_test_database(self):
        from fuelcore.report.comparison import compare_report
        from fuelcore.report_view import saved_rpx

        report = self.load()
        generated = report.save_generated(self.output)
        report = EditableReport.open(generated, cache=self.output / "cache", backup=self.output)
        self.db.execute("UPDATE cusdec SET BANAME = ? WHERE SERSNO = ?", ("TEST CHANGED", REPORTS[0]))
        self.db.commit()
        self.sql.clear()
        differences, warnings = compare_report(report, self.settings, connection=self.db)
        self.assertFalse(warnings)
        matches = [diff for diff in differences[0] if diff.label == "CUSDEC.BANAME"]
        self.assertEqual([diff.database for diff in matches], ["TEST CHANGED"])
        self.assertFalse(any(diff.label == "CUSDEC.BANAME" for diff in differences[1]))
        self.assertTrue(self.sql and all(sql.startswith("SELECT ") for sql in self.sql))
        self.assertEqual({kind for _, kind, _ in saved_rpx(self.output)}, {"資料庫產生", "原檔備份"})

    def test_gui_load_trial_and_confirm_print(self):
        import tkinter as tk
        from tkinter import ttk

        from fuelcore.gui import Workbench
        from fuelcore.report.printing import plan_print

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
        app = Workbench(root, self.output / "fuelcore.ini")
        app.refresh = lambda: None
        app.show("報單列印")
        page = app.report_page
        page.output = self.output

        def await_task():
            limit = time.monotonic() + 20
            while app.busy and time.monotonic() < limit:
                root.update()
                time.sleep(0.01)
            self.assertFalse(app.busy)

        with patch.object(page, "show_error", side_effect=AssertionError):
            root.update()
            page.load_task(self.load)
            await_task()
            self.assertEqual(page.pages.current(), 0)
            self.assertEqual(len(page.pages["values"]), 4)
            self.assertEqual(tuple(page.buttons), ("修改欄位", "修改數量", "列印報單", "列印更正申請書",
                                                   "輸出 PDF", "更正申請書 Excel", "另存 Rpx"))
            with patch.object(page.canvas, "winfo_width", return_value=1124), \
                    patch.object(page.canvas, "winfo_height", return_value=824), \
                    patch.object(page.canvas, "winfo_fpixels", return_value=144):
                page.preview()
                self.assertLessEqual(abs(page.image.width() - 1100), 1)
                page.zoom.set("整頁")
                page.preview()
                self.assertLessEqual(abs(page.image.height() - 800), 1)
                page.zoom.set("150%")
                page.preview()
                self.assertGreater(page.image.width(), 1500)
                page.zoom.set("符合寬度")
            self.assertTrue(page.buttons["另存 Rpx"].instate(["disabled"]))
            files = set(self.output.rglob("*"))
            dialog = page.open_trial()
            self.assertTrue(dialog.exchange_entry.instate(["readonly"]))
            self.assertTrue(all(entry.instate(["readonly"]) for entry in dialog.price_entries))
            self.assertEqual(dialog.price_entries[0].get(), "902.70")
            self.assertEqual(dialog.exchange_entry.get(), "32.215")
            dialog.values[0][0].set("21")
            self.assertIn("1,364", dialog.total.get())
            dialog.window.destroy()

            page.buttons["修改欄位"].invoke()
            window = next(widget for widget in page.page.winfo_children() if isinstance(widget, tk.Toplevel))
            picker = next(widget for widget in window.winfo_children() if isinstance(widget, ttk.Combobox))
            editor = next(widget for widget in window.winfo_children() if isinstance(widget, tk.Text))
            front = page.report.batch.fronts[0]
            quantity = TrialFields(front).items[0][1]["quantity"]
            picker.current(next(i for i, (position, _, _) in enumerate(page.report.fields(0))
                                if front.children[position] is quantity))
            editor.delete("1.0", "end")
            editor.insert("1.0", "21")
            next(widget for widget in window.winfo_children() if isinstance(widget, ttk.Button)).invoke()
            await_task()
            self.assertEqual(set(self.output.rglob("*")), files)
            self.assertEqual(page.dirty_notice.get(), "已修改，未存檔")
            self.assertFalse(page.buttons["另存 Rpx"].instate(["disabled"]))
            self.assertEqual(page.report.correction_values(0, None, None)[1].fee, D("1364"))

            with patch("fuelcore.report_view.messagebox.askyesnocancel", return_value=None) as ask:
                page.pages.current(1)
                page.select_page()
                ask.assert_not_called()
                page.pages.current(2)
                page.select_page()
                self.assertEqual(page.pages.current(), 1)
                page.load_task(lambda: self.fail("cancelled reload should not run"))
                app.close()
                self.assertEqual(ask.call_count, 3)
                self.assertTrue(root.winfo_exists())

            printer = FakePrinter()
            plan = plan_print(self.settings, printer)

            def respond(*args, **kwargs):
                self.assertFalse(printer.opens)
                self.assertIn("4", args[1])
                return respond.yes

            with patch("fuelcore.report_view.plan_print", return_value=plan), patch.object(
                page, "settings", return_value=self.settings
            ), patch("fuelcore.report_view.messagebox.askokcancel", side_effect=respond):
                respond.yes = False
                page.buttons["列印報單"].invoke()
                await_task()
                self.assertFalse(printer.opens)
                respond.yes = True
                page.buttons["列印報單"].invoke()
                await_task()
                self.assertEqual(len(printer.opens), 1)
                self.assertIn("21.00", [text[0] for text in printer.job.pages[0][1]])
            self.assertEqual(page.dirty_notice.get(), "已修改，未存檔")
            self.assertFalse(list(self.output.glob("*.Rpx")))
            with patch("fuelcore.report_view.messagebox.askyesnocancel", return_value=True):
                page.pages.current(2)
                page.select_page()
                await_task()
            self.assertEqual(page.pages.current(), 2)
            self.assertEqual(page.dirty_notice.get(), "")
            self.assertEqual(len(list(self.output.glob("*.Rpx"))), 1)
            page.pages.current(0)
            page.select_page()
            dialog = page.open_trial()
            dialog.values[0][0].set("22")
            dialog.apply_button.invoke()
            await_task()
            with patch("fuelcore.report_view.messagebox.askyesnocancel", return_value=False):
                page.pages.current(2)
                page.select_page()
            self.assertEqual(page.pages.current(), 2)
            self.assertEqual(page.dirty_notice.get(), "")
            self.assertEqual(page.report.inputs(0)[0].quantity, D("21"))
            self.assertEqual(len(list(self.output.glob("*.Rpx"))), 1)
        self.assertTrue(self.sql and all(sql.startswith("SELECT ") for sql in self.sql))
