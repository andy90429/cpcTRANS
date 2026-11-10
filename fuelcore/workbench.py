import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from .convert import MESSAGE, convert_file, declaration_number, read_txt
from .db import Database
from .logic_check import run_logic_check
from .schema import identifier, init_sqlite
from .settings import Settings, config_path, parse_config, read_config


PATH_FIELDS = ("txt_path", "cbsapp_path")
DB_FIELDS = ("driver", "host", "port", "user", "password", "database", "code_database")
DEFAULTS = {"driver": "sqlite", "port": "3306"}
PENDING = "未轉入"
IMPORTED = "已轉入"
FAILED = "失敗"
UNREADABLE = "無法讀取"
UNKNOWN = "待確認"


def config_source(path):
    return path if path.exists() else path.with_suffix(".ini.example")


def load_fields(path):
    config = read_config(config_source(path))
    values = {key: config.get("paths", key, fallback="") for key in PATH_FIELDS}
    values.update({key: config.get("database", key, fallback=DEFAULTS.get(key, ""))
                   for key in DB_FIELDS})
    values["check_after_convert"] = (
        "yes" if config.getboolean("workflow", "check_after_convert", fallback=True) else "no"
    )
    return values


def resolve_folder(base, field, value):
    if not value.strip():
        return None
    path = (base / value.strip()).resolve()
    if not path.is_dir():
        raise ValueError(f"{field}：找不到目錄 {path}")
    if field == "cbsapp_path" and not (path / "CBSAPP.exe").is_file():
        raise ValueError(f"{field}：找不到 {path / 'CBSAPP.exe'}")
    return path


def edited_config(path, values, *, check_paths=True):
    config = read_config(config_source(path))
    if not config.has_section("paths"):
        config.add_section("paths")
    for field in PATH_FIELDS:
        if check_paths:
            resolve_folder(path.parent, field, values[field])
        config["paths"][field] = values[field].strip()
    for field in DB_FIELDS:
        value = values[field]
        config["database"][field] = value if field == "password" else value.strip()
    if "check_after_convert" in values:
        if not config.has_section("workflow"):
            config.add_section("workflow")
        config["workflow"]["check_after_convert"] = values["check_after_convert"]
    if values["driver"] == "mysql":
        port = values["port"].strip()
        if not port.isdecimal() or len(port) > 5 or not 1 <= int(port) <= 65535:
            raise ValueError("port：請輸入 1 到 65535 之間的埠號")
    settings = Settings(config, path.parent)
    return config, settings


def save_fields(path, values):
    config, _ = edited_config(path, values)
    updates = {
        "paths": {key: config["paths"][key] for key in PATH_FIELDS},
        "database": {key: config["database"][key] for key in DB_FIELDS},
    }
    if "check_after_convert" in values:
        updates["workflow"] = {"check_after_convert": config["workflow"]["check_after_convert"]}
    save_options(path, updates)


def ini_layout(lines, config):
    options, ends = {}, {}
    section = key = None
    indent = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        current = len(line) - len(line.lstrip())
        if key is not None and current > indent:
            options[section][key].append(index)
        else:
            heading = config.SECTCRE.match(stripped)
            if heading:
                section, key = heading.group("header"), None
                options.setdefault(section, {})
            else:
                key = config.optionxform(re.split(r"[:=]", stripped, maxsplit=1)[0].rstrip())
                options[section][key] = [index]
                indent = current
        ends[section] = index
    return options, ends


def save_options(path, updates, *, replace_section=None):
    """Validate the candidate INI and atomically replace only the selected options."""
    path = config_path(path)
    source = config_path(config_source(path))
    original = source.read_bytes()
    encoding = "utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = original.decode(encoding)
    config = parse_config(text, source)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    options, ends = ini_layout(lines, config)
    indentation = [re.match(r"[^\S\r\n]*", line)[0] for line in lines]
    insertions = {}
    for section, values in updates.items():
        existing = options.get(section, {})
        changes = {config.optionxform(key): value for key, value in values.items()}
        if section == replace_section:
            changes.update((key, None) for key in existing.keys() - changes.keys())
        additions = []
        for key, value in changes.items():
            if key in existing and value == config[section][key]:
                continue
            anchor = existing.get(key) or (existing[next(reversed(existing))] if existing else None)
            indent = "" if anchor is None else indentation[anchor[0]]
            rendered = ""
            if value is not None:
                continuation = newline + indent + "    "
                value_text = value.replace("\n", continuation)
                rendered = f"{indent}{key} = {value_text}{newline}"
            if key in existing:
                start, *continued = existing[key]
                if continued:
                    continuation_lines = set(continued)
                    for index in range(start + 1, continued[-1] + 1):
                        if index in continuation_lines or not lines[index].strip():
                            lines[index] = ""
                lines[start] = rendered
            elif rendered:
                additions.append(rendered)
        if additions:
            if section in ends:
                index = ends[section] + 1
                insertions.setdefault(index, []).extend(additions)
            else:
                insertions.setdefault(len(lines), []).extend((newline, f"[{section}]{newline}", *additions))
    for index in sorted(insertions, reverse=True):
        if index and lines[index - 1] and not lines[index - 1].endswith(("\n", "\r")):
            lines[index - 1] += newline
        lines[index:index] = insertions[index]
    candidate = "".join(lines)
    saved = parse_config(candidate, path)
    Settings(saved, path.parent)
    for section, values in updates.items():
        if any(saved.get(section, key, fallback=None) != value for key, value in values.items()):
            raise ValueError("設定含有 INI 無法保留的內容，未儲存。")
    for section in config.sections():
        if section != replace_section:
            changed = {config.optionxform(key) for key in updates.get(section, {})}
            if any(saved.get(section, key, fallback=None) != value
                   for key, value in config.items(section) if key not in changed):
                raise ValueError("無法保留其他設定，未儲存。")
    if candidate != text or not path.exists():
        if (source != path and path.exists()) or source.read_bytes() != original:
            raise ValueError("設定檔已由其他程式更新，請重新載入後再儲存。")
        atomic_write_config(path, candidate.encode(encoding))


