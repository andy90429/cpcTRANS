"""Run the local CBS checker over its JSON subprocess interface."""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional, Tuple

from .db import Database
from .settings import validate_test_database


@dataclass(frozen=True)
class CheckMessage:
    level: str
    rule_id: str
    field: str
    item: Optional[int]
    text: str


@dataclass(frozen=True)
class CheckResult:
    sersno: str
    status: str
    rules_total: int = 0
    rules_unparsed: int = 0
    basic_passed: Optional[bool] = None
    item_passed: Optional[bool] = None
    saved: bool = False
    messages: Tuple[CheckMessage, ...] = ()

    @property
    def passed(self):
        return self.status == "passed" and self.rules_unparsed == 0

    @property
    def label(self):
        if self.passed:
            return "✓ 通過"
        return "未檢查" if self.status == "error" else "✗ 未通過"


@dataclass(frozen=True)
class CheckRun:
    plugin_version: str
    results: Tuple[CheckResult, ...]

    def to_json(self):
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def errors(ids, message):
    detail = CheckMessage("error", "", "", None, message)
    return CheckRun("", tuple(CheckResult(number, "error", messages=(detail,)) for number in ids))


def connection_info(settings, save):
    if not settings.test_only:
        raise ValueError("邏輯檢查必須設定 test_only = yes")
    if settings.driver == "sqlite":
        path = settings.path.resolve()
        if save:
            validate_test_database(path.stem)
        return {"driver": "sqlite", "test_only": True, "path": str(path)}
    if settings.driver != "mysql":
        raise ValueError("邏輯檢查只接受 SQLite 或 MySQL")
    rules = settings.logic_check.rules_database
    names = {"declarations": settings.database, "codes": settings.code_database, "rules": rules}
    if settings.edi:
        names["xml"] = settings.edi.xmlset_database
    for name in names.values():
        validate_test_database(name)
    if len({name.casefold() for name in names.values()}) != len(names):
        raise ValueError("邏輯檢查的報單、代碼、規則與 XML 測試庫須分開")
    mysql = {key: settings.mysql[key] for key in ("host", "port", "user", "password")}
    return {"driver": "mysql", "test_only": True, "schemas": names, "mysql": mysql}


def parse_run(payload, ids, save, secret):
    def string(value):
        if not isinstance(value, str):
            raise ValueError
        return value.replace(secret, "〈已遮蔽〉") if secret else value

    raw = json.loads(payload)
    version = string(raw["plugin_version"])
    if not version or not isinstance(raw["results"], list):
        raise ValueError
    results = []
    for row in raw["results"]:
        sersno, status = row["sersno"], row["status"]
        total, unparsed = row["rules_total"], row["rules_unparsed"]
        basic, item, saved = row["basic_passed"], row["item_passed"], row["saved"]
        if (not isinstance(sersno, str) or status not in ("passed", "failed", "error")
                or type(total) is not int or total < 0 or type(unparsed) is not int or unparsed < 0
                or any(flag is not None and type(flag) is not bool for flag in (basic, item))
                or type(saved) is not bool or (saved and not save) or not isinstance(row["messages"], list)):
            raise ValueError
        messages = []
        for msg in row["messages"]:
            level, index = msg["level"], msg["item"]
            if level not in ("warning", "error") or (index is not None and (type(index) is not int or index < 0)):
                raise ValueError
            messages.append(CheckMessage(level, string(msg["rule_id"]), string(msg["field"]), index, string(msg["text"])))
        result = CheckResult(sersno, status, total, unparsed, basic, item, saved, tuple(messages))
        if status == "passed" and not result.passed:
            result = replace(result, status="failed")
        results.append(result)
    if Counter(result.sersno for result in results) != Counter(ids):
        return errors(ids, "外掛回傳的報單號碼與要求不符")
    by_number = {result.sersno: result for result in results}
    return CheckRun(version, tuple(by_number[number] for number in ids))


def run_logic_check(sersnos, settings, save=False):
    from .runtime import record

    run = _run_logic_check(sersnos, settings, save)
    for status in ("passed", "failed", "error"):
        count = sum(result.status == status for result in run.results)
        if count:
            record("logic_check_save" if save else "logic_check", status, count=count)
    return run


def _run_logic_check(sersnos, settings, save):
    ids = tuple(dict.fromkeys(number.strip().upper() for number in sersnos))
    if not ids or any(not re.fullmatch(r"[A-Z0-9]{14}", number) for number in ids):
        return errors(ids, "請提供 14 碼英數報單號碼")
    try:
        config = connection_info(settings, save)
    except (OSError, ValueError, KeyError):
        return errors(ids, "邏輯檢查拒絕連線。請確認 test_only = yes、測試庫名稱與 [logic_check] 規則庫設定。")
    if not save:
        return _run_plugin(ids, settings, save, config)
    try:
        with closing(Database(settings, read_only=True)) as db:
            states = {}
            for start in range(0, len(ids), 300):
                batch = ids[start:start + 300]
                markers = ", ".join("?" for _ in batch)
                rows = db.select(f"SELECT SERSNO, STATUS FROM cusdec WHERE MSGCODE = ? AND SERSNO IN ({markers})",
                                 ("5203S", *batch))
                states.update((number.upper(), (status or "").strip()) for number, status in rows)
    except Exception:
        return errors(ids, "無法確認測試庫報單狀態，已拒絕寫回。")
    allowed, rejected = [], {}
    for number in ids:
        if number not in states:
            message = "測試庫找不到這張報單，已拒絕寫回。"
        elif states[number] not in ("Y", "0", ""):
            message = "報單已送出、已有回覆或狀態不允許，已拒絕寫回邏輯檢查結果。"
        else:
            allowed.append(number)
            continue
        rejected[number] = errors((number,), message).results[0]
    run = _run_plugin(allowed, settings, save, config) if allowed else CheckRun("", ())
    by_number = {result.sersno: result for result in run.results}
    by_number.update(rejected)
    return CheckRun(run.plugin_version, tuple(by_number[number] for number in ids))


def _run_plugin(ids, settings, save, config):
    options = settings.logic_check
    plugin = os.environ.get("FUELCORE_LOGIC_PLUGIN") or options.plugin_dir
    script = Path(plugin).resolve() / "run_check.py" if plugin else None
    if script is None or not script.is_file():
        return errors(ids, "找不到本機邏輯檢查外掛 run_check.py")
    if not options.python_exe and getattr(sys, "frozen", False):
        return errors(ids, "封裝版請在 [logic_check] 指定 python_exe")
    path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", prefix="fuelcore-check-", suffix=".json", delete=False) as temp:
            path = Path(temp.name).resolve()
            json.dump(config, temp, ensure_ascii=False)
        cmd = [options.python_exe or sys.executable, "-E", "-s", "-B", str(script), "--sersno", *ids, "--db-json", str(path)]
        if save:
            cmd.append("--save")
        child = subprocess.run(cmd, cwd=script.parent, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, timeout=options.timeout_seconds, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        run = parse_run(child.stdout.decode("utf-8"), ids, save, config.get("mysql", {}).get("password", ""))
        if child.returncode and any(result.status != "error" for result in run.results):
            return errors(ids, "邏輯檢查外掛異常結束")
        return run
    except subprocess.TimeoutExpired:
        return errors(ids, "本機邏輯檢查逾時")
    except OSError:
        return errors(ids, "無法啟動本機邏輯檢查或建立暫存連線檔")
    except (ValueError, TypeError, KeyError, AttributeError):
        return errors(ids, "本機邏輯檢查回傳的 JSON 格式無效")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
