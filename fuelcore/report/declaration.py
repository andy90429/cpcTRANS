import re
from copy import deepcopy
from decimal import Decimal
from pathlib import Path, PureWindowsPath

from ..schema import identifier
from .formula import FormulaError, as_text, evaluate, format_value, truth
from .layout import content_height, drawn
from .rpx import RpxDocument, RpxError, Value


DATA_TABLES = ("INVITEM", "DOC", "PERMITNO", "SUMSTC", "SUMEXP", "SUMPAY", "TAX", "TAXITEM",
               "CONTAINERNO", "CUSDEC_4", "SUPCODE")
ITEM_SOURCES = ("INVITEM", "項次_INV主檔", "PERMITNO", "PERMITNO1", "PERMITNO2", "SUPCODE1", "SUPCODE2")


class ReportAssets:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise RpxError(f"找不到報表資料夾：{self.root}")
        self.files = {}
        for path in self.root.rglob("*"):
            if path.suffix.lower() in {".rpt", ".rpx", ".bmp"} and path.is_file():
                resolved = path.resolve()
                try:
                    resolved.relative_to(self.root)
                except ValueError:
                    continue
                self.files.setdefault(path.name.casefold(), []).append(resolved)

    def find(self, name):
        basename = PureWindowsPath(name).name.casefold()
        paths = self.files.get(basename, [])
        direct = next((path for path in paths if path.parent == self.root), None)
        if direct is not None:
            return direct
        if len(paths) != 1:
            issue = "找不到" if not paths else "有多份同名檔案"
            raise RpxError(f"{issue} {name}（{self.root}）")
        return paths[0]

    def page(self, name):
        doc = RpxDocument.read(self.find(name).read_bytes())
        page = next((root for root in doc.roots if root.class_name == "TGPage" and root.children), None)
        if page is None:
            raise RpxError(f"{name} 找不到 TGPage")
        return page


def normalized(row):
    return {key.upper(): "" if value is None else value for key, value in row.items()}


def order_key(row):
    def order(key):
        value = as_text(row.get(key)).strip()
        return int(value) if value.isdecimal() else 0

    return tuple(order(key) for key in ("項次號碼", "ITEM", "PORDERFLD", "PART"))


