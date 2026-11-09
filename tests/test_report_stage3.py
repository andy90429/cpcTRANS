import ast
import importlib.util
import os
import re
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal as D, ROUND_DOWN, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from fuelcore.report.calc import ItemInput, OriginalItem, Trial
from fuelcore.report.editing import EditableReport, TrialFields, report_ids

from test_edi import load_fixture_tables, settings
from test_printing import FakePrinter
from test_report import PDF_AVAILABLE, REPORTS


def sample_trial():
    items = tuple(OriginalItem(str(i), *map(D, values)) for i, values in enumerate(
        (("20", "902.70", "20000", "21757", "581609"), ("25", "1312.20", "25000", "30123", "1056813"),
         ("60", "902.70", "60000", "65270", "1744829")), 1))
    return Trial.from_original(items, D("32.215"), D(105000), D(105), D(1353), D(1353))


def changed_first(trial, quantity, price="902.70", density=None):
    return (ItemInput(D(quantity), D(price), D(density) if density else None), *trial.inputs()[1:])


class TrialTest(unittest.TestCase):
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
        from fuelcore.report.correction import workbook

        trial = sample_trial()
        result = trial.calculate(changed_first(trial, "21"))
        content = BytesIO()
        workbook(trial, result, "DAG515A10E0777", "115/10/23").save(content)
        sheet = load_workbook(content, rich_text=True).active
        self.assertEqual(str(sheet["A4"].value), "     報單號碼  DAG515A10E0777")
        self.assertEqual([(part.text, part.font.u) for part in sheet["A4"].value][1:],
                         [("DA", "single"), ("G515A10E0777", None)])
        self.assertIn("A4:C4", sheet.merged_cells)
        self.assertIn("115年10月23日", sheet["A5"].value)
        self.assertEqual(sheet["B8"].border.bottom.style, "thin")
        self.assertEqual(sheet["B8"].alignment.horizontal, "center")
        self.assertTrue(sheet.print_options.horizontalCentered)
        self.assertTrue(sheet.sheet_properties.pageSetUpPr.fitToPage)
        self.assertTrue(sheet.row_dimensions[15].hidden)
        self.assertTrue(sheet.row_dimensions[16].hidden)
        self.assertFalse(sheet.row_dimensions[12].hidden)
        self.assertIn("ROUNDDOWN", sheet["C30"].value)
        for col, expected in (("B", trial.calculate()), ("C", result)):
            for row, name in ((9, "usd"), (10, "twd"), (20, "net"), (21, "quantity"),
                              (22, "liters"), (27, "packages"), (28, "gross"), (30, "fee")):
                self.assertEqual(evaluate_cell(sheet, f"{col}{row}"), getattr(expected, name))
        sheet["C12"] = D("18.125")
        updated = trial.calculate(changed_first(trial, "18.125"))
        self.assertEqual(evaluate_cell(sheet, "C30"), updated.fee)
        self.assertEqual(evaluate_cell(sheet, "C13"), updated.rows[0].liters)


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
        reopened = EditableReport.open(path, cache=self.output / "cache")
        trial = reopened.trial(0)
        self.assertEqual(trial.calculate(), sample_trial().calculate())
        self.assertEqual(report_ids(self.settings, "TEST", connection=self.db), list(reversed(REPORTS)))
        edited, edited_path = reopened.apply(0, changed_first(trial, "21"), self.output)
        self.assertEqual(edited.release(0), reopened.release(0))
        self.assertEqual(edited.batch.fronts[1], reopened.batch.fronts[1])
        self.assertEqual(TrialFields(edited.batch.fronts[0]).totals["fee"].text("Caption"), "1,364")
        self.assertNotEqual(edited_path, path)
        self.assertEqual(sha256(path.read_bytes()).digest(), digest)
        position = edited.fields(1)[0][0]
        modified, other_path = edited.edit(1, position, "FIELD COPY", self.output)
        self.assertNotEqual(other_path, edited_path)
        self.assertEqual(modified.batch.fronts[0], edited.batch.fronts[0])
        self.assertEqual(modified.batch.fronts[1].children[position].text("Caption"), "FIELD COPY")
        with closing(pypdfium2.PdfDocument(edited.export_pdf(self.output))) as pdf:
            self.assertEqual(len(pdf), 4)
        self.assertTrue(self.sql and all(sql.startswith("SELECT ") for sql in self.sql))

    def test_gui_load_trial_and_confirm_print(self):
        import tkinter as tk
        from fuelcore.gui import Workbench
        from fuelcore.report.printing import plan_print

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.addCleanup(root.destroy)
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
            dialog = page.open_trial()
            dialog.values[0][0].set("21")
            self.assertIn("1,364", dialog.total.get())
            dialog.save.invoke()
            await_task()
            self.assertEqual(len(list(self.output.glob("*.Rpx"))), 1)

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
                page.buttons["直接列印"].invoke()
                await_task()
                self.assertFalse(printer.opens)
                respond.yes = True
                page.buttons["直接列印"].invoke()
                await_task()
                self.assertEqual(len(printer.opens), 1)
                self.assertIn("21.00", [text[0] for text in printer.job.pages[0][1]])
        self.assertTrue(self.sql and all(sql.startswith("SELECT ") for sql in self.sql))
