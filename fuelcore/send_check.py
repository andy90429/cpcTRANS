import re
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from .convert import MESSAGE
from .db import Database
from .edi.xml import row_text, upper_keys
from .rules import buyer_code


RELATIVE_TOLERANCE = Decimal("0.001")


@dataclass(frozen=True)
class Item:
    number: str
    name: str
    quantity: Optional[Decimal]
    unit: str
    net_weight: Optional[Decimal]
    unitamt: Optional[Decimal]
    fobprice: Optional[Decimal]


@dataclass
class Declaration:
    sersno: str
    ship_name: str = ""
    call_sign: str = ""
    buyer: str = ""
    status: str = ""
    items: Tuple[Item, ...] = ()
    blocks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def can_send(self):
        return self.status == "Y" and bool(self.items) and not (self.blocks or self.errors)

    @property
    def label(self):
        if not self.can_send:
            return "不能送"
        return "需確認" if self.warnings else "可模擬"


def split_numbers(values):
    return list(dict.fromkeys(number.upper() for number in re.split(r"[\s,，]+", " ".join(values)) if number))


def number(row, key):
    try:
        value = Decimal(row_text(row, key).strip())
    except InvalidOperation:
        return None
    # The database fields are finite, fixed-scale numbers. Ignore corrupted
    # SQLite values that cannot participate safely in the comparisons.
    return value if value.is_finite() and -999 <= value.adjusted() <= 999 else None


def declaration_date(row):
    try:
        return date.fromisoformat(row_text(row, "SERSDATE").strip().split(" ")[0].split("T")[0])
    except ValueError:
        return None


def normalized_name(name):
    return " ".join(name.upper().split())


def optional_rows(db, sql, params):
    try:
        return db.select_dicts(sql, params)
    except Exception:
        # SQLite and MySQL use different exceptions for unreadable optional data.
        return None


def item_rows(db, sersno):
    return optional_rows(
        db, f"SELECT * FROM {db.table('invitem')} WHERE MSGCODE = ? AND SERSNO = ? ORDER BY PINVORDER",
        (MESSAGE, sersno),
    )


def read_items(rows):
    return tuple(
        Item(row_text(row, "項次號碼"), row_text(row, "報單品名").strip() or row_text(row, "貨品名稱").strip(),
             number(row, "數量"), row_text(row, "數量單位").strip(), number(row, "報單淨重"),
             number(row, "UNITAMT"), number(row, "FOBPRICE"))
        for row in map(upper_keys, rows)
    )


def mismatch(actual, expected, tolerance=None):
    if actual is None:
        return False
    if tolerance is None:
        tolerance = max(Decimal(1), expected * RELATIVE_TOLERANCE)
    return abs(actual - expected) > tolerance


def check_items(declaration, header, rows):
    for item, row in zip(declaration.items, map(upper_keys, rows)):
        label = f"第 {item.number} 項"
        blank = "數量" in row and not row_text(row, "數量").strip()
        if blank or (item.quantity is not None and item.quantity <= 0):
            declaration.blocks.append(f"{label}數量空白或不大於 0。")
        if item.quantity is None:
            continue
        multiplier = {"TNE": 1000, "KGM": 1}.get(item.unit)
        if multiplier and mismatch(item.net_weight, item.quantity * multiplier):
            declaration.warnings.append(f"{label}報單淨重與數量換算不符。")
        if item.unitamt is not None and mismatch(item.fobprice, item.quantity * item.unitamt):
            declaration.warnings.append(f"{label} FOBPRICE 與數量 × UNITAMT 不符。")

    items = declaration.items
    pkgunit = row_text(header, "PKGUNIT").strip()
    if items and pkgunit and all(item.unit == pkgunit and item.quantity is not None for item in items):
        total = sum(item.quantity for item in items)
        if mismatch(number(header, "PKGTOTAL"), total, Decimal("0.5")):
            declaration.warnings.append("主檔 PKGTOTAL 與項次數量合計不符。")
    if items and all(item.net_weight is not None for item in items):
        total = sum(item.net_weight for item in items)
        if mismatch(number(header, "GWEIGHT"), total):
            declaration.warnings.append("主檔 GWEIGHT 與項次淨重合計不符。")


