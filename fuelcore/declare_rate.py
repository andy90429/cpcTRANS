"""Customs ten-day rates: validate, preview and explicitly import into a test database."""

import calendar
import json
import re
import ssl
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, build_opener

from .db import Database
from .settings import Settings, validate_test_database


BASE_URL = "https://portal.sw.nat.gov.tw/APGQ/GC331!downLoad?formBean.downLoadFile="
CURRENT = "CURRENT_JSON"
HISTORY = "HISTORY_TXT"
TABLE = "mydeclarerate"
SCALE = Decimal("0.00001")
MAX_VALUE = Decimal("10000000000")
MAX_DOWNLOAD = 8 * 1024 * 1024


class RateError(ValueError):
    pass


class RateDownloadError(RateError):
    pass


@dataclass(frozen=True, order=True)
class Period:
    year: int
    month: int
    part: int

    @property
    def text(self):
        return f"{self.year:03d}/{self.month:02d}"

    @classmethod
    def for_date(cls, value):
        if not 1 <= value.year - 1911 <= 999:
            raise RateError("民國年超出範圍")
        return cls(value.year - 1911, value.month, min((value.day - 1) // 10 + 1, 3))

    def previous(self):
        if self.part > 1:
            return Period(self.year, self.month, self.part - 1)
        return Period(self.year, self.month - 1, 3) if self.month > 1 else Period(self.year - 1, 12, 3)


@dataclass(frozen=True)
class Rate:
    code: str
    buy: Decimal
    sell: Decimal


@dataclass(frozen=True)
class Change:
    rate: Rate
    previous: tuple

    @property
    def action(self):
        if not self.previous:
            return "新增"
        return "相同" if self.previous == ((self.rate.buy, self.rate.sell),) else "更新"


@dataclass(frozen=True)
class Plan:
    period: Period
    changes: tuple
    history_differences: tuple
    history_note: str


def amount(value):
    if not isinstance(value, (str, int, Decimal, float)) or isinstance(value, bool):
        raise RateError("匯率數值無效")
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 < number < MAX_VALUE:
            rounded = number.quantize(SCALE, rounding=ROUND_HALF_UP)
            if 0 < rounded < MAX_VALUE:
                return rounded
    except InvalidOperation:
        pass
    raise RateError("匯率數值必須大於 0 且可存為 DECIMAL(15,5)")


def valid_code(code):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code):
        raise RateError("幣別必須為 3 碼大寫英文字母")
    return code


def source_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise RateError("匯率日期須為 YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise RateError("匯率日期無效") from None


def parse_current(data):
    try:
        current = json.loads(data.decode("utf-8-sig"), parse_float=Decimal)
        start, end = source_date(current["start"]), source_date(current["end"])
        if start.day not in (1, 11, 21):
            raise RateError("本旬開始日必須是 1、11 或 21 日")
        last_day = start.day + 9 if start.day < 21 else calendar.monthrange(start.year, start.month)[1]
        if end != start.replace(day=last_day):
            raise RateError("本旬結束日與開始日不符")
        rates = {}
        if not isinstance(current["items"], list):
            raise RateError("本旬匯率 items 格式錯誤")
        for row in current["items"]:
            code = valid_code(row["code"])
            if code in rates:
                raise RateError("本旬匯率幣別重複")
            rates[code] = Rate(code, amount(row["buyValue"]), amount(row["sellValue"]))
    except (AttributeError, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise RateError("本旬匯率 JSON 格式錯誤") from None
    if "USD" not in rates:
        raise RateError("本旬匯率缺少 USD")
    return Period.for_date(start), tuple(rates[code] for code in sorted(rates))


def parse_history(data):
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise RateError("歷史匯率文字格式錯誤") from None
    history = {}
    for index, line in enumerate(lines, 1):
        match = re.fullmatch(r"([A-Z]{3})([0-9]{3})([0-9]{2})([1-3])([0-9]{8})([0-9]{8})", line)
        if not match:
            raise RateError(f"歷史匯率第 {index} 行格式錯誤")
        code, year, month, part, sell, buy = match.groups()
        period = Period(int(year), int(month), int(part))
        if not period.year or not 1 <= period.month <= 12 or (period, code) in history:
            raise RateError(f"歷史匯率第 {index} 行期別錯誤或重複")
        history[period, code] = Rate(code, amount(Decimal(buy).scaleb(-5)), amount(Decimal(sell).scaleb(-5)))
    if not any(code == "USD" for _, code in history):
        raise RateError("歷史匯率缺少 USD")
    return history


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_rate(name):
    if name not in (CURRENT, HISTORY):
        raise RateError("未知的匯率來源")
    context = ssl.create_default_context()
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    try:
        with opener.open(BASE_URL + name, timeout=20) as response:
            data = response.read(MAX_DOWNLOAD + 1)
    except OSError:
        raise RateDownloadError(f"連不到關務署匯率來源（{name}）") from None
    if len(data) > MAX_DOWNLOAD:
        raise RateError("匯率下載內容超出大小限制")
    return data


def load_sources(files=(), downloader=None):
    downloader = downloader or download_rate
    if files:
        if len(files) not in (1, 2):
            raise RateError("--from-files 請提供本旬 JSON，並可選擇再提供歷史 TXT")
        period, rates = parse_current(Path(files[0]).read_bytes())
        if len(files) == 1:
            return period, rates, None, "未提供歷史 TXT，僅比對本旬"
        return period, rates, parse_history(Path(files[1]).read_bytes()), ""
    try:
        period, rates = parse_current(downloader(CURRENT))
    except OSError:
        raise RateDownloadError("連不到關務署匯率來源（CURRENT_JSON）") from None
    try:
        history = parse_history(downloader(HISTORY))
    except (RateDownloadError, OSError):
        return period, rates, None, "連不到歷史匯率來源，僅比對本旬"
    return period, rates, history, ""


def require_test(settings):
    if getattr(settings, "test_only", False) is not True:
        raise RateError("匯率作業需要 test_only = yes")
    if settings.driver == "sqlite":
        validate_test_database(settings.path.resolve().stem)
    elif settings.driver == "mysql":
        for name in (settings.database, settings.code_database, settings.mysql["database"]):
            validate_test_database(name)
        if settings.database != settings.mysql["database"] or settings.database == settings.code_database:
            raise RateError("匯率作業需要分開的報單與代碼測試庫")
    else:
        raise RateError("匯率作業只接受 SQLite 或 MySQL")


def normalize_stored(value):
    try:
        return amount(value)
    except RateError:
        return "格式錯誤" if value is not None else "空白"


def stored_period(db, period, *, lock=False):
    sql = f"SELECT DR002, DR005, DR006 FROM {db.table(TABLE)} WHERE DR003 = ? AND DR004 = ? ORDER BY DR002, DR001"
    if lock and db.mysql:
        sql += " FOR UPDATE"
    rows = {}
    for code, buy, sell in db.select(sql, (period.text, period.part)):
        rows.setdefault(code, []).append((normalize_stored(buy), normalize_stored(sell)))
    return {code: tuple(values) for code, values in rows.items()}


def compare_current(db, period, rates, *, lock=False):
    rows = stored_period(db, period, lock=lock)
    return tuple(Change(rate, rows.get(rate.code, ())) for rate in rates)


def compare_history(db, period, history):
    differences = []
    for _ in range(9):
        period = period.previous()
        if period.year < 1:
            break
        for code, values in sorted(stored_period(db, period).items()):
            expected = history.get((period, code))
            if not expected or values != ((expected.buy, expected.sell),):
                differences.append((period, code, values, expected))
    return tuple(differences)


def plan_rates(settings, *, files=(), downloader=None, connection=None, database=Database):
    require_test(settings)
    period, rates, history, note = load_sources(files, downloader)
    with closing(database(settings, connection, read_only=True)) as db:
        changes = compare_current(db, period, rates)
        differences = compare_history(db, period, history) if history is not None else ()
    return Plan(period, changes, differences, note)


def save_rates(settings, plan, *, connection=None, database=Database):
    require_test(settings)
    pending = tuple(change for change in plan.changes if change.action != "相同")
    if not pending:
        return 0
    with closing(database(settings, connection)) as db:
        if db.mysql:
            db.require_transactional_tables([(settings.code_database, TABLE)])
        db.begin()
        try:
            rates = tuple(change.rate for change in plan.changes)
            if compare_current(db, plan.period, rates, lock=True) != plan.changes:
                raise RateError("匯率資料已變動，請重新比對並確認")
            for change in pending:
                rate = change.rate
                db.execute(f"DELETE FROM {db.table(TABLE)} WHERE DR003 = ? AND DR004 = ? AND DR002 = ?",
                           (plan.period.text, plan.period.part, rate.code))
                db.insert(TABLE, dict(DR002=rate.code, DR003=plan.period.text, DR004=plan.period.part,
                                      DR005=f"{rate.buy:.5f}", DR006=f"{rate.sell:.5f}"))
            if any(change.action != "相同" for change in compare_current(db, plan.period, rates, lock=True)):
                raise RateError("匯率寫入後讀回不一致，已回滾")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return len(pending)


def conversion_rate_note(db, values):
    try:
        stamp = str(values["SERSDATE"]).split(" ", 1)[0]
        period = Period.for_date(date.fromisoformat(stamp))
        rows = db.select(f"SELECT DR005 FROM {db.table(TABLE)} WHERE DR002 = ? AND DR003 = ? AND DR004 = ?",
                         (values["MONEY"], period.text, period.part))
        if len(rows) == 1:
            expected, actual = amount(rows[0][0]), amount(values["FOREX"])
            if expected != actual:
                return (f"每旬匯率提醒：{period.text} 第 {period.part} 旬 {values['MONEY']} 買進 {expected:.5f}，"
                        f"報單 FOREX {actual:.5f}")
    except Exception:
        # This optional lookup must also tolerate older test databases without the rate table.
        return None
    return None


def display_rates(values):
    return "、".join(f"買進 {buy}／賣出 {sell}" for buy, sell in values) or "無資料"


def report_text(plan):
    lines = [f"每旬匯率比對 {plan.period.text} 第 {plan.period.part} 旬"]
    for change in plan.changes:
        rate = change.rate
        lines.append(f"{change.action} {rate.code}｜{display_rates(change.previous)} → "
                     f"{display_rates(((rate.buy, rate.sell),))}")
    if plan.history_note:
        lines.append(plan.history_note)
    else:
        lines.append(f"最近 9 旬歷史比對不一致 {len(plan.history_differences)} 筆")
        for period, code, stored, expected in plan.history_differences:
            official = ((expected.buy, expected.sell),) if expected else ()
            lines.append(f"{period.text} 第 {period.part} 旬 {code}｜測試庫 {display_rates(stored)}｜公告 {display_rates(official)}")
    return "\n".join(lines) + "\n"


def run_rate_command(args):
    try:
        settings = Settings.load(args.config)
        plan = plan_rates(settings, files=args.from_files)
        report = report_text(plan)
        if args.command == "rate-simulate":
            args.output.mkdir(parents=True, exist_ok=True)
            path = args.output / f"每旬匯率模擬_{datetime.now():%Y%m%d_%H%M%S}.txt"
            with path.open("x", encoding="utf-8-sig") as stream:
                stream.write(report)
            print(path)
        else:
            print(report, end="")
            if all(change.action == "相同" for change in plan.changes):
                print("本旬匯率皆相同。")
                return 0
            if not args.yes and input("輸入 y 確認寫入測試庫：").strip().casefold() != "y":
                print("已取消匯率寫入。")
                return 0
            print(f"已寫入並核對 {save_rates(settings, plan)} 筆匯率。")
    except (EOFError, KeyboardInterrupt):
        print("已取消匯率作業。")
        return 1
    except RateError as error:
        print(f"匯率作業失敗：{error}")
        return 1
    except Exception:
        print("匯率作業失敗，請檢查本機檔案、輸出位置或測試資料庫設定。")
        return 1
    return 0
