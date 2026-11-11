"""Report copies and the binding between printable fields and trial inputs."""

import re
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import count
from pathlib import Path
from tempfile import mkdtemp

from ..db import Database
from . import ReportBatch, prepare_report, report_resources, safe_destination
from .calc import ItemInput, OriginalItem, Trial, ZERO, decimal_input
from .declaration import DeclarationReader
from .formula import format_value
from .retention import mark_outputs
from .rpx import RpxDocument, RpxError


def expression(field):
    return re.sub(r"\s+", "", field.text("Field") or field.text("PrtConditions")).upper()


def caption(field):
    return field.text("Caption") if field is not None else ""


def find_field(fields, pattern):
    return next((field for field in fields if re.search(pattern, expression(field))), None)


def number(field, *, required=False):
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", caption(field))
    if not match and required:
        raise RpxError("報單缺少試算所需的數量、單價或匯率")
    return decimal_input(match[0]) if match else ZERO


def show_number(field, value, prefix=""):
    if field is None:
        return
    pattern = field.text("FormatStr") or "#,##0.00####"
    field.set_text("Caption", prefix + format_value(value, pattern) if value or not prefix else "")


TOTAL_FIELDS = {
    "exchange": r"^(?:\[CUSDEC\.)?FOREX\]?$",
    "usd": r"^(?:\[CUSDEC\.)?TOTALAMT\]?$",
    "twd": r"^(?:\[CUSDEC\.)?TWDPRICE\]?$",
    "net": r"^(?:\[CUSDEC\.)?TOT_NETWT\]?$",
    "quantity": r"^(?:\[SUMEXP\.)?EXPQUAN\]?$",
    "duty": r"TOT_DUTYAMT",
    "liters": r"SUMSTC\.STCQUAN",
    "gross": r"^(?:\[CUSDEC\.)?GWEIGHT\]?$",
    "packages": r"^(?:\[CUSDEC\.)?PKGTOTAL\]?$",
    "tax_total": r"^(?:\[CUSDEC\.)?TAXTOTAL\]?$",
    "release": r"^(?:\[CUSDEC\.)?CUSISDATE\]?$",
    "id": r"^(?:\[CUSDEC\.)?SERSNO\]?$",
}
ITEM_FIELDS = {
    "quantity": r"^(?:\[(?:INVITEM|項次_INV主檔)\.)?數量\]?$",
    "price": r"UNITAMT",
    "unit": r"數量單位",
    "net": r"報單淨重",
    "twd": r"DUTYAMT",
    "liters": r"統計數量",
    "stat_unit": r"統計單位",
}


