"""Build the export correction form and keep its numeric cells recalculable."""

import re
from io import BytesIO
from decimal import Decimal
from zipfile import ZipFile

from .calc import BARREL_KL, FEE_RATE
from .editing import new_copy


TITLE = "連線出口報單更正申請書"
INTRO = (
    (1, TITLE, 21, "標楷體", 16, "center", True),
    (2, "Application for the Correction of On-Line Export Declaration", 11.1, "Times New Roman", 8, "center", True),
    (3, "一、 本公司    自行   報關出口           油料           貨物 1  批，計    件。", 12.95, "標楷體", 10, "left", False),
    (6, "1. The undersigned company entrusted ________to file the export declaration of ____piece(s) of cargo "
        "with declaration No.     /      /         /        /        , which was released on __________(year/month/day).",
     39, "Times New Roman", 10, "justify", False),
    (7, "Since some errors have been found in the electronic data, we hereby apply for correction of the following items:",
     26.1, "Times New Roman", 10, "justify", False),
)
FOOTER = (
    (1, "二、說明: 因該船實際加油時油艙容量不足，致與預估申報有差異，申請修改。", 14.25, "標楷體", "left"),
    (2, "2. Explanation:", 12.75, "Times New Roman", "left"),
    (4, "三、檢附證件: 修改前、修改後出口報單，交貨紀錄，賒銷提交貨單。", 12.95, "標楷體", "left"),
    (5, "3.Attached Documents:", 12.75, "標楷體", "left"),
    (6, "       此致", 12.75, "標楷體", "left"),
    (7, "              To", 12.95, "Times New Roman", "left"),
    (8, "財政部關務署   台中  關稅局", 12.95, "標楷體", "left"),
    (9, "        Customs Office, Ministry of Finance", 12.95, "標楷體", "left"),
    (10, "申請人 : 台灣中油股份有限公司", 12.75, "標楷體", "center"),
    (11, "                                                                  Applicant:", 12.95, "Times New Roman", "left"),
    (12, "申請日期:      年    月    日", 12.95, "標楷體", "center"),
    (13, "Date of Application (year/month/day)", 12.95, "Times New Roman", "center"),
)


def format_release(release):
    match = re.fullmatch(r"(\d+)/(\d+)/(\d+)", release.strip())
    if match:
        year, month, day = match.groups()
        return f"{year}年{int(month):02d}月{int(day):02d}日"
    return release.strip() or "      年    月    日"


