import ast
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.request import HTTPSHandler

from fuelcore import agent_lookup as agents


PORT = ('<input type="hidden" name="__RequestVerificationToken" value="fake">'
        '<select name="port"><option value="TXG" selected></option></select>')


def movement(name="測試一號\tTEST SHIP 1", **overrides):
    row = dict(visaNo="<a>ATXG000000001</a>", vesselNo="V00001", port="TXG",
               agentName="測試船務代理股份有限公司", etaDt="2026-11-09T23:00:00",
               spWharfCode="TXGX001\t測試一號碼頭", shipName=name, departed="")
    return dict(row, **overrides)


def berth(**overrides):
    return dict(dict(visaNo="ATXG000000001", vesselNo="V00001", port="TXG", agentName="測試船務代理股份有限公司",
                     etaDt="2026/11/09 23:00:00", spWharfCode=None, estWharf1="TXGX001\t測試一號碼頭",
                     vesselCname="測試一號", vesselEname="TEST SHIP 1", spSts="進港作業"), **overrides)


def page(*rows, **options):
    return json.dumps({"result": rows, "count": options.get("count", len(rows))})


class LookupTest(unittest.TestCase):
    def lookup(self, name, *responses, **kwargs):
        session = Mock()
        session.request.side_effect = responses
        with patch.object(agents, "PublicSession", return_value=session):
            result = agents.lookup_ship(name, **kwargs)
        self.assertEqual(session.request.call_count, len(responses))
        return result, session.request.call_args_list

    def test_found_switches_port_and_queries_only_matching_detail(self):
        result, calls = self.lookup(" M/V  test  ship 1 ", PORT.replace("TXG", "APG"), PORT,
                                    page(movement()), '{"callSign":"TEST01"}')
        self.assertEqual(result, agents.ShipResult("found", "TEST SHIP 1", "測試一號", "TEST01",
                                                   "測試船務代理股份有限公司", "測試一號碼頭", "2026-11-09T23:00:00"))
        self.assertEqual(calls[1][1]["form"]["port"], "TXG")
        self.assertEqual(calls[2][1]["payload"]["ShipName"], "TEST SHIP 1")
        self.assertEqual(calls[3][1]["form"]["visaNo"], "ATXG000000001")

    def test_normalizes_prefixes_whitespace_and_chinese_name(self):
        for name, source_name in (("MV TEST SHIP 1", "測試一號\tTEST SHIP 1"),
                                  ("M.V. TEST SHIP 1", "測試一號\tTEST SHIP 1"),
                                  ("測試 一號", "測試一號\tTEST SHIP 1"),
                                  ("M/V測試一號", "測試一號\tTEST SHIP 1"),
                                  ("TEST SHIP 1", "測試一號\tTESTSHIP1")):
            with self.subTest(name=name):
                result, _ = self.lookup(name, PORT, page(movement(source_name)), '{"callSign":"TEST01"}')
                self.assertEqual(result.status, "found")

    def test_partial_match_is_not_found_and_queries_filtered_berth(self):
        result, calls = self.lookup("SHIP 1", PORT, page(movement()), page(berth()))
        self.assertEqual(result.status, "not_found")
        self.assertTrue(calls[2][0][0].endswith(agents.BERTH + "GridSearchData"))
        self.assertEqual(calls[2][1]["payload"]["ShipName"], "SHIP 1")

    def test_same_vessel_latest_visa_across_pages(self):
        old = movement(visaNo="ATXG000000009", etaDt="2026-11-08T23:00:00")
        new = movement(visaNo="ATXG000000002", etaDt="2026-11-09T12:00:00", agentName="新測試代理")
        with patch.object(agents, "PAGE_SIZE", 1):
            result, calls = self.lookup("TEST SHIP 1", PORT, page(old, count=2), page(new, count=2),
                                        '{"callSign":"TEST01"}')
        self.assertEqual(result.agent_full, "新測試代理")
        self.assertEqual([call[1]["payload"]["skip"] for call in calls[1:3]], [0, 1])
        self.assertEqual(calls[3][1]["form"]["visaNo"], "ATXG000000002")

    def test_different_vessels_are_ambiguous_even_with_same_agent(self):
        another = movement(visaNo="ATXG000000002", vesselNo="V00002", spWharfCode="測試二號碼頭")
        result, _ = self.lookup("TEST SHIP 1", PORT, page(movement(), another))
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual([ship.berth for ship in result.candidates], ["測試一號碼頭", "測試二號碼頭"])
        self.assertTrue(all(ship.ship_name and ship.agent_full and ship.eta for ship in result.candidates))

    def test_departed_visas_excluded_from_both_views(self):
        result, _ = self.lookup("TEST SHIP 1", PORT, page(movement(departed="<a>已離港</a>")),
                                page(berth(), berth(visaNo="ATXG000000002", spSts="已出港")))
        self.assertEqual(result.status, "not_found")

    def test_berth_fallback_and_no_call_sign(self):
        for detail in ('{"callSign":null}', '{"vesselEname":"TEST SHIP 1"}'):
            with self.subTest(detail=detail):
                result, calls = self.lookup("TEST SHIP 1", PORT, page(), page(berth()), detail)
                self.assertEqual(result.status, "no_call_sign")
                self.assertEqual(result.agent_full, "測試船務代理股份有限公司")
                self.assertEqual(result.call_sign, "")
                self.assertEqual(calls[2][1]["payload"]["ShipName"], "TEST SHIP 1")

    def test_ignored_shipname_filter_retries_unfiltered_and_matches_locally(self):
        unrelated = movement("TEST SHIP 2", visaNo="ATXG000000002", vesselNo="V00002")
        response = page(movement(), unrelated)
        result, calls = self.lookup("TEST SHIP 1", PORT, response, response, '{"callSign":"TEST01"}')
        self.assertEqual(result.status, "found")
        self.assertEqual([call[1]["payload"]["ShipName"] for call in calls[1:3]], ["TEST SHIP 1", ""])

    def test_berth_ignored_filter_uses_same_fallback(self):
        response = page(berth(vesselEname="TEST SHIP 2", vesselCname="測試二號",
                              visaNo="ATXG000000002", vesselNo="V00002"), berth())
        result, calls = self.lookup("TEST SHIP 1", PORT, page(), response, response, '{"callSign":"TEST01"}')
        self.assertEqual(result.status, "found")
        self.assertEqual([call[1]["payload"]["ShipName"] for call in calls[2:4]], ["TEST SHIP 1", ""])

    def test_invalid_grid_or_detail_raises_format_error(self):
        for responses in ((PORT, '{"count":1,"result":[{"unexpected":true}]}'),
                          (PORT, page(movement()), '{"unexpected":true}')):
            with self.subTest(responses=responses), self.assertRaises(agents.SourceFormatError):
                self.lookup("TEST SHIP 1", *responses)

    def test_network_error_propagates(self):
        with self.assertRaisesRegex(agents.NetworkError, "offline"):
            self.lookup("TEST SHIP 1", PORT, agents.NetworkError("offline"))

    def test_request_retries_timeout(self):
        session = agents.PublicSession()
        session.opener = MagicMock()
        opened = session.opener.open.return_value
        response = opened.__enter__.return_value
        response.read.return_value = b"test"
        response.headers.get_content_charset.return_value = "utf-8"
        session.opener.open.side_effect = [TimeoutError, opened]
        with patch.object(agents.time, "sleep") as delay:
            self.assertEqual(session.request(agents.TPNET), "test")
        self.assertEqual(session.opener.open.call_count, 2)
        self.assertEqual(session.opener.open.call_args[1]["timeout"], 20)
        delay.assert_called_once()

    def test_python37_syntax_and_no_fuelcore_import(self):
        source = Path(agents.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 7))
        self.assertFalse(any(isinstance(node, ast.ImportFrom) and (
            node.level or (node.module or "").startswith("fuelcore")) for node in ast.walk(tree)))
        self.assertFalse(any(isinstance(node, ast.Name) and node.id in ("eval", "exec") for node in ast.walk(tree)))

    def test_agent_cli_reports_found_ship(self):
        from fuelcore.__main__ import main

        output = io.StringIO()
        result = agents.ShipResult("found", "TEST SHIP 1", "測試一號", "TEST01", "測試代理", "一號碼頭", "23:00")
        with patch.object(agents, "lookup_ship", return_value=result) as lookup, redirect_stdout(output):
            with patch("sys.argv", ["fuelcore", "agent", "--name", "TEST SHIP 1"]):
                self.assertEqual(main(), 0)
        lookup.assert_called_once_with("TEST SHIP 1")
        for text in ("found", "TEST01", "測試代理", "一號碼頭", "23:00"):
            self.assertIn(text, output.getvalue())


class MappingTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(folder.cleanup)
        self.mapping = Path(folder.name) / "mapping.csv"
        self.full = "測試船務代理股份有限公司"

    def resolve(self, shorts=()):
        return agents.resolve_short(self.full, shorts, self.mapping)

    def test_mapping_priority_update_and_bom(self):
        agents.save_mapping(self.mapping, self.full, "原簡稱")
        agents.save_mapping(self.mapping, self.full, "新簡稱")
        self.assertTrue(self.mapping.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.mapping.read_text(encoding="utf-8-sig").splitlines(),
                         ["完整名稱,簡稱", self.full + ",新簡稱"])
        self.assertEqual(self.resolve(["測試", "船務"]).short, "新簡稱")

    def test_unique_substring_ambiguous_and_unmatched_shorts(self):
        self.assertEqual(self.resolve(["測試", "無關", "測試"]).short, "測試")
        self.assertEqual(self.resolve(["測試", "船務", "無關"]).candidates, ("測試", "船務"))
        self.assertEqual(self.resolve(["無關"]).status, "needs_selection")
        self.assertEqual(self.resolve(["無關"]).candidates, ("無關",))

    def test_excel_cp950_csv(self):
        self.mapping.write_bytes(("完整名稱,簡稱\r\n" + self.full + ",測試\r\n").encode("cp950"))
        self.assertEqual(self.resolve().short, "測試")
        agents.save_mapping(self.mapping, self.full, "新簡稱")
        self.assertTrue(self.mapping.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_ignores_extra_empty_columns(self):
        self.mapping.write_text("完整名稱,簡稱,\n" + self.full + ",測試,\n", encoding="utf-8-sig")
        self.assertEqual(self.resolve().short, "測試")

    def test_bad_csv_encoding(self):
        self.mapping.write_bytes(b"\xff\xff")
        with self.assertRaisesRegex(ValueError, "代理行對照表編碼無法辨識，請另存為 CSV UTF-8"):
            self.resolve()


@unittest.skipUnless(os.getenv("FUELCORE_ONLINE") == "1", "需設定 FUELCORE_ONLINE=1")
class OnlineTest(unittest.TestCase):
    def test_taichung_one_ship(self):
        name = os.getenv("FUELCORE_ONLINE_SHIP")
        if not name:
            session = agents.PublicSession()
            token = agents._select_port(session, "TXG")
            rows = agents._primary_pages(session, agents.DYNAMIC, "TXG", token, "")
            name = next((agents._names(row, agents.DYNAMIC)[0] for row in rows
                         if not agents._value(agents._visible(agents._text(row["departed"])))
                         and agents._names(row, agents.DYNAMIC)[0]), "")
            self.assertTrue(name, "臺中港清單目前沒有可測試的船")
        requests = []
        original_open = HTTPSHandler.https_open

        def count(handler, request):
            requests.append(request.full_url)
            return original_open(handler, request)

        start = time.monotonic()
        with patch.object(HTTPSHandler, "https_open", count):
            result = agents.lookup_ship(name)
        elapsed = time.monotonic() - start
        print("{}｜呼號 {}｜{}｜{} 次 HTTPS｜{:.2f} 秒".format(
            result.ship_name, result.call_sign, result.agent_full, len(requests), elapsed), flush=True)
        self.assertEqual(result.status, "found")
        self.assertTrue(result.agent_full)