class TrialFields:
    def __init__(self, page):
        fields = sorted(page.children, key=lambda field: (field.number("Top"), field.number("Left")))
        self.totals = {key: find_field(fields, pattern) for key, pattern in TOTAL_FIELDS.items()}
        self.totals["fee"] = next((field for field in fields if expression(field) == "TAXAMT"
                                   and field.text("SourceStr").upper() in {"", "TAX_B52"}), None)
        marks = [field for field in fields if "項次號碼" in expression(field)]
        end = self.totals["net"].number("Top") if self.totals["net"] else float("inf")
        boundaries = [field.number("Top") for field in marks] + [end]
        self.items = []
        for mark, start, stop in zip(marks, boundaries, boundaries[1:]):
            block = [field for field in fields if start <= field.number("Top") < stop]
            columns = {key: find_field(block, pattern) for key, pattern in ITEM_FIELDS.items()}
            self.items.append((caption(mark).strip().rstrip("."), columns))

    def trial(self):
        items = tuple(OriginalItem(
            label, number(fields["quantity"], required=True), number(fields["price"], required=True),
            number(fields["net"]), number(fields["liters"]), number(fields["twd"]),
            caption(fields["unit"]).strip().upper(), caption(fields["stat_unit"]).strip(" ()").upper(),
        ) for label, fields in self.items)
        if len({item.unit for item in items}) > 1 or len({item.stat_unit for item in items}) > 1:
            raise RpxError("試算需要各項次使用相同的數量與統計單位")
        values = self.totals
        fee = number(values["fee"])
        totals = {key: number(values[key]) for key in ("usd", "twd", "net", "quantity", "liters")
                  if values[key] is not None}
        return Trial.from_original(items, number(values["exchange"], required=True), number(values["gross"]),
                                   number(values["packages"]), fee, number(values["tax_total"]) if values["tax_total"] else fee,
                                   filed_totals=totals)

    def snapshot(self, result):
        rows = tuple(replace(row, **{key: number(fields[key])
                                     for key in ("net", "liters", "twd") if fields[key] is not None})
                     for (_, fields), row in zip(self.items, result.rows))
        totals = {key: number(self.totals[key]) for key in
                  ("usd", "twd", "net", "quantity", "liters", "gross", "packages", "fee", "tax_total")
                  if self.totals[key] is not None}
        return replace(result, rows=rows, **totals)

    def inputs(self, densities=(), *, original=None):
        price_items = self.items if original is None else original.items
        return tuple(ItemInput(number(fields["quantity"], required=True), number(filed["price"], required=True),
                               densities[index] if index < len(densities) else None)
                     for index, ((_, fields), (_, filed)) in enumerate(zip(self.items, price_items)))

    def apply(self, result):
        show_number(self.totals["exchange"], result.exchange)
        for (_, fields), row in zip(self.items, result.rows):
            for key in ("quantity", "price", "net"):
                show_number(fields[key], getattr(row, key))
            show_number(fields["liters"], row.liters, "(")
            show_number(fields["twd"], row.twd, "TWD")
        for key in ("usd", "twd", "net", "quantity", "gross", "packages", "fee", "tax_total"):
            show_number(self.totals[key], getattr(result, key))
        show_number(self.totals["liters"], result.liters, "(")
        show_number(self.totals["duty"], result.twd, "TWD")


def report_ids(settings, prefix="", *, connection=None):
    prefix = prefix.strip().upper()
    if prefix and not re.fullmatch(r"[A-Z0-9]{1,14}", prefix):
        raise RpxError("報單號碼只接受英數字元")
    with closing(Database(settings, connection, read_only=True)) as db:
        reader = DeclarationReader(db)
        table = reader.tables.get("cusdec")
        if not table:
            raise RpxError("測試資料庫缺少 CUSDEC")
        rows = db.select(f"SELECT DISTINCT SERSNO FROM {db.table(table, reader.schema)} "
                         "WHERE MSGCODE IN (?, ?) AND SERSNO LIKE ? ORDER BY SERSNO DESC LIMIT 100",
                         ("5203S", "5203A", prefix + "%"))
    return [str(row[0]) for row in rows]


def new_copy(folder, stem, suffix, content, assets, *, category=None):
    folder = safe_destination(folder, assets)
    folder.mkdir(parents=True, exist_ok=True)
    if category:
        prefix = {"print": "列印", "generated": "產生", "correction": "更正"}[category]
        folder = Path(mkdtemp(prefix=prefix + "_", dir=folder))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for index in count():
        serial = f"_{index}" if index else ""
        path = folder / f"{stem}_{stamp}{serial}{suffix}"
        try:
            stream = path.open("xb")
        except FileExistsError:
            continue
        with stream:
            stream.write(content)
        if category:
            mark_outputs(folder, {path.name: category})
        return path


def rpx_number(page):
    field = find_field(page.children, TOTAL_FIELDS["id"])
    number = re.sub(r"[^A-Z0-9]", "", caption(field).upper())
    if re.fullmatch(r"[A-Z0-9]{14}", number):
        return number
    match = re.search(r"\b[A-Z0-9]{14}\b", page.text("PageTitle").upper())
    if not match:
        raise RpxError("Rpx 正面缺少 14 碼報單號碼")
    return match[0]


