import sqlite3
from contextlib import closing

from .schema import CODE_TABLES, identifier, read_schema


class Database:
    def __init__(self, settings, connection=None):
        self.mysql = settings.driver == "mysql"
        self.settings = settings
        self.owns_connection = connection is None
        if connection is not None:
            self.connection = connection
        elif self.mysql:
            import pymysql
            self.connection = pymysql.connect(**settings.mysql, charset="utf8", autocommit=True)
        else:
            uri = settings.path.resolve().as_uri() + "?mode=rw"
            self.connection = sqlite3.connect(uri, uri=True, isolation_level=None)

    def close(self):
        if self.owns_connection:
            self.connection.close()

    def table(self, name):
        if self.mysql and name in CODE_TABLES:
            return identifier(self.settings.code_database) + "." + identifier(name)
        return identifier(name)

    def select(self, sql, params=()):
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql.replace("?", "%s") if self.mysql else sql, params)
            return cursor.fetchall()

    def execute(self, sql, params=()):
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql.replace("?", "%s") if self.mysql else sql, params)
            return cursor.lastrowid

    def insert(self, table, values):
        columns = ", ".join(map(identifier, values))
        markers = ", ".join("?" for _ in values)
        return self.execute(f"INSERT INTO {self.table(table)} ({columns}) VALUES ({markers})",
                            tuple(values.values()))

    def begin(self):
        if not self.mysql and self.connection.in_transaction:
            raise ValueError("SQLite 連線已有進行中的交易，請先提交或回滾")
        self.execute("START TRANSACTION" if self.mysql else "BEGIN IMMEDIATE")

    def check_engines(self):
        if not self.mysql:
            return
        tables = [(self.settings.database, table) for table in read_schema()]
        tables.extend((self.settings.code_database, table) for table in CODE_TABLES)
        rows = self.select(
            "SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA IN (?, ?)",
            (self.settings.database, self.settings.code_database),
        )
        engines = {(schema, table): engine for schema, table, engine in rows}
        invalid = []
        for schema, table in tables:
            engine = engines.get((schema, table))
            if (engine or "").lower() != "innodb":
                invalid.append(f"{schema}.{table} ({engine or '查無引擎'})")
        if invalid:
            raise ValueError("測試資料表必須使用 InnoDB 才能完整回滾，請檢查 " + "、".join(invalid))

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()
