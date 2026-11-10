import configparser
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock

from fuelcore import Settings, convert_file, convert_files
from fuelcore import rules
from fuelcore.generate import generate
from fuelcore.convert import read_txt
from fuelcore.db import Database
from fuelcore.schema import CODE_TABLES, init_sqlite, read_schema, statements


EXAMPLE = Path(__file__).resolve().parents[1] / "fuelcore.ini.example"
HEADER_CONSTANTS = {
    "MSGCODE": "5203S", "SERSTYPE": "G5", "PORTCODE": "TWTXG", "PORTNAME": "TAICHUNG ,TAIWAN",
    "DESTCODE": "TWZ99", "DESTNAME": "SHIP USE", "MSGNO": "22", "TAXCHECK": "N", "PKGUNIT": "TNE",
    "MAWBNO": "NIL", "MARKS": "N/M", "EDIMARKS": "N/M", "TRAN": "13", "賣方編號": "03707901",
    "SLBAN": "03707901", "SLCODE": "58", "SLNAME": "CPC CORPORATION. TAIWAN",
    "SLADDR": "No.2 Zuo Nan Road,Nannan Distrist, Nanzi District,Kaohsiung,TAIWAN,R.O.C.",
    "SLCNAME": "台灣中油股份有限公司", "SLCADDR": "高雄市楠梓區宏南里左楠路2號",
    "客戶簡稱": "CPC CORPORATION. TAI", "TERM": "FOB", "MONEY": "USD", "SATYPE": "1",
    "IETYPE": "2", "是否已計算": 1, "是否已邏輯": 0, "STATUS": "0", "原狀態": "0",
    "UPDATE": "9", "不印日報": "N", "IS_PERMITNO": "Y", "金額小數位": 2,
}
ITEM_CONSTANTS = {
    "MSGCODE": "5203S", "帶值": "*", "數量單位": "TNE", "商標": "NO BRAND", "GOODBRAND": "NIL",
    "CRTNO": "NIL", "QuantityRate": 1, "統計單位": "LTR", "RATIO_DUTY": 100, "RATIO_RATE": 100,
    "RATIO_AMT": 100, "RATIO_GOOD": 100, "IS_DUTYAMT_BYINPUT": "N", "IS_GOODRATE_BYINPUT": "N",
    "免計營業稅": "N", "換算淨重": "N", "UNITCURR": "USD", "UNITTERM": "FOB",
}
TAX_NAME = "出口推廣貿易服務費"