@dataclass
class EditableReport:
    batch: ReportBatch
    document: RpxDocument
    originals: list
    densities: dict

    @classmethod
    def from_database(cls, settings, ids, **options):
        batch = prepare_report(settings, ids, **options)
        document = RpxDocument.create(f"出口報單 {batch.ids[0]}", batch.fronts)
        return cls(batch, document, deepcopy(batch.fronts), {})

    @classmethod
    def open(cls, path, *, reports=None, cache=None, backup=None):
        path = Path(path)
        if path.suffix.casefold() != ".rpx":
            raise RpxError("請選擇 .Rpx 報單檔")
        assets, cache = report_resources(reports, cache)
        data = path.read_bytes()
        document = RpxDocument.read(data)
        fronts = [page for page in document.roots if page.class_name == "TGPage"]
        ids, pages = [], []
        back = assets.page("N5203CUS.Rpt")
        if not fronts:
            raise RpxError("Rpx 沒有報單頁面")
        for page in fronts:
            ids.append(rpx_number(page))
            pages.extend(((page, False), (deepcopy(back), True)))
        if backup is not None:
            new_copy(Path(backup) / "原檔備份", path.stem, path.suffix, data, assets)
        return cls(ReportBatch(ids, assets, cache, pages, fronts, []), document, deepcopy(fronts), {})

    def trial(self, index):
        return TrialFields(self.originals[index]).trial()

    def inputs(self, index):
        return TrialFields(self.batch.fronts[index]).inputs(self.densities.get(index, ()))

    def exchange(self, index):
        return number(TrialFields(self.batch.fronts[index]).totals["exchange"], required=True)

    def correction_values(self, index, inputs, exchange):
        trial = self.trial(index)
        if inputs is not None:
            return trial, trial.calculate(inputs, exchange=exchange)
        fields = TrialFields(self.batch.fronts[index])
        inputs = tuple(replace(entry, price=item.price) for entry, item in zip(self.inputs(index), trial.items))
        current = trial.calculate(inputs)
        return trial, fields.snapshot(current)

    def release(self, index):
        return caption(find_field(self.batch.fronts[index].children, TOTAL_FIELDS["release"]))

    def number_caption(self, index):
        return caption(find_field(self.batch.fronts[index].children, TOTAL_FIELDS["id"]))

    def fields(self, index):
        return [(position, field.name, caption(field)) for position, field in enumerate(self.batch.fronts[index].children)
                if field.get("Caption") is not None and field.boolean("Visible", True) and field.boolean("isVisible", True)]

    def edit(self, index, field_index, value):
        if field_index not in {position for position, _, _ in self.fields(index)}:
            raise RpxError("找不到可修改的顯示欄位")
        page = self.batch.fronts[index]
        field = page.children[field_index]
        current = TrialFields(page)
        for item_index, (_, columns) in enumerate(current.items):
            if columns["quantity"] is field:
                inputs = list(current.inputs(self.densities.get(index, ()),
                                             original=TrialFields(self.originals[index])))
                quantity = decimal_input(value)
                if quantity == inputs[item_index].quantity:
                    return self
                inputs[item_index] = replace(inputs[item_index], quantity=quantity)
                return self.apply(index, inputs)
        copy = deepcopy(self)
        copy.batch.fronts[index].children[field_index].set_text("Caption", value)
        return copy

    def apply(self, index, inputs, *, exchange=None):
        result = self.trial(index).calculate(inputs, exchange=exchange)
        copy = deepcopy(self)
        TrialFields(copy.batch.fronts[index]).apply(result)
        copy.densities[index] = tuple(entry.density for entry in inputs)
        return copy

    def save(self, folder, index):
        return new_copy(folder, self.batch.ids[index], ".Rpx", self.document.to_bytes(), self.batch.assets)

    def save_generated(self, folder):
        return new_copy(folder, self.batch.ids[0], ".Rpx", self.document.to_bytes(), self.batch.assets,
                        category="generated")

    def pdf(self, *, cache_backgrounds=True):
        from .pdf import render_pdf

        cache = self.batch.cache if cache_backgrounds else None
        return render_pdf(self.batch.pages, self.batch.assets, cache, self.batch.warnings)

    def export_pdf(self, folder):
        return new_copy(folder, self.batch.ids[0], ".pdf", self.pdf(), self.batch.assets, category="print")
