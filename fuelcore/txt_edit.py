"""In-memory editing of a pipe-delimited transfer file."""

import re
import shutil
from codecs import BOM_UTF8, getincrementaldecoder
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from itertools import count
from pathlib import Path
from tempfile import NamedTemporaryFile

from .convert import decode_txt, parse_txt_lines
from .report.calc import density_api, density_from_api, derive_api, rounded, table_factor, ton_to_liters


BACKUP_FOLDER = "原檔備份"
HEADER_FIELDS = {
    0: "專責人員代號", 1: "報單號碼", 4: "出口地點代碼", 6: "推廣貿易服務費率",
    11: "報單日期（民國）", 13: "總淨重（公斤）", 15: "總數量（公噸）", 17: "總統計數量（LTR）",
    20: "船名", 21: "航次", 22: "IMO", 23: "買方編號", 24: "買方英文名稱",
    25: "總金額（台幣）", 26: "總金額（美元）", 27: "船舶呼號", 28: "匯率（美元）",
    29: "申請審驗方式", 31: "推廣貿易服務費（台幣）",
}
ITEM_FIELDS = {
    1: "交貨文件號碼", 2: "稅則號別", 3: "油品代碼", 4: "油品英文名稱", 6: "0",
    7: "淨重（公斤）", 8: "稅費規則字串", 9: "數量（公噸）", 11: "統計數量（LTR）",
    12: "金額（台幣）", 13: "金額（美元）", 14: "單價（美元／公噸）", 15: "統計方式",
}
TOTALS = {13: 7, 15: 9, 17: 11, 25: 12, 26: 13}
HEADER_PLACES = {6: None, 13: 0, 15: 3, 17: 0, 25: 0, 26: 2, 28: 5, 31: 0}
ITEM_PLACES = {7: 0, 9: 3, 11: 0, 12: 0, 13: 2, 14: None}


def numeric(text):
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() and abs(value) < 10**18 and value.as_tuple().exponent >= -24 else None


def formatted(value, original, fallback):
    old = numeric(original)
    places = max(0, -old.as_tuple().exponent) if old is not None else fallback
    with localcontext() as context:
        context.prec = 100
        return format(value if places is None else rounded(value, places), "f")


def rounded_product(left, right, places=0):
    with localcontext() as context:
        context.prec = 100
        return rounded(left * right, places)


def field_value(text, original, places):
    if any(delimiter in text for delimiter in ("|", "\r", "\n")):
        raise ValueError("單一欄位不可含直線分隔符號或換行。")
    value = numeric(text) if places is not ... else None
    return formatted(value, original, places) if value is not None else text


def cp950_fields(line):
    # A multibyte character can contain a pipe byte. Split only decoded pipes.
    decoder = getincrementaldecoder("cp950")()
    start = 0
    for index, byte in enumerate(line):
        if decoder.decode(bytes((byte,))) == "|":
            yield line[start:index]
            start = index + 1
    yield line[start:]


