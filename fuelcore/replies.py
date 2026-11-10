import glob
import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Tuple

from .convert import MESSAGE
from .db import Database
from .edi.xml import row_text, upper_keys
from .settings import validate_test_database


NAMES = {
    "N5107": "應補辦事項通知",
    "N5109": "查驗貨物通知",
    "N5204": "出口放行通知",
    "NX5106": "不受理報單通知",
    "NX903": "錯誤或異常通知",
    "N5110": "稅費繳納證",
}
WATCHED = ("N5107", "N5204")
RECEIVED = "接收日期時間"
DATE_PREFIX = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(.*)$")
EXTRA_FIELDS = {
    "N5109": (("派驗日期時間", "派驗"), ("查驗區", "查驗區"), ("驗貨關員", "關員")),
    "NX5106": (("回應代碼", "回應代碼"), ("備註", "備註")),
    "NX903": (("原訊息類別", "原訊息類別"), ("原訊息編號", "原訊息編號")),
    "N5110": (("稅費合計", "稅費合計"), ("繳款截止日", "繳款截止日")),
}
PDF_FIELD = re.compile(r"\{(SERSNO|BANAME|BAID|BYNAME)\}")


class ReplyError(ValueError):
    pass


@dataclass(frozen=True)
class Reply:
    code: str
    received: datetime
    sersno: str
    ship_name: str
    content: str
    method: str
    release_time: str
    requirements: Tuple[str, ...]
    header_ship: str
    call_sign: str
    buyer: str


@dataclass(frozen=True)
class ReplyList:
    replies: Tuple[Reply, ...]
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class Notification:
    reply: Reply
    kind: str
    pdfs: Tuple[Path, ...]
    missing: Tuple[str, ...]


@dataclass(frozen=True)
class PollResult:
    logs: Tuple[str, ...]
    notifications: Tuple[Notification, ...]
    notes: Tuple[str, ...]


def day_patterns(day):
    return tuple(dict.fromkeys(
        separator.join((str(day.year), month, day_of_month)) + "%"
        for separator in ("-", "/")
        for month in (str(day.month), f"{day.month:02d}")
        for day_of_month in (str(day.day), f"{day.day:02d}")
    ))


def received_at(row):
    match = DATE_PREFIX.fullmatch(row_text(row, RECEIVED).strip())
    if not match:
        return None
    year, month, day, suffix = match.groups()
    try:
        value = datetime.fromisoformat(f"{year}-{int(month):02d}-{int(day):02d}{suffix}")
    except ValueError:
        return None
    return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value


def open_database(settings):
    if settings.edi is None:
        raise ReplyError("請在 fuelcore.ini 設定 [edi] 測試庫。")
    try:
        if not settings.test_only:
            raise ValueError
        if settings.driver == "mysql":
            for name in (settings.edi.edi_database, settings.mysql["database"]):
                validate_test_database(name)
        return Database(settings, read_only=True)
    except Exception:
        raise ReplyError("無法開啟 EDI 測試庫。") from None


