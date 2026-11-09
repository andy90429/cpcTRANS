import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache

from .xml import EdiError, row_text


REPLY_CODES = {"N5107", "N5204"}
FIELD = re.compile(r"\[([A-Za-z0-9_]+)\.([^\[\]\r\n]+)\]")
TOKEN = re.compile(r"\s*(\[[^\[\]\r\n]+\]|'[A-Za-z0-9]+'|[0-9]+|[A-Za-z_][A-Za-z0-9_.]*|[,()=])")
SELECTORS = {"GovernmentProcedure..tw_TransportTypeCode", "TransportContractDocument..TypeCode"}


@dataclass
class ReplyRow:
    table: str
    fields: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


@dataclass(frozen=True)
class Switch:
    selector: str
    choices: dict


def reply_code(filename):
    prefix, dot, _ = filename.partition(".")
    return prefix.upper() if dot else ""


def tag_name(element):
    return element.tag.rsplit("}", 1)[-1]


def find_children(element, name):
    return [child for child in element if tag_name(child) == name]


def element_text(element):
    return (element.text or "").strip()


@lru_cache(maxsize=256)
def parse_mapping(expression):
    if len(expression) > 4096:
        raise EdiError("回覆 PFIELD 設定過長")
    parts = []
    end = 0
    while end < len(expression):
        match = TOKEN.match(expression, end)
        if not match:
            raise EdiError("回覆 PFIELD 有不支援的語法")
        parts.append(match[1])
        end = match.end()
    tokens = iter(parts)

    def take(expected=None):
        part = next(tokens, None)
        if part is None or (expected is not None and part.casefold() != expected.casefold()):
            raise EdiError("回覆 PFIELD 的 Switch 設定不完整")
        return part

    def mapping():
        part = take()
        match = FIELD.fullmatch(part)
        if match:
            return match[1].upper(), match[2]
        if part.casefold() != "switch":
            raise EdiError("回覆 PFIELD 只接受欄位或 Switch")
        take("(")
        function = take().casefold()
        take("(")
        if function == "xmlnode":
            selector = take()
            if selector not in SELECTORS:
                raise EdiError(f"回覆 PFIELD 不支援 XML 節點 {selector}")
        elif function == "loopsequence":
            selector = "LoopSequence"
        else:
            raise EdiError("回覆 PFIELD 不支援這個 Switch 選擇器")
        take(")")
        take(",")
        choices = {}
        while True:
            key = take().strip("'")
            if not re.fullmatch(r"[A-Za-z0-9]+", key) or key in choices:
                raise EdiError("回覆 PFIELD 的 Switch 分支代碼無效或重複")
            take("=")
            choices[key] = mapping()
            delimiter = take()
            if delimiter == ")":
                return Switch(selector, choices)
            if delimiter != ",":
                raise EdiError("回覆 PFIELD 的 Switch 分支分隔錯誤")

    try:
        result = mapping()
    except RecursionError as error:
        raise EdiError("回覆 PFIELD 的 Switch 層級過深") from error
    if next(tokens, None) is not None:
        raise EdiError("回覆 PFIELD 的 Switch 後有多餘語法")
    return result


def destination(mapping, ancestors, occurrence):
    while isinstance(mapping, Switch):
        if mapping.selector == "LoopSequence":
            key = str(occurrence)
        else:
            parent, child = mapping.selector.split("..")
            key = ""
            for ancestor in reversed(ancestors):
                fields = (node for node in ancestor.iter() if tag_name(node) == parent)
                nodes = next((matches for node in fields if (matches := find_children(node, child))), ())
                if nodes:
                    key = element_text(nodes[0])
                    break
        mapping = mapping.choices.get(key)
    return mapping


class ReplyTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise EdiError("回覆 XML 不接受 DTD")


def convert_reply(config, data, filename):
    code = reply_code(filename)
    if code not in REPLY_CODES or config.code != code or config.root.table != code:
        raise EdiError(f"未支援的回覆格式或設定：{filename}")
    try:
        element = ET.fromstring(data, parser=ET.XMLParser(target=ReplyTreeBuilder()))
    except ET.ParseError as error:
        raise EdiError(f"回覆 XML 格式不正確：{error}") from error
    if tag_name(element) != config.root.name or (
        element.tag.startswith("{") and not element.tag.startswith(f"{{urn:wco:datamodel:TW:{code}:")
    ):
        raise EdiError(f"回覆 XML 根節點與 {code} 不符")
    main = ReplyRow(code)

    def walk(spec, ancestors, row_stack, occurrence):
        current = ancestors[-1]
        for node in spec.children:
            if node.children:
                limit = int(node.row.get("PRECORDCOUNT") or 1)
                for index, child in enumerate(find_children(current, node.name)[:limit], 1):
                    child_stack = row_stack
                    if node.table:
                        row = ReplyRow(node.table)
                        row_stack[-1].children.append(row)
                        child_stack = (*row_stack, row)
                    walk(node, (*ancestors, child), child_stack, index)
                continue
            expression = row_text(node.row, "PFIELD").strip()
            if not expression:
                continue
            segments = [find_children(current, name) for name in node.name.split("+")]
            if not all(segments):
                continue
            target = destination(parse_mapping(expression), ancestors, occurrence)
            if target is None:
                continue
            table, column = target
            row = next((row for row in reversed(row_stack) if row.table == table), None)
            if row is None:
                raise EdiError(f"回覆欄位 {column} 無法對應到 {table} 的父列")
            row.fields[column] = "".join(element_text(group[0]) for group in segments)

    walk(config.root, (element,), (main,), 1)
    if not main.fields.get("報單號碼"):
        raise EdiError("回覆 XML 缺少報單號碼")
    return main


def default_value(expression, filename, now, sequence):
    expression = expression.strip()
    key = re.sub(r"\s+", "", expression).casefold()
    if key == "getdatetime()":
        return now.strftime("%Y-%m-%d %H:%M:%S")
    if key == "xmlfilename()":
        return filename
    if key == "daysequence()":
        return str(sequence)
    if key == "format('yyyymmdd',getdatetime())+format('0000',daysequence())":
        return f"{now:%Y%m%d}{sequence:04d}"
    if re.fullmatch(r"[A-Za-z0-9_.+-]*", expression):
        return expression
    if re.fullmatch(r"'(?:[^']|'')*'", expression):
        return expression[1:-1].replace("''", "'")
    raise EdiError("回覆欄位預設值設定不支援")


def apply_defaults(row, defaults, filename, now, sequence):
    for default in defaults:
        if row_text(default, "PTABLENAME").upper() == row.table:
            column = row_text(default, "PFIELDNAME")
            if column.upper() in {"MAINID", "SUBID", "PID"} or not column:
                raise EdiError("回覆預設值不可指定關聯識別碼")
            row.fields[column] = default_value(row_text(default, "PDEFAULTVALUE"), filename, now, sequence)
    for child in row.children:
        apply_defaults(child, defaults, filename, now, sequence)