class TxtEditor:
    def __init__(self, path, source_folder=None):
        self.path = Path(path).resolve()
        self.source_folder = Path(source_folder).resolve() if source_folder else self.path.parent
        self.original_bytes = self.path.read_bytes()
        text, self.encoding = decode_txt(self.original_bytes)
        self.parts = re.split(r"(\r\n|\n)", text)
        self.positions = [index for index in range(0, len(self.parts), 2) if self.parts[index].strip()]
        header, items = parse_txt_lines(self.parts[::2])
        self.original_header = tuple(header)
        self.original_items = tuple(tuple(item) for item in items)
        self.inferred = tuple(self.infer_density(item) for item in self.original_items)
        self.restore()

    @staticmethod
    def infer_density(item):
        quantity, liters = numeric(item[9]), numeric(item[11])
        if quantity is not None and quantity > 0 and liters is not None and liters > 0:
            api = derive_api(quantity, liters)
            if api is not None:
                return density_from_api(api)
        return None, None, None

    def restore(self):
        self.header = list(self.original_header)
        self.items = [list(item) for item in self.original_items]
        self.densities = [density for density, _, _ in self.inferred]

    @property
    def dirty(self):
        return (tuple(self.header) != self.original_header
                or any(tuple(item) != old for item, old in zip(self.items, self.original_items)))

    def write_header(self, column, value):
        text = formatted(value, self.original_header[column], HEADER_PLACES[column])
        parsed = numeric(text)
        if parsed is None or numeric(self.header[column]) != parsed:
            self.header[column] = text

    def write_item(self, index, column, value):
        text = formatted(value, self.original_items[index][column], ITEM_PLACES[column])
        parsed = numeric(text)
        if parsed is None or numeric(self.items[index][column]) != parsed:
            self.items[index][column] = text

    def edit_header(self, column, text):
        if text == self.header[column]:
            return
        value = field_value(text, self.original_header[column], HEADER_PLACES.get(column, ...))
        if value == self.header[column]:
            return
        self.header[column] = value
        if column == 15 and len(self.items) == 1:
            self.items[0][9] = field_value(value, self.original_items[0][9], ITEM_PLACES[9])
            self.recalculate_item(0)
        elif column == 28:
            for index in range(len(self.items)):
                self.recalculate_amount(index)
            self.update_totals(25, 26)
            self.update_fee()

    def edit_item(self, index, column, text):
        if column == 0 and text:
            raise ValueError("項次 I0 必須空白。")
        if text == self.items[index][column]:
            return
        value = field_value(text, self.original_items[index][column], ITEM_PLACES.get(column, ...))
        if value == self.items[index][column]:
            return
        self.items[index][column] = value
        if column in (9, 14):
            self.recalculate_item(index)

    def edit_density(self, index, text):
        if not text.strip():
            self.densities[index] = None
            return
        density = numeric(text)
        if density is None:
            raise ValueError("請輸入有效的密度。")
        table_factor(density_api(density))
        if density != self.densities[index]:
            self.densities[index] = density
            self.recalculate_liters(index)
            self.update_totals(17)

    def calculation(self, index):
        density = self.densities[index]
        if density is None:
            return None, None, None
        api = density_api(density)
        factor = table_factor(api)
        quantity = numeric(self.items[index][9])
        return api, factor, ton_to_liters(quantity, factor) if quantity is not None and quantity >= 0 else None

    def recalculate_liters(self, index):
        _, _, liters = self.calculation(index)
        if liters is not None:
            self.write_item(index, 11, liters)

    def recalculate_amount(self, index):
        item = self.items[index]
        quantity, price = numeric(item[9]), numeric(item[14])
        exchange = numeric(self.header[28])
        if quantity is None or price is None:
            return
        usd = rounded_product(quantity, price, 2)
        self.write_item(index, 13, usd)
        if exchange is not None:
            self.write_item(index, 12, rounded_product(usd, exchange))

    def recalculate_item(self, index):
        quantity = numeric(self.items[index][9])
        if quantity is not None:
            self.write_item(index, 7, quantity * 1000)
        self.recalculate_liters(index)
        self.recalculate_amount(index)
        self.update_totals(*TOTALS)
        self.update_fee()

    def total(self, column):
        values = [numeric(item[TOTALS[column]]) for item in self.items]
        return sum(values) if all(value is not None for value in values) else None

    def update_totals(self, *columns):
        for column in columns:
            total = self.total(column)
            if total is not None:
                self.write_header(column, total)

    def fee(self):
        twd, rate = numeric(self.header[25]), numeric(self.header[6])
        return rounded_product(twd, rate) if twd is not None and rate is not None else None

    def update_fee(self):
        fee = self.fee()
        if fee is not None:
            self.write_header(31, fee)

    def warnings(self):
        messages = []
        for index, item in enumerate(self.items):
            label = f"第 {index + 1} 項"
            quantity, price = numeric(item[9]), numeric(item[14])
            _, _, liters = self.calculation(index)
            if liters is None:
                messages.append(f"{label}無法核對 LTR，請確認噸數及密度。")
            elif numeric(item[11]) != liters:
                messages.append(f"{label} LTR 應為 {liters:f}。")
            if quantity is None or price is None:
                messages.append(f"{label}無法核對美元金額，請確認噸數及單價。")
            elif numeric(item[13]) != rounded_product(quantity, price, 2):
                messages.append(f"{label}美元金額與噸數 × 單價（2 位）不同。")
        for column in (17, 26, 25, 15):
            total = self.total(column)
            if total is None or numeric(self.header[column]) != total:
                messages.append(f"H{column} {HEADER_FIELDS[column]}與項次合計不同或無法核對。")
        fee = self.fee()
        if fee is None or numeric(self.header[31]) != fee:
            messages.append("H31 推廣費與台幣 × 費率（四捨五入）不同或無法核對。")
        return messages

    def to_bytes(self):
        if self.encoding == "cp950":
            return self.cp950_bytes()
        parts = self.parts.copy()
        for position, row in zip(self.positions, (self.header, *self.items)):
            parts[position] = "|".join(row)
        return "".join(parts).encode(self.encoding)

    def cp950_bytes(self):
        bom = BOM_UTF8 if self.original_bytes.startswith(BOM_UTF8) else b""
        parts = re.split(rb"(\r\n|\n)", self.original_bytes[len(bom):])
        for position, row, original in zip(
            self.positions, (self.header, *self.items), (self.original_header, *self.original_items)
        ):
            if tuple(row) == original:
                continue
            try:
                parts[position] = b"|".join(
                    raw if value == previous else value.encode("cp950")
                    for raw, value, previous in zip(cp950_fields(parts[position]), row, original)
                )
            except UnicodeEncodeError:
                raise ValueError("修改內容含有 cp950（Big5）無法儲存的字元。") from None
        return bom + b"".join(parts)

    def save(self):
        if not self.dirty:
            return None
        if self.path.read_bytes() != self.original_bytes:
            raise ValueError("原 TXT 已由其他程式修改，請關閉編輯視窗後重新開啟。")
        data = self.to_bytes()
        backup = self.backup_original()
        temp = None
        try:
            with NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temp = Path(stream.name)
                stream.write(data)
            shutil.copymode(self.path, temp)
            if self.path.read_bytes() != self.original_bytes:
                raise ValueError("原 TXT 已由其他程式修改，請關閉編輯視窗後重新開啟。")
            temp.replace(self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return backup

    def backup_original(self):
        folder = self.source_folder / BACKUP_FOLDER
        folder.mkdir(exist_ok=True)
        stem = f"{self.path.stem}.{datetime.now():%Y%m%d_%H%M%S_%f}"
        for index in count():
            suffix = f"_{index}" if index else ""
            backup = folder / f"{stem}{suffix}.txt"
            try:
                stream = backup.open("xb")
            except FileExistsError:
                continue
            try:
                with stream:
                    stream.write(self.original_bytes)
            except OSError:
                backup.unlink(missing_ok=True)
                raise
            return backup