class DeclarationReader:
    def __init__(self, db, *, enrich=True):
        self.db = db
        settings = db.settings
        self.schema = getattr(settings, "database", None)
        self.tables = db.table_names(self.schema)
        if not enrich:
            return
        self.edi_schema = settings.edi.edi_database if settings.edi else None
        self.edi_tables = db.table_names(self.edi_schema) if db.mysql and self.edi_schema else self.tables if not db.mysql else {}
        self.code_schema = getattr(settings, "code_database", None)
        self.code_tables = db.table_names(self.code_schema) if db.mysql else self.tables
        self.code_columns = {}

    def rows(self, name, sersno, message=None):
        table = self.tables.get(name.casefold())
        if not table:
            if name in ("CUSDEC", "INVITEM"):
                raise RpxError(f"測試資料庫缺少 {name}")
            return []
        qualified = self.db.table(table, self.schema)
        sql = f"SELECT * FROM {qualified} WHERE SERSNO = ? AND MSGCODE = ?" if message else (
            f"SELECT * FROM {qualified} WHERE SERSNO = ? AND MSGCODE IN (?, ?)")
        params = (sersno, message) if message else (sersno, "5203S", "5203A")
        return sorted((normalized(row) for row in self.db.select_dicts(sql, params)), key=order_key)

    def route(self, sersno):
        candidates = []
        for code in ("n5107", "n5204"):
            name = self.edi_tables.get(code)
            if not name:
                continue
            table = self.db.table(name, self.edi_schema)
            records = self.db.select(f"SELECT `通關方式`, `接收日期時間`, `訊息參考號碼` FROM {table} WHERE `報單號碼` = ?",
                                     (sersno,))
            candidates.extend((str(date or ""), str(ref or ""), str(route).strip()) for route, date, ref in records
                              if re.fullmatch(r"C[12]|C3M?", str(route).strip(), re.I))
        return max(candidates)[-1].upper() if candidates else ""

    def lookup(self, table, key, value, fields, condition=None):
        name = self.code_tables.get(table.casefold())
        if not name or not as_text(value).strip():
            return []
        if name not in self.code_columns:
            self.code_columns[name] = self.db.column_names(name, self.code_schema)
        available = self.code_columns[name]
        if key.casefold() not in available or (condition and condition[0].casefold() not in available):
            return []
        columns = ", ".join(f"{identifier(column)} AS {identifier(alias)}" for column, alias in fields.items()
                            if column.casefold() in available)
        if not columns:
            return []
        sql = f"SELECT {columns} FROM {self.db.table(name, self.code_schema)} WHERE {identifier(key)} = ?"
        params = (value,)
        if condition:
            sql += f" AND {identifier(condition[0])} = ?"
            params += (condition[1],)
        rows = self.db.select_dicts(sql + " LIMIT 2", params)
        return [normalized(rows[0])] if len(rows) == 1 else []

    def read(self, sersno, *, compare=False):
        headers = self.rows("CUSDEC", sersno)
        if len(headers) != 1:
            raise RpxError(f"{sersno} 找不到唯一的 5203 報單")
        header = headers[0]
        if compare:
            return {"CUSDEC": headers, **{name: self.rows(name, sersno, header["MSGCODE"])
                                         for name in ("INVITEM", "PERMITNO", "SUMSTC")}}
        header["CUSISCODE"] = as_text(header.get("CUSISCODE")).strip() or self.route(sersno)
        refunds = as_text(header.get("退稅清表")).strip()
        header["退稅清表"] = int(refunds) if refunds.isdecimal() else 0
        tables = {name.upper(): self.rows(name, sersno, header["MSGCODE"]) for name in DATA_TABLES}
        tables["CUSDEC"] = headers
        if not tables["INVITEM"]:
            raise RpxError(f"{sersno} 缺少報單項次")
        tables["帶值_報單類別代碼"] = self.lookup("MyFormKind", "FK003", header.get("SERSTYPE"),
                                                 {"FK004": "名稱"}, ("FK002", "E"))
        tables["帶值_客戶代碼"] = self.lookup("MyClient", "CT002", header.get("賣方編號"), {"CT008": "聯絡電話"})
        tables["帶值_買方代碼"] = self.lookup("MyBuyer", "BY002", header.get("買方編號"), {"BY009": "聯絡電話"})
        for index, (key, field) in enumerate((("GP011", "STPLACE"), ("GP001", "進倉代碼ID"),
                                              ("GP001", "出倉代碼ID"), ("GP011", "ASTPLACE")), 1):
            tables[f"帶值_貨物存放處代碼{index}"] = self.lookup("MyGoodsPlace", key, header.get(field), {"GP005": "簡稱"})
        agent = self.lookup("MyDeclareContainerNo", "DCN004", header.get("CUSMANNO"),
                            {"DCN005": "專責人姓名", "DCN002": "編號"})
        tables["帶值_報關行箱號"] = agent
        tables["帶值_報關行代碼"] = self.lookup("MyDeclareTrade", "DT002", agent[0].get("編號") if agent else "",
                                             {"DT004": "中文名稱", "DT010": "負責人"})
        for row in tables["CUSDEC_4"]:
            row["BANCUSNO"] = f"製造保稅廠統一編號:{row.get('REBAN', '')} 監管編號:{row.get('RECUSNO', '')}" if row.get("REBAN") else ""
        return tables