class RulesTest(unittest.TestCase):
    def test_5_1_names_and_voyage(self):
        notes = []
        self.assertEqual(rules.clean_name(" D. &AMP; E. &  CO.,LTD ", "BYNAME", notes),
                         "D. E. CO.,LTD")
        self.assertEqual(rules.clean_name("ATLAS", "BANAME", notes), "ATLAS")
        self.assertEqual(rules.voyage(" ", notes), "NIL")
        self.assertEqual(rules.voyage("001", notes), "001")
        self.assertEqual(len(notes), 2)

    def test_5_2_buyer_code(self):
        self.assertEqual(rules.buyer_code("ALPHA BUNKER TRADING CO.,LTD."), "AABRTG")
        self.assertEqual(rules.buyer_code("OCEAN STAR MARINE SERVICES CO.,LTD."), "ONSRME")
        self.assertEqual(rules.buyer_code("D. E. CO.,LTD"), "DECOLD")
        self.assertEqual(rules.buyer_code("A1B"), "AB")

    def test_5_4_country(self):
        notes = []
        settings = Settings.load(EXAMPLE)
        self.assertEqual(rules.country("AABRTG", settings.countries, notes), ("JP", "日本"))
        self.assertEqual(rules.country("ONSRME", settings.countries, notes), ("HK", "香港"))
        self.assertEqual(rules.country("OTHER", settings.countries, notes), ("ZZ", "OTHER"))
        self.assertEqual(len(notes), 2)

    def test_5_5_products(self):
        notes = []
        self.assertEqual(rules.product_code("113F 61181003", notes), "113-F61181-003")
        self.assertEqual(rules.product_code("113F 51007000", notes), "113-F51007-000")
        self.assertEqual(rules.product_code("bad code", notes), "bad code")
        self.assertEqual(len(notes), 3)

    def test_5_6_other(self):
        other = "一\r\n\r\n二\r三\n四"
        self.assertEqual(rules.other_text(other), (other, "一  二 三 四"))
        self.assertIn("\n\n本批", Settings.load(EXAMPLE).other)

    def test_5_7_reference(self):
        date, roc = rules.declaration_date("115/10/29")
        self.assertEqual(date, "2026-10-29 00:00:00")
        self.assertEqual(rules.reference_number(roc, "TEST00A10E0799"), "E11510290799")

    def test_test_only_and_mysql_schema(self):
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(EXAMPLE, encoding="utf-8")
        cfg["database"].update(driver="mysql", host="test.invalid", user="tester",
                               password="", database="production", code_database="test_codes")
        with self.assertRaises(ValueError):
            Settings(cfg, EXAMPLE.parent)
        cfg["database"]["database"] = "test_declarations"
        cfg["database"]["test_only"] = "no"
        with self.assertRaises(ValueError):
            Settings(cfg, EXAMPLE.parent)
        cfg["database"]["test_only"] = "yes"
        sql = "\n".join(statements(Settings(cfg, EXAMPLE.parent)))
        self.assertIn("USE `test_declarations`", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `test_codes`.`mybuyer`", sql)
        self.assertIn("`PID` int(11) NOT NULL AUTO_INCREMENT", sql)
        self.assertEqual(sql.count("ENGINE=InnoDB"), len(read_schema()) + len(CODE_TABLES))

    def test_innodb_error_lists_each_invalid_table(self):
        settings = Mock(driver="mysql", database="test_declarations", code_database="test_codes")
        db = Database(settings, connection=Mock())
        tables = [(settings.database, name) for name in read_schema()]
        tables.extend((settings.code_database, name) for name in CODE_TABLES)
        db.select = Mock(return_value=[
            (schema, name, "MyISAM" if name in ("cusdec", "mybuyer") else "InnoDB")
            for schema, name in tables if name != "tax"
        ])
        with self.assertRaises(ValueError) as caught:
            db.check_engines()
        self.assertIn("test_declarations.cusdec (MyISAM)", str(caught.exception))
        self.assertIn("test_codes.mybuyer (MyISAM)", str(caught.exception))
        self.assertIn("test_declarations.tax (查無引擎)", str(caught.exception))
        self.assertNotIn("test_declarations.invitem", str(caught.exception))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.settings = Settings.load(EXAMPLE)
        self.settings.path = self.folder / "test.sqlite3"
        init_sqlite(self.settings)

    def query(self, sql, params=()):
        with closing(sqlite3.connect(str(self.settings.path))) as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(str(self.settings.path))) as db, db:
            db.execute(sql, params)

    def test_sqlite_declares_cbs_types_without_changing_stored_text(self):
        declared = {row["name"]: row["type"] for row in self.query("PRAGMA table_info(invitem)")}
        self.assertEqual(declared["數量"], "TEXT DOUBLE(15,4)")
        self.assertEqual(declared["報單淨重"], "TEXT DECIMAL(16,6)")
        self.assertEqual(declared["項次號碼"], "INTEGER")
        header = {row["name"]: row["type"] for row in self.query("PRAGMA table_info(cusdec)")}
        self.assertEqual(header["SERSDATE"], "TEXT DATETIME")
        self.assertTrue(convert_file(self.sample(), self.settings).ok)
        row, = self.query("SELECT 數量, typeof(數量) AS kind FROM invitem")
        self.assertEqual((row["數量"], row["kind"]), ("33.5000", "text"))

    def sample(self, suffix="0001", **changes):
        h = [""] * 38
        i = [""] * 17
        for index, value in {0: "X0000", 1: "TEST00A10E" + suffix, 4: "TXG00F0W", 6: "0.0004",
                             11: "115/10/29", 13: "33500", 15: "33.5", 17: "36000",
                             20: "OCEAN PIONEER", 22: "9292182", 23: "DD9000001",
                             24: "ALPHA BUNKER TRADING CO.,LTD.", 25: "11218874", 26: "30000.99",
                             27: " zz 1 ", 28: "31.43750", 29: "7", 31: "4488"}.items():
            h[index] = value
        for index, value in {1: "13D5363E 464077", 2: "27101949009", 3: "113F 61181003",
                             4: "Low Sulfur Marine Residual Fuels MF-180(0.5%)", 6: "0",
                             7: "33500", 8: "473,,,,,,,,,T,,,,,,,,,", 9: "33.5",
                             11: "36000", 12: "11218874", 13: "30000.99", 14: "900.25", 15: "78"}.items():
            i[index] = value
        for key, value in changes.items():
            (h if key[0] == "h" else i)[int(key[1:])] = value
        path = self.folder / (suffix + ".txt")
        path.write_bytes(("\r\n" + "|".join(h) + "\r\n\r\n" + "|".join(i) + "\r\n").encode("utf-8-sig"))
        return path

    def assert_fields(self, row, expected):
        for key, value in expected.items():
            actual = row[key]
            if isinstance(value, (int, D)):
                self.assertEqual(D(actual), D(value), key)
            else:
                self.assertEqual(actual, value, key)

    def convert(self, path):
        result = convert_file(path, self.settings)
        self.assertTrue(result.ok, result.message)
        return result

    def test_5_2_buyer_insert_fill_conflict_and_disable(self):
        self.convert(self.sample())
        self.assertEqual(self.query("SELECT BY015 FROM mybuyer")[0][0], "AABRTG")
        for marker in ("", ".", "-", "NIL"):
            with self.subTest(marker=marker):
                self.execute("UPDATE mybuyer SET BY015 = ?", (marker,))
                suffix = str(len(self.query("SELECT 1 FROM cusdec")) + 1).zfill(4)
                self.convert(self.sample(suffix))
                self.assertEqual(self.query("SELECT BY015 FROM mybuyer")[0][0], "AABRTG")
        self.execute("UPDATE mybuyer SET BY015 = ?", ("OLD",))
        result = self.convert(self.sample("0100"))
        self.assertTrue(any("OLD" in note for note in result.notes))
        self.assertEqual(self.query("SELECT BY015 FROM mybuyer")[0][0], "OLD")
        self.settings.write_codes = False
        self.convert(self.sample("0101", h23="DD9000002", h27="ZZ2"))
        self.assertEqual(len(self.query("SELECT 1 FROM mybuyer")), 1)
        self.assertEqual(len(self.query("SELECT 1 FROM myshipname")), 1)

    def test_5_3_ship_insert_imo_and_name_fallback(self):
        result = self.convert(self.sample())
        self.assertTrue(any("已確認寫入" in note and "重開" in note for note in result.notes))
        self.convert(self.sample("0002", h27="", h22=""))
        self.convert(self.sample("0003", h27="", h20="another vessel"))
        self.assertEqual([row[0] for row in self.query("SELECT BAID FROM cusdec ORDER BY SERSNO")],
                         ["ZZ1", "ZZ1", "ZZ1"])

    def test_5_3_ship_ambiguous_missing_and_too_long(self):
        self.execute("INSERT INTO myshipname (SN002,SN003,SN004) VALUES (?,?,?)",
                     ("ZZ2", "OTHER", "9292182"))
        self.convert(self.sample())
        result = self.convert(self.sample("0002", h20="UNKNOWN", h27=""))
        self.assertIn("請手動填呼號", result.notes)
        self.assertEqual(self.query("SELECT BAID FROM cusdec WHERE SERSNO LIKE '%0002'")[0][0], "")
        result = self.convert(self.sample("0003", h27="ZZ123456789"))
        self.assertTrue(any("超過 10" in note for note in result.notes))
        self.assertEqual(len(self.query("SELECT 1 FROM myshipname")), 2)

    def test_5_3_ship_fill_imo_name_and_warn_mismatch(self):
        self.execute("INSERT INTO myshipname (SN002) VALUES (?)", (" zz1 ",))
        result = self.convert(self.sample())
        self.assertTrue(any("補船名" in note and "重開" in note for note in result.notes))
        row = self.query("SELECT SN003, SN004 FROM myshipname")[0]
        self.assertEqual(tuple(row), ("OCEAN PIONEER", "9292182"))
        result = self.convert(self.sample("0002", h22="9999999"))
        self.assertTrue(any("9292182" in note and "9999999" in note for note in result.notes))
        self.assertEqual(self.query("SELECT SN004 FROM myshipname")[0][0], "9292182")

    def test_5_3_ship_failure_isolated(self):
        self.execute("CREATE TRIGGER reject_ship BEFORE INSERT ON myshipname "
                     "BEGIN SELECT RAISE(ABORT, 'test ship failure'); END")
        result = self.convert(self.sample())
        self.assertTrue(any("test ship failure" in note for note in result.notes))
        self.assertEqual(len(self.query("SELECT 1 FROM cusdec")), 1)
        self.assertEqual(len(self.query("SELECT 1 FROM myshipname")), 0)

    def test_duplicate_leaves_all_tables_unchanged(self):
        self.convert(self.sample())
        result = convert_file(self.sample(h23="DD9000002", h27="ZZ2"), self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "已存在資料")
        self.assertEqual(len(self.query("SELECT 1 FROM mybuyer")), 1)
        self.assertEqual(len(self.query("SELECT 1 FROM myshipname")), 1)

    def test_failure_in_last_table_rolls_back_every_write(self):
        self.execute("CREATE TRIGGER reject_tax BEFORE INSERT ON tax "
                     "BEGIN SELECT RAISE(ABORT, 'test tax failure'); END")
        result = convert_file(self.sample(), self.settings)
        self.assertFalse(result.ok)
        self.assertIn("test tax failure", result.message)
        for table in ("cusdec", "invitem", "doc", "permitno", "sumexp", "sumstc",
                      "sumpay", "tax", "taxitem", "mybuyer", "myshipname"):
            self.assertEqual(self.query("SELECT 1 FROM " + table), [], table)

    def test_unknown_product_does_not_stop_batch(self):
        results = convert_files([self.sample("0002"), self.sample(i3="999F 99999999")], self.settings)
        self.assertEqual([result.ok for result in results], [False, True])
        self.assertIn("油品 999-F99999-999 不在油品表，請先新增", results[0].message)

    def test_q1_through_q5_and_field_scales(self):
        result = self.convert(self.sample())
        h = self.query("SELECT * FROM cusdec")[0]
        i = self.query("SELECT * FROM invitem")[0]
        self.assert_fields(h, {"ITEMCODE": "27101949009", "PKGTOTAL": 34, "FOREX": "31.43800",
                               "FOBPRICE": "30000.99", "文件名稱": None, "CUSBOXNO": "Z000",
                               "REGISTRYID": "00A10E", "SHIPPINGID": "0001"})
        self.assert_fields(i, {"稅則編號": "27101941007", "數量": D("33.5"), "STCTYPE": "78",
                               "B52_項次稅費": "4487.54960", "報單品名":
                               "113-F61181-003\nLOW SULFUR MARINE RESIDUAL FUELS\nMF-180(0.5%)"})
        self.assertEqual(self.query("SELECT PAYTYPE FROM sumpay")[0][0], "08")
        self.assertEqual(D(self.query("SELECT EXPQUAN FROM sumexp")[0][0]), D("33.5"))
        self.assertTrue(any("33.5 → 34" in note for note in result.notes))

    def test_half_tonne_keeps_fractions_except_package_total(self):
        self.convert(self.sample(h13="500", h15="0.5", h17="540", h26="450.25",
                                 i7="500", i9="0.5", i11="540", i13="450.25"))
        h = self.query("SELECT PKGTOTAL, FOBPRICE FROM cusdec")[0]
        self.assert_fields(h, {"PKGTOTAL": 1, "FOBPRICE": "450.25"})
        self.assertEqual(self.query("SELECT EXPQUAN FROM sumexp")[0][0], "0.50000")

    def test_external_sqlite_connection_is_reused_and_left_open(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(";\n".join(statements(self.settings)) + ";")
            self.settings.path = self.folder / "does-not-exist.sqlite3"
            first = convert_file(self.sample(), self.settings, connection=db)
            self.assertTrue(first.ok, first.message)
            batch = convert_files([self.sample("0003"), self.sample("0002"), self.sample()],
                                  self.settings, connection=db)
            self.assertEqual([result.ok for result in batch], [False, True, True])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cusdec").fetchone(), (3,))
            self.assertFalse(db.in_transaction)
            self.assertFalse(self.settings.path.exists())

    def test_external_sqlite_transaction_is_left_intact(self):
        with closing(sqlite3.connect(str(self.settings.path))) as db:
            db.execute("INSERT INTO mybuyer (BY002) VALUES (?)", ("pending",))
            result = convert_file(self.sample(), self.settings, connection=db)
            self.assertFalse(result.ok)
            self.assertIn("已有進行中的交易", result.message)
            self.assertTrue(db.in_transaction)
            self.assertEqual(db.execute("SELECT BY002 FROM mybuyer").fetchone(), ("pending",))

    def sample_paths(self):
        folder = os.environ.get("FUELCORE_SAMPLES")
        if not folder:
            self.skipTest("請設定 FUELCORE_SAMPLES 指向外部版的 txt樣本")
        paths = sorted(Path(folder).rglob("*.txt"))
        self.assertEqual(len(paths), 50)
        return paths

    def test_50_samples_constants_and_totals(self):
        paths = self.sample_paths()
        results = convert_files(paths, self.settings)
        self.assertEqual([(r.sersno, r.message) for r in results if not r.ok], [])
        for path in paths:
            h, items = read_txt(path)
            with self.subTest(file=path.name):
                def rows(table):
                    return self.query("SELECT * FROM " + table + " WHERE SERSNO = ?", (h[1],))
                header, = rows("cusdec")
                self.assert_fields(header, HEADER_CONSTANTS)
                self.assert_fields(header, {"CUSBOXNO": "Z000", "CUSMANNO": h[0], "鍵檔人員": "USER1",
                                            "CreateUserName": "USER1", "ModifyUserName": "USER1",
                                            "LastModifyUser": "USER1"})
                item_rows = sorted(rows("invitem"), key=lambda row: row["PINVORDER"])
                self.assertEqual(len(item_rows), len(items))
                for expected, row in zip(items, item_rows):
                    self.assert_fields(row, ITEM_CONSTANTS)
                    self.assert_fields(row, {"報單淨重": D(expected[7]), "數量": D(expected[9]),
                                             "統計數量": D(expected[11]), "DUTYAMT": D(expected[12]),
                                             "小計金額": D(expected[13]), "單價": D(expected[14]),
                                             "UNITAMT": D(expected[14]), "FOBUNITAMT": D(expected[14]),
                                             "FOBPRICE": D(expected[13]), "TAXRULE": expected[8],
                                             "B52_項次稅費": rules.rounded(D(expected[12]) * D(h[6]), 4)})
                for column, source in (("GWEIGHT", "報單淨重"), ("NWEIGHT", "報單淨重"),
                                       ("TOT_NETWT", "報單淨重"), ("TOT_AMOUNT", "小計金額"),
                                       ("TOTALAMT", "小計金額"), ("TWDPRICE", "DUTYAMT"),
                                       ("TOT_DUTYAMT", "DUTYAMT")):
                    self.assertEqual(D(header[column]), sum(D(row[source]) for row in item_rows), column)
                tonnes = sum(D(item[9]) for item in items)
                self.assert_fields(header, {"PKGTOTAL": rules.rounded(D(h[15]), 0),
                                            "FOBPRICE": D(h[26])})
                self.assert_fields(rows("sumexp")[0], {"MSGCODE": "5203S", "PART": 1, "EXPUNIT": "TNE",
                                                      "EXPQUAN": tonnes})
                self.assert_fields(rows("sumstc")[0], {"MSGCODE": "5203S", "PART": 1, "STCUNIT": "LTR",
                                                      "STCQUAN": sum(D(row["統計數量"]) for row in item_rows)})
                self.assert_fields(rows("sumpay")[0], {"MSGCODE": "5203S", "PART": 1, "PAYTYPE": "08",
                                                      "PAYAMT": D(h[25])})
                self.assert_fields(rows("tax")[0], {"MSGCODE": "5203S", "PART": 1, "TAXNO": "B52",
                                                   "TAXCODENAME": TAX_NAME, "PORDERFLD": 1, "TAXAMT": D(h[31])})
                for table in ("doc", "permitno", "taxitem"):
                    self.assertEqual(len(rows(table)), len(items))
                for index, row in enumerate(sorted(rows("doc"), key=lambda r: r["PART"]), 1):
                    self.assert_fields(row, {"MSGCODE": "5203S", "PART": index, "PORDERFLD": index,
                                             "CERTNO": items[index - 1][1]})
                for index, row in enumerate(sorted(rows("permitno"), key=lambda r: r["ITEM"]), 1):
                    self.assert_fields(row, {"MSGCODE": "5203S", "ITEM": index, "PART": 1, "PORDERFLD": 1,
                                             "PERMITNO": "PERMIT0000001", "PERMITITEM": "001"})
                for index, row in enumerate(sorted(rows("taxitem"), key=lambda r: r["ITEM"]), 1):
                    self.assert_fields(row, {"MSGCODE": "5203S", "ITEM": index, "PART": 1, "PORDERFLD": 1,
                                             "TAXCODE": "B52", "TAXRATIO": 100, "稅費名稱": TAX_NAME,
                                             "TAXRATE": D(h[6]) * 100})

    def test_generator_required_cases_and_arithmetic(self):
        self.sample_paths()
        paths = generate(os.environ["FUELCORE_SAMPLES"], self.folder / "generated", seed=55)
        records = [read_txt(path) for path in paths]
        for h, items in records:
            self.assertRegex(h[1], r"^TEST\d{10}$")
            for item in items:
                self.assertEqual(D(item[12]), rules.rounded(D(item[13]) * D(h[28]), 0))
                self.assertEqual(D(item[13]), rules.rounded(D(item[9]) * D(item[14]), 2))
            for source, target in ((7, 13), (9, 15), (11, 17), (12, 25), (13, 26)):
                self.assertEqual(D(h[target]), sum(D(item[source]) for item in items))
            self.assertEqual(D(h[25]), rules.rounded(D(h[26]) * D(h[28]), 0))
            self.assertEqual(D(h[31]), rules.rounded(D(h[25]) * D(h[6]), 0))
        headers = [h for h, _ in records]
        self.assertTrue({10, 11, 20, 21}.issubset({int(h[11][-2:]) for h in headers}))
        self.assertTrue(any(int(h[11][-2:]) >= 28 for h in headers))
        self.assertTrue(any(not h[27] and not h[20] and h[22] for h in headers))
        self.assertTrue(any(not h[27] and not h[22] and h[20] for h in headers))
        self.assertTrue(any(len(h[20]) > 40 for h in headers))
        self.assertTrue(any(" & " in h[20] for h in headers))
        self.assertTrue(any("&AMP;" in h[20] for h in headers))
        self.assertTrue(any(not h[21] for h in headers))
        self.assertTrue(any(len({item[3] for item in items}) == 2 for _, items in records))
        self.assertTrue(any(D(h[15]) == 0 for h in headers))
        self.assertTrue(any(D(h[15]) >= 50000 for h in headers))
        self.assertTrue(any(len(h[15].split(".")[-1]) > 5 for h in headers))
        self.assertEqual([(r.sersno, r.message) for r in convert_files(paths, self.settings) if not r.ok], [])