class ReplyStore:
    def __init__(self, db):
        self.db = db
        self.schema = db.settings.edi.edi_database
        try:
            self.names = db.table_names(self.schema)
        except Exception:
            raise ReplyError("無法讀取 EDI 測試庫的資料表。") from None
        self.notes = {}
        self.headers = {}
        # CSV fixtures use text columns. Real MySQL MainID is numeric.
        self.main_id = "MainID" if db.mysql else "CAST(MainID AS INTEGER)"

    def read(self, code, columns, suffix="", params=()):
        name = self.names.get(code.casefold())
        if name is None:
            self.notes[code] = f"測試庫缺少 {code}，已略過。"
            return None
        try:
            rows = self.db.select_dicts(f"SELECT {columns} FROM {self.db.table(name, self.schema)}{suffix}", params)
        except Exception:
            self.notes[code] = f"無法讀取 {code}，已略過。"
            return None
        return [upper_keys(row) for row in rows]

    def header(self, sersno, transport):
        code = {"1": "5203S", "4": "5203A"}.get(transport, MESSAGE)
        key = sersno, code
        if key not in self.headers:
            result = ("", "", "")
            try:
                rows = self.db.select(f"SELECT BANAME, BAID, BYNAME FROM {self.db.table('cusdec')} "
                                      "WHERE SERSNO = ? AND MSGCODE = ?", key) if sersno else ()
                if rows:
                    result = tuple(str(value or "").strip() for value in rows[0])
            except Exception:
                self.notes["cusdec"] = "無法讀取報單主檔的船名、呼號及買方，已略過。"
            self.headers[key] = result
        return self.headers[key]

    def details(self, code, field, main_id):
        if main_id is None:
            self.notes[code] = f"回覆缺少 MainID，無法讀取 {code}。"
            return ()
        rows = self.read(code, f"`{field}`", " WHERE MainID = ?", (main_id,))
        return tuple(dict.fromkeys(value for row in rows or () if (value := row_text(row, field).strip())))

    def reply(self, code, row, received):
        sersno = row_text(row, "報單號碼").strip()
        method = row_text(row, "通關方式").strip()
        release_time = row_text(row, "放行日期時間").strip()
        main_id = row.get("MAINID")
        requirements = ()
        ship, call_sign, buyer = self.header(sersno, row_text(row, "海空運別").strip())
        header_ship = ship
        if code == "N5107":
            requirements = self.details("N5107_11", "應補辦事項", main_id)
            parts = [f"通關方式 {method}" if method else "", requirement_text(requirements),
                     labelled("補辦期限", row_text(row, "補辦期限"))]
        elif code == "N5204":
            reported_ship = row_text(row, "船舶名稱").strip()
            if reported_ship and set(reported_ship.upper()) != {"Z"}:
                ship = reported_ship
            amount = " ".join(filter(None, (row_text(row, "放行件數").strip(), row_text(row, "件數單位").strip())))
            parts = [release_text(method), labelled("放行時間", release_time), labelled("放行件數", amount)]
        else:
            fields = EXTRA_FIELDS[code]
            missing = [field for field, _ in fields if field not in row]
            if missing:
                self.notes[code] = f"{code} 缺少規格欄位 {'、'.join(missing)}，已略過缺漏欄位。"
            parts = [labelled(label, row_text(row, field)) for field, label in fields]
            if code == "N5109":
                dispatched = received_at({RECEIVED: row_text(row, "派驗日期時間")})
                if dispatched:
                    parts[0] = f"派驗 {dispatched:%Y-%m-%d %H:%M}"
                parts.insert(0, "查驗")
            elif code == "NX5106":
                parts[-1] = unescape(parts[-1])
            elif code == "NX903":
                errors = self.details("NX903_1", "錯誤說明", main_id)
                parts.append(labelled("錯誤說明", "、".join(errors)))
        return Reply(code, received, sersno, ship, "；".join(filter(None, parts)), method, release_time,
                     requirements, header_ship, call_sign, buyer)

    def daily(self, code, day, after=None, through=None):
        params = day_patterns(day)
        date_filter = " OR ".join(f"`{RECEIVED}` LIKE ?" for _ in params)
        suffix = f" WHERE ({date_filter})"
        if after is not None:
            suffix += f" AND {self.main_id} > ? AND {self.main_id} <= ?"
            params += (after, through)
        rows = self.read(code, "*", suffix, params)
        if rows is None:
            return None
        replies = []
        for row in rows:
            received = received_at(row)
            if received and received.date() == day:
                replies.append(self.reply(code, row, received))
        return replies

    def last_notice_method(self, sersno):
        if not sersno:
            return ""
        rows = self.read("N5107", "`通關方式`",
                         f" WHERE `報單號碼` = ? ORDER BY {self.main_id} DESC LIMIT 1", (sersno,))
        return row_text(rows[0], "通關方式").strip() if rows else ""


def labelled(label, value):
    return f"{label} {value.strip()}" if value.strip() else ""


def requirement_text(requirements):
    return labelled("應補辦", "、".join(requirements))


def release_text(method, previous=""):
    if previous and method and previous != method:
        return f"放行（{previous} 改 {method}）"
    return f"放行（{method}）" if method else "放行"