class Context:
    def __init__(self, tables, item, index, variables):
        self.tables = dict(tables)
        self.item = item
        self.variables = {**variables, "ord": index}
        self.tables["項次_INV主檔"] = self.tables["INVITEM"] = [item] if item else []
        pinv = as_text(item.get("PINVORDER") or item.get("項次號碼"))
        for table in ("PERMITNO", "SUPCODE"):
            rows = [row for row in tables.get(table, ()) if pinv and as_text(row.get("ITEM")) == pinv]
            self.tables[table] = rows
            for order in (1, 2):
                self.tables[f"{table}{order}"] = [row for row in rows if as_text(row.get("PORDERFLD") or 1) == str(order)]
        self.tables["TAX_B52"] = [row for row in tables.get("TAX", ()) if row.get("TAXNO") == "B52"]
        self.tables["TAX_OTHER"] = [row for row in tables.get("TAX", ()) if row.get("TAXNO") != "B52"]

    def column(self, key):
        source, _, field = key.upper().partition(".")
        return [row.get(field, "") for row in self.tables.get(source, ())]

    def get(self, key):
        key = key.upper()
        flags = {"換行符號": "\r\n", "合並大小品名": False, "是否不印料號": False, "當前頁碼": 1, "所有頁碼": 1}
        if key in flags:
            return flags[key]
        if "." in key:
            return next(iter(self.column(key)), "")
        sources = ("PERMITNO1", "INVITEM", "CUSDEC", "SUMSTC", "SUMEXP", "TAX_B52") if key.startswith("PERMIT") else (
            "INVITEM", "CUSDEC", "SUMSTC", "SUMEXP", "TAX_B52")
        return next((row[key] for source in sources for row in self.tables.get(source, ()) if key in row), "")

    def compute(self, formula):
        return evaluate(formula, self.get, self.column, self.variables)


def fill_field(field, context, warnings, *, native=True):
    if not field.boolean("Visible", True) or not field.boolean("isVisible", True):
        field.set("Visible", Value(8))
        return field
    condition = field.text("PrtConditions").strip()
    expression = field.text("Field").strip()
    source = field.text("SourceStr").strip()
    if field.get("Field") is None and not native:
        expression, condition = condition, ""
    caption = field.text("Caption")
    try:
        if condition and not truth(context.compute(condition)):
            field.set("Visible", Value(8))
        if expression:
            if expression in {"-", "KGM"}:
                value = expression
            elif source and not any(char in expression for char in "[]()\r\n") and source.upper() in context.tables:
                value = context.get(f"{source}.{expression}")
            elif expression == caption.strip() and not any(char in expression for char in "[]()"):
                value = context.get(expression) or caption
            else:
                value = context.compute(expression)
            pattern = field.text("FormatStr")
            if pattern and (not any(char in expression for char in "[]()+") or isinstance(value, (int, float, Decimal))
                            or re.fullmatch(r"[X ./-]+", pattern)):
                value = format_value(value, pattern)
            field.set_text("Caption", as_text(value))
    except FormulaError as error:
        warnings.append(f"{field.name or field.class_name}：{error}")
        if expression:
            field.set_text("Caption", "")
    return field


def display_fields(page):
    return [field for field in page.children if field.class_name != "TGBand"]


def item_range(page):
    bands = sorted((field for field in page.children if field.class_name == "TGBand"), key=lambda field: field.number("Top"))
    detail = next((field for field in bands if field.text("SourceStr").upper() in ITEM_SOURCES[:2]), None)
    if detail:
        start = detail.number("Top")
        end = next((field.number("Top") for field in bands if field.number("Top") > start), start + detail.number("Height"))
        footer = next((field.number("Top") for field in bands if field.text("BandStyle") == "ptPageFoot"), end)
        return start, end, footer
    return None


def fields_in(rows, start, end):
    return [field for field in rows if start <= field.number("Top") < end]


def move_block(fields, top, original_top):
    for field in fields:
        field.set_number("Top", top + field.number("Top") - original_top)


def is_separator(field):
    return bool(re.fullmatch(r"-{3,}", field.text("Caption").strip()))


def fit_total_slots(fields):
    for field in fields:
        expression = (field.text("Field") or field.text("PrtConditions")).upper()
        source = field.text("SourceStr").upper()
        if field.text("Alignment") == "taRightJustify" and (
            source in {"SUMEXP", "SUMSTC"} or "SUMSTC.STCQUAN" in expression or expression == "TOT_NETWT"
        ):
            # These quantity slots start slightly left of the printed column rule.
            inset = min(8, field.number("Width", 1) - 1)
            if inset > 0:
                field.set_number("Left", field.number("Left") + inset)
                field.set_number("Width", field.number("Width") - inset)


