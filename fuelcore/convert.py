import re
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List

from . import codes, rules
from .db import Database
from .schema import read_schema


MESSAGE = "5203S"
TAX_NAME = "出口推廣貿易服務費"
HEADER_FIXED = {
    "SERSTYPE": "G5", "PORTCODE": "TWTXG", "PORTNAME": "TAICHUNG ,TAIWAN",
    "DESTCODE": "TWZ99", "DESTNAME": "SHIP USE", "MSGNO": "22", "TAXCHECK": "N",
    "PKGUNIT": "TNE", "MAWBNO": "NIL", "MARKS": "N/M", "EDIMARKS": "N/M", "TRAN": "13",
    "賣方編號": "03707901", "SLBAN": "03707901", "SLCODE": "58",
    "SLNAME": "CPC CORPORATION. TAIWAN",
    "SLADDR": "No.2 Zuo Nan Road,Nannan Distrist, Nanzi District,Kaohsiung,TAIWAN,R.O.C.",
    "SLCNAME": "台灣中油股份有限公司", "SLCADDR": "高雄市楠梓區宏南里左楠路2號",
    "客戶簡稱": "CPC CORPORATION. TAI", "TERM": "FOB", "MONEY": "USD",
    "SATYPE": "1", "IETYPE": "2", "是否已計算": 1, "是否已邏輯": 0,
    "STATUS": "0", "原狀態": "0", "UPDATE": "9", "不印日報": "N", "IS_PERMITNO": "Y",
    "金額小數位": 2,
}
ITEM_FIXED = {
    "帶值": "*", "數量單位": "TNE", "商標": "NO BRAND", "GOODBRAND": "NIL",
    "CRTNO": "NIL", "QuantityRate": 1, "統計單位": "LTR", "RATIO_DUTY": 100,
    "RATIO_RATE": 100, "RATIO_AMT": 100, "RATIO_GOOD": 100, "IS_DUTYAMT_BYINPUT": "N",
    "IS_GOODRATE_BYINPUT": "N", "免計營業稅": "N", "換算淨重": "N", "UNITCURR": "USD",
    "UNITTERM": "FOB",
}
SCHEMA = read_schema()


@dataclass
class Result:
    ok: bool = False
    sersno: str = ""
    message: str = ""
    notes: List[str] = field(default_factory=list)


def read_txt(path):
    lines = [line.split("|") for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
             if line.strip()]
    if not lines or len(lines[0]) != 38:
        raise ValueError("主檔必須有 38 欄")
    header, *items = lines
    if not items or any(len(item) != 17 or item[0] != "" for item in items):
        raise ValueError("至少須有一個項次，每項 17 欄且第 0 欄須空白")
    return header, items


def number(value):
    numeric = Decimal(value)
    if not numeric.is_finite():
        raise ValueError("數字必須是有限值")
    return numeric


def fit(value, kind, label, notes):
    if kind.startswith("varchar"):
        value = str(value)
        length = int(kind[8:-1])
        if len(value) > length:
            notes.append(f"{label} 超過 {length} 字，已截斷")
        return value[:length]
    if kind.startswith(("int(", "bigint(")):
        bits = 64 if kind.startswith("bigint") else 32
        integer = int(number(value))
        if not -(2 ** (bits - 1)) <= integer < 2 ** (bits - 1):
            raise ValueError(f"{label} 超出整數欄位範圍")
        return integer
    if kind.startswith(("decimal(", "double(")):
        width, scale = map(int, re.search(r"\((\d+),(\d+)\)", kind).groups())
        amount = rules.rounded(number(value), scale)
        if abs(amount) >= 10 ** (width - scale):
            raise ValueError(f"{label} 超出數值欄位範圍")
        return format(amount, "f")
    return value


def insert(db, table, key, values, notes):
    values = {**key, **values}
    db.insert(table, {column: fit(value, SCHEMA[table][column]["COLUMN_TYPE"],
                                  f"{table}.{column}", notes)
                      for column, value in values.items()})


def package_total(value, notes):
    # TODO(Q3): 待確認 PKGTOTAL 的小數公噸處理，目前四捨五入至整數。
    integer = int(rules.rounded(value, 0))
    if integer != value:
        notes.append(f"PKGTOTAL 四捨五入：{value} → {integer}")
    return integer