def workbook(trial, after, number, release, *, cached_values=None):
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.worksheet.page import PageMargins

    book = Workbook()
    sheet = book.active
    sheet.title = "修改報表"
    sheet.sheet_view.showGridLines = False
    sheet.sheet_format.defaultRowHeight = 12.75
    book.calculation.fullCalcOnLoad = True

    def merged(row, value, height, family, size, align, bold=False, underline=None):
        sheet.merge_cells(start_row=row, end_row=row, start_column=1, end_column=3)
        cell = sheet.cell(row, 1, value)
        cell.font = Font(name=family, size=size, bold=bold, underline=underline)
        cell.alignment = Alignment(horizontal=align, vertical="top", wrap_text=True)
        sheet.row_dimensions[row].height = height

    for args in INTRO:
        merged(*args)
    regular = InlineFont(rFont="標楷體", sz=10, family=4, charset=136)
    underlined = InlineFont(rFont="標楷體", sz=10, family=4, charset=136, u="single")
    parts = number.split("/")
    if len(parts) >= 5:
        number = " / ".join(part.strip() for part in parts[:5])
    sheet["A4"] = CellRichText([TextBlock(regular, "     報單號碼  "), TextBlock(underlined, number)])
    sheet["A4"].alignment = Alignment(horizontal="left", vertical="top")
    sheet.row_dimensions[4].height = 12.95
    merged(5, f"     於{format_release(release)}放行，其中電腦資料傳輸錯誤，申請更正下列事項:",
           12.95, "標楷體", 10, "left")
    thin = Side(style="thin", color="000000")
    for col, label in enumerate(("項目\nItem No.", "資料列載情形\nData at Present", "修改情形\nData after Correction"), 1):
        cell = sheet.cell(8, col, label)
        cell.font = Font(name="標楷體", size=12)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
    sheet.row_dimensions[8].height = 32.25

    before = trial.calculate()
    results = (before, after)
    count = len(trial.items)
    net_rows = [11 + 3 * index for index in range(count)]
    qty_rows = [row + 1 for row in net_rows]
    stat_rows = [row + 2 for row in net_rows]
    total_net, total_qty, total_stat = range(11 + 3 * count, 14 + 3 * count)
    twd_rows = list(range(total_stat + 1, total_stat + 1 + count))
    total_twd, packages, gross, blank, fee, fee_sum = range(twd_rows[-1] + 1, twd_rows[-1] + 7)
    quantities = [value for result in results for value in (result.quantity, *(r.quantity for r in result.rows))]
    qty_decimals = 3 if any(abs(round(q * 100) - q * 100) > Decimal("1e-6") for q in quantities) else 2
    places = "0" * qty_decimals
    fmt_qty = rf'"    "#,##0.{places}_)"TNE";"    "\(#,##0.{places}\)"TNE"'
    fmt_usd = r'"USD "#,##0.00_);\("USD "#,##0.00\)'
    fmt_twd = r'"TWD "#,##0_);\("TWD "#,##0\)'
    fmt_net = r'#,##0.00_)"KGM";\(#,##0.00\)"KGM"'
    fmt_stat = r'#,##0.00_)"LTR";\(#,##0.00\)"LTR"'
    numeric = Font(name="標楷體", size=12, family=4, charset=136)
    totals = ("usd", "twd", "net", "quantity", "liters", "packages", "gross", "fee")
    item_fields = ("net", "quantity", "liters", "twd")

    def same(a, b):
        return abs(a - b) < Decimal("1e-9")

    changed = (any(not same(getattr(before, k), getattr(after, k)) for k in totals)
               or any(not same(getattr(a, k), getattr(b, k))
                      for a, b in zip(before.rows, after.rows) for k in item_fields))

    def line(row, label, values, cells, fmt="General"):
        sheet.row_dimensions[row].height = 16.5
        sheet.row_dimensions[row].hidden = changed and same(*values)
        for col, value in enumerate((label, *cells), 1):
            cell = sheet.cell(row, col, value)
            cell.font = numeric
            cell.alignment = Alignment(horizontal="center" if col == 1 else "left",
                                       vertical="center", wrap_text=True)
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin if row == fee_sum else None)
            if col != 1:
                cell.number_format = fmt
            if cached_values is not None and cell.data_type == "f":
                cached_values[cell.coordinate] = values[col - 2]

    def total(row, label, attr, forms, fmt):
        line(row, label, [getattr(result, attr) for result in results], ["=" + form for form in forms], fmt)

    def item_value(col, result, index):
        return f"{col}{qty_rows[index]}*{result.rows[index].price}"

    def item_label(group, index):
        match = re.match(r"[+-]?\d+", str(trial.items[index].number).strip())
        number = int(match[0]) if match else index + 1
        label = f"{group}.{number}"
        return float(label) if 0 <= number < 10 else label

    usd = ["ROUND(" + "+".join(item_value(col, result, index) for index in range(count)) + ",2)"
           for col, result in zip("BC", results)]
    total(9, 16, "usd", usd, fmt_usd)
    total(10, 21, "twd", [f"{col}{total_twd}" for col in "BC"], fmt_twd)
    for index in range(count):
        pair = [result.rows[index] for result in results]
        qty_refs = [f"{col}{qty_rows[index]}" for col in "BC"]
        line(qty_rows[index], item_label(38, index), [row.quantity for row in pair],
             [row.quantity for row in pair], fmt_qty)
        line(net_rows[index], item_label(37, index), [row.net for row in pair],
             [f"=ROUND({ref}*{row.net_ratio},2)" for ref, row in zip(qty_refs, pair)], fmt_net)
        formulas = [
            f"=ROUND({ref}/{row.factor}*{BARREL_KL}*1000,0)" if row.factor else
            f"=ROUND({ref}*{row.stat_ratio},{row.stat_places})" for ref, row in zip(qty_refs, pair)
        ]
        line(stat_rows[index], item_label(39, index), [row.liters for row in pair], formulas, fmt_stat)
        formulas = [f"=ROUND({item_value(col, result, index)}*{result.exchange},0)"
                    for col, result in zip("BC", results)]
        line(twd_rows[index], item_label(40, index), [row.twd for row in pair], formulas, fmt_twd)

    def summed(row, label, key, refs, fmt):
        total(row, label, key, ["+".join(f"{col}{ref}" for ref in refs) for col in "BC"], fmt)

    summed(total_net, "TOTAL", "net", net_rows, fmt_net)
    summed(total_qty, None, "quantity", qty_rows, fmt_qty)
    summed(total_stat, None, "liters", stat_rows, fmt_stat)
    summed(total_twd, "TOTAL", "twd", twd_rows, fmt_twd)
    package_scale = "" if trial.package_ratio == 1 else f"*{trial.package_ratio}"
    total(packages, 42, "packages",
          [f"ROUNDDOWN({col}{total_qty}{package_scale},0)" for col in "BC"], r'#,##0_)"TNE";\(#,##0\)"TNE"')
    total(gross, 44, "gross",
          [f"{col}{total_net}" if trial.gross_ratio == 1 else f"ROUND({col}{total_net}*{trial.gross_ratio},2)"
           for col in "BC"], r'#,##0.00_)"公斤";\(#,##0.00\)"公斤"')
    line(blank, None, [before.fee, after.fee], [None, None])
    fee_formulas = [f"ROUNDDOWN({FEE_RATE}*{col}10,0)" for col in "BC"]
    if trial.fee_offset:
        fee_formulas = [f"MAX(0,{formula}-({trial.fee_offset}))" for formula in fee_formulas]
    total(fee, "推廣貿易服務費", "fee", fee_formulas, "#,##0")
    total(fee_sum, "合計", "fee", [f"{col}{fee}" for col in "BC"], "#,##0")
    for offset, value, height, family, alignment in FOOTER:
        merged(fee_sum + offset, value, height, family, 10, alignment,
               underline="single" if offset == 9 else None)
    for col, width in zip("ABC", (21.5, 37.5, 41.5)):
        sheet.column_dimensions[col].width = width
        sheet.column_dimensions[col].bestFit = True
    sheet.print_area = f"A1:C{fee_sum + 13}"
    sheet.print_options.horizontalCentered = True
    sheet.print_options.verticalCentered = True
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.orientation = "portrait"
    sheet.page_setup.fitToWidth = sheet.page_setup.fitToHeight = 1
    sheet.page_margins = PageMargins(left=0.5, right=0.5, top=0.5, bottom=0.5, header=0.59, footer=0.69)
    return book


