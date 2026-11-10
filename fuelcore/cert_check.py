"""Inspect a PKCS#12 file with Windows CryptoAPI and an in-memory certificate store."""

import ctypes
import getpass
import os
import sys
import warnings
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


NO_PERSIST_KEY = 0x8000
X509_ENCODING = 0x00000001
PKCS7_ENCODING = 0x00010000
SIMPLE_DISPLAY = 4
ISSUER_FLAG = 1
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class CertificateError(ValueError):
    pass


class Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(wintypes.BYTE))]


class Algorithm(ctypes.Structure):
    _fields_ = [("oid", ctypes.c_char_p), ("parameters", Blob)]


class CertificateInfo(ctypes.Structure):
    _fields_ = [("version", wintypes.DWORD), ("serial", Blob), ("signature", Algorithm),
                ("issuer", Blob), ("not_before", wintypes.FILETIME), ("not_after", wintypes.FILETIME),
                ("subject", Blob)]


class CertificateContext(ctypes.Structure):
    _fields_ = [("encoding", wintypes.DWORD), ("encoded", ctypes.POINTER(wintypes.BYTE)),
                ("length", wintypes.DWORD), ("info", ctypes.POINTER(CertificateInfo)),
                ("store", wintypes.HANDLE)]


ContextPointer = ctypes.POINTER(CertificateContext)


@dataclass(frozen=True)
class Certificate:
    subject: str
    issuer: str
    not_before: datetime
    not_after: datetime
    has_private_key: bool
    self_signed: bool


@dataclass(frozen=True)
class CertificateCheck:
    filename: str
    certificates: tuple
    signing_certificate: Certificate
    days_left: int
    status: str
    recorded_mismatch: bool


def is_windows():
    return os.name == "nt"


def windows_api():
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    types = {
        "PFXVerifyPassword": (wintypes.BOOL, ctypes.POINTER(Blob), wintypes.LPCWSTR, wintypes.DWORD),
        "PFXImportCertStore": (wintypes.HANDLE, ctypes.POINTER(Blob), wintypes.LPCWSTR, wintypes.DWORD),
        "CertEnumCertificatesInStore": (ContextPointer, wintypes.HANDLE, ContextPointer),
        "CertFreeCertificateContext": (wintypes.BOOL, ContextPointer),
        "CertCloseStore": (wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD),
        "CertGetNameStringW": (wintypes.DWORD, ContextPointer, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD),
        "CertGetCertificateContextProperty": (wintypes.BOOL, ContextPointer, wintypes.DWORD,
                                              ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)),
        "CertCompareCertificateName": (wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(Blob), ctypes.POINTER(Blob)),
        "CryptVerifyCertificateSignatureEx": (wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                             ContextPointer, wintypes.DWORD, ContextPointer,
                                             wintypes.DWORD, ctypes.c_void_p),
    }
    for name, (result, *args) in types.items():
        function = getattr(dll, name)
        function.restype, function.argtypes = result, args
    return dll


