import ast
import operator
import re
from decimal import Decimal, DecimalException
from functools import lru_cache


class ExpressionError(ValueError):
    pass


TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<field>\[(?P<table>[A-Za-z0-9_]+)\.(?P<column>[^\[\]\r\n]+)\])"
    r"|(?P<string>'(?:[^'\\\r\n]|\\.)*'|\"(?:[^\"\\\r\n]|\\.)*\")"
    r"|(?P<number>[0-9]+(?:\.[0-9]*)?|\.[0-9]+)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op><>|!=|==|<=|>=|[-+*/(),=<>])"
)
NAMES = {"mid", "format", "iif", "switch", "and", "or", "not", "true", "false"}
ARITY = {"mid": 3, "format": 2, "iif": 3, "_field": 2, "_number": 1}
ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt,
           ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le}


def source_tokens(expression):
    end = 0
    while end < len(expression):
        match = TOKEN.match(expression, end)
        if not match:
            raise ExpressionError(f"運算式在第 {end + 1} 字元使用不支援的語法")
        end = match.end()
        kind, value = match.lastgroup, match.group()
        if kind == "space":
            continue
        if kind == "field":
            yield f"_field({match['table']!r}, {match['column']!r})"
        elif kind == "number":
            yield f"_number({value!r})"
        elif kind == "name":
            name = value.lower()
            if name not in NAMES:
                raise ExpressionError(f"運算式不支援名稱 {value}")
            yield name.title() if name in ("true", "false") else name
        else:
            yield {"=": "==", "<>": "!="}.get(value, value) if kind == "op" else value


def validate(node):
    if isinstance(node, ast.Constant) and type(node.value) in (str, bool):
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        name, count = node.func.id, len(node.args)
        if (name == "switch" and count >= 2 and count % 2 == 0) or ARITY.get(name) == count:
            for arg in node.args:
                validate(arg)
            return
    if isinstance(node, ast.BinOp) and type(node.op) in ARITHMETIC:
        validate(node.left)
        validate(node.right)
        return
    if isinstance(node, ast.Compare) and all(type(op) in COMPARE for op in node.ops):
        for part in (node.left, *node.comparators):
            validate(part)
        return
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for part in node.values:
            validate(part)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.UAdd, ast.USub)):
        validate(node.operand)
        return
    raise ExpressionError("運算式使用不支援的語法或函式參數")


@lru_cache(maxsize=1024)
def compile_expression(expression):
    if len(expression) > 4096:
        raise ExpressionError("運算式過長")
    try:
        tree = ast.parse(" ".join(source_tokens(expression)), mode="eval").body
        validate(tree)
    except (SyntaxError, RecursionError) as error:
        raise ExpressionError("運算式語法錯誤") from error
    return tree


def number(value):
    result = Decimal(str(value).strip() or "0")
    if not result.is_finite():
        raise ExpressionError("運算式只接受有限數字")
    return result


def formatted(pattern, value):
    match = re.fullmatch(r"f([0-9]{1,2})", str(pattern), re.IGNORECASE)
    if match:
        return format(number(value), f".{int(match[1])}f")
    if str(pattern).upper() == "YYYY-MM-DD":
        day = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(value))
        if day:
            return f"{day[1]}-{int(day[2]):02d}-{int(day[3]):02d}"
    return str(value)


def run(node, context):
    def value(child):
        return run(child, context)

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Call):
        name = node.func.id
        if name == "iif":
            test, yes, no = node.args
            return value(yes if value(test) else no)
        if name == "switch":
            for test, result in zip(node.args[::2], node.args[1::2]):
                if value(test):
                    return value(result)
            return ""
        args = [value(arg) for arg in node.args]
        if name == "_field":
            table, column = args
            field = context.get(table.upper(), {}).get(column.upper())
            return "" if field is None else str(field)
        if name == "_number":
            return number(args[0])
        if name == "mid":
            text, start, length = args
            start, length = int(number(start)), int(number(length))
            if start < 1 or length < 0:
                raise ExpressionError("mid 起點須從 1 開始，長度不可為負數")
            return str(text)[start - 1:start - 1 + length]
        return formatted(*args)
    if isinstance(node, ast.BinOp):
        left, right = value(node.left), value(node.right)
        if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
            return left + right
        return ARITHMETIC[type(node.op)](number(left), number(right))
    if isinstance(node, ast.Compare):
        left = value(node.left)
        for op, right_node in zip(node.ops, node.comparators):
            right = value(right_node)
            pair = (left, right) if isinstance(left, str) and isinstance(right, str) else (number(left), number(right))
            if not COMPARE[type(op)](*pair):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        return (all if isinstance(node.op, ast.And) else any)(value(part) for part in node.values)
    operand = value(node.operand)
    if isinstance(node.op, ast.Not):
        return not operand
    return -number(operand) if isinstance(node.op, ast.USub) else number(operand)


def evaluate(expression, context):
    if not expression or not expression.strip():
        return ""
    try:
        return run(compile_expression(expression.strip()), context)
    except (DecimalException, OverflowError, RecursionError) as error:
        raise ExpressionError("運算式的數值無法計算") from error
