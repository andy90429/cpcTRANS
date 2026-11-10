import json
import os
import sqlite3
import ssl
import sys
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from fuelcore import Settings, convert_file
from fuelcore.__main__ import main
from fuelcore.declare_rate import (
    CURRENT, HISTORY, Period, RateDownloadError, RateError, download_rate,
    parse_current, parse_history, plan_rates, report_text, save_rates,
)
from fuelcore.schema import init_sqlite
import test_fuelcore as transfer_tests


def current(**changes):
    document = dict(start="20261101", end="20261110", items=[
        dict(code="USD", buyValue="32.455", sellValue="32.555"),
        dict(code="JPY", buyValue="0.22", sellValue="0.24"),
        dict(code="EUR", buyValue="36.123456", sellValue="36.35"),
    ])
    document.update(changes)
    return json.dumps(document).encode("utf-8")


HISTORY_DATA = b"ZAR1151110020010000192100\r\nUSD1151030325550003245500\r\n"


@pytest.fixture
def db_settings(tmp_path):
    settings = Settings.load(transfer_tests.EXAMPLE)
    settings.path = tmp_path / "fuelcore_test.sqlite3"
    init_sqlite(settings)
    return settings


def seed(settings, values):
    with closing(sqlite3.connect(settings.path)) as db, db:
        db.executemany("INSERT INTO mydeclarerate (DR002,DR003,DR004,DR005,DR006) VALUES (?,?,?,?,?)", values)


def rows(settings):
    with closing(sqlite3.connect(settings.path)) as db:
        return db.execute("SELECT DR001,DR002,DR005,DR006 FROM mydeclarerate ORDER BY DR002,DR003").fetchall()


def fake_download(name):
    return {CURRENT: current(), HISTORY: HISTORY_DATA}[name]


def test_parse_current_and_history_layout():
    period, rates = parse_current(current())
    assert period == Period(115, 11, 1)
    assert rates[0].buy == Decimal("36.12346")
    assert parse_current(current(start="20240221", end="20240229"))[0] == Period(113, 2, 3)
    zar = parse_history(HISTORY_DATA)[period, "ZAR"]
    assert (zar.buy, zar.sell) == (Decimal("1.92100"), Decimal("2.00100"))


@pytest.mark.parametrize("data", [
    current(start="20261102"), current(end="20261111"),
    current(items=[dict(code="EUR", buyValue="1", sellValue="2")]),
    current(items=[dict(code="USD", buyValue="1", sellValue="2"), dict(code="jpy", buyValue="1", sellValue="2")]),
    current(items=[dict(code="USD", buyValue="0", sellValue="2")]),
    current(items=[dict(code="USD", buyValue="1", sellValue="NaN")]),
    current(items=[dict(code="USD", buyValue="1", sellValue=True)]),
])
def test_invalid_current_rejects_entire_document(data):
    with pytest.raises(RateError):
        parse_current(data)


@pytest.mark.parametrize("data", [HISTORY_DATA + b"bad\n", HISTORY_DATA.splitlines()[0],
                                  HISTORY_DATA.replace(b"00192100", b"00000000")])
def test_invalid_history_rejects_entire_document(data):
    with pytest.raises(RateError):
        parse_history(data)


def test_plan_actions_and_history_is_read_only(db_settings):
    seed(db_settings, [
        ("USD", "115/11", 1, "32.455000", "32.555"),
        ("JPY", "115/11", 1, "0.21000", "0.24"),
        ("USD", "115/10", 3, "31.00", "32.555"),
        ("USD", "115/07", 3, "30", "31"),  # older than nine completed periods
    ])
    before = db_settings.path.read_bytes()
    with closing(sqlite3.connect(db_settings.path)) as db:
        assert [row[2] for row in db.execute("PRAGMA table_info(mydeclarerate)")][4:6] == [
            "TEXT DECIMAL(15,5)", "TEXT DECIMAL(15,5)"]
        statements = []
        db.set_trace_callback(statements.append)
        plan = plan_rates(db_settings, downloader=fake_download, connection=db)
        assert statements and all(sql.startswith("SELECT ") for sql in statements)
    assert db_settings.path.read_bytes() == before
    assert {change.rate.code: change.action for change in plan.changes} == {"EUR": "新增", "JPY": "更新", "USD": "相同"}
    assert len(plan.history_differences) == 1
    assert "最近 9 旬歷史比對不一致 1 筆" in report_text(plan)


def test_download_fallback_and_local_files(db_settings, tmp_path):
    def offline_history(name):
        if name == HISTORY:
            raise RateDownloadError("連不到")
        return current()

    assert "僅比對本旬" in plan_rates(db_settings, downloader=offline_history).history_note
    file = tmp_path / "fake-current.json"
    file.write_bytes(current())
    downloader = Mock(side_effect=AssertionError("network"))
    assert "未提供歷史" in plan_rates(db_settings, files=[file], downloader=downloader).history_note
    downloader.assert_not_called()