def filetime(value):
    ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
    return FILETIME_EPOCH + timedelta(microseconds=ticks // 10)


def cert_name(api, context, flags):
    length = api.CertGetNameStringW(context, SIMPLE_DISPLAY, flags, None, None, 0)
    if length <= 1:
        return ""
    result = ctypes.create_unicode_buffer(length)
    if api.CertGetNameStringW(context, SIMPLE_DISPLAY, flags, None, result, length) != length:
        raise CertificateError("無法取得憑證名稱")
    return result.value


def describe(api, context):
    info = context.contents.info.contents
    encoding = X509_ENCODING | PKCS7_ENCODING
    same_name = api.CertCompareCertificateName(encoding, ctypes.byref(info.subject), ctypes.byref(info.issuer))
    # 2 identifies a certificate context for both the signature subject and issuer.
    self_signed = bool(same_name and api.CryptVerifyCertificateSignatureEx(
        None, encoding, 2, context, 2, context, 0, None))
    # NO_PERSIST_KEY returns an in-memory key context. Query it without opening a key provider.
    has_key = any(api.CertGetCertificateContextProperty(context, prop, None,
                                                       ctypes.byref(wintypes.DWORD())) for prop in (5, 78, 79))
    return Certificate(cert_name(api, context, 0), cert_name(api, context, ISSUER_FLAG),
                       filetime(info.not_before), filetime(info.not_after), has_key, self_signed)


def read_windows_pfx(data, password):
    if not is_windows():
        raise CertificateError("只能在 Windows 檢查")
    api = windows_api()
    payload = (wintypes.BYTE * len(data)).from_buffer_copy(data)
    blob = Blob(len(data), payload)
    key = ctypes.create_unicode_buffer(password)
    try:
        if not api.PFXVerifyPassword(ctypes.byref(blob), key, 0):
            raise CertificateError("密碼打不開 PFX 或檔案格式無效")
        store = api.PFXImportCertStore(ctypes.byref(blob), key, NO_PERSIST_KEY)
        if not store:
            raise CertificateError("無法在記憶體開啟 PFX")
    finally:
        ctypes.memset(key, 0, ctypes.sizeof(key))
    context = None
    try:
        certificates = []
        while True:
            context = api.CertEnumCertificatesInStore(store, context)
            if not context:
                return tuple(certificates)
            certificates.append(describe(api, context))
    finally:
        if context:
            api.CertFreeCertificateContext(context)
        api.CertCloseStore(store, 0)


def pfx_path(value):
    if value is None or not str(value).strip():
        raise CertificateError("請提供 PFX 檔名")
    path = Path(value)
    try:
        exists = path.is_file()
    except (OSError, ValueError):
        exists = False
    if not exists:
        raise CertificateError(f"找不到 PFX 檔案 {path.name}")
    return path


def read_certificates(path, password, reader):
    try:
        data = path.read_bytes()
    except OSError:
        raise CertificateError(f"無法讀取 PFX 檔案 {path.name}") from None
    try:
        return tuple(reader(data, password))
    except CertificateError as error:
        # Only messages created by the native reader are known to be safe.
        if reader is read_windows_pfx:
            text = str(error)
            raise CertificateError(text.replace(password, "〈已遮蔽〉") if password else text) from None
        raise CertificateError("密碼打不開 PFX 或無法讀取憑證") from None
    except Exception:
        raise CertificateError("密碼打不開 PFX 或無法讀取憑證") from None


def check_pfx(path, password, *, recorded_expiry=None, reader=None, now=None):
    path = pfx_path(path)
    if reader is None and not is_windows():
        raise CertificateError("只能在 Windows 檢查")
    if "\0" in password:
        raise CertificateError("密碼打不開 PFX")
    certificates = read_certificates(path, password, reader or read_windows_pfx)
    if password:
        certificates = tuple(replace(cert, subject=cert.subject.replace(password, "〈已遮蔽〉"),
                                     issuer=cert.issuer.replace(password, "〈已遮蔽〉")) for cert in certificates)
    del password
    if not certificates:
        raise CertificateError("PFX 沒有任何憑證")
    candidates = [cert for cert in certificates if cert.has_private_key] or [
        cert for cert in certificates if not cert.self_signed]
    if not candidates:
        raise CertificateError("PFX 找不到可選的簽章憑證")
    signing = max(candidates, key=lambda cert: cert.not_after)
    now = now or datetime.now(timezone.utc)
    days = (signing.not_after.date() - now.date()).days
    status = "已過期" if signing.not_after <= now else "30 天內到期" if days <= 30 else "正常"
    return CertificateCheck(path.name, certificates, signing, days, status,
                            recorded_expiry is not None and recorded_expiry != signing.not_after.date())


def expiry_date(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except ValueError:
        raise CertificateError("登錄到期日請用 YYYY-MM-DD") from None


def run_cert_command(args):
    try:
        path = pfx_path(args.pfx)
        if not is_windows():
            raise CertificateError("只能在 Windows 檢查")
        recorded = expiry_date(args.recorded_expiry) if args.recorded_expiry is not None else None
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("請輸入 PFX 密碼：")
        try:
            result = check_pfx(path, password, recorded_expiry=recorded)
        finally:
            del password
        print(f"{result.filename}｜主體 {result.signing_certificate.subject or '未提供'}｜"
              f"到期日 {result.signing_certificate.not_after.date()}｜剩餘 {result.days_left} 天｜{result.status}")
        if result.recorded_mismatch:
            print("提醒：登錄到期日與憑證到期日不一致。")
        return int(result.status == "已過期")
    except CertificateError as error:
        print(error, file=sys.stderr)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning, OSError):
        print("無法在不回顯的情況下讀取 PFX 密碼，請從互動式終端機執行。", file=sys.stderr)
    return 1
