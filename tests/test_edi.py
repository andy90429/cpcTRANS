import csv
import os
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from fuelcore.db import Database
from fuelcore.edi import EdiError, SendXmlBuilder, XmlSettings, check_send_xml, simulate_send
from fuelcore.edi.expression import ExpressionError, evaluate
from fuelcore.edi.simulation import day_sequence
from fuelcore.schema import identifier
from fuelcore.settings import Settings, read_config


FIXTURES = Path(__file__).with_name("fixtures") / "edi"
CONFIG = Path(__file__).resolve().parents[1] / "fuelcore.ini.example"
REPORTS = ("TEST00A10E0777", "TEST00A10E0781")


def settings(edi_database="test_edi", xmlset_database="test_xmlset"):
    config = read_config(CONFIG)
    config["edi"] = dict(edi_database=edi_database, xmlset_database=xmlset_database,
                         company_code="00000000", user="USER1")
    return Settings(config, CONFIG.parent)


def load_fixture_tables(connection):
    for path in (FIXTURES / "db").glob("*.csv"):
        table = identifier(path.stem.split(".", 1)[1])
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            columns = next(reader)
            fields = ", ".join(f"{identifier(column)} TEXT" for column in columns)
            connection.execute(f"CREATE TABLE {table} ({fields})")
            markers = ", ".join("?" for _ in columns)
            connection.executemany(f"INSERT INTO {table} VALUES ({markers})", reader)
    connection.commit()