def check_reference_data(db, declaration, header, history_table):
    if history_table:
        rows = optional_rows(db, f"SELECT COUNT(*) AS total, MAX(`傳送時間`) AS latest FROM {history_table} "
                             "WHERE `報單號碼` = ? AND `訊息簡稱` = ?", (declaration.sersno, MESSAGE))
        if rows and rows[0]["total"]:
            history = rows[0]
            latest = history["latest"] or "不明"
            declaration.warnings.append(f"以前送過 {history['total']} 次（最後一次 {latest}）。")

    buyer = declaration.buyer.strip()
    if buyer and "BYBAN" in header and row_text(header, "BYBAN").strip() != buyer_code(buyer):
        declaration.warnings.append("買方代號 BYBAN 與買方名稱推導的代號不同。")
    call_sign = declaration.call_sign
    if not call_sign:
        if "BAID" in header:
            declaration.warnings.append("呼號空白。")
    elif declaration.ship_name.strip():
        table = db.table("myshipname")
        rows = optional_rows(db, f"SELECT SN003 FROM {table} WHERE UPPER(TRIM(SN002)) = ?", (call_sign.upper(),))
        names = {normalized_name(row_text(upper_keys(row), "SN003")) for row in rows or ()} - {""}
        if names - {normalized_name(declaration.ship_name)}:
            declaration.warnings.append("船舶代碼表的船名與報單船名不同。")


def item_signature(items):
    if not items or any(not item.name or not item.unit or item.quantity is None for item in items):
        return ()
    return tuple(sorted((item.name, item.quantity, item.unit) for item in items))


def check_duplicates(db, declarations, dates):
    signatures = {entry.sersno: item_signature(entry.items) for entry in declarations}
    for entry in declarations:
        day, signature = dates.get(entry.sersno), signatures[entry.sersno]
        if day is None or not signature:
            continue
        matches = {
            other for other, other_day in dates.items()
            if other != entry.sersno and other_day is not None and abs((day - other_day).days) <= 3
            and signatures[other] == signature
        }
        if entry.call_sign:
            table = db.table("cusdec")
            recent = optional_rows(
                db, f"SELECT SERSNO, SERSDATE FROM {table} WHERE MSGCODE = ? AND UPPER(TRIM(BAID)) = ? "
                "AND SERSNO <> ? ORDER BY SERSDATE DESC, SERSNO DESC LIMIT 10",
                (MESSAGE, entry.call_sign.upper(), entry.sersno),
            )
            for row in map(upper_keys, recent or ()):
                other, other_day = row_text(row, "SERSNO"), declaration_date(row)
                if other in signatures or other_day is None or abs((day - other_day).days) > 3:
                    continue
                if item_signature(read_items(item_rows(db, other) or ())) == signature:
                    matches.add(other)
        if matches:
            entry.warnings.append("可能重複報單：" + "、".join(sorted(matches)) + "。")


def prepare(sersnos, settings) -> List[Declaration]:
    """Read test declarations and report what should be fixed or confirmed before simulation."""
    declarations = [Declaration(number) for number in split_numbers(sersnos)]
    if not declarations:
        return declarations
    try:
        db = Database(settings, read_only=True)
    except Exception:
        for entry in declarations:
            entry.errors.append("無法開啟測試資料庫。")
        return declarations

    dates = {}
    today = date.today()
    with closing(db):
        history_table = None
        if settings.edi:
            schema = settings.edi.edi_database
            try:
                history_name = db.table_names(schema).get("sendmsgrcds")
            except Exception:
                history_name = None
            if history_name:
                history_table = db.table(history_name, schema)
        for entry in declarations:
            headers = optional_rows(db, f"SELECT * FROM {db.table('cusdec')} WHERE MSGCODE = ? AND SERSNO = ?",
                                    (MESSAGE, entry.sersno))
            if not headers:
                entry.errors.append("無法讀取報單主檔。" if headers is None else "找不到報單。")
                continue
            header = upper_keys(headers[0])
            entry.ship_name = row_text(header, "BANAME")
            entry.call_sign = row_text(header, "BAID").strip()
            entry.buyer = row_text(header, "BYNAME")
            entry.status = row_text(header, "STATUS").strip()
            if entry.status != "Y":
                entry.errors.append("尚未通過邏輯檢查（F6）或目前狀態不允許模擬。")

            day = dates[entry.sersno] = declaration_date(header)
            if day and day > today:
                entry.blocks.append("報關日期在今天之後。")
            elif day and (today - day).days > 7:
                entry.warnings.append("報關日期距今已超過 7 天。")

            rows = item_rows(db, entry.sersno)
            entry.items = read_items(rows or ())
            if not entry.items:
                entry.errors.append("無法讀取報單項次。" if rows is None else "報單沒有項次。")
            check_items(entry, header, rows or ())
            check_reference_data(db, entry, header, history_table)
        check_duplicates(db, declarations, dates)
    return declarations