def atomic_write_config(path, data):
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, suffix=".ini", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(data)
        temp.replace(path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


@dataclass(frozen=True)
class InputFile:
    path: Path
    folder: str
    sersno: str = ""
    status: str = PENDING
    error: str = ""


def recent_folder(name, earliest, today):
    if len(name) != 8 or not name.isascii() or not name.isdecimal():
        return True
    try:
        day = date(int(name[:4]), int(name[4:6]), int(name[6:]))
    except ValueError:
        return True
    return earliest <= day <= today


def find_inputs(folder, extra=(), *, show_all=False, today=None, on_error=None):
    today = today or date.today()
    earliest = today - timedelta(days=59)
    paths = set(extra)
    if folder is not None:
        for parent, dirs, names in os.walk(folder, onerror=on_error):
            if not show_all:
                dirs[:] = [name for name in dirs if recent_folder(name, earliest, today)]
            paths.update(Path(parent) / name for name in names if Path(name).suffix.lower() == ".txt")
    files = {}
    for path in paths:
        if path.suffix.lower() != ".txt":
            continue
        label = path.parent.name
        if folder is not None:
            try:
                label = str(path.parent.relative_to(folder))
            except ValueError:
                pass
        modified = 0
        try:
            path = path.resolve()
            if path in files:
                continue
            modified = path.stat().st_mtime_ns
            header, _ = read_txt(path)
            file = InputFile(path, label, declaration_number(header))
        except (OSError, ValueError, RuntimeError) as error:
            file = InputFile(path, label, status=UNREADABLE, error=str(error))
        files[path] = modified, file
    ordered = sorted(files.values(), key=lambda entry: (-entry[0], str(entry[1].path).casefold()))
    return [file for _, file in ordered]


def scan_inputs(config_path, extra=(), failures=None, *, show_all=False, today=None):
    warnings = []
    folder = None
    config = None
    try:
        config = read_config(config_source(config_path))
        path = config.get("paths", "txt_path", fallback="")
        folder = resolve_folder(config_path.parent, "txt_path", path)
    except (OSError, ValueError) as error:
        warnings.append(str(error))
    files = find_inputs(folder, extra, show_all=show_all, today=today,
                        on_error=lambda error: warnings.append(f"無法掃描目錄：{error}"))
    if folder is not None and not files:
        warnings.append(f"未找到 TXT，掃描目錄：{folder}")
    imported = None
    if config is not None:
        try:
            settings = Settings(config, config_path.parent)
            imported = imported_numbers(settings, (file.sersno for file in files))
        except Exception as error:
            warnings.append(f"無法確認資料庫狀態：{error}")
    return folder, list(file_statuses(files, imported, failures or {})), warnings


def imported_numbers(settings, numbers):
    numbers = sorted(set(numbers) - {""})
    if not numbers or (settings.driver == "sqlite" and not settings.path.exists()):
        return set()
    imported = set()
    with closing(Database(settings)) as db:
        for start in range(0, len(numbers), 300):
            batch = numbers[start:start + 300]
            markers = ", ".join("?" for _ in batch)
            rows = db.select(f"SELECT SERSNO FROM cusdec WHERE MSGCODE = ? AND SERSNO IN ({markers})",
                             (MESSAGE, *batch))
            imported.update(row[0] for row in rows)
    return imported


def file_statuses(files, imported, failures):
    for file in files:
        if file.error:
            yield file
        elif imported is not None and file.sersno in imported:
            yield replace(file, status=IMPORTED)
        elif file.path in failures:
            yield replace(file, status=FAILED, error=failures[file.path])
        elif imported is None:
            yield replace(file, status=UNKNOWN)
        else:
            yield file


def prepare_database(settings):
    if settings.driver == "sqlite" and not settings.path.exists():
        init_sqlite(settings)


def convert_batch(paths, settings, notify):
    prepare_database(settings)
    successful = []
    for path in paths:
        result = convert_file(path, settings)
        notify(path, result)
        if result.ok:
            successful.append(result.sersno)
    run = run_logic_check(successful, settings, save=True) if successful and settings.check_after_convert else None
    return successful, run


def check_summaries(ids, settings):
    """Read the saved declaration names and item values for the result window."""
    summaries = {}
    try:
        with closing(Database(settings, read_only=True)) as db:
            for number in ids:
                key = (MESSAGE, number)
                headers = db.select("SELECT BANAME, BAID, BYNAME FROM cusdec WHERE MSGCODE = ? AND SERSNO = ?", key)
                if not headers:
                    continue
                items = db.select(
                    "SELECT `報單品名`, `貨品名稱`, `數量`, `數量單位` FROM invitem "
                    "WHERE MSGCODE = ? AND SERSNO = ? ORDER BY PINVORDER", key,
                )
                summaries[number] = (headers[0], items)
    except Exception:
        return summaries, "部分報單資料無法讀取，邏輯檢查結果仍可查看。"
    return summaries, ""


def test_database(settings):
    if settings.driver == "sqlite" and not settings.path.exists():
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("SELECT 1")
        return "SQLite 驗證成功，首次轉檔時會建立測試庫。"
    with closing(Database(settings)) as db:
        db.select("SELECT 1")
        if db.mysql:
            db.select("SHOW TABLES FROM " + identifier(settings.code_database))
    return "測試資料庫連線成功。"