class EdiTest(unittest.TestCase):
    def test_expression_whitelist_and_arithmetic(self):
        context = {"CUSDEC": {"SERSNO": "測試=1", "SATYPE": "1", "SERSDATE": "2026/1/2"}}
        self.assertEqual(evaluate("mid([CusDec.SERSNO],2,2) + '='", context), "試==")
        self.assertEqual(evaluate("iif([CUSDEC.SATYPE]='1',format('f2', 12 / 5), '')", context), "2.40")
        self.assertEqual(evaluate("Switch(1<0, 'a', 2<>0, 'b')", context), "b")
        self.assertEqual(evaluate("format('YYYY-MM-DD', [CUSDEC.SERSDATE])", context), "2026-01-02")
        for expression in ("().__class__", "open('x')", "[CUSDEC.SERSNO].upper()", "iif(1,'a',open('x'))",
                           "1 ** 2", "(1, 2)", "'abc'[0]", "_field('CUSDEC','SERSNO')"):
            with self.subTest(expression=expression), self.assertRaises(ExpressionError):
                evaluate(expression, context)

    def test_send_checks_goods_exporter_transport_and_count(self):
        root = ET.Element("Declaration")
        with self.assertRaisesRegex(EdiError, "GoodsShipment"):
            check_send_xml(root, "TEST", 1)
        goods = ET.SubElement(root, "GoodsShipment")
        with self.assertRaisesRegex(EdiError, "項次"):
            check_send_xml(root, "TEST", 0)
        ET.SubElement(goods, "GovernmentAgencyGoodsItem")
        with self.assertRaisesRegex(EdiError, "Exporter"):
            check_send_xml(root, "TEST", 1)
        ET.SubElement(goods, "Exporter")
        with self.assertRaisesRegex(EdiError, "Consignment"):
            check_send_xml(root, "TEST", 1)
        ET.SubElement(goods, "Consignment")
        check_send_xml(root, "TEST", 1)

    def test_database_loader_without_local_files(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE xmlsetcontent (PSetupCode, PXMLNAME, PLEVELS, PORDER);
            CREATE TABLE tb_TableStartNode (PSetupCode);
            CREATE TABLE tb_TableRelations (PDatabase);
            INSERT INTO xmlsetcontent VALUES ('N5203', 'Declaration', 0, 1);
        """)
        with patch.dict(os.environ, {"FUELCORE_CBS_CONFIG": ""}):
            config = XmlSettings.load(Database(settings(), connection))
        self.assertEqual(config.root.name, "Declaration")

    def test_edi_settings_require_test_database_names(self):
        with self.assertRaisesRegex(ValueError, "test"):
            settings(edi_database="edi4")
        with self.assertRaisesRegex(ValueError, "test"):
            settings(xmlset_database="xmlsetcontent")

    def test_read_only_database_rejects_writes_even_with_injected_connection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE report (status)")
        db = Database(settings(), connection, read_only=True)
        with self.assertRaisesRegex(ValueError, "唯讀"):
            db.execute("INSERT INTO report VALUES ('S')")
        with self.assertRaisesRegex(ValueError, "SELECT"):
            db.select("DELETE FROM report")
        with self.assertRaisesRegex(ValueError, "唯讀"):
            db.commit()
        self.assertEqual(db.select("SELECT * FROM report"), [])

    def test_day_sequence_same_day_and_reset(self):
        today = date(2026, 10, 21)
        row = {"OUTPUTDATE": "2026/10/21 00:00:00", "OUTPUTSEQUENCE": "25"}
        self.assertEqual(day_sequence(row, today), 25)
        self.assertEqual(day_sequence(row, date(2026, 10, 22)), 0)
        self.assertEqual(day_sequence({}, today), 0)


@unittest.skipUnless(os.environ.get("FUELCORE_CBS_CONFIG"), "請設定 FUELCORE_CBS_CONFIG 指向本機 CBS 設定 CSV")
class EdiFixtureTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        load_fixture_tables(self.connection)
        self.settings = settings()
        self.db = Database(self.settings, self.connection)
        self.config = XmlSettings.load(self.db)

    def test_send_xml_exact_bytes(self):
        builder = SendXmlBuilder(self.config, self.db)
        for sersno in REPORTS:
            with self.subTest(sersno=sersno):
                self.assertEqual(builder.xml_bytes(sersno),
                                 (FIXTURES / "expected" / "send" / f"{sersno}.xml").read_bytes())

    def test_missing_cusdec_other_keeps_goods(self):
        self.connection.execute("DELETE FROM cusdec_other")
        root = ET.fromstring(SendXmlBuilder(self.config, self.db).xml_bytes(REPORTS[0]))
        goods = root.find("{*}GoodsShipment")
        self.assertIsNotNone(goods.find("{*}Buyer"))
        self.assertIsNotNone(goods.find("{*}Exporter"))
        self.assertIsNotNone(goods.find("{*}Consignment"))
        self.assertEqual(len(goods.findall("{*}GovernmentAgencyGoodsItem")), 3)

    def test_rejects_ambiguous_child_relation(self):
        self.connection.executescript("""
            CREATE TABLE containerno (SERSNO, MSGCODE);
            CREATE TABLE containerid (SERSNO, MSGCODE);
            INSERT INTO containerno VALUES ('TEST00A10E0777', '5203S'), ('TEST00A10E0777', '5203S');
            INSERT INTO containerid VALUES ('TEST00A10E0777', '5203S');
        """)
        with self.assertRaisesRegex(EdiError, "CONTAINERID.*CONTAINERNO"):
            SendXmlBuilder(self.config, self.db).xml_bytes(REPORTS[0])

    def test_simulation_issues_only_select_and_keeps_sequence_and_status(self):
        self.connection.execute("UPDATE cusdec SET STATUS = 'Y'")
        self.connection.commit()
        original_changes = self.connection.total_changes
        statements = []
        self.connection.set_trace_callback(statements.append)
        now = datetime(2026, 10, 21, 11, 24, 45, 445000)
        with tempfile.TemporaryDirectory() as temp:
            result = simulate_send(self.settings, [*REPORTS, "MISSING"], temp,
                                   now=now, connection=self.connection)
            self.assertEqual((result.planned, result.rejected), (2, 1))
            files = sorted(p.name for p in result.directory.glob("*.xml"))
            self.assertEqual(files, ["N5203.00000000202610210002.xml", "N5203.00000000202610210003.xml"])
            report = (result.directory / "模擬結果.txt").read_text(encoding="utf-8-sig")
            self.assertIn("OutputSequence=1 → 3", report)
            self.assertIn('"User": "USER1"', report)
            self.assertEqual(report.count('"封包號碼": "N5203.20261021112445445"'), 2)
            self.assertIn('"ICTLNO": "N5203.20261021112445445"', report)
            self.assertIn('"STATUS": "S"', report)
            self.assertIn('"ItemCount": 3', report)
            self.assertIn("略過 MISSING", report)
        self.assertTrue(statements)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT ") for sql in statements), statements)
        self.assertEqual(self.connection.total_changes, original_changes)
        self.assertEqual(self.db.select("SELECT DISTINCT STATUS FROM cusdec"), [("Y",)])
        self.assertEqual(self.db.select("SELECT OutputSequence FROM sequence"), [("1",)])

    def test_simulation_rejects_sent_status_without_using_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            result = simulate_send(self.settings, REPORTS, temp, connection=self.connection)
            self.assertEqual((result.planned, result.rejected), (0, 2))
            self.assertEqual(list(result.directory.glob("*.xml")), [])
            report = (result.directory / "模擬結果.txt").read_text(encoding="utf-8-sig")
            self.assertIn("STATUS=S，僅 Y 可規劃送信", report)
            self.assertNotIn("OutputSequence=", report)


if __name__ == "__main__":
    unittest.main()
