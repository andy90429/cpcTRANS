import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


def rounded(value, scale):
    return value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def clean_name(value, column, notes):
    cleaned = " ".join(re.sub(r"&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]*);|&",
                              "", value, flags=re.I).split())
    if cleaned != value:
        notes.append(f"{column} 名稱清理：{value} → {cleaned}")
    return cleaned


def voyage(value, notes):
    if not value.strip():
        notes.append("航次空白，填入 NIL")
        return "NIL"
    return value


def buyer_code(value):
    words = re.findall(r"[A-Za-z]+", re.sub(r"[0-9]", "", value))
    return "".join(word[0] + word[-1] if len(word) > 1 else word for word in words)[:6].upper()


def country(code, countries, notes):
    if code not in countries:
        return "ZZ", "OTHER"
    key, name = countries[code]
    notes.append(f"買方 {code} 國家自動填 {key}（{name}）")
    return key, name


def product_code(value, notes):
    match = re.fullmatch(r"([0-9]{3})([A-Za-z][0-9]{5})([0-9]{3})", "".join(value.split()))
    if match is None:
        notes.append(f"油品代碼格式不符，保留原樣：{value}")
        return value
    code = "-".join(match.groups())
    notes.append(f"油品代碼：{value} → {code}")
    return code


def other_text(value):
    return value, re.sub(r"\r\n|\r|\n", " ", value)


def declaration_date(value):
    year, month, day = map(int, value.split("/"))
    date = datetime(year + 1911, month, day)
    return date.isoformat(sep=" "), f"{year:03d}{month:02d}{day:02d}"


def reference_number(roc_date, sersno):
    return "E" + roc_date + sersno[-4:]