def pack_item(fields, y):
    visible = [field for field in fields if drawn(field)]
    labels = [field for field in visible if not is_separator(field)]
    if not labels:
        return [], y
    top = min(field.number("Top") for field in labels)
    description = next((field for field in labels if "報單品名" in field.text("Field") and field.boolean("WordWrap")), None)
    if description:
        end = description.number("Left") + description.number("Width")
        left_column = [field for field in labels if field.number("Left") < end]
        earliest = min(field.number("Top") for field in left_column)
        move_block(left_column, top, earliest)
    move_block(labels, y, top)
    for field in visible:
        if is_separator(field):
            field.set_number("Top", y - 2)
            field.set_number("Height", 1)
    return visible, max(field.number("Top") + content_height(field) for field in labels) + 3


def build_page(template, tables, warnings):
    page = deepcopy(template)
    fields = display_fields(page)
    items = tables["INVITEM"]
    variables = {field.name.casefold(): field.text("Field") or field.text("Caption") for field in fields if field.name}
    context = Context(tables, items[0], 1, variables)
    bounds = item_range(page)
    if not bounds:
        native = any(field.class_name == "TGBand" or field.get("Field") is not None for field in page.children)
        page.children = [fill_field(field, context, warnings, native=native) for field in fields]
        return page

    detail, total, footer = bounds
    item_fields = fields_in(fields, detail, total)
    total_fields = fields_in(fields, total, footer)
    fixed = [fill_field(field, context, warnings) for field in fields if field not in item_fields and field not in total_fields]
    notes_band = next((band for band in page.children if band.class_name == "TGBand" and band.text("SourceStr") == "SERSNO"), None)
    if notes_band:
        bottom = notes_band.number("Top") + notes_band.number("Height")
        for field in fixed:
            if "[CUSDEC.OTHER]" in field.text("Field") and drawn(field):
                top = min(field.number("Top"), bottom - content_height(field) - 2)
                if top < footer:
                    raise RpxError("其他申報事項超出正面欄位可用範圍")
                field.set_number("Top", top)
    if not item_fields:
        raise RpxError("報表明細區沒有可用欄位")
    if len(items) == 1:
        item_block = [fill_field(deepcopy(field), context, warnings) for field in item_fields]
        totals = [fill_field(deepcopy(field), context, warnings) for field in total_fields]
        fit_total_slots(totals)
        item_bottom = max((field.number("Top") + content_height(field) for field in item_block if drawn(field)), default=0)
        totals_top = min((field.number("Top") for field in totals if drawn(field)), default=footer)
        if item_bottom <= totals_top:
            page.children = fixed + item_block + totals
            return page

    # The static form reserves a detail band and a footer. Reuse the band for every item,
    # packing only the occupied rows so blank permit fields do not consume another line.
    y = detail + 4
    packed = []
    for index, item in enumerate(items, 1):
        item_context = Context(tables, item, index, variables)
        block = [fill_field(deepcopy(field), item_context, warnings) for field in item_fields]
        visible, y = pack_item(block, y)
        packed.extend(visible)
    totals = [fill_field(deepcopy(field), context, warnings) for field in total_fields]
    fit_total_slots(totals)
    for top in sorted({field.number("Top") for field in totals if drawn(field)}):
        line = [field for field in totals if drawn(field) and field.number("Top") == top]
        for field in line:
            if is_separator(field):
                field.set_number("Height", 2)
        height = max(2 if is_separator(field) else content_height(field) for field in line)
        move_block(line, y, top)
        y += height + 1
        packed.extend(line)
    if y > footer:
        sersno = tables["CUSDEC"][0]["SERSNO"]
        raise RpxError(f"{sersno} 的項次超出單張正面範本明細區（{y} > {footer}）")
    page.children = fixed + packed
    return page
