import operator
import re
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from functools import lru_cache


class FormulaError(ValueError):
    pass


TOKEN = re.compile(r"\s*(?:(\[[^\[\]\r\n]+\])|('(?:[^'\r\n]|'')*'|\"(?:[^\"\r\n]|\"\")*\")"
                   r"|([0-9]+(?:\.[0-9]+)?)|([^\W\d]\w*)|(<>|!=|<=|>=|==|[()+*/=<>,-]))")
PRECEDENCE = {"OR": 1, "AND": 2, "=": 3, "==": 3, "<>": 3, "!=": 3, "<": 3, ">": 3,
              "<=": 3, ">=": 3, "+": 4, "-": 4, "*": 5, "/": 5}
ARITY = {"IIF": 3, "MID": 3, "FMT": 2, "FORMAT": 2, "MYORD": 1, "COUNT": 1,
         "ADDALL": 2, "GETPUBVAR": 1, "TEXTVAL": 1}
COMPARE = {"=": operator.eq, "==": operator.eq, "<>": operator.ne, "!=": operator.ne,
           "<": operator.lt, ">": operator.gt, "<=": operator.le, ">=": operator.ge}


@lru_cache(maxsize=1024)
def parse(source):
    source = source.strip()
    if not source or len(source) > 16384:
        raise FormulaError("公式空白或過長")
    tokens, offset = [], 0
    while offset < len(source):
        match = TOKEN.match(source, offset)
        if not match:
            raise FormulaError(f"公式第 {offset + 1} 字元無法辨識")
        tokens.append((match.lastindex, match[match.lastindex]))
        offset = match.end()
    tokens.append((0, ""))
    index = 0

    def current():
        return tokens[index][1].upper()

    def take(expected=None):
        nonlocal index
        token = tokens[index]
        if not token[0] or (expected is not None and current() != expected):
            raise FormulaError("公式參數或括號不完整")
        index += 1
        return token

    def expression(minimum=1):
        kind, raw = take()
        word = raw.upper()
        if kind == 5 and raw == "(":
            left = expression()
            take(")")
        elif (kind == 5 and raw in "+-") or (kind == 4 and word == "NOT"):
            left = ("unary", word, expression(3 if word == "NOT" else 6))
        elif kind == 1:
            left = ("ref", raw[1:-1].strip())
        elif kind == 2:
            left = ("literal", raw[1:-1].replace(raw[0] * 2, raw[0]))
        elif kind == 3:
            left = ("literal", Decimal(raw))
        elif kind == 4 and current() == "(":
            take("(")
            if word not in ARITY:
                raise FormulaError(f"不支援報表函式 {raw}")
            args = []
            for n in range(ARITY[word]):
                if n:
                    take(",")
                args.append(expression())
            take(")")
            left = ("call", word, args)
        elif kind == 4 and word in ("TRUE", "FALSE"):
            left = ("literal", word == "TRUE")
        elif kind == 4 and word not in PRECEDENCE:
            left = ("ref", raw)
        else:
            raise FormulaError("不支援的報表公式")
        while PRECEDENCE.get(current(), 0) >= minimum:
            op = take()[1].upper()
            left = ("binary", op, left, expression(PRECEDENCE[op] + 1))
        return left

    try:
        tree = expression()
    except RecursionError as error:
        raise FormulaError("報表公式層級過深") from error
    if current():
        raise FormulaError("報表公式有多餘內容")
    return tree


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def decimal(value):
    number = Decimal(as_text(value).replace(",", "").strip() or "0")
    if not number.is_finite() or abs(number.adjusted()) > 100:
        raise FormulaError("報表數值超出範圍")
    return number


def truth(value):
    return value != 0 if isinstance(value, (int, float, Decimal)) else as_text(value).casefold() in {"true", "y", "1"}


