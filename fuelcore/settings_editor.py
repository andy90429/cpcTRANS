import json
import re
from contextlib import closing

from .db import Database
from .settings import Settings, other_fields, read_config
from .workbench import config_source, save_options


PRODUCT_FIELDS = ("tariff", "description", "approval", "permit", "permit_item")


def unsupported_cp950(text):
    characters = {}
    for char in text:
        try:
            char.encode("cp950")
        except UnicodeEncodeError:
            characters[char] = None
    return tuple(characters)


def save_other(path, enabled, fields, text):
    save_options(path, {"other": {"enabled": enabled, "fields": ",".join(other_fields(fields)),
                                  "text_json": json.dumps(text, ensure_ascii=False)}})


def valid_product(code):
    return re.fullmatch(r"[0-9]{3}-[A-Z][0-9]{5}-[0-9]{3}", code) is not None


def valid_buyer(code):
    return re.fullmatch(r"[A-Z0-9]{6}", code) is not None


def save_products(path, rows):
    old = read_config(config_source(path))
    values = {}
    for code, row in rows:
        if not valid_product(code):
            raise ValueError(f"油品代碼 {code} 須符合 999-X99999-999。")
        if code in values:
            raise ValueError(f"油品代碼重複：{code}")
        original = old.get("products", code, fallback=None)
        values[code] = original if original is not None and json.loads(original) == row else json.dumps(
            row, ensure_ascii=False)
    save_options(path, {"products": values}, replace_section="products")


def save_countries(path, rows):
    values = {}
    for code, country, name in rows:
        if not valid_buyer(code):
            raise ValueError(f"買方代號 {code} 須為 6 碼英數。")
        if code in values:
            raise ValueError(f"買方代號重複：{code}")
        if not country or len(country.split()) != 1 or not name:
            raise ValueError("請填寫國家代碼與國家名稱。")
        values[code] = f"{country} {name}"
    save_options(path, {"buyer_countries": values}, replace_section="buyer_countries")


def lookup_note(path, code):
    config = read_config(config_source(path))
    settings = Settings(config, path.parent)
    schema = getattr(settings, "code_database", None)
    with closing(Database(settings, read_only=True)) as db:
        name = db.table_names(schema).get("mynoteabbr")
        if name is None:
            raise ValueError("測試代碼庫沒有 mynoteabbr 資料表。")
        try:
            rows = db.select(f"SELECT NA003, NA004 FROM {db.table(name, schema)} WHERE NA002 = ? LIMIT 1", (code,))
        except Exception:
            raise ValueError("無法讀取測試代碼表 mynoteabbr。") from None
    if not rows:
        raise ValueError("找不到代碼。")
    name, text = rows[0]
    return str(name or code), str(text or "").replace("\r\n", "\n").replace("\r", "\n")
