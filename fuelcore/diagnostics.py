"""Read-only installation checks and optional comparison of a legacy Rpx."""

import os
import struct
import sys
from contextlib import closing
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .declare_rate import require_test
from .report import validate_report_assets
from .report.comparison import compare_fronts
from .report.editing import rpx_number
from .report.rpx import RpxDocument, RpxError
from .runtime import app_version
from .schema import read_schema
from .settings import Settings, read_reports_directory
from .workbench import config_source


@dataclass(frozen=True)
class Diagnostics:
    summary: str
    details: str


def table_check(settings):
    with closing(Database(settings, read_only=True)) as db:
        names = db.table_names(getattr(settings, "database", None))
        missing = sorted(set(read_schema()) - names.keys())
    return "測試庫缺少報單表 " + "、".join(missing) if missing else "測試庫唯讀連線與報單表正常"


def collect_diagnostics(config_path, old_rpx=None):
    config_path = Path(config_path)
    lines = [f"fuelcore {app_version()} 上機診斷", f"環境 {sys.platform} / {struct.calcsize('P') * 8} 位元",
             "舊版差異範圍請見 CPCtrans-comparison.md。"]
    try:
        schema_count = len(read_schema())
        lines.append(f"封裝資料表規格 {schema_count} 張")
    except (OSError, ValueError, KeyError):
        lines.append("無法載入封裝資料表規格")
    settings = None
    database_ready = False
    try:
        settings = Settings.load(config_source(config_path))
        require_test(settings)
    except (OSError, ValueError, KeyError):
        settings = None
        lines.append("設定檔無法讀取，或資料庫未通過測試庫檢查")
    if settings:
        lines.append(f"設定已載入（{settings.driver} 測試庫）")
        if settings.driver == "mysql":
            settings = copy(settings)
            settings.mysql = dict(settings.mysql, connect_timeout=5, read_timeout=5, write_timeout=5)
        try:
            lines.append(table_check(settings))
            database_ready = True
        except Exception:
            lines.append("測試庫唯讀檢查失敗，請核對本機設定與資料庫是否已建立")
        plugin = os.environ.get("FUELCORE_LOGIC_PLUGIN") or settings.logic_check.plugin_dir
        try:
            installed = bool(plugin and (Path(plugin) / "run_check.py").is_file())
        except OSError:
            installed = False
        lines.append("F6 本機外掛：" + ("已找到入口" if installed else "未找到入口"))
        lines.append("EDI 測試庫：" + ("已設定" if settings.edi else "未設定"))
        lines.append("報單印表機：" + ("已指定" if settings.printer else "未指定"))
    try:
        validate_report_assets(read_reports_directory(config_source(config_path)))
    except (OSError, ValueError):
        lines.append("CBS 報表資源目錄：未設定或缺少可讀取的範本及底圖")
    else:
        lines.append("CBS 報表資源目錄：可讀取")
    details = []
    if old_rpx and database_ready:
        try:
            document = RpxDocument.read(Path(old_rpx).read_bytes())
            fronts = [page for page in document.roots if page.class_name == "TGPage"]
            numbers = [rpx_number(page) for page in fronts]
            if not numbers:
                raise RpxError("Rpx 無正面")
            differences, warnings = compare_fronts(numbers, fronts, settings)
            lines.append(f"舊版 Rpx 共 {len(numbers)} 張")
            if warnings:
                lines.append("有報單無法核對，請確認已將相同報單轉入測試庫")
            for index, page in enumerate(differences, 1):
                if page:
                    lines.append(f"第 {index} 張差異欄位：" + "、".join(diff.label for diff in page))
                    details.extend(f"第 {index} 張 {diff.label}｜舊 Rpx {diff.rpx}｜測試庫 {diff.database}"
                                   for diff in page)
            lines.append(f"可比對的欄位差異共 {sum(map(len, differences))} 筆")
        except (OSError, ValueError):
            lines.append("無法讀取選取的舊版 Rpx 或辨識報單號碼")
    elif old_rpx:
        lines.append("測試庫無法使用，未比對舊版 Rpx")
    else:
        lines.append("本次未選擇舊版 Rpx")
    return Diagnostics("\n".join(lines) + "\n", "\n".join(details))
