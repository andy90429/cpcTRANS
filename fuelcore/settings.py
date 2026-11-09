import configparser
import json
import re
from dataclasses import dataclass
from pathlib import Path


def read_config(path):
    path = Path(path).resolve()
    if path.name.casefold() in {"txtrans.ini", "cpctrans.ini"}:
        raise ValueError(f"請使用 fuelcore 的設定檔：{path}")
    config = configparser.ConfigParser(interpolation=None)
    with path.open(encoding="utf-8-sig") as stream:
        try:
            config.read_file(stream)
        except configparser.Error as error:
            raise ValueError(f"INI 格式錯誤：{path}") from error
    return config


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


class Settings:
    def __init__(self, config, base):
        db = config["database"]
        if not db.getboolean("test_only", fallback=False):
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
        self.other = options["other"]
        self.countries = {key.upper(): tuple(value.split(maxsplit=1))
                          for key, value in config["buyer_countries"].items()}
        self.products = {key.upper(): json.loads(value)
                         for key, value in config["products"].items()}
        self.edi = EdiSettings.from_section(config["edi"]) if config.has_section("edi") else None

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        return cls(read_config(path), path.parent)