def test_downloader_keeps_certificate_checks_and_rejects_redirects():
    context = ssl.create_default_context()
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    context.verify_flags |= strict
    before = context.verify_flags
    opener = Mock()
    opener.open.side_effect = OSError
    with patch("fuelcore.declare_rate.ssl.create_default_context", return_value=context), patch(
        "fuelcore.declare_rate.build_opener", return_value=opener
    ) as create:
        with pytest.raises(RateDownloadError, match="連不到"):
            download_rate(CURRENT)
    assert context.verify_flags == before & ~strict
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    proxy, _, redirects = create.call_args[0]
    assert proxy.proxies == {}
    assert redirects.redirect_request(None, None, 302, "", {}, "https://invalid.example") is None
    opener.open.assert_called_once()
    assert opener.open.call_args[0][0].startswith("https://portal.sw.nat.gov.tw/")
    assert opener.open.call_args[1]["timeout"] == 20


def test_import_reads_back_and_preserves_identical_row(db_settings):
    seed(db_settings, [("USD", "115/11", 1, "32.455", "32.555"), ("JPY", "115/11", 1, "0.21", "0.24")])
    previous = {code: key for key, code, *_ in rows(db_settings)}
    plan = plan_rates(db_settings, downloader=fake_download)
    assert save_rates(db_settings, plan) == 2
    actual = {code: (key, buy, sell) for key, code, buy, sell in rows(db_settings)}
    assert actual["USD"][0] == previous["USD"]
    assert actual["JPY"][0] != previous["JPY"]
    assert actual["EUR"][1:] == ("36.12346", "36.35000")
    assert all(change.action == "相同" for change in plan_rates(db_settings, downloader=fake_download).changes)


@pytest.mark.parametrize("trigger,error", [
    ("CREATE TRIGGER bad BEFORE INSERT ON mydeclarerate WHEN new.DR002='JPY' "
     "BEGIN SELECT RAISE(ABORT,'fixture'); END", sqlite3.IntegrityError),
    ("CREATE TRIGGER bad AFTER INSERT ON mydeclarerate WHEN new.DR002='JPY' "
     "BEGIN UPDATE mydeclarerate SET DR005='9' WHERE DR001=new.DR001; END", RateError),
])
def test_partial_write_and_readback_mismatch_roll_back(db_settings, trigger, error):
    seed(db_settings, [("JPY", "115/11", 1, "0.21", "0.24")])
    with closing(sqlite3.connect(db_settings.path)) as db, db:
        db.execute(trigger)
    before = db_settings.path.read_bytes()
    plan = plan_rates(db_settings, downloader=fake_download)
    with pytest.raises(error):
        save_rates(db_settings, plan)
    assert db_settings.path.read_bytes() == before


def test_rejects_stale_plan_and_non_test_before_download(db_settings):
    plan = plan_rates(db_settings, downloader=fake_download)
    seed(db_settings, [("USD", "115/11", 1, "31", "32")])
    with pytest.raises(RateError, match="已變動"):
        save_rates(db_settings, plan)
    assert len(rows(db_settings)) == 1
    downloader = Mock(side_effect=AssertionError("network"))
    db_settings.path = db_settings.path.with_name("production.sqlite3")
    with pytest.raises(ValueError, match="test"):
        plan_rates(db_settings, downloader=downloader)
    with pytest.raises(ValueError, match="test"):
        save_rates(db_settings, plan)
    downloader.assert_not_called()


def test_cli_local_simulation_and_import_confirmation(db_settings, tmp_path, capsys):
    config = tmp_path / "fuelcore.ini"
    config.write_bytes(transfer_tests.EXAMPLE.read_bytes())
    source = tmp_path / "fake-current.json"
    source.write_bytes(current())
    report_dir = tmp_path / "reports"
    before = db_settings.path.read_bytes()
    args = ["--config", str(config), "--from-files", str(source)]
    with patch("fuelcore.declare_rate.download_rate", side_effect=AssertionError("network")):
        with patch.object(sys, "argv", ["fuelcore", "rate-simulate", "--output", str(report_dir), *args]):
            assert main() == 0
        path, = report_dir.glob("每旬匯率模擬_*.txt")
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "新增 USD" in path.read_text(encoding="utf-8-sig")
        assert db_settings.path.read_bytes() == before
        with patch.object(sys, "argv", ["fuelcore", "rate-import", *args]), patch("builtins.input", return_value="n"):
            assert main() == 0
        assert db_settings.path.read_bytes() == before
        with patch.object(sys, "argv", ["fuelcore", "rate-import", "--yes", *args]):
            assert main() == 0
    assert "已寫入並核對 3 筆" in capsys.readouterr().out


def test_conversion_discrepancy_is_a_note(db_settings, tmp_path):
    seed(db_settings, [("USD", "115/10", 3, "31.12345", "32.555")])
    result = convert_file(transfer_tests.DatabaseTest.sample(SimpleNamespace(folder=tmp_path)), db_settings)
    assert result.ok
    assert any("每旬匯率提醒" in note and "31.12345" in note and "31.43800" in note for note in result.notes)


@pytest.mark.skipif(os.getenv("FUELCORE_LIVE_RATE") != "1", reason="需要 FUELCORE_LIVE_RATE=1 才連關務署")
def test_live_current_has_usd():
    _, rates = parse_current(download_rate(CURRENT))
    assert any(rate.code == "USD" for rate in rates)
