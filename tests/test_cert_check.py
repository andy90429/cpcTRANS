import os
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from fuelcore.__main__ import main
from fuelcore.cert_check import Certificate, CertificateError, check_pfx, read_windows_pfx


NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def cert(name, days, *, private=False, self_signed=False):
    return Certificate(name, name if self_signed else "測試發行者", NOW - timedelta(days=90),
                       NOW + timedelta(days=days), private, self_signed)


@pytest.fixture
def fake_file(tmp_path):
    folder = tmp_path / "private-location"
    folder.mkdir()
    path = folder / "fake-input.bin"
    path.write_bytes(b"arbitrary fixture bytes")
    return path


@pytest.mark.parametrize("days,status", [(-1, "已過期"), (0, "已過期"), (1, "30 天內到期"),
                                         (30, "30 天內到期"), (31, "正常")])
def test_expiry_status(fake_file, days, status):
    value = check_pfx(fake_file, "", now=NOW, reader=lambda *_: [cert("測試主體", days, private=True)])
    assert value.days_left == days
    assert value.status == status
    assert value.certificates[0].issuer == "測試發行者"


def test_private_key_priority_and_non_self_signed_fallback(fake_file):
    short = cert("較早", 60)
    long = cert("較晚", 80)
    root = cert("根", 200, self_signed=True)
    private = cert("簽章用", 40, private=True)
    earlier_private = cert("較早簽章", 35, private=True)
    for certificates, expected in (([short, long, root, private, earlier_private], private),
                                   ([short, long, root], long)):
        result = check_pfx(fake_file, "", now=NOW, reader=lambda *_: certificates)
        assert result.signing_certificate == expected
        assert result.certificates == tuple(certificates)


def test_recorded_expiry_and_no_certificates(fake_file):
    reader = lambda *_: [cert("測試主體", 31)]
    assert check_pfx(fake_file, "", now=NOW, reader=reader, recorded_expiry=date(2026, 11, 10)).recorded_mismatch
    assert not check_pfx(fake_file, "", now=NOW, reader=reader,
                         recorded_expiry=date(2026, 12, 11)).recorded_mismatch
    with pytest.raises(CertificateError, match="沒有任何憑證"):
        check_pfx(fake_file, "", reader=lambda *_: [])
    with pytest.raises(CertificateError, match="找不到可選"):
        check_pfx(fake_file, "", reader=lambda *_: [cert("根", 200, self_signed=True)])


def test_path_errors_only_use_basename(tmp_path):
    path = tmp_path / "private-location" / "missing.pfx"
    with pytest.raises(CertificateError) as error:
        check_pfx(path, "")
    assert str(error.value) == "找不到 PFX 檔案 missing.pfx"
    with pytest.raises(CertificateError, match="請提供 PFX 檔名"):
        check_pfx("  ", "")


def test_reader_error_cannot_expose_password(fake_file):
    value = secrets.token_urlsafe()
    with pytest.raises(CertificateError) as error:
        check_pfx(fake_file, value, reader=Mock(side_effect=ValueError(value)))
    assert "密碼打不開" in str(error.value)
    assert value not in str(error.value)
    assert error.value.__suppress_context__


def test_cli_outputs_only_safe_values_and_warns_on_recorded_date(fake_file, capsys):
    value = secrets.token_urlsafe()
    actual = datetime(2099, 11, 1, tzinfo=timezone.utc)
    reader = Mock(return_value=[Certificate("測試主體 " + value, "測試發行者", NOW, actual, True, False)])
    with patch("fuelcore.cert_check.is_windows", return_value=True), patch(
        "fuelcore.cert_check.getpass.getpass", return_value=value
    ), patch("fuelcore.cert_check.read_windows_pfx", reader), patch.object(
        sys, "argv", ["fuelcore", "cert-check", str(fake_file), "--recorded-expiry", "2027-11-30"]
    ):
        assert main() == 0
    captured = capsys.readouterr()
    assert "2099-11-01" in captured.out and "登錄到期日與憑證到期日不一致" in captured.out
    assert "測試主體" in captured.out and fake_file.name in captured.out
    assert "測試發行者" not in captured.out
    assert value not in captured.out + captured.err
    assert str(fake_file.parent) not in captured.out + captured.err


def test_cli_wrong_password_returns_one_without_leaking(fake_file, capsys):
    value = secrets.token_urlsafe()
    with patch("fuelcore.cert_check.is_windows", return_value=True), patch(
        "fuelcore.cert_check.getpass.getpass", return_value=value
    ), patch("fuelcore.cert_check.read_windows_pfx", side_effect=RuntimeError(value)), patch.object(
        sys, "argv", ["fuelcore", "cert-check", str(fake_file)]
    ):
        assert main() == 1
    output = capsys.readouterr()
    assert value not in output.out + output.err
    assert "密碼打不開" in output.err


def test_non_windows_reports_platform_without_prompt(fake_file, capsys):
    with patch("fuelcore.cert_check.is_windows", return_value=False), patch(
        "fuelcore.cert_check.getpass.getpass"
    ) as prompt, patch.object(sys, "argv", ["fuelcore", "cert-check", str(fake_file)]):
        assert main() == 1
    prompt.assert_not_called()
    assert "只能在 Windows 檢查" in capsys.readouterr().err


def test_native_reader_requests_memory_only_and_wipes_buffer():
    api = Mock()
    api.PFXVerifyPassword.return_value = True
    api.PFXImportCertStore.return_value = 101
    api.CertEnumCertificatesInStore.return_value = None
    with patch("fuelcore.cert_check.is_windows", return_value=True), patch(
        "fuelcore.cert_check.windows_api", return_value=api
    ):
        assert read_windows_pfx(b"fixture", secrets.token_urlsafe()) == ()
    _, buffer, flags = api.PFXImportCertStore.call_args[0]
    assert flags == 0x8000
    assert not any(map(ord, buffer))
    api.CertCloseStore.assert_called_once_with(101, 0)


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows crypt32")
def test_windows_invalid_bytes_report_open_failure(fake_file):
    with pytest.raises(CertificateError, match="密碼打不開 PFX"):
        check_pfx(fake_file, secrets.token_urlsafe())
