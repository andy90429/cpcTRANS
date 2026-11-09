import csv
import os
import shutil
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from fuelcore.db import Database
from fuelcore.edi import EdiError, XmlSettings, convert_reply, import_replies, simulate_receive
from fuelcore.edi.reply import default_value
from fuelcore.schema import identifier
from test_edi import FIXTURES, load_fixture_tables, settings


NOW = datetime(2026, 11, 9, 11, 14, 38)
RECV = FIXTURES / "recv"
EXPECTED = FIXTURES / "expected" / "recv"
IGNORED = {"MainID", "SubID", "PID", "接收日期時間", "訊息參考號碼"}


def prepare_receive_database(connection):
    load_fixture_tables(connection)
    for path in EXPECTED.glob("*.csv"):
        table = identifier(path.stem.split(".", 1)[1])
        with path.open(encoding="utf-8-sig", newline="") as stream:
            columns = next(csv.reader(stream))
        fields = []
        for index, column in enumerate(columns):
            kind = "INTEGER PRIMARY KEY" if index == 0 else "TEXT"
            if index and column in {"MainID", "SubID"}:
                kind = "INTEGER"
            if column == "總毛重":
                kind = "REAL"
            fields.append(f"{identifier(column)} {kind}")
        connection.execute(f"DROP TABLE {table}")
        connection.execute(f"CREATE TABLE {table} ({', '.join(fields)})")
    connection.execute("""
        CREATE TABLE RcvMsgRcds (
            `郵箱號碼`, `報單號碼`, `訊息簡稱`, `接收時間`, `訊息參考號碼`, `傳送方代號`,
            `接收方代號`, `備份檔名`, `檔案位元數`, `User`
        )
    """)
    connection.commit()


class ReplyDefaultsTest(unittest.TestCase):
    def test_supported_defaults(self):
        args = ("N5107.test", NOW, 8)
        self.assertEqual(default_value("GetDateTime()", *args), "2026-11-09 11:14:38")
        self.assertEqual(default_value("XmlFileName()", *args), args[0])
        self.assertEqual(default_value("DaySequence()", *args), "8")
        self.assertEqual(default_value("Format('YYYYMMDD',GetDateTime()) + Format('0000', DaySequence())", *args),
                         "202611090008")
        self.assertEqual(default_value("'測試值'", *args), "測試值")
        self.assertEqual(default_value("2", *args), "2")
        with self.assertRaises(EdiError):
            default_value("open('x') + DaySequence()", *args)


