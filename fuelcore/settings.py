import configparser
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .ships import SHIP_FILENAME


def config_path(path):
    path = Path(path).resolve()
    if path.name.casefold() in {"txtrans.ini", "cpctrans.ini"}:
        raise ValueError(f"請使用 fuelcore 的設定檔：{path}")
    return path


def parse_config(text, path):
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read_string(text, source=str(path))
    except configparser.Error as error:
        raise ValueError(f"INI 格式錯誤：{path}") from error
    return config


def read_config(path):
    path = config_path(path)
    return parse_config(path.read_text(encoding="utf-8-sig"), path)


def reports_directory(config, base):
    directory = config.get("print", "reports_dir", fallback="").strip()
    return Path(base) / directory if directory else None


def read_reports_directory(path):
    """Read the print path independently of the database settings."""
    try:
        config = read_config(path)
    except (OSError, ValueError):
        return None
    return reports_directory(config, Path(path).resolve().parent)


OTHER_FIELDS = ("OTHER", "EDIOTHER")


def other_fields(value):
    fields = tuple(dict.fromkeys(field.strip().upper() for field in value.split(",") if field.strip()))
    if any(field not in OTHER_FIELDS for field in fields):
        raise ValueError("其他申報事項的寫入欄位只接受 OTHER、EDIOTHER。")
    return fields


def validate_test_database(name):
    if not re.fullmatch(r"(?:test_[a-zA-Z0-9_]+|[a-zA-Z0-9_]+_test(?:_code)?)", name):
        raise ValueError("MySQL 資料庫名稱必須標示 test")


@dataclass(frozen=True)
class EdiSettings:
    edi_database: str
    xmlset_database: str
    company_code: str
    user: str

    @classmethod
    def from_section(cls, section):
        values = {key: section.get(key, "").strip() for key in cls.__annotations__}
        for key in ("edi_database", "xmlset_database"):
            validate_test_database(values[key])
        if not re.fullmatch(r"[0-9]{8}", values["company_code"]):
            raise ValueError("[edi] company_code 必須是 8 碼數字")
        if not values["user"]:
            raise ValueError("[edi] user 不可留空")
        return cls(**values)


@dataclass(frozen=True)
class LogicCheckSettings:
    plugin_dir: Path = None
    python_exe: str = ""
    timeout_seconds: int = 120
    rules_database: str = ""

    @classmethod
    def load(cls, config, base):
        section = "logic_check"
        directory = config.get(section, "plugin_dir", fallback="").strip()
        executable = config.get(section, "python_exe", fallback="").strip()
        if executable and ("/" in executable or "\\" in executable):
            executable = str(Path(base) / executable)
        timeout = config.getint(section, "timeout_seconds", fallback=120)
        if timeout <= 0:
            raise ValueError("[logic_check] timeout_seconds 必須大於 0")
        return cls(Path(base) / directory if directory else None, executable, timeout,
                   config.get(section, "rules_database", fallback="").strip())


@dataclass(frozen=True)
class NotifySettings:
    base: Path
    enabled: bool
    c3: bool
    c1a18: bool
    poll_seconds: int
    c3_pdfs: tuple
    c1a18_pdfs: tuple
    print_command: str

    @classmethod
    def load(cls, config, base):
        section = "notify"
        seconds = config.getint(section, "poll_seconds", fallback=60)
        if seconds <= 0:
            raise ValueError("[notify] poll_seconds 必須大於 0")

        def paths(key):
            return tuple(line.strip() for line in config.get(section, key, fallback="").splitlines() if line.strip())

        return cls(Path(base), config.getboolean(section, "enabled", fallback=True),
                   config.getboolean(section, "c3", fallback=True),
                   config.getboolean(section, "c1a18", fallback=True),
                   seconds, paths("c3_pdfs"), paths("c1a18_pdfs"),
                   config.get(section, "print_command", fallback="").strip())


class Settings:
    def __init__(self, config, base):
        self.ships_path = Path(base) / SHIP_FILENAME
        db = config["database"]
        self.test_only = db.getboolean("test_only", fallback=False)
        if not self.test_only:
            raise ValueError("必須設定 test_only = yes")
        self.driver = db["driver"]
        if self.driver == "sqlite":
            self.path = Path(base) / db["path"]
        elif self.driver == "mysql":
            self.database = db["database"]
            self.code_database = db["code_database"]
            for name in (self.database, self.code_database):
                validate_test_database(name)
            if self.database == self.code_database or not db["host"].strip():
                raise ValueError("請設定測試主機及兩個不同的測試資料庫")
            self.mysql = dict(host=db["host"], port=db.getint("port", 3306),
                              user=db["user"], password=db["password"],
                              database=self.database)
        else:
            raise ValueError("driver 只接受 sqlite 或 mysql")
        options = config["conversion"]
        self.user_code = options.get("user_code", "USER1")
        self.cusboxno = options["cusboxno"]
        self.write_codes = options.getboolean("write_codes", True)
        self.other = (json.loads(config["other"]["text_json"]) if config.has_option("other", "text_json")
                      else options["other"])
        if not isinstance(self.other, str):
            raise ValueError("其他申報事項必須是文字。")
        self.other_enabled = config.getboolean("other", "enabled", fallback=True)
        self.other_fields = other_fields(config.get("other", "fields", fallback=",".join(OTHER_FIELDS)))
        self.countries = {key.upper(): tuple(value.split(maxsplit=1))
                          for key, value in config["buyer_countries"].items()}
        self.products = {key.upper(): json.loads(value)
                         for key, value in config["products"].items()}
        self.edi = (EdiSettings.from_section(config["edi"])
                    if config.has_section("edi") and config.getboolean("edi", "enabled", fallback=True) else None)
        self.printer = config.get("print", "printer", fallback="").strip()
        self.reports_dir = reports_directory(config, base)
        self.logic_check = LogicCheckSettings.load(config, base)
        self.check_after_convert = config.getboolean("workflow", "check_after_convert", fallback=True)
        self.archive_after_convert = config.getboolean("workflow", "archive_after_convert", fallback=True)
        txt_path = config.get("paths", "txt_path", fallback="").strip()
        self.txt_source = Path(base) / txt_path if txt_path else None
        self.notify = NotifySettings.load(config, base)
        self.admin_password = config.get("app", "admin_password", fallback="")
        self.cleanup_days = {key: config.getint("cleanup", key + "_days", fallback=default)
                             for key, default in (("print", 30), ("diag", 30), ("generated", 7), ("correction", 90))}
        if any(value < 0 for value in self.cleanup_days.values()):
            raise ValueError("[cleanup] 天數不可小於 0。")

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        return cls(read_config(path), path.parent)
