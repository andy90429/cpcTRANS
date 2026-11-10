import csv
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

# PyInstaller places the bundled CSV beside this module under its runtime directory.
SCHEMA = Path(__file__).resolve().with_name("schema.csv")
CODE_TABLES = {
    "mybuyer": "BY002 varchar(10) PRIMARY KEY, BY003 varchar(200), "
               "BY012 varchar(2), BY013 varchar(50), BY015 varchar(6), "
               "BY017 varchar(5), BY018 varchar(50)",
    "myshipname": "SN001 {identity}, SN002 varchar(10), "
                  "SN003 varchar(40), SN004 varchar(25)",
    "mydeclarerate": "DR001 {identity}, DR002 varchar(3) NOT NULL, DR003 varchar(6) NOT NULL, "
                     "DR004 int NOT NULL, DR005 {rate} NOT NULL, DR006 {rate} NOT NULL, "
                     "UNIQUE (DR003, DR004, DR002)",
}


def identifier(name):
    return "`" + name.replace("`", "``") + "`"


def read_schema(path=SCHEMA):
    tables = defaultdict(dict)
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            kind = row["COLUMN_TYPE"]
            if not re.fullmatch(r"(?:varchar\(\d+\)|(?:bigint|int)\(\d+\)|"
                                r"(?:decimal|double)\(\d+,\d+\)|datetime|longtext|mediumtext)", kind):
                raise ValueError("無法識別的欄位型別 " + kind)
            tables[row["TABLE_NAME"]][row["COLUMN_NAME"]] = row
    return dict(tables)


def sqlite_type(kind):
    if kind.startswith(("int(", "bigint(")):
        return "INTEGER"
    if kind.startswith(("decimal(", "double(", "datetime")):
        # The leading TEXT keeps SQLite's text affinity, so stored values stay exactly as written
        # (33.5000 is not turned into 33.5). The rest records the CBS type for tools that read the
        # declared type, such as the local logic checker.
        return "TEXT " + kind.upper()
    return "TEXT"


def statements(settings, path=SCHEMA):
    mysql = settings.driver == "mysql"
    if mysql:
        for database in (settings.database, settings.code_database):
            yield "CREATE DATABASE IF NOT EXISTS " + identifier(database) + " DEFAULT CHARACTER SET utf8"
        yield "USE " + identifier(settings.database)
    suffix = " ENGINE=InnoDB DEFAULT CHARSET=utf8" if mysql else ""
    for table, columns in read_schema(path).items():
        definitions, primary = [], []
        for column, row in columns.items():
            kind = row["COLUMN_TYPE"]
            identity = row["EXTRA"] == "auto_increment"
            if not mysql:
                kind = sqlite_type(kind)
            definition = identifier(column) + " " + kind
            if row["IS_NULLABLE"] == "NO":
                definition += " NOT NULL"
            if identity:
                definition += " AUTO_INCREMENT" if mysql else " PRIMARY KEY AUTOINCREMENT"
            if row["COLUMN_KEY"] == "PRI" and (mysql or not identity):
                primary.append(identifier(column))
            definitions.append(definition)
        if primary:
            definitions.append("PRIMARY KEY (" + ", ".join(primary) + ")")
        yield "CREATE TABLE IF NOT EXISTS " + identifier(table) + " (" + ", ".join(definitions) + ")" + suffix
    identity = "int NOT NULL AUTO_INCREMENT PRIMARY KEY" if mysql else "INTEGER PRIMARY KEY AUTOINCREMENT"
    rate = "decimal(15,5)" if mysql else sqlite_type("decimal(15,5)")
    for table, definition in CODE_TABLES.items():
        name = identifier(table)
        if mysql:
            name = identifier(settings.code_database) + "." + name
        yield "CREATE TABLE IF NOT EXISTS " + name + " (" + definition.format(identity=identity, rate=rate) + ")" + suffix


def init_sqlite(settings, path=SCHEMA):
    if settings.driver != "sqlite":
        raise ValueError("此工具只直接建立 SQLite")
    with closing(sqlite3.connect(str(settings.path))) as db:
        db.executescript("BEGIN;\n" + ";\n".join(statements(settings, path)) + ";\nCOMMIT;")
