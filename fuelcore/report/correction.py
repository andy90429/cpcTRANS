"""Build the export correction form and keep its numeric cells recalculable."""

import re
from io import BytesIO
from unicodedata import east_asian_width

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


def excel_sum(refs):
    return "SUM(" + ",".join(refs) + ")"


def workbook(trial, after, number, release):
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.worksheet.page import PageMargins

    book = Workbook()
    sheet = book.active
    sheet.title = TITLE
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
    runs = [TextBlock(regular, "     報單號碼  ")]
    if number.upper().startswith("DA"):
        runs.append(TextBlock(underlined, number[:2]))
        number = number[2:]
    runs.append(TextBlock(regular, number))
    merged(4, CellRichText(runs), 12.95, "標楷體", 10, "left")
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
    qty_decimals = 3 if any(row.quantity != round(row.quantity, 2) for result in results for row in result.rows) else 2
    unit = trial.items[0].unit
    stat_unit = trial.items[0].stat_unit
    fmt_qty = '"    "#,##0.' + ("0" * qty_decimals) + f'_)"{unit}"'
    fmt_usd, fmt_twd = '"USD "#,##0.00_)', '"TWD "#,##0_)'
    fmt_net, fmt_stat = '#,##0.00_)"KGM"', f'#,##0.00_)"{stat_unit}"'
    numeric = Font(name="標楷體", size=12, family=4, charset=136)
    widths = [21.5, 37.5, 41.5]

    def line(row, label, values, cells, fmt="General"):
        sheet.row_dimensions[row].height = 16.5
        sheet.row_dimensions[row].hidden = values[0] == values[1]
        for col, value in enumerate((label, *cells), 1):
            cell = sheet.cell(row, col, value)
            cell.font = numeric
            cell.alignment = Alignment(horizontal="center" if col == 1 else "left",
                                       vertical="center", wrap_text=True)
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin if row == fee_sum else None)
            if col != 1:
                cell.number_format = fmt
            display = str(label or "") if col == 1 else f"{values[col - 2]:,f}".rstrip("0").rstrip(".")
            length = sum(2 if east_asian_width(char) in "FW" else 1 for char in display)
            widths[col - 1] = max(widths[col - 1], length + (12 if col != 1 else 3))

    def total(row, label, attr, forms, fmt):
        line(row, label, [getattr(result, attr) for result in results], ["=" + form for form in forms], fmt)

    sheet["E8"] = "試算參數（列印範圍外）"
    sheet["E9"] = "匯率"
    sheet["F9"] = trial.exchange
    sheet["E10"] = "商港服務費差數"
    sheet["F10"] = trial.fee_offset
    sheet["E11"], sheet["F11"], sheet["G11"] = "項次", "更正前單價", "更正後單價"
    for index, item in enumerate(trial.items):
        row = 12 + index
        sheet.cell(row, 5, item.number)
        sheet.cell(row, 6, before.rows[index].price)
        sheet.cell(row, 7, after.rows[index].price)

    def usd_row(col, index):
        price_col = "F" if col == "B" else "G"
        return f"ROUND({col}{qty_rows[index]}*${price_col}${12 + index},2)"

    total(9, 16, "usd", [excel_sum(usd_row(col, index) for index in range(count)) for col in "BC"], fmt_usd)
    total(10, 21, "twd", [f"{col}{total_twd}" for col in "BC"], fmt_twd)
    for index, item in enumerate(trial.items):
        pair = [result.rows[index] for result in results]
        qty_refs = [f"{col}{qty_rows[index]}" for col in "BC"]
        line(qty_rows[index], f"38.{item.number}", [row.quantity for row in pair],
             [row.quantity for row in pair], fmt_qty)
        line(net_rows[index], f"37.{item.number}", [row.net for row in pair],
             [f"=ROUND({ref}*{row.net_ratio},2)" for ref, row in zip(qty_refs, pair)], fmt_net)
        formulas = [
            f"=ROUND({ref}/{row.factor}*{BARREL_KL},3)*1000" if row.factor else
            f"=ROUND({ref}*{row.stat_ratio},{row.stat_places})" for ref, row in zip(qty_refs, pair)
        ]
        line(stat_rows[index], f"39.{item.number}", [row.liters for row in pair], formulas, fmt_stat)
        prices = (f"$F${12 + index}", f"$G${12 + index}")
        formulas = [
            f"=IF(AND({ref}={item.quantity},{price}={item.price}),{item.twd},ROUND({usd_row(col, index)}*$F$9,0))"
            for col, ref, price in zip("BC", qty_refs, prices)
        ]
        line(twd_rows[index], f"40.{item.number}", [row.twd for row in pair], formulas, fmt_twd)

    def summed(row, label, key, refs, fmt, rounding=False):
        forms = [excel_sum(f"{col}{ref}" for ref in refs) for col in "BC"]
        total(row, label, key, [f"ROUND({form},2)" for form in forms] if rounding else forms, fmt)

    summed(total_net, "TOTAL", "net", net_rows, fmt_net)
    summed(total_qty, None, "quantity", qty_rows, fmt_qty, True)
    summed(total_stat, None, "liters", stat_rows, fmt_stat, True)
    summed(total_twd, "TOTAL", "twd", twd_rows, fmt_twd)
    total(packages, 42, "packages", [f"ROUNDDOWN({col}{total_qty}*{trial.package_ratio},0)" for col in "BC"], '#,##0_)"TNE"')
    total(gross, 44, "gross", [f"ROUND({col}{total_net}*{trial.gross_ratio},2)" for col in "BC"], '#,##0.00_)"公斤"')
    line(blank, None, [before.fee, after.fee], [None, None])
    total(fee, "推廣貿易服務費", "fee",
          [f"MAX(0,ROUNDDOWN({FEE_RATE}*{col}{total_twd},0)-$F$10)" for col in "BC"], "#,##0")
    total(fee_sum, "合計", "fee", [f"{col}{fee}" for col in "BC"], "#,##0")
    for offset, value, height, family, alignment in FOOTER:
        merged(fee_sum + offset, value, height, family, 10, alignment,
               underline="single" if offset == 9 else None)
    for col, width in zip("ABC", widths):
        sheet.column_dimensions[col].width = width
        sheet.column_dimensions[col].bestFit = True
    for col, width in zip("EFG", (28, 17, 17)):
        sheet.column_dimensions[col].width = width
    sheet.print_area = f"A1:C{fee_sum + 13}"
    sheet.print_options.horizontalCentered = True
    sheet.print_options.verticalCentered = True
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.orientation = "portrait"
    sheet.page_setup.fitToWidth = sheet.page_setup.fitToHeight = 1
    sheet.page_margins = PageMargins(left=0.5, right=0.5, top=0.5, bottom=0.5, header=0.59, footer=0.69)
    return book


def export_correction(report, index, inputs, folder):
    trial = report.trial(index)
    number = report.batch.ids[index]
    book = workbook(trial, trial.calculate(inputs), number, report.release(index))
    output = BytesIO()
    book.save(output)
    return new_copy(folder, f"{number}_更正申請書", ".xlsx", output.getvalue(), report.batch.assets)
