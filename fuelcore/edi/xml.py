import csv
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.sax.saxutils import escape

from ..convert import MESSAGE
from .expression import evaluate


CODE = "N5203"
XML_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
XML_ATTRIBUTE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.-]*:)?[A-Za-z_][A-Za-z0-9_.-]*")
CONFIG_TABLES = ("xmlsetcontent", "tb_tablestartnode", "tb_tablerelations")


class EdiError(ValueError):
    pass


def upper_keys(row):
    return {key.upper(): value for key, value in row.items()}


def row_text(row, key):
    value = row.get(key)
    return "" if value is None else str(value)


def numeric(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


@dataclass
class Node:
    name: str
    row: dict
    children: list = field(default_factory=list)
    table: str = ""


class XmlSettings:
    def __init__(self, content, starts, relations, *, code=CODE, defaults=()):
        self.code = code
        default_rows = (upper_keys(row) for row in defaults)
        self.defaults = [row for row in default_rows if row.get("PSETUPCODE") == code]
        mapping = {}
        for raw in starts:
            row = upper_keys(raw)
            if row.get("PSETUPCODE") == code:
                key = (row["PSTARTNODE"], int(row.get("PSTARTSEQUENCE") or 1))
                mapping[key] = row["PTABLENAME"].upper()
        self.parents = {}
        for raw in relations:
            row = upper_keys(raw)
            if row_text(row, "PDATABASE").upper() == "CBSDATA":
                self.parents[row["PFOREIGNTABLENAME"].upper()] = row["PPRIMARYTABLENAME"].upper()
        rows = (upper_keys(row) for row in content)
        rows = sorted((row for row in rows if row.get("PSETUPCODE") == code),
                      key=lambda row: numeric(row.get("PORDER")))
        self.root = self.build_tree(rows, mapping, code)

    @classmethod
    def from_csv(cls, folder, *, code=CODE):
        def read(table):
            path = Path(folder) / f"xmlsetcontent.{table}.csv"
            with path.open(encoding="utf-8-sig", newline="") as stream:
                return list(csv.DictReader(stream))

        return cls.from_tables(read, code)

    @classmethod
    def load(cls, db, *, code=CODE):
        folder = os.environ.get("FUELCORE_CBS_CONFIG")
        if folder:
            return cls.from_csv(folder, code=code)
        schema = db.settings.edi.xmlset_database
        names = db.table_names(schema)

        def read(table):
            if table not in names:
                raise EdiError(f"XML 設定缺少資料表 {schema}.{table}")
            return db.select_dicts(f"SELECT * FROM {db.table(names[table], schema)}")

        return cls.from_tables(read, code)

    @classmethod
    def from_tables(cls, read, code):
        defaults = read("tb_definefieldvalue") if code != CODE else ()
        return cls(*(read(table) for table in CONFIG_TABLES), code=code, defaults=defaults)

    @staticmethod
    def build_tree(rows, mapping, code):
        stack = []
        root = None
        occurrences = Counter()
        for row in rows:
            name = row_text(row, "PXMLNAME")
            level = int(row.get("PLEVELS") or 0)
            parts = name.split("+") if code != CODE else [name]
            if not all(XML_NAME.fullmatch(part) for part in parts) or not 0 <= level <= len(stack):
                raise EdiError(f"{code} XML 節點名稱或層級不正確：{name}")
            stack = stack[:level]
            if level:
                parent = stack[-1]
                key = f"{parent.name}..{name}"
                occurrences[key] += 1
                table = mapping.get((key, occurrences[key]), "")
                node = Node(name, row, table=table)
                parent.children.append(node)
            else:
                expected = "Declaration" if code == CODE else "Response"
                if root is not None or name != expected:
                    raise EdiError(f"{code} XML 設定的根節點必須是 {expected}")
                root = node = Node(name, row, table=mapping.get((name, 1), ""))
            stack.append(node)
        if root is None:
            raise EdiError(f"XML 轉換設定沒有 {code}")
        return root


def field_text(value, node):
    kind = node.row.get("PTYPE")
    if kind == "I" and str(value).strip():
        integer = numeric(value)
        if integer == integer.to_integral_value():
            value = str(int(integer))
    elif kind == "F" and isinstance(value, Decimal):
        value = format(value.normalize(), "f")
    text = re.sub(r"\r\n|[\r\n]", " ", str(value)).strip()
    limit = int(node.row.get("PLENGTH") or 0)
    return text[:limit] if limit > 0 else text


def render_bytes(root, properties):
    attrs = []
    for line in properties.splitlines():
        name, equals, value = line.strip().partition("=")
        if not name:
            continue
        if not equals or not XML_ATTRIBUTE.fullmatch(name.strip()):
            raise EdiError("XML 根節點屬性格式錯誤")
        attrs.append(' {}="{}"'.format(name.strip(), escape(value.strip(), {'"': '&quot;'})))
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<{root.tag}{''.join(attrs)}>"]

    def append(node, depth):
        prefix = "  " * depth
        if len(node):
            lines.append(f"{prefix}<{node.tag}>")
            for child in node:
                append(child, depth + 1)
            lines.append(f"{prefix}</{node.tag}>")
        else:
            lines.append(f"{prefix}<{node.tag}>{escape(node.text or '')}</{node.tag}>")

    for child in root:
        append(child, 1)
    lines.append(f"</{root.tag}>")
    data = "\r\n".join(lines).encode("utf-8-sig")
    try:
        ET.fromstring(data)
    except ET.ParseError as error:
        raise EdiError(f"XML 格式不正確：{error}") from error
    return data


def check_send_xml(root, sersno, item_count):
    def children(element, name):
        return [child for child in element if child.tag.rsplit("}", 1)[-1] == name]

    shipments = children(root, "GoodsShipment")
    if len(shipments) != 1:
        raise EdiError(f"{sersno} XML 須有一個貨物段（GoodsShipment）")
    goods = shipments[0]
    actual = len(children(goods, "GovernmentAgencyGoodsItem"))
    if not actual or actual != item_count:
        raise EdiError(f"{sersno} XML 項次數 {actual} 與報單項次 {item_count} 不符，或沒有貨物項次")
    for name in ("Exporter", "Consignment"):
        if not children(goods, name):
            raise EdiError(f"{sersno} XML 缺少 {name}")


@dataclass(frozen=True)
class SendDocument:
    data: bytes
    declaration: dict
    item_count: int


class SendXmlBuilder:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.schema = getattr(db.settings, "database", None)
        self.tables = db.table_names(self.schema)

    def document(self, sersno, *, ready_only=False):
        loaded = {}

        def rows(table):
            if table not in loaded:
                name = self.tables.get(table.casefold())
                if name is None and table in ("CUSDEC", "INVITEM"):
                    raise EdiError(f"測試庫缺少資料表 {table}")
                data = self.db.select_dicts(
                    f"SELECT * FROM {self.db.table(name, self.schema)} WHERE SERSNO = ? AND MSGCODE = ?",
                    (sersno, MESSAGE),
                ) if name is not None else []
                loaded[table] = [upper_keys(row) for row in data]
            return loaded[table]

        declarations = rows("CUSDEC")
        if len(declarations) != 1:
            raise EdiError(f"{sersno} 找不到唯一的 {MESSAGE} 報單")
        declaration = declarations[0]
        status = row_text(declaration, "STATUS").strip()
        if ready_only and status != "Y":
            raise EdiError(f"{sersno} STATUS={status or '空白'}，僅 Y 可規劃送信")

        def elements(node, context):
            table = node.table
            records = rows(table) if table and table != "CUSDEC" else [{}]
            parent = self.config.parents.get(table)
            if parent == "INVITEM":
                item = numeric(context.get("INVITEM", {}).get("項次號碼"))
                records = [record for record in records if numeric(record.get("ITEM")) == item]
            elif parent and parent != "CUSDEC" and records:
                if not context.get(parent) or len(rows(parent)) != 1:
                    raise EdiError(f"{sersno} 無法判定 {table} 應對應哪一筆 {parent}")
            records = sorted(records, key=lambda row: (numeric(row.get("項次號碼") or row.get("PART")),
                                                        numeric(row.get("PORDERFLD"))))
            limit = int(node.row.get("PRECORDCOUNT") or 1)
            for record in records[:limit] or [{}]:
                scoped = {**context, table: record} if table and table != "CUSDEC" else context
                condition = row_text(node.row, "PIIF").strip()
                if condition and not evaluate(condition, scoped):
                    continue
                element = ET.Element(node.name)
                if node.children:
                    for child in node.children:
                        element.extend(elements(child, scoped))
                    if len(element):
                        yield element
                else:
                    element.text = field_text(evaluate(node.row.get("PFIELD"), scoped), node)
                    if element.text:
                        yield element

        spec = self.config.root
        root = ET.Element(spec.name)
        context = {"CUSDEC": declaration, "CUSDEC_OTHER": next(iter(rows("CUSDEC_OTHER")), {})}
        for node in spec.children:
            root.extend(elements(node, context))
        count = len(rows("INVITEM"))
        check_send_xml(root, sersno, count)
        return SendDocument(render_bytes(root, row_text(spec.row, "PPROPERTY")), declaration, count)

    def xml_bytes(self, sersno):
        return self.document(sersno).data