@unittest.skipUnless(os.environ.get("FUELCORE_CBS_CONFIG"), "需要本機 CBS 設定 CSV")
class ReceiveTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        prepare_receive_database(self.connection)
        self.settings = settings()
        self.db = Database(self.settings, self.connection)

    def assert_matches_expected(self):
        def comparable(row):
            return {key: "" if value is None else str(value) for key, value in row.items() if key not in IGNORED}

        for path in sorted(EXPECTED.glob("*.csv")):
            with self.subTest(table=path.name), path.open(encoding="utf-8-sig", newline="") as stream:
                expected = [comparable(row) for row in csv.DictReader(stream)]
                name = identifier(path.stem.split(".", 1)[1])
                actual = [comparable(row) for row in self.db.select_dicts(f"SELECT * FROM {name} ORDER BY 1")]
                self.assertEqual(actual, expected)

    def test_import_matches_expected_and_links_parent_ids(self):
        results = import_replies(self.settings, RECV, now=NOW, connection=self.connection)
        self.assertEqual([(r.status, r.reference) for r in results],
                         [("已匯入", "202611090001"), ("已匯入", "202611090002")])
        self.assert_matches_expected()
        parent = self.db.select("SELECT MainID FROM n5107")[0][0]
        sub_id, main_id = self.db.select("SELECT SubID, MainID FROM n5107_1")[0]
        self.assertEqual(parent, main_id)
        self.assertEqual(self.db.select("SELECT SubID, MainID FROM n5107_11"), [(sub_id, main_id)])
        for table, reference in zip(("n5107", "n5204"), ("202611090001", "202611090002")):
            actual = self.db.select(f"SELECT `訊息參考號碼` FROM {table}")[0][0]
            self.assertRegex(actual, r"^\d{8}\d{4}$")
            self.assertEqual(actual, reference)
        self.assertEqual(self.db.select("SELECT InputSequence, OutputSequence FROM sequence"), [("2", "1")])
        self.assertEqual(self.db.select("SELECT `訊息簡稱`, `訊息參考號碼` FROM RcvMsgRcds ORDER BY rowid"),
                         [("N5107", "202611090001"), ("N5204", "202611090002")])
        row = self.db.select("SELECT STATUS, `UPDATE`, CUSISCODE, CUSISDATE, RECVDATE, RECVMSGCODE, MCTLNO "
                             "FROM cusdec WHERE SERSNO = 'TEST00A10E0781'")
        self.assertEqual(row, [("P", "*", "C1", "2026-10-22 11:27:45", "2026-11-09 11:14:38", "N5204", "202611090002")])
        retried = import_replies(self.settings, RECV, now=NOW, connection=self.connection)
        self.assertEqual([result.status for result in retried], ["略過", "略過"])
        with tempfile.TemporaryDirectory() as temp:
            original = RECV / "N5107.QAM0980U3001"
            lower = Path(temp) / original.name.lower()
            shutil.copyfile(original, lower)
            retried = import_replies(self.settings, lower, now=NOW, connection=self.connection)
            self.assertEqual(retried[0].status, "略過")
        self.assertEqual(self.db.select("SELECT COUNT(*) FROM RcvMsgRcds"), [(2,)])

    def test_malformed_and_partial_write_rollback_then_continue(self):
        self.connection.executescript("""
            CREATE TRIGGER fail_receipt BEFORE INSERT ON RcvMsgRcds
            WHEN NEW.`備份檔名` LIKE '%N5107.001badwrite'
            BEGIN SELECT RAISE(ABORT, '測試寫入失敗'); END;
        """)
        statements = []
        self.connection.set_trace_callback(statements.append)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            malformed = folder / "N5107.000broken"
            malformed.write_bytes(b"<Response>")
            partial = folder / "N5107.001badwrite"
            shutil.copyfile(RECV / "N5107.QAM0980U3001", partial)
            for path in RECV.iterdir():
                shutil.copyfile(path, folder / path.name)
            original = {path: path.read_bytes() for path in folder.iterdir()}
            results = import_replies(self.settings, folder, now=NOW, connection=self.connection)
            self.assertEqual([r.status for r in results], ["失敗", "失敗", "已匯入", "已匯入"])
            self.assertIn("XML", results[0].message)
            self.assertIn("測試寫入失敗", results[1].message)
            self.assertEqual([r.reference for r in results[2:]], ["202611090001", "202611090002"])
            self.assertEqual({path: path.read_bytes() for path in folder.iterdir()}, original)
        self.assertIn("ROLLBACK", statements)
        self.assert_matches_expected()
        self.assertEqual(self.db.select("SELECT COUNT(*) FROM RcvMsgRcds"), [(2,)])

    def test_skip_n5109_and_simulation_only_selects(self):
        statements = []
        self.connection.set_trace_callback(statements.append)
        before = self.connection.total_changes
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            unsupported = folder / "N5109.example"
            unsupported.write_bytes(b"not XML")
            result = import_replies(self.settings, unsupported, now=NOW, connection=self.connection)
            self.assertEqual(result[0].status, "略過")
            self.assertEqual(unsupported.read_bytes(), b"not XML")
            statements.clear()
            report, results = simulate_receive(self.settings, [RECV, unsupported], folder,
                                               now=NOW, connection=self.connection)
            self.assertEqual([r.status for r in results], ["規劃", "略過", "規劃"])
            content = report.read_text(encoding="utf-8-sig")
            for text in ("N5107_11", "N5204", "RcvMsgRcds", "CUSDEC", '"STATUS": "P"', "InputSequence=2", "略過 N5109"):
                self.assertIn(text, content)
        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT ") for statement in statements), statements)
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(self.db.select("SELECT InputSequence FROM sequence"), [("3",)])

    def test_final_sequence_failure_rolls_back_cusdec_and_receive_record(self):
        notice = RECV / "N5107.QAM0980U3001"
        release = RECV / "N5204.QAM0981F7001"
        self.assertEqual(import_replies(self.settings, notice, now=NOW, connection=self.connection)[0].status, "已匯入")
        original = self.db.select("SELECT STATUS, `UPDATE`, MCTLNO FROM cusdec WHERE SERSNO = 'TEST00A10E0781'")
        self.connection.executescript("""
            CREATE TRIGGER fail_sequence BEFORE UPDATE ON sequence
            BEGIN SELECT RAISE(ABORT, '序號更新失敗'); END;
        """)
        failed = import_replies(self.settings, release, now=NOW, connection=self.connection)[0]
        self.assertEqual(failed.status, "失敗")
        self.assertIn("序號更新失敗", failed.message)
        self.assertEqual(self.db.select("SELECT STATUS, `UPDATE`, MCTLNO FROM cusdec WHERE SERSNO = 'TEST00A10E0781'"), original)
        self.assertEqual(self.db.select("SELECT COUNT(*) FROM n5204"), [(0,)])
        self.assertEqual(self.db.select("SELECT COUNT(*) FROM RcvMsgRcds"), [(1,)])
        self.assertEqual(self.db.select("SELECT InputSequence FROM sequence"), [("1",)])
        self.connection.execute("DROP TRIGGER fail_sequence")
        retried = import_replies(self.settings, release, now=NOW, connection=self.connection)[0]
        self.assertEqual((retried.status, retried.reference), ("已匯入", "202611090002"))

    def test_switch_uses_transport_type_and_document_type(self):
        config = XmlSettings.load(self.db, code="N5107")
        path = RECV / "N5107.QAM0980U3001"
        xml = ET.parse(path).getroot()
        xml.find(".//{*}tw_TransportTypeCode").text = "1"
        xml.find(".//{*}TransportContractDocument/{*}TypeCode").text = "714"
        reply = convert_reply(config, ET.tostring(xml), path.name)
        self.assertEqual(reply.fields["分提單號碼"], "NIL")
        self.assertNotIn("託運單主號", reply.fields)
        self.assertEqual(reply.fields["報關箱號"], "Z000")
        with self.assertRaisesRegex(EdiError, "DTD"):
            convert_reply(config, b"<!DOCTYPE Response [<!ENTITY test 'x'>]><Response/>", path.name)
