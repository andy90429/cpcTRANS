import sqlite3
from contextlib import closing

from .schema import CODE_TABLES, identifier, read_schema


class Database:
    def __init__(self, settings, connection=None, *, read_only=False):
        self.mysql = settings.driver == "mysql"
        self.settings = settings
        self.read_only = read_only
        self.owns_connection = connection is None
        if connection is not None:
            self.connection = connection
        elif self.mysql:
            import pymysql
            self.connection = pymysql.connect(**settings.mysql, charset="utf8", autocommit=True)
        else:
            uri = settings.path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw")
            self.connection = sqlite3.connect(uri, uri=True, isolation_level=None)

    def close(self):
        if self.owns_connection:
            self.connection.close()

    def table(self, name, schema=None):
        if self.mysql:
            if schema is None and name in CODE_TABLES:
                schema = self.settings.code_database
            if schema is not None:
                return identifier(schema) + "." + identifier(name)
        return identifier(name)

    def select(self, sql, params=()):
        self.require_select(sql)
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql.replace("?", "%s") if self.mysql else sql, params)
            return cursor.fetchall()

    def select_dicts(self, sql, params=()):
        self.require_select(sql)
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql.replace("?", "%s") if self.mysql else sql, params)
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def table_names(self, schema=None):
        if self.mysql:
            rows = self.select("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = ?", (schema,))
        else:
            rows = self.select("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        return {row[0].casefold(): row[0] for row in rows}

    def require_select(self, sql):
        if self.read_only and not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("唯讀資料庫只接受 SELECT")

    def require_writable(self):
        if self.read_only:
            raise ValueError("唯讀資料庫禁止寫入或控制交易")

    def execute(self, sql, params=()):
        self.require_writable()
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql.replace("?", "%s") if self.mysql else sql, params)
            return cursor.lastrowid

    def insert(self, table, values, *, schema=None):
        columns = ", ".join(map(identifier, values))
        markers = ", ".join("?" for _ in values)
        return self.execute(f"INSERT INTO {self.table(table, schema)} ({columns}) VALUES ({markers})",
                            tuple(values.values()))

    def begin(self):
        if not self.mysql and self.connection.in_transaction:
            raise ValueError("SQLite 連線已有進行中的交易，請先提交或回滾")
        self.execute("START TRANSACTION" if self.mysql else "BEGIN IMMEDIATE")

    def check_engines(self, code_tables=CODE_TABLES):
        if not self.mysql:
            return
        tables = [(self.settings.database, table) for table in read_schema()]
        tables.extend((self.settings.code_database, table) for table in code_tables)
        self.require_transactional_tables(tables)

    def require_transactional_tables(self, tables):
        if not self.mysql or not tables:
            return
        schemas = sorted({schema for schema, _ in tables})
        markers = ", ".join("?" for _ in schemas)
        rows = self.select(
            "SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA IN ({markers})", schemas,
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
        self.require_writable()
        self.connection.commit()

    def rollback(self):
        self.require_writable()
        self.connection.rollback()