def correction_bytes(trial, after, number, release):
    cached_values = {}
    book = workbook(trial, after, number, release, cached_values=cached_values)
    raw = BytesIO()
    book.save(raw)
    raw.seek(0)
    output = BytesIO()
    # Keep the filed/current display values alongside the recalculable Excel formulas.
    # openpyxl writes the empty value as <v></v> with lxml installed and as <v /> without it.
    formula_cell = re.compile(rb'(<c r="([A-Z]+\d+)"[^>]*><f>[^<]*</f>)<v\s*(?:/>|></v>)')
    with ZipFile(raw) as source, ZipFile(output, "w") as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == "xl/worksheets/sheet1.xml":
                def cache(match):
                    value = cached_values[match[2].decode()]
                    return match[1] + b"<v>" + format(value, "f").encode() + b"</v>"

                data, replaced = formula_cell.subn(cache, data)
                if replaced != len(cached_values):
                    raise ValueError("更正申請書的公式儲存格無法寫入顯示值")
            target.writestr(entry, data)
    return output.getvalue()


def export_correction(report, index, inputs, folder, *, exchange=None):
    trial, after = report.correction_values(index, inputs, exchange)
    number = report.batch.ids[index]
    content = correction_bytes(trial, after, report.number_caption(index) or number, report.release(index))
    return new_copy(folder, f"{number}_更正申請書", ".xlsx", content, report.batch.assets)
