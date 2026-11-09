import ast
import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fuelcore import agent_lookup as agents


HTML = Path(__file__).with_name("fixtures") / "agent_sdci.html"
SHIPS = [
    agents.ShipAgent("TEST SHIP 1", "TEST01", "9000001", "測試船務代理股份有限公司", "", "", "tpnet2", "測試一號"),
    agents.ShipAgent("TEST SHIP 2", "TEST02", "9000002", "示範海運股份有限公司", "", "", "tpnet2"),
    agents.ShipAgent("TEST SHIP 3", "TEST03", "9000003", "範例船務代理股份有限公司", "", "", "sdci"),
]


class SourceTest(unittest.TestCase):
    def test_request_retries_timeout_with_same_request(self):
        session = agents.PublicSession()
        session.opener = Mock()
        response = session.opener.open.return_value.__enter__ = Mock()
        session.opener.open.return_value.__exit__ = Mock(return_value=False)
        response.return_value.read.return_value = b"test"
        response.return_value.headers.get_content_charset.return_value = "utf-8"
        session.opener.open.side_effect = [TimeoutError, session.opener.open.return_value]
        with patch.object(agents.time, "sleep") as delay:
            self.assertEqual(session.request(agents.TPNET), "test")
        self.assertEqual(session.opener.open.call_count, 2)
        self.assertEqual(session.opener.open.call_args.kwargs["timeout"], 20)
        delay.assert_called_once()

    def test_port_switch_and_primary_pages(self):
        row = dict(visaNo="ATXG000000001", vesselNo="V00001", port="TXG", agentName=SHIPS[0].agent_full,
                   etaDt="2026/11/09 23:00", spWharfCode=None, shipName="測試一號\tTEST SHIP 1", departed="")
        second = dict(row, visaNo="ATXG000000002", vesselNo="V00002", shipName="測試二號\tTEST SHIP 2")
        third = dict(row, visaNo="ATXG000000003", vesselNo="V00003", shipName="TEST SHIP 3", departed="已離港")
        berth = dict(visaNo=row["visaNo"], vesselNo=row["vesselNo"], port="TXG", agentName=row["agentName"],
                     etaDt=row["etaDt"], spWharfCode=None, estWharf1="TXGX001\t1號碼頭",
                     vesselCname="測試一號", vesselEname="TEST SHIP 1")
        session = Mock()
        session.request.side_effect = [
            '<input type="hidden" name="__RequestVerificationToken" value="old">',
            '<input type="hidden" name="__RequestVerificationToken" value="new">'
            '<select name="port"><option value="TXG" selected></option></select>',
            json.dumps({"result": [row, second], "count": 3}),
            json.dumps({"result": [third], "count": 3}),
            json.dumps({"result": [berth], "count": 1}),
        ]
        with patch.object(agents, "PAGE_SIZE", 2):
            rows, token = agents._primary_rows(session, "TXG")
        self.assertEqual(token, "new")
        self.assertEqual(list(rows), [row["visaNo"], second["visaNo"]])
        self.assertEqual(rows[row["visaNo"]][0].ship_name_zh, "測試一號")
        self.assertEqual(rows[row["visaNo"]][0].berth, "1號碼頭")
        self.assertEqual(session.request.call_args_list[1].kwargs["form"]["port"], "TXG")
        self.assertEqual([call.kwargs["payload"]["skip"] for call in session.request.call_args_list[2:]], [0, 2, 0])

    def test_parse_sdci_and_follow_filtered_next_page(self):
        original = HTML.read_text(encoding="utf-8")
        rows, form, index, count = agents._sdci_page(original, "TXG")
        self.assertEqual((index, count, len(rows)), (0, 1, 3))
        self.assertEqual(rows[0][4], SHIPS[0].agent_full)
        self.assertEqual(rows[0][16:19], ["TEST01", "9000001", "V00001"])
        first = original.replace("'pageCount':1", "'pageCount':2")
        second = first.replace("'pageIndex':0", "'pageIndex':1")
        session = Mock()
        session.request.side_effect = [first, second]
        matches = agents._sdci_history(session, original, "TXG", "V00001")
        self.assertEqual(len(matches), 2)
        first_call, next_call = session.request.call_args_list
        self.assertIn("APPLYSEARCHPANELFILTER", first_call.kwargs["form"]["__EVENTARGUMENT"])
        self.assertEqual(next_call.kwargs["form"]["__EVENTARGUMENT"], "12|PAGERONCLICK3|PBN")
        self.assertEqual(form["__VIEWSTATE"], "fake")

    def test_changed_format_is_an_error(self):
        original = HTML.read_text(encoding="utf-8")
        with self.assertRaisesRegex(agents.SourceFormatError, "表格欄位"):
            agents._sdci_page(original.replace("船務代理</td>", "代理</td>"), "TXG")
        session = Mock()
        session.request.return_value = '{"count":1,"result":[{"unexpected":true}]}'
        with self.assertRaisesRegex(agents.SourceFormatError, "清單欄位"):
            agents._primary_pages(session, agents.DYNAMIC, "TXG", "fake")

    def test_python37_syntax_and_no_fuelcore_import(self):
        source = Path(agents.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 7))
        self.assertFalse(any(isinstance(node, ast.ImportFrom) and (
            node.level or (node.module or "").startswith("fuelcore")) for node in ast.walk(tree)))
        self.assertFalse(any(isinstance(node, ast.Name) and node.id in ("eval", "exec") for node in ast.walk(tree)))


class LocalTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(folder.cleanup)
        self.cache = Path(folder.name) / "ships.json"
        self.mapping = Path(folder.name) / "mapping.csv"

    def cache_at(self, age=0, ships=SHIPS):
        stamp = datetime.now(timezone.utc) - timedelta(hours=age)
        self.cache.write_text(json.dumps({"fetched_at": stamp.isoformat(),
                                         "ships": [asdict(ship) for ship in ships]}), encoding="utf-8")
        return stamp

    def lookup(self, **kwargs):
        return agents.lookup(cache_path=self.cache, **kwargs)

    def test_fresh_cache_lookup_order_prefixes_and_no_match(self):
        stamp = self.cache_at()
        with patch.object(agents, "fetch_port_ships") as fetch:
            self.assertEqual(self.lookup(imo="9000001", call_sign="TEST02").matched_by, "imo")
            result = self.lookup(imo="9999999", call_sign=" test 02 ", ship_name="TEST SHIP 1")
            self.assertEqual((result.ship, result.matched_by, result.fetched_at), (SHIPS[1], "call_sign", stamp))
            for name in ("m/v test  ship  1", "MV TEST SHIP 1", "M.V. TEST SHIP 1", "測試一號"):
                self.assertEqual(self.lookup(ship_name=name).ship, SHIPS[0])
            self.assertEqual(self.lookup(ship_name="TEST SHIP").status, "not_found")
            fetch.assert_not_called()

    def test_different_agents_are_ambiguous(self):
        other = replace(SHIPS[0], agent_full="另一家測試代理行")
        self.cache_at(ships=[SHIPS[0], other])
        result = self.lookup(imo="9000001", call_sign="TEST01")
        self.assertEqual((result.status, result.ship, result.matched_by), ("ambiguous", None, "imo"))
        self.assertEqual(result.candidates, (SHIPS[0], other))

    def test_expired_and_forced_refresh(self):
        stamp = self.cache_at(age=7)
        with patch.object(agents, "fetch_port_ships", return_value=SHIPS) as fetch:
            self.assertGreater(self.lookup(imo="9000001").fetched_at, stamp)
            self.lookup(imo="9000001", refresh=True)
            self.assertEqual(fetch.call_count, 2)

    def test_network_failure_keeps_old_cache_but_format_error_is_visible(self):
        stamp = self.cache_at(age=7)
        before = self.cache.read_bytes()
        with patch.object(agents, "fetch_port_ships", side_effect=agents.NetworkError("offline")):
            result = self.lookup(imo="9000001")
        self.assertEqual((result.ship, result.fetched_at, result.stale, result.warning),
                         (SHIPS[0], stamp, True, "offline"))
        self.assertEqual(before, self.cache.read_bytes())
        with patch.object(agents, "fetch_port_ships", side_effect=agents.SourceFormatError("changed")):
            with self.assertRaises(agents.SourceFormatError):
                self.lookup(imo="9000001")

    def test_mapping_priority_update_and_bom(self):
        full = "測試船務代理股份有限公司"
        agents.save_mapping(self.mapping, full, "原簡稱")
        agents.save_mapping(self.mapping, full, "新簡稱")
        self.assertTrue(self.mapping.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.mapping.read_text(encoding="utf-8-sig").splitlines(),
                         ["完整名稱,簡稱", full + ",新簡稱"])
        result = agents.resolve_short(full, ["測試", "船務"], self.mapping)
        self.assertEqual((result.status, result.short), ("mapped", "新簡稱"))

    def test_unique_substring_ambiguous_and_unmatched_shorts(self):
        resolve = lambda shorts: agents.resolve_short("測試船務代理股份有限公司", shorts, self.mapping)
        self.assertEqual(resolve(["測試", "無關", "測試"]).short, "測試")
        self.assertEqual(resolve(["測試", "船務", "無關"]).candidates, ("測試", "船務"))
        self.assertEqual(resolve(["無關"]).status, "needs_selection")
        self.assertEqual(resolve(["無關"]).candidates, ("無關",))


@unittest.skipUnless(os.getenv("FUELCORE_ONLINE") == "1", "需設定 FUELCORE_ONLINE=1")
class OnlineTest(unittest.TestCase):
    def test_taichung_public_list(self):
        ships = agents.fetch_port_ships()
        print("臺中港 {} 筆".format(len(ships)), flush=True)
        self.assertTrue(ships)
        self.assertTrue(any(ship.agent_full for ship in ships))