def format_value(value, pattern):
    text = as_text(value)
    if any(token in pattern for token in ("YY", "mm", "dd")):
        parts = re.match(r"^(\d{2,4})[-/](\d{1,2})[-/](\d{1,2})", text)
        if not parts:
            return text
        year, month, day = map(int, parts.groups())
        year = year - 1911 if year >= 1911 else year
        return re.sub(r"^民國\s*", "", pattern).replace("YY", f"{year:03d}").replace("mm", f"{month:02d}").replace("dd", f"{day:02d}")
    if "X" in pattern and re.fullmatch(r"[X ./-]+", pattern):
        chars = iter(text)
        out, used = [], 0
        for char in pattern:
            if used == len(text):
                break
            if char == "X":
                used += 1
                char = next(chars)
            out.append(char)
        return "".join(out) + "".join(chars)
    if "#" not in pattern and "0" not in pattern:
        return text
    sections = pattern.split(";")
    if not text.strip() and len(sections) < 3:
        return ""
    try:
        amount = decimal(value)
    except DecimalException:
        return text
    section = sections[2] if amount == 0 and len(sections) > 2 else sections[1] if amount < 0 and len(sections) > 1 else sections[0]
    if section.startswith('"') and section.endswith('"'):
        return section[1:-1]
    fraction = section.partition(".")[2]
    required, maximum = fraction.count("0"), fraction.count("0") + fraction.count("#")
    rounded = abs(amount).quantize(Decimal(1).scaleb(-maximum), rounding=ROUND_HALF_UP)
    rendered = format(rounded, f"{',' if ',' in section else ''}.{maximum}f")
    if maximum > required:
        whole, _, rest = rendered.partition(".")
        rest = rest.rstrip("0").ljust(required, "0")
        rendered = whole + ("." + rest if rest else "")
    if amount < 0:
        return f"({rendered})" if section.startswith("(") else "-" + rendered
    return rendered


def evaluate(source, resolve, column=lambda key: (), variables=None):
    variables = variables or {}

    def run(node):
        tag, value, *args = node
        if tag == "literal":
            return value
        if tag == "ref":
            return resolve(value)
        if tag == "unary":
            operand = run(args[0])
            return not truth(operand) if value == "NOT" else decimal(operand) * (-1 if value == "-" else 1)
        if tag == "call":
            nodes = args[0]
            if value == "IIF":
                return run(nodes[1] if truth(run(nodes[0])) else nodes[2])
            if value in {"COUNT", "ADDALL"}:
                if nodes[0][0] != "ref":
                    raise FormulaError(f"{value} 必須指定資料欄位")
                values = [as_text(item) for item in column(nodes[0][1]) if as_text(item).strip()]
                return len(values) if value == "COUNT" else ("\r\n" + " " * min(256, max(0, int(decimal(run(nodes[1])))))).join(values)
            values = [run(item) for item in nodes]
            if value == "MID":
                text, start, length = values
                start = max(0, int(decimal(start)) - 1)
                return as_text(text)[start:start + max(0, int(decimal(length)))]
            if value in {"FMT", "FORMAT"}:
                return format_value(values[0], as_text(values[1]))
            if value == "GETPUBVAR":
                return variables.get(as_text(values[0]).casefold(), "")
            if value == "TEXTVAL":
                return variables.get(as_text(values[0]).casefold(), "")
            return as_text(values[0])
        left = run(args[0])
        if value == "AND":
            return truth(left) and truth(run(args[1]))
        if value == "OR":
            return truth(left) or truth(run(args[1]))
        right = run(args[1])
        if value in COMPARE:
            pair = as_text(left).strip(), as_text(right).strip()
            if all(pair) or isinstance(left, (int, float, Decimal)) or isinstance(right, (int, float, Decimal)):
                try:
                    pair = decimal(left), decimal(right)
                except DecimalException:
                    pass
            return COMPARE[value](*pair)
        if value == "+":
            if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
                return decimal(left) + decimal(right)
            return as_text(left) + as_text(right)
        return {"-": operator.sub, "*": operator.mul, "/": operator.truediv}[value](decimal(left), decimal(right))

    try:
        return run(parse(source))
    except (DecimalException, OverflowError, RecursionError) as error:
        raise FormulaError("無法計算報表數值") from error
