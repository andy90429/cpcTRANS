"""Anonymous Taichung ship-agent lookup, portable to Python 3.7."""

import csv
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


TPNET = "https://tpnet2.twport.com.tw"
SDCI = "https://sdci.twport.com.tw/khbweb/TipcShip.aspx"
DYNAMIC = "/IFA_PUBLIC/ShipDynamic/"
BERTH = "/IFA_PUBLIC/ShipBerthDispatch/"
DEFAULT_DIR = Path(".fuelcore-agent")
DEFAULT_CACHE = DEFAULT_DIR / "ships.json"
DEFAULT_MAPPING = DEFAULT_DIR / "mapping.csv"
MAPPING_COLUMNS = ("完整名稱", "簡稱")
GRID = "ASPxGridView1"
PAGE_SIZE = 15


class AgentLookupError(Exception):
    pass


class SourceFormatError(AgentLookupError):
    pass


class NetworkError(AgentLookupError):
    pass


@dataclass(frozen=True)
class ShipAgent:
    ship_name: str
    call_sign: str
    imo: str
    agent_full: str
    berth: str
    eta: str
    source: str
    ship_name_zh: str = ""


@dataclass(frozen=True)
class LookupResult:
    status: str
    ship: Optional[ShipAgent]
    matched_by: Optional[str]
    fetched_at: datetime
    candidates: Tuple[ShipAgent, ...] = ()
    stale: bool = False
    warning: str = ""


@dataclass(frozen=True)
class ShortResult:
    status: str
    short: Optional[str]
    candidates: Tuple[str, ...] = ()


class SameSiteRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if (url.scheme != "https" or url.netloc != urlsplit(req.full_url).netloc
                or (url.netloc == "sdci.twport.com.tw" and url.path != urlsplit(SDCI).path)):
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
    """Collect anonymous form controls and the numbered SDCI grid cells."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.inputs = {}
        self.port = None
        self.headers = {}
        self.rows = []
        self.words = []
        self.in_port = False
        self.row = None
        self.cell = None
        self.header = None
        self.header_depth = 0
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get("id", "")
        if tag == "input" and attrs.get("name") and attrs.get("type") == "hidden":
            self.inputs[attrs["name"]] = attrs.get("value", "")
        if tag == "select":
            self.in_port = attrs.get("name") in ("port", "R_港口")
        if tag == "option" and self.in_port and "selected" in attrs:
            self.port = attrs.get("value")
        if tag == "tr" and re.fullmatch(GRID + r"_DXDataRow\d+", ident):
            self.row = []
        if tag == "td":
            match = re.fullmatch(GRID + r"_col(\d+)", ident)
            if match:
                self.header = int(match[1])
                self.headers[self.header] = []
            if self.header is not None:
                self.header_depth += 1
            if self.row is not None and self.cell is None:
                self.cell = []

    def handle_endtag(self, tag):
        if tag == "select":
            self.in_port = False
        if tag == "td":
            if self.header is not None:
                self.header_depth -= 1
                if not self.header_depth:
                    self.header = None
            if self.cell is not None:
                self.row.append(" ".join("".join(self.cell).split()))
                self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        self.words.append(data)
        if self.cell is not None:
            self.cell.append(data)
        if self.header is not None:
            self.headers[self.header].append(data)


def _text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceFormatError("船舶欄位型別已變更")
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


def _primary_pages(session, section, port, token):
    required = {"visaNo", "vesselNo", "port", "agentName", "etaDt", "spWharfCode"}
    required.update({"shipName", "departed"} if section == DYNAMIC
                    else {"vesselEname", "vesselCname", "estWharf1"})
    rows = []
    total = None
    while total is None or len(rows) < total:
        data = session.request(TPNET + section + "GridSearchData", token=token,
                               referer=TPNET + section + "Index",
                               payload=dict(skip=len(rows), take=PAGE_SIZE, requiresCounts=True,
                                            ShipName="", VisaNo="", Pier=None, AgentName="", CallSign=""))
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
        rows.extend(batch)
    return rows


def _primary_rows(session, port):
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
    token = _token(page)
    by_visa = {}
    departed = set()
    for section in (DYNAMIC, BERTH):
        seen = set()
        for row in _primary_pages(session, section, port, token):
            visa = _visible(_text(row["visaNo"]))
            vessel = _text(row["vesselNo"])
            if not visa or visa in seen or not vessel:
                raise SourceFormatError("TPNet 2.0 簽證缺漏或分頁重複")
            seen.add(visa)
            if section == DYNAMIC and _value(_visible(_text(row["departed"]))):
                departed.add(visa)
                continue
            if visa in departed:
                continue
            if section == DYNAMIC:
                names = _text(row["shipName"]).split("\t")
                en, zh = (names[-1], names[0]) if len(names) > 1 else (names[0], "")
            else:
                en, zh = _text(row["vesselEname"]), _text(row["vesselCname"])
            berth = _value(row["spWharfCode"]) or _value(row.get("estWharf1"))
            ship = ShipAgent(en or zh, "", "", _value(row["agentName"]),
                             berth.split("\t")[-1], _value(row["etaDt"]), "tpnet2", zh)
            if not ship.ship_name:
                raise SourceFormatError("TPNet 2.0 清單缺少船名")
            if visa in by_visa:
                earlier, existing_vessel = by_visa[visa]
                if vessel != existing_vessel:
                    raise SourceFormatError("TPNet 2.0 同一簽證出現不同船舶號數")
                ship = replace(earlier, berth=earlier.berth or ship.berth,
                               eta=earlier.eta or ship.eta, agent_full=earlier.agent_full or ship.agent_full)
            by_visa[visa] = (ship, vessel)
    return by_visa, token


def _dashboard_call(session, visa, port, token):
    data = session.request(TPNET + DYNAMIC + "GetDashboardComplex",
                           form=dict(visaNo=visa, port=port, __RequestVerificationToken=token),
                           token=token, referer=TPNET + DYNAMIC + "Index")
    row = _json(data, "TPNet 2.0 明細")
    # A berth-only record can be absent from the movement dashboard.
    if row is None or row == {}:
        return ""
    if not isinstance(row, dict) or "callSign" not in row:
        raise SourceFormatError("TPNet 2.0 船舶明細缺少呼號欄")
    return _value(row["callSign"])


def _sdci_form(page):
    fields = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED", "__EVENTVALIDATION")
    if not all(field in page.inputs for field in fields):
        raise SourceFormatError("SDCI 缺少 ASP.NET 表單狀態欄位")
    return {field: page.inputs[field] for field in fields}


def _sdci_page(raw, port):
    page = PublicHTML(raw)
    expected = {0: "船名(AIS)", 3: "簽證編號", 4: "船務代理", 5: "碼頭",
                16: "船舶呼號(AIS)", 17: "IMO(航港局AIS)", 18: "船舶號數"}
    if page.port != port or any(
            "".join("".join(page.headers.get(i, ())).split()) != name
            for i, name in expected.items()):
        raise SourceFormatError("SDCI 港別或船舶表格欄位已變更")
    state = re.search(r"ASPx\.createControl\(ASPxClientGridView,'ASPxGridView1'.*?"
                      r"'stateObject':\{([^}]+)\}", raw, re.S)
    keys = re.search(r"'keys':\[([^]]*)\]", state[1]) if state else None
    callback = re.search(r"'callbackState':'([^']+)'", state[1]) if state else None
    numbers = [re.search(r"'" + name + r"':(\d+)", raw) for name in ("pageIndex", "pageCount", "pageRowCount")]
    if not keys or not callback or not all(numbers):
        raise SourceFormatError("SDCI 分頁狀態格式已變更")
    index, count, row_count = (int(match[1]) for match in numbers)
    if len(page.rows) != row_count or any(len(row) < 19 for row in page.rows):
        raise SourceFormatError("SDCI 船舶資料列數或欄位不完整")
    form = _sdci_form(page)
    form.update({"R_港口": port, "__EVENTTARGET": GRID, GRID: json.dumps({
        "focusedRow": "", "keys": re.findall(r"'([^']*)'", keys[1]), "callbackState": callback[1],
    }, separators=(",", ":"))})
    return page.rows, form, index, count


def _sdci_history(session, initial, port, vessel):
    rows = []
    _, form, _, _ = _sdci_page(initial, port)
    command, value = "APPLYSEARCHPANELFILTER", vessel
    index = 0
    while True:
        form[GRID + "$DXSE"] = vessel
        form["__EVENTARGUMENT"] = "{}|{}{}|{}".format(len(command), command, len(value), value)
        data = session.request(SDCI, form=form, referer=SDCI)
        batch, form, current, count = _sdci_page(data, port)
        if current != index:
            raise SourceFormatError("SDCI 未回傳所要求的頁碼")
        rows.extend(row for row in batch if row[18] == vessel)
        if current + 1 >= count:
            return rows
        command, value = "PAGERONCLICK", "PBN"
        index += 1


def _unique(values):
    values = set(filter(None, values))
    return next(iter(values)) if len(values) == 1 else ""


def fetch_port_ships(port="臺中港"):
    """Fetch the public movement/berthing lists, with targeted IMO enrichment."""
    if port not in ("臺中港", "台中港", "TXG"):
        raise ValueError("目前只支援臺中港（TXG）")
    port = "TXG"
    session = PublicSession()
    primary, token = _primary_rows(session, port)
    if not primary:
        return []
    sdci = PublicSession()
    initial = sdci.request(SDCI)
    page = PublicHTML(initial)
    if page.port != port:
        form = _sdci_form(page)
        form["R_港口"] = port
        initial = sdci.request(SDCI, form=form, referer=SDCI)
    histories = {}
    ships = []
    for visa, (ship, vessel) in primary.items():
        call = _dashboard_call(session, visa, port, token)
        if vessel not in histories:
            histories[vessel] = _sdci_history(sdci, initial, port, vessel)
        history = histories[vessel]
        exact = [row for row in history if row[3] == visa]
        imo = _unique(row[17] for row in (exact or history) if re.fullmatch(r"\d{7}", row[17]))
        fallback_agent = _unique(row[4] for row in exact)
        ships.append(replace(ship, call_sign=call or _unique(_value(row[16]) for row in (exact or history)),
                             imo=imo, agent_full=ship.agent_full or fallback_agent,
                             source="sdci" if not ship.agent_full and fallback_agent else "tpnet2"))
    return list(dict.fromkeys(ships))


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


def _cache(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            record = json.load(stream)
        stamp = datetime.fromisoformat(record["fetched_at"])
        if stamp.tzinfo is None or not isinstance(record["ships"], list):
            raise ValueError
        ships = [ShipAgent(**row) for row in record["ships"]]
        if any(not all(isinstance(value, str) for value in asdict(ship).values()) for ship in ships):
            raise ValueError
        return ships, stamp
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


def _ships(cache_path, refresh):
    cached = _cache(cache_path)
    now = datetime.now(timezone.utc)
    if cached is not None and not refresh and timedelta(0) <= now - cached[1] < timedelta(hours=6):
        return cached[0], cached[1], ""
    try:
        ships = fetch_port_ships()
    except NetworkError as error:
        if cached is None:
            raise
        return cached[0], cached[1], str(error)
    stamp = datetime.now(timezone.utc)
    data = {"fetched_at": stamp.isoformat(), "ships": [asdict(ship) for ship in ships]}
    _atomic_write(cache_path, lambda stream: json.dump(data, stream, ensure_ascii=False, indent=2), "utf-8")
    return ships, stamp, ""


def refresh_port_ships(cache_path=DEFAULT_CACHE):
    """Refresh the Taichung cache, reporting an old timestamp and warning on network failure."""
    return _ships(cache_path, True)


def _normalized(value, field):
    value = " ".join((value or "").upper().split())
    if field == "ship_name":
        value = re.sub(r"^(?:M/V|M\.V\.|MV)(?:\s+|(?<=[/.])(?=[A-Z]))", "", value)
    elif field == "call_sign":
        value = value.replace(" ", "")
    return value


def lookup(imo=None, call_sign=None, ship_name=None, cache_path=DEFAULT_CACHE, refresh=False):
    criteria = (("imo", imo), ("call_sign", call_sign), ("ship_name", ship_name))
    if not any(_normalized(value, field) for field, value in criteria):
        raise ValueError("請提供 IMO、呼號或船名")
    ships, stamp, warning = _ships(cache_path, refresh)
    for field, value in criteria:
        key = _normalized(value, field)
        if not key:
            continue
        matches = [ship for ship in ships if key in {
            _normalized(getattr(ship, field), field),
            _normalized(ship.ship_name_zh, field) if field == "ship_name" else "",
        }]
        if matches:
            if len({ship.agent_full for ship in matches}) > 1:
                return LookupResult("ambiguous", None, field, stamp, tuple(matches), bool(warning), warning)
            return LookupResult("found", matches[0], field, stamp, tuple(matches), bool(warning), warning)
    return LookupResult("not_found", None, None, stamp, stale=bool(warning), warning=warning)


def _mappings(path):
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream)
            if rows.fieldnames != list(MAPPING_COLUMNS):
                raise ValueError("代理行對照表須包含「完整名稱,簡稱」兩欄")
            return {row[MAPPING_COLUMNS[0]].strip(): row[MAPPING_COLUMNS[1]].strip() for row in rows
                    if row.get(MAPPING_COLUMNS[0]) and row.get(MAPPING_COLUMNS[1])}
    except FileNotFoundError:
        return {}


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
