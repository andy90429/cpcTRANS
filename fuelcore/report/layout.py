import math
import re


def units(text):
    return sum(2 if ord(char) > 127 else 1 for char in text)


def wrap_lines(text, columns=None):
    out = []
    for paragraph in re.split(r"\r\n|\r|\n", text):
        if columns is None:
            out.append(paragraph)
            continue
        line = ""
        first = True
        for token in re.findall(r"[^\s\x80-\U0010ffff]+|[^\x00-\x7f\s]|\s+", paragraph):
            if token.isspace():
                if line or first:
                    line += token
                continue
            if line and units(line) + units(token) > columns:
                out.append(line.rstrip())
                line = ""
                first = False
            if units(token) > columns:
                for char in token:
                    if line and units(line + char) > columns:
                        out.append(line.rstrip())
                        line = ""
                        first = False
                    line += char
            else:
                line += token
        if line or first:
            out.append(line.rstrip())
    return out


def font_height(field):
    return abs(field.number("Font.Height", -13)) or 13


def field_lines(field):
    height = font_height(field)
    columns = max(1, math.floor(field.number("Width", 1) / math.ceil(height / 2))) if field.boolean("WordWrap") else None
    return wrap_lines(field.text("Caption"), columns)


def drawn(field):
    return field.boolean("Visible", True) and bool(field.text("Caption").strip() or "line" in field.class_name.lower())


def content_height(field):
    if field.boolean("AutoSize", True) and field.text("Caption").strip():
        return max(1, len(field_lines(field))) * font_height(field)
    return max(1, field.number("Height", font_height(field)))
