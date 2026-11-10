"""Compare only visible Rpx fields with a single database source."""

import re
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, DecimalException

from ..db import Database
from .declaration import Context, DeclarationReader
from .formula import as_text, format_value
from .rpx import RpxError


TABLES = ("CUSDEC", "INVITEM", "PERMITNO", "SUMSTC")
SOURCES = dict.fromkeys(TABLES)
SOURCES.update({"項次_INV主檔": None, "PERMITNO1": None, "PERMITNO2": None})
DIRECT = re.compile(r"\[([^\.\[\]]+)\.([^\[\]]+)\]")
BARE = re.compile(r"[\w]+")
NUMBER = re.compile(r"[+-]?[0-9][0-9,]*(?:\.[0-9]+)?")
DATE = re.compile(r"(?:民國\s*)?(\d{2,4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T].*)?")


@dataclass(frozen=True)
class Difference:
    position: int
    label: str
    rpx: str
    database: str


def direct_source(field, context):
    value = field.text("Field").strip()
    if field.get("Field") is None:
        value = field.text("PrtConditions").strip()
    source = field.text("SourceStr").strip().upper()
    qualified = DIRECT.fullmatch(value)
    if qualified:
        source, value = (part.strip().upper() for part in qualified.groups())
    elif BARE.fullmatch(value):
        value = value.upper()
        if not source:
            matches = [table for table in TABLES if any(value in row for row in context.tables.get(table, ()))]
            if len(matches) != 1:
                return None
            source = matches[0]
    else:
        return None
    if source not in SOURCES:
        return None
    rows = context.tables.get(source, ())
    if len(rows) != 1 or value not in rows[0]:
        return None
    return f"{source}.{value}", rows[0][value]


def numeric_caption(text):
    text = text.strip()
    if text.upper() == "NIL":
        return Decimal(0)
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return Decimal(text.replace(",", "")) if NUMBER.fullmatch(text) else None


def comparable(rpx, value, pattern):
    raw = as_text(value)
    formatted = as_text(format_value(value, pattern)) if pattern else raw
    left = rpx.strip()
    right = formatted.strip()
    first, second = DATE.fullmatch(left), DATE.fullmatch(raw.strip())
    if first and second:
        def roc(match):
            year, month, day = map(int, match.groups())
            return (year - 1911 if year >= 1911 else year), month, day

        return roc(first) == roc(second), formatted
    if not (pattern and re.fullmatch(r"[X ./-]+", pattern)):
        first, second = numeric_caption(left), numeric_caption(raw)
        if first is not None and second is not None and (pattern or "NIL" in (left.upper(), raw.strip().upper())
                                                         or not isinstance(value, str)):
            places = max(0, -first.as_tuple().exponent)
            if left.upper() == "NIL" and second != 0:
                shown = numeric_caption(right)
                return shown == 0, formatted
            try:
                expected = numeric_caption(format_value(second, "0" + ("." + "0" * places if places else "")))
            except DecimalException:
                expected = None
            return first == expected, formatted
    return left.replace("\r\n", "\n") == right.replace("\r\n", "\n"), formatted


def page_differences(page, tables):
    items = tables["INVITEM"]
    item_ids = {as_text(item.get("項次號碼")).strip(): item for item in items}
    markers = [(field.number("Top"), item_ids[match[1]])
               for field in page.children if "項次號碼" in (field.text("Field") or field.text("PrtConditions"))
               if (match := re.fullmatch(r"\s*(\d+)\.?\s*", field.text("Caption")))
               and match[1] in item_ids]
    markers.sort(key=lambda marker: marker[0])
    differences = []
    for index, field in enumerate(page.children):
        if (field.get("Caption") is None or not field.boolean("Visible", True)
                or not field.boolean("isVisible", True)):
            continue
        if len(items) == 1:
            item = items[0]
        else:
            item = next((item for top, item in reversed(markers) if top <= field.number("Top")), {})
        found = direct_source(field, Context(tables, item, 1, {}))
        if found is None:
            continue
        label, value = found
        rpx = field.text("Caption")
        equal, database = comparable(rpx, value, field.text("FormatStr"))
        if not equal:
            differences.append(Difference(index, label, rpx, database))
    return tuple(differences)


def compare_report(report, settings, *, connection=None):
    differences, warnings = [], []
    try:
        with closing(Database(settings, connection, read_only=True)) as db:
            reader = DeclarationReader(db, enrich=False)
            for number, page in zip(report.batch.ids, report.batch.fronts):
                try:
                    tables = reader.read(number, compare=True)
                    differences.append(page_differences(page, tables))
                except RpxError:
                    warnings.append(f"{number} 無法讀取對應的測試庫報單。")
                    differences.append(())
    except Exception:
        return [()] * len(report.batch.ids), ["無法讀取測試庫，已開啟 Rpx 預覽。"]
    return differences, warnings
