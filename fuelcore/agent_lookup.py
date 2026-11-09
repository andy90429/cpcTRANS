"""Anonymous Taichung ship-agent lookup, portable to Python 3.7."""

import csv
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
from io import StringIO
from pathlib import Path
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


TPNET = "https://tpnet2.twport.com.tw"
DYNAMIC = "/IFA_PUBLIC/ShipDynamic/"
BERTH = "/IFA_PUBLIC/ShipBerthDispatch/"
DEFAULT_DIR = Path(".fuelcore-agent")
DEFAULT_MAPPING = DEFAULT_DIR / "mapping.csv"
MAPPING_COLUMNS = ("完整名稱", "簡稱")
PAGE_SIZE = 15


class AgentLookupError(Exception):
    pass


class SourceFormatError(AgentLookupError):
    pass


class NetworkError(AgentLookupError):
    pass


@dataclass(frozen=True)
class ShipCandidate:
    ship_name: str
    ship_name_zh: str
    agent_full: str
    berth: str
    eta: str


@dataclass(frozen=True)
class ShipResult:
    status: str
    ship_name: str = ""
    ship_name_zh: str = ""
    call_sign: str = ""
    agent_full: str = ""
    berth: str = ""
    eta: str = ""
    candidates: Tuple[ShipCandidate, ...] = ()


@dataclass(frozen=True)
class ShortResult:
    status: str
    short: Optional[str]
    candidates: Tuple[str, ...] = ()


class SameSiteRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != "https" or url.netloc != urlsplit(req.full_url).netloc:
            raise SourceFormatError("公開查詢頁導向非預期網址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicSession:
    def __init__(self):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()), SameSiteRedirect())
        self.last_request = None

    def request(self, url, *, form=None, payload=None, token=None, referer=None):
        headers = {"User-Agent": "fuelcore/1.0 (internal ship-agent lookup)"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urlencode(form).encode("ascii")
        if token:
            headers["RequestVerificationToken"] = token
        if referer:
            headers["Referer"] = referer
        for attempt in range(2):
            if self.last_request is not None:
                time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
            try:
                with self.opener.open(Request(url, body, headers), timeout=20) as response:
                    return response.read().decode(response.headers.get_content_charset() or "utf-8-sig")
            except HTTPError as error:
                if error.code not in (408, 429) and not 500 <= error.code <= 599:
                    raise NetworkError("港務查詢 HTTP {}".format(error.code)) from error
                if attempt or error.code == 429:
                    # Respect rate limiting; do not immediately resend an explicit 429.
                    raise NetworkError("港務查詢 HTTP {}".format(error.code)) from error
            except (OSError, HTTPException) as error:
                if attempt:
                    raise NetworkError("港務查詢連線失敗：{}".format(type(error).__name__)) from error
            except (UnicodeError, LookupError) as error:
                raise SourceFormatError("無法讀取港務網站的回應編碼") from error
            finally:
                self.last_request = time.monotonic()


class PublicHTML(HTMLParser):
    """Read the anonymous port selector, token and visible link labels."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.inputs = {}
        self.port = None
        self.words = []
        self.in_port = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") and attrs.get("type") == "hidden":
            self.inputs[attrs["name"]] = attrs.get("value", "")
        if tag == "select":
            self.in_port = attrs.get("name") == "port"
        if tag == "option" and self.in_port and "selected" in attrs:
            self.port = attrs.get("value")

    def handle_endtag(self, tag):
        if tag == "select":
            self.in_port = False

    def handle_data(self, data):
        self.words.append(data)


def _text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceFormatError("TPNet 2.0 船舶欄位型別已變更")
    return value.strip()


def _visible(value):
    return "".join(PublicHTML(value).words).strip() if "<" in value else value


def _value(value):
    value = _text(value)
    return "" if value.upper() in ("-", "N/A", "NULL") else value


def _json(raw, source):
    try:
        return json.loads(raw)
    except ValueError as error:
        raise SourceFormatError("{} 未回傳預期的 JSON".format(source)) from error


def _token(page):
    token = page.inputs.get("__RequestVerificationToken")
    if not token:
        raise SourceFormatError("TPNet 2.0 缺少公開表單 token")
    return token


def _select_port(session, port):
    path = DYNAMIC + "Index"
    page = PublicHTML(session.request(TPNET + path))
    if page.port != port:
        page = PublicHTML(session.request(
            TPNET + "/Account/SwitchPortAnonymous",
            form=dict(port=port, returnUrl=path, __RequestVerificationToken=_token(page)),
            referer=TPNET + path,
        ))
    if page.port != port:
        raise SourceFormatError("TPNet 2.0 未切換到指定港別")
    return _token(page)


def _query_name(name):
    name = " ".join(name.upper().split())
    return re.sub(r"^(?:M/V|M\.V\.)\s*|^MV(?:\s+|$)", "", name)


def _name_key(name):
    return "".join(_query_name(name).split())


def _names(row, section):
    if section == BERTH:
        return _text(row["vesselEname"]), _text(row["vesselCname"])
    names = _text(row["shipName"]).split("\t")
    return (names[-1].strip(), names[0].strip()) if len(names) > 1 else (names[0], "")


def _primary_pages(session, section, port, token, ship_name):
    required = {"visaNo", "vesselNo", "port", "agentName", "etaDt", "spWharfCode"}
    required.update({"shipName", "departed"} if section == DYNAMIC
                    else {"vesselEname", "vesselCname", "estWharf1", "spSts"})
    rows = []
    total = None
    key = _name_key(ship_name)
    while total is None or len(rows) < total:
        data = session.request(TPNET + section + "GridSearchData", token=token,
                               referer=TPNET + section + "Index",
                               payload=dict(skip=len(rows), take=PAGE_SIZE, requiresCounts=True,
                                            ShipName=ship_name, VisaNo="", Pier=None, AgentName="", CallSign=""))
        page = _json(data, "TPNet 2.0")
        if not isinstance(page, dict) or type(page.get("count")) is not int or page["count"] < 0:
            raise SourceFormatError("TPNet 2.0 清單缺少有效的 count")
        batch = page.get("result")
        if (not isinstance(batch, list) or len(batch) > PAGE_SIZE
                or any(not isinstance(row, dict) or not required <= row.keys() or row["port"] != port
                       for row in batch)):
            raise SourceFormatError("TPNet 2.0 清單欄位或港別已變更")
        if total is None:
            total = page["count"]
        if total != page["count"] or len(rows) + len(batch) > total or (not batch and len(rows) < total):
            raise SourceFormatError("TPNet 2.0 分頁筆數不一致")
        if key and any(not any(key in _name_key(name) for name in _names(row, section)) for row in batch):
            # A verified unrelated row means this endpoint ignored the filter.
            return _primary_pages(session, section, port, token, "")
        rows.extend(batch)
    return rows


def _dashboard_call(session, visa, port, token):
    data = session.request(TPNET + DYNAMIC + "GetDashboardComplex",
                           form=dict(visaNo=visa, port=port, __RequestVerificationToken=token),
                           token=token, referer=TPNET + DYNAMIC + "Index")
    row = _json(data, "TPNet 2.0 明細")
    if row is None or row == {}:
        return ""
    if not isinstance(row, dict):
        raise SourceFormatError("TPNet 2.0 船舶明細缺少呼號欄")
    if "callSign" not in row and not {"vesselEname", "vesselCname"}.intersection(row):
        raise SourceFormatError("TPNet 2.0 船舶明細缺少呼號欄")
    return _value(row.get("callSign"))


def _latest_key(row, visa):
    stamp = _value(row.get("spReqDt")) or _value(row["etaDt"]) or _value(row.get("spActportDt"))
    # Both endpoints publish local timestamps, with ISO or slash separators.
    if stamp:
        try:
            stamp = datetime.fromisoformat(stamp.replace("/", "-").replace("Z", "+00:00"))
        except ValueError as error:
            raise SourceFormatError("TPNet 2.0 船舶時間格式已變更") from error
        stamp = stamp.replace(tzinfo=None)
    return stamp or datetime.min, visa


def _matching_visits(rows, section, key, departed):
    visits = {}
    seen = set()
    for row in rows:
        visa = _visible(_text(row["visaNo"]))
        vessel = _text(row["vesselNo"])
        if not visa or visa in seen or not vessel:
            raise SourceFormatError("TPNet 2.0 簽證缺漏或分頁重複")
        seen.add(visa)
        en, zh = _names(row, section)
        if not (en or zh):
            raise SourceFormatError("TPNet 2.0 清單缺少船名")
        left = (_value(_visible(_text(row["departed"]))) if section == DYNAMIC else
                _text(row["spSts"]) in ("已出港", "船舶已出港", "已離港", "出港完成", "出港作業完成"))
        if left:
            departed.add(visa)
        if visa in departed or key not in (_name_key(en), _name_key(zh)):
            continue
        berth = _value(row["spWharfCode"]) or _value(row.get("estWharf1"))
        candidate = ShipCandidate(en or zh, zh, _value(row["agentName"]), berth.split("\t")[-1],
                                  _value(row["etaDt"]))
        rank = _latest_key(row, visa)
        if vessel not in visits or rank > visits[vessel][0]:
            visits[vessel] = (rank, visa, candidate)
    return list(visits.values())


def lookup_ship(ship_name, port="TXG"):
    """Query one vessel by exact normalized English or Chinese name."""
    name = _query_name(ship_name)
    if not name:
        raise ValueError("請提供船名")
    if port not in ("TXG", "臺中港", "台中港"):
        raise ValueError("目前只支援臺中港（TXG）")
    port = "TXG"
    session = PublicSession()
    token = _select_port(session, port)
    departed = set()
    for section in (DYNAMIC, BERTH):
        rows = _primary_pages(session, section, port, token, name)
        matches = _matching_visits(rows, section, _name_key(name), departed)
        if matches:
            break
    if not matches:
        return ShipResult("not_found")
    if len(matches) > 1:
        return ShipResult("ambiguous", candidates=tuple(candidate for _, _, candidate in matches))
    _, visa, candidate = matches[0]
    call = _dashboard_call(session, visa, port, token)
    return ShipResult("found" if call else "no_call_sign", call_sign=call, **asdict(candidate))


def _atomic_write(path, write, encoding):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="",
                                         dir=str(path.parent), delete=False) as stream:
            temp = stream.name
            write(stream)
        os.replace(temp, path)
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)


def _mappings(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return {}
    for encoding in ("utf-8-sig", "cp950"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        break
    else:
        raise ValueError("代理行對照表編碼無法辨識，請另存為 CSV UTF-8")
    with StringIO(text, newline="") as stream:
        rows = csv.reader(stream)
        if tuple(next(rows, [])[:2]) != MAPPING_COLUMNS:
            raise ValueError("代理行對照表前兩欄須為「完整名稱,簡稱」")
        return {row[0].strip(): row[1].strip() for row in rows
                if len(row) >= 2 and row[0].strip() and row[1].strip()}


def resolve_short(agent_full, known_shorts, mapping_csv):
    agent_full = agent_full.strip()
    mapped = _mappings(mapping_csv).get(agent_full)
    if mapped:
        return ShortResult("mapped", mapped)
    shorts = tuple(dict.fromkeys(short.strip() for short in known_shorts if short.strip()))
    matches = tuple(short for short in shorts if short in agent_full)
    if len(matches) == 1:
        return ShortResult("matched", matches[0], matches)
    return ShortResult("needs_selection", None, matches or shorts)


def save_mapping(mapping_csv, agent_full, short):
    agent_full, short = agent_full.strip(), short.strip()
    if not agent_full or not short:
        raise ValueError("完整名稱與簡稱不可空白")
    mappings = _mappings(mapping_csv)
    mappings[agent_full] = short

    def write(stream):
        writer = csv.writer(stream)
        writer.writerow(MAPPING_COLUMNS)
        writer.writerows(mappings.items())

    _atomic_write(mapping_csv, write, "utf-8-sig")