def today_replies(settings, day=None):
    day = day or date.today()
    with closing(open_database(settings)) as db:
        store = ReplyStore(db)
        replies = [reply for code in NAMES for reply in store.daily(code, day) or ()]
    return ReplyList(tuple(sorted(replies, key=lambda reply: reply.received, reverse=True)), tuple(store.notes.values()))


def load_positions(path):
    try:
        with path.open(encoding="utf-8") as stream:
            positions = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ReplyError("無法讀取海關回覆監看位置。") from None
    if not isinstance(positions, dict) or any(
        code not in WATCHED or type(value) is not int or value < 0 for code, value in positions.items()
    ):
        raise ReplyError("海關回覆監看位置格式錯誤。")
    return positions


def save_positions(path, positions):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.name + ".", suffix=".tmp",
                                dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(positions, stream, sort_keys=True)
        temporary.replace(path)
    except OSError:
        raise ReplyError("無法儲存海關回覆監看位置。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def notification_kind(method, requirements):
    if method.upper().startswith("C3"):
        return "C3"
    if method.upper().startswith("C1") and any(value.strip().upper() == "A18" for value in requirements):
        return "C1A18"
    return ""


def expand_pdf_paths(patterns, base, fields):
    found, missing = {}, []
    for template in patterns:
        required = PDF_FIELD.findall(template)
        display = PDF_FIELD.sub(lambda match: fields.get(match[1]) or match[0], template)
        if any(not fields.get(key) or any(char in fields[key] for char in "/\\:") for key in required):
            missing.append(display)
            continue
        pattern = PDF_FIELD.sub(lambda match: glob.escape(fields[match[1]]), template)
        if not Path(pattern).is_absolute():
            pattern = str(Path(glob.escape(str(base))) / pattern)
        matches = []
        for item in sorted(glob.glob(pattern)):
            path = Path(item)
            if path.suffix.lower() == ".pdf" and path.is_file():
                matches.append(path)
        if matches:
            found.update((path.resolve(), None) for path in matches)
        else:
            missing.append(display)
    return tuple(found), tuple(dict.fromkeys(missing))


def poll(settings, state_path):
    state_path = Path(state_path)
    positions = load_positions(state_path)
    updated = positions.copy()
    logs, notifications = [], []
    with closing(open_database(settings)) as db:
        store = ReplyStore(db)
        new = []
        day = date.today()
        for code in WATCHED:
            maximum = store.read(code, f"MAX({store.main_id}) AS highest")
            if maximum is None:
                continue
            highest = int(maximum[0]["HIGHEST"] or 0)
            if code not in positions:
                updated[code] = highest
            elif highest > positions[code]:
                replies = store.daily(code, day, positions[code], highest)
                if replies is not None:
                    new.extend(replies)
                    updated[code] = highest

        for reply in sorted(new, key=lambda row: (row.received, row.code)):
            if reply.code == "N5204":
                previous = store.last_notice_method(reply.sersno)
                text = " ".join(filter(None, (release_text(reply.method, previous), reply.release_time)))
                logs.append(f"{reply.sersno}：{text}")
                continue
            text = f"通關方式 {reply.method or '不明'}"
            if reply.requirements:
                text += f"（{requirement_text(reply.requirements)}）"
            logs.append(f"{reply.sersno}：{text}")
            kind = notification_kind(reply.method, reply.requirements)
            if settings.notify.enabled and reply.sersno and kind and getattr(settings.notify, kind.lower()):
                fields = dict(SERSNO=reply.sersno, BANAME=reply.header_ship, BAID=reply.call_sign, BYNAME=reply.buyer)
                patterns = settings.notify.c3_pdfs if kind == "C3" else settings.notify.c1a18_pdfs
                pdfs, missing = expand_pdf_paths(patterns, settings.notify.base, fields)
                notifications.append(Notification(reply, kind, pdfs, missing))
    if updated != positions:
        save_positions(state_path, updated)
    return PollResult(tuple(logs), tuple(notifications), tuple(store.notes.values()))