def header_values(db, h, items, products, settings, notes):
    date, roc = rules.declaration_date(h[11])
    buyer_name = rules.clean_name(h[24], "BYNAME", notes)
    ship_name = rules.clean_name(h[20], "BANAME", notes)
    buyer_code = rules.buyer_code(buyer_name)
    country_code, country_name = rules.country(buyer_code, settings.countries, notes)
    other, ediother = rules.other_text(settings.other)
    values = dict(HEADER_FIXED)
    values.update(APLCHECK=h[29], STPLACE=h[4], BYCRTYCODE=country_code, BYCRTYNAME=country_name,
                  SERSDATE=date, CreateDateTime=date, GWEIGHT=h[13], NWEIGHT=h[13], TOT_NETWT=h[13],
                  PKGTOTAL=package_total(number(h[15]), notes), OTHER=other,
                  EDIOTHER=ediother, BANO=rules.voyage(h[21], notes), BACALLNO=h[22],
                  BANAME=ship_name, CUSBOXNO=settings.cusboxno, CUSMANNO=h[0],
                  BYNAME=buyer_name, BYBAN=buyer_code, FOBPRICE=h[26],
                  TOT_AMOUNT=h[26], TOTALAMT=h[26], TWDPRICE=h[25], TOT_DUTYAMT=h[25],
                  FOREX=rules.rounded(number(h[28]), 3), TAXTOTAL=h[31], ITEMNAME=products[0][0],
                  ITEMCODE=items[0][2], REGISTRYID=h[1][4:10], SHIPPINGID=h[1][-4:],
                  LastModifyDateTime=datetime.now().isoformat(sep=" ", timespec="seconds"))
    values.update({"買方編號": h[23], "買方簡稱": buyer_name[:20],
                   "參考號碼": rules.reference_number(roc, h[1])})
    for column in ("鍵檔人員", "LastModifyUser", "CreateUserName", "ModifyUserName"):
        values[column] = settings.user_code
    # TODO(Q4): Omit every unspecified cusdec column so the database supplies its default.
    codes.buyer(db, fit(h[23], "varchar(10)", "mybuyer.BY002", notes),
                fit(buyer_name, "varchar(200)", "mybuyer.BY003", notes),
                buyer_code, settings.write_codes, notes)
    values["BAID"] = codes.maintain_ship(db, h[27], h[22], ship_name, settings.write_codes, notes)
    return values


def write_details(db, key, h, items, products, notes):
    fee = number(h[6])
    for index, (item, (code, product)) in enumerate(zip(items, products), 1):
        values = dict(ITEM_FIXED)
        values.update({
            "項次號碼": index, "PINVORDER": index, "貨品編號": item[4][:30],
            "貨品名稱": f"{code} {item[4].upper()}",
            # TODO(Q5): Confirm the configured fixed line breaks.
            "報單品名": product["description"], "數量": item[9], "單價": item[14],
            "UNITAMT": item[14], "FOBUNITAMT": item[14], "小計金額": item[13], "FOBPRICE": item[13],
            # TODO(Q1): Item tariff comes from the product table, header ITEMCODE comes from I2.
            "稅則編號": product["tariff"], "報單淨重": item[7], "統計數量": item[11],
            "DUTYAMT": item[12], "STCTYPE": item[15],
            "B52_項次稅費": rules.rounded(number(item[12]) * fee, 4),
            "簽審文號": product["approval"], "TAXRULE": item[8],
        })
        insert(db, "invitem", key, values, notes)
        insert(db, "doc", key, dict(PART=index, PORDERFLD=index, CERTNO=item[1]), notes)
        insert(db, "permitno", key, dict(ITEM=index, PART=1, PERMITNO=product["permit"],
                                        PERMITITEM=product["permit_item"], PORDERFLD=1), notes)
        insert(db, "taxitem", key, dict(ITEM=index, PART=1, TAXCODE="B52", TAXRATE=fee * 100,
                                       TAXRATIO=100, 稅費名稱=TAX_NAME, PORDERFLD=1), notes)
    tonnes = sum(number(item[9]) for item in items)
    insert(db, "sumexp", key, dict(PART=1, EXPQUAN=tonnes, EXPUNIT="TNE"), notes)
    insert(db, "sumstc", key, dict(PART=1, STCQUAN=sum(number(item[11]) for item in items),
                                  STCUNIT="LTR"), notes)
    # TODO(Q2): Keep PAYTYPE at 08 even when an item has STCTYPE 78.
    insert(db, "sumpay", key, dict(PART=1, PAYTYPE="08", PAYAMT=h[25]), notes)
    insert(db, "tax", key, dict(PART=1, TAXNO="B52", TAXAMT=h[31],
                               TAXCODENAME=TAX_NAME, PORDERFLD=1), notes)


def write_declaration(db, h, items, settings, notes):
    key = dict(MSGCODE=MESSAGE, SERSNO=h[1])
    lock = " FOR UPDATE" if db.mysql else ""
    if db.select("SELECT 1 FROM cusdec WHERE MSGCODE = ? AND SERSNO = ?" + lock,
                 (MESSAGE, h[1])):
        raise ValueError("已存在資料")
    products = []
    for item in items:
        code = rules.product_code(item[3], notes)
        if code not in settings.products:
            raise ValueError(f"油品 {code} 不在油品表，請先新增")
        products.append((code, settings.products[code]))
    values = header_values(db, h, items, products, settings, notes)
    insert(db, "cusdec", key, values, notes)
    write_details(db, key, h, items, products, notes)


def transact(header, items, settings, notes, connection):
    for attempt in range(2):
        pending = []
        with closing(Database(settings, connection)) as db:
            db.begin()
            try:
                db.check_engines()
                write_declaration(db, header, items, settings, pending)
                db.commit()
            except BaseException as error:
                db.rollback()
                # Two concurrent MySQL inserts can collide on the same gap lock.
                if db.mysql and attempt == 0 and error.args and error.args[0] in (1205, 1213):
                    continue
                notes.extend(pending)
                raise
        notes.extend(pending)
        return


def convert_file(path, settings, connection=None):
    result = Result()
    try:
        header, items = read_txt(path)
        result.sersno = header[1]
        if len(result.sersno) != 14:
            raise ValueError("報單號碼必須是 14 碼")
        transact(header, items, settings, result.notes, connection)
        result.ok = True
    except Exception as error:
        result.message = str(error)
    return result


def convert_files(paths, settings, connection=None):
    ordered = sorted(map(Path, paths), key=lambda path: (path.name, str(path)))
    return [convert_file(path, settings, connection=connection) for path in ordered]
