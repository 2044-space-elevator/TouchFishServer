"""
数据库 translator aka YWD2023fixXSFXer
qwq
为了防止 xsfx do not know how to write SQL，now we have GoogleTranslate(for SQL!) 
"""
from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod



class CursorProxy:
    """假的 cursor.ai 对象"""

    def __init__(self, real_cursor, dialect: "SQLDialect", connection):
        self._cursor = real_cursor
        self._dialect = dialect
        self._connection = connection
        self._last_sql = ""
        self._last_rowcount = 0
        self._last_sql_upper = ""

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._dialect.lastrowid(self._cursor,
                                        rowcount=self._last_rowcount,
                                        sql_upper=self._last_sql_upper,
                                        connection=self._connection)

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        self._cursor.close()

    def __getattr__(self, name):
        """getattr"""
        return getattr(self._cursor, name)

    def execute(self, sql: str, params=None):
        self._dialect._learn_schema(sql)
        translated = self._dialect.translate_sql(sql)
        self._last_sql = sql
        self._last_sql_upper = sql.upper()
        try:
            if params is not None:
                self._cursor.execute(translated, params)
            else:
                self._cursor.execute(translated)
        except Exception as e:
            code = getattr(e, 'args', [None])[0] if e.args else None
            if code in self._dialect.ignorable_error_codes:
                return self
            raise
        self._last_rowcount = self._cursor.rowcount
        return self

    def executemany(self, sql: str, params_list):
        self._dialect._learn_schema(sql)
        translated = self._dialect.translate_sql(sql)
        self._last_sql = sql
        self._last_sql_upper = sql.upper()
        try:
            self._cursor.executemany(translated, params_list)
        except Exception as e:
            code = getattr(e, 'args', [None])[0] if e.args else None
            if code in self._dialect.ignorable_error_codes:
                return self
            raise
        self._last_rowcount = self._cursor.rowcount
        return self


class SQLDialect(ABC):
    """S  Q  L  A  l  c  h  e  m  y"""

    placeholder: str = "?"
    paramstyle: str = "qmark"

    retryable_error_types: tuple = ()
    IntegrityError: type = Exception
    DatabaseError: type = Exception
    ignorable_error_codes: tuple = ()  # 特定错误码静默忽略（如 MySQL 1061 重复索引）

    # PG SQL: i love you!
    table_pk_map: dict[str, list[str]]
    table_auto_pk_map: dict[str, str]

    def __init__(self):
        self.table_pk_map = {}
        self.table_auto_pk_map = {}
        self._schema_lock = threading.Lock()
        self._known_tables: set[str] = set()

    @abstractmethod
    def connect(self, dsn):
        """db connect"""

    def translate_sql(self, sql: str) -> str:
        """Google Translate for SQL"""
        return sql

    def lastrowid(self, cursor, rowcount: int = 0, sql_upper: str = "",
                  connection=None) -> int:
        """返回最后插入的行的 ID。"""
        return cursor.lastrowid if rowcount > 0 else 0

    def _learn_schema(self, sql: str):
        """解析 CREATE TABLE 语句以提取主键列，以便后续翻译。"""
        sql_upper = sql.upper().replace("\n", " ")
        m = re.match(
            r'\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)',
            sql_upper,
        )
        if not m:
            return
        table = m.group(1).lower()
        self._known_tables.add(table)

        # google search INTEGER PRIMARY KEY AUTOINCREMENT
        pk_matches = re.findall(
            r'(\w+)\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
            sql_upper,
        )
        if pk_matches:
            with self._schema_lock:
                self.table_pk_map[table] = [c.lower() for c in pk_matches]
                self.table_auto_pk_map[table] = pk_matches[0].lower()
            return

        # 查找 PRIMARY KEY(col1, col2, ...)
        m2 = re.search(r'PRIMARY\s+KEY\s*\(([^)]+)\)', sql_upper)
        if m2:
            cols = [c.strip().lower() for c in m2.group(1).split(',')]
            with self._schema_lock:
                self.table_pk_map[table] = cols
            return

        # 查找 col TYPE PRIMARY KEY (no AUTOINCREMENT)
        m3 = re.findall(r'(\w+)\s+(?:TEXT|INTEGER|INT|REAL|BLOB)\s+PRIMARY\s+KEY', sql_upper)
        if m3:
            with self._schema_lock:
                self.table_pk_map[table] = [c.lower() for c in m3]

    def _get_pk(self, table: str) -> list[str]:
        with self._schema_lock:
            return self.table_pk_map.get(table.lower(), [])

    def _placeholderize(self, sql: str) -> str:
        """Replace ? with the dialect's placeholder character."""
        if self.placeholder == "?":
            return sql
        return sql.replace("?", self.placeholder)


class SQLiteDialect(SQLDialect):
    placeholder = "?"
    paramstyle = "qmark"

    def __init__(self):
        super().__init__()
        import sqlite3
        self.retryable_error_types = (sqlite3.OperationalError,)
        self.IntegrityError = sqlite3.IntegrityError
        self.DatabaseError = sqlite3.DatabaseError

    def connect(self, dsn):
        import sqlite3
        conn = sqlite3.connect(dsn, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def translate_sql(self, sql: str) -> str:
        """SQLite: no translation needed."""
        return sql


class MySQLDialect(SQLDialect):
    placeholder = "%s"
    paramstyle = "format"

    def __init__(self):
        super().__init__()
        self._table_text_columns = {}
        try:
            import pymysql.err
            self.retryable_error_types = (
                pymysql.err.OperationalError,
            )
            self.IntegrityError = pymysql.err.IntegrityError
            self.DatabaseError = pymysql.err.MySQLError
            self.ignorable_error_codes = (1061,)  # Duplicate key name (index already exists)
        except ImportError:
            pass

    def connect(self, dsn):
        try:
            import pymysql
        except ImportError:
            raise ImportError(
                "pymysql is required for MySQL support. Install: pip install pymysql"
            )
        db_name = dsn.get("database", "touchfish_v5")
        try:
            conn = pymysql.connect(
                host=dsn.get("host", "localhost"),
                port=int(dsn.get("port", 3306)),
                user=dsn["user"],
                password=dsn["password"],
                database=db_name,
                charset="utf8mb4",
                autocommit=False,
            )
        except pymysql.err.OperationalError as e:
            if e.args[0] != 1049:
                raise
            sql = "CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(db_name)
            print("数据库 '{}' 不存在，自动执行: {}".format(db_name, sql))
            init_conn = pymysql.connect(
                host=dsn.get("host", "localhost"),
                port=int(dsn.get("port", 3306)),
                user=dsn["user"],
                password=dsn["password"],
                charset="utf8mb4",
                autocommit=True,
            )
            with init_conn.cursor() as c:
                c.execute(sql)
            init_conn.close()
            conn = pymysql.connect(
                host=dsn.get("host", "localhost"),
                port=int(dsn.get("port", 3306)),
                user=dsn["user"],
                password=dsn["password"],
                database=db_name,
                charset="utf8mb4",
                autocommit=False,
            )
        with conn.cursor() as c:
            c.execute("SELECT VERSION()")
            ver = c.fetchone()[0]
        major = int(ver.split(".")[0])
        if major < 8:
            conn.close()
            raise RuntimeError("TouchFish V5 需要 MySQL 8.0 或更高版本。")
        return conn

    def translate_sql(self, sql: str) -> str:
        sql = self._placeholderize(sql)
        sql = self._translate_keyed_text_columns(sql)

        # Strip DEFAULT from TEXT columns (MySQL doesn't allow defaults on TEXT/BLOB)
        sql = re.sub(
            r'\bTEXT\b(.*?)\s+DEFAULT\s+(?:\'[^\']*\'|\S+)',
            r'TEXT\1',
            sql,
            flags=re.IGNORECASE,
        )

        # INSERT OR REPLACE INTO ... → REPLACE INTO ...
        sql = re.sub(
            r'\bINSERT\s+OR\s+REPLACE\s+INTO\b',
            'REPLACE INTO',
            sql,
            flags=re.IGNORECASE,
        )

        # INSERT OR IGNORE INTO ... → INSERT IGNORE INTO ...
        sql = re.sub(
            r'\bINSERT\s+OR\s+IGNORE\s+INTO\b',
            'INSERT IGNORE INTO',
            sql,
            flags=re.IGNORECASE,
        )

        # ON CONFLICT(x) DO UPDATE SET c = EXCLUDED.c → AS new ON DUPLICATE KEY UPDATE c = new.c
        sql = re.sub(
            r'\)\s*\bON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET\b',
            ') AS new ON DUPLICATE KEY UPDATE',
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r'\bEXCLUDED\.(\w+)',
            r'new.\1',
            sql,
            flags=re.IGNORECASE,
        )

        # INSERT INTO ... DEFAULT VALUES  →  INSERT INTO ... () VALUES ()
        sql = re.sub(
            r'\bINSERT\s+INTO\s+(\w+)\s+DEFAULT\s+VALUES\b',
            r'INSERT INTO \1 () VALUES ()',
            sql,
            flags=re.IGNORECASE,
        )

        # DDL: AUTOINCREMENT → AUTO_INCREMENT
        sql = re.sub(
            r'\b(INT(?:EGER)?)\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
            r'\1 PRIMARY KEY AUTO_INCREMENT',
            sql,
            flags=re.IGNORECASE,
        )

        # DDL: strip COLLATE NOCASE (MySQL default collation is case-insensitive)
        sql = re.sub(r'\s+COLLATE\s+NOCASE\b', '', sql, flags=re.IGNORECASE)

        # Fix typo TEXT REAL → REAL
        sql = re.sub(r'\bTEXT\s+REAL\b', 'REAL', sql, flags=re.IGNORECASE)

        # CREATE [UNIQUE] INDEX IF NOT EXISTS → strip IF NOT EXISTS (MySQL < 8.0.29)
        sql = re.sub(
            r'\bCREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\b',
            r'CREATE \1INDEX',
            sql,
            flags=re.IGNORECASE,
        )

        # DROP INDEX IF EXISTS name → DROP INDEX name (MySQL doesn't support IF EXISTS for DROP INDEX)
        sql = re.sub(
            r'\bDROP\s+INDEX\s+IF\s+EXISTS\s+',
            'DROP INDEX ',
            sql,
            flags=re.IGNORECASE,
        )

        # some unique index: strip WHERE ... IS NOT NULL clause for CREATE INDEX statements (MySQL doesn't support partial indexes)
        if re.search(r'\bCREATE\s+(?:UNIQUE\s+)?INDEX\b', sql, re.IGNORECASE):
            sql = re.sub(
                r'\s+WHERE\s+(\w+)\s+IS\s+NOT\s+NULL\b',
                '',
                sql,
                flags=re.IGNORECASE,
            )

        # PRAGMA table_info(x) → information_schema 
        sql = self._translate_pragma(sql)

        # sqlite_master → information_schema
        sql = self._translate_sqlite_master(sql)

        # MySQL requires every derived table (subquery in FROM) to have an alias
        if re.search(r'\bFROM\s*\(\s*SELECT\b', sql, re.IGNORECASE):
            sql = re.sub(
                r'\)\s*\n\s*(WHERE|ORDER|GROUP|LIMIT|HAVING)\b',
                r') AS _sub\n\1',
                sql,
                flags=re.IGNORECASE,
            )

        # MySQL \ is a string escape char; double backslash in ESCAPE clause
        sql = sql.replace("ESCAPE '\\'", "ESCAPE '\\\\'")

        # Backtick known table names that may be MySQL reserved words (e.g. `groups`)
        for table in sorted(self._known_tables, key=len, reverse=True):
            sql = re.sub(
                r'(?<![`\'"])\b' + re.escape(table) + r'\b(?![`\'"])',
                '`' + table + '`',
                sql,
                flags=re.IGNORECASE,
            )

        return sql

    @staticmethod
    def _split_definitions(body: str) -> list[str]:
        definitions = []
        start = 0
        depth = 0
        for index, char in enumerate(body):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ',' and depth == 0:
                definitions.append(body[start:index])
                start = index + 1
        definitions.append(body[start:])
        return definitions

    def _translate_keyed_text_columns(self, sql: str) -> str:
        create = re.match(
            r'(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\()(.*)(\)\s*)$',
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if create:
            table = create.group(2).lower()
            definitions = self._split_definitions(create.group(3))
            text_columns = set()
            key_columns = set()
            for definition in definitions:
                column = re.match(r'\s*(\w+)\s+TEXT\b', definition, re.IGNORECASE)
                if column:
                    name = column.group(1).lower()
                    text_columns.add(name)
                    if re.search(r'\b(?:PRIMARY\s+KEY|UNIQUE)\b', definition, re.IGNORECASE):
                        key_columns.add(name)
                constraint = re.match(
                    r'\s*(?:CONSTRAINT\s+\w+\s+)?(?:PRIMARY\s+KEY|UNIQUE)\s*\(([^)]+)\)',
                    definition,
                    re.IGNORECASE,
                )
                if constraint:
                    key_columns.update(
                        part.strip().strip('`"').lower()
                        for part in constraint.group(1).split(',')
                    )
            for index, definition in enumerate(definitions):
                column = re.match(r'\s*(\w+)\s+TEXT\b', definition, re.IGNORECASE)
                if column and column.group(1).lower() in key_columns:
                    definitions[index] = re.sub(
                        r'\bTEXT\b', 'VARCHAR(255)', definition,
                        count=1, flags=re.IGNORECASE,
                    )
            self._table_text_columns[table] = text_columns - key_columns
            return create.group(1) + ','.join(definitions) + create.group(4)

        index = re.match(
            r'(\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s+(\w+)\s*\()(.*)(\).*)$',
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if not index:
            return sql
        text_columns = self._table_text_columns.get(index.group(2).lower(), set())
        columns = self._split_definitions(index.group(3))
        for column_index, column_definition in enumerate(columns):
            column = re.match(r'(\s*)(\w+)(\s+(?:ASC|DESC))?\s*$', column_definition, re.IGNORECASE)
            if column and column.group(2).lower() in text_columns:
                columns[column_index] = '{}{}(191){}'.format(
                    column.group(1), column.group(2), column.group(3) or '',
                )
        return index.group(1) + ','.join(columns) + index.group(4)

    def _translate_pragma(self, sql: str) -> str:
        m = re.match(
            r'\s*PRAGMA\s+table_info\((\w+)\)\s*',
            sql,
            re.IGNORECASE,
        )
        if not m:
            return sql
        return (
            "SELECT ordinal_position - 1 AS cid,"
            " column_name AS name, data_type AS type,"
            " CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS \"notnull\","
            " column_default AS dflt_value,"
            " CASE WHEN column_key = 'PRI' THEN 1 ELSE 0 END AS pk"
            " FROM information_schema.columns"
            " WHERE table_name = '{}' AND table_schema = DATABASE()"
            " ORDER BY ordinal_position"
        ).format(m.group(1))

    def _translate_sqlite_master(self, sql: str) -> str:
        """Translate SELECT name FROM sqlite_master ... GLOB ... queries."""
        upper = sql.upper()
        if "SQLITE_MASTER" not in upper:
            return sql
        m = re.search(
            r"GLOB\s+'U\[0-9]\*'",
            sql,
        )
        if not m:
            return sql
        sql = re.sub(
            r'\bSELECT\s+name\s+FROM\s+sqlite_master\b',
            'SELECT TABLE_NAME AS name FROM information_schema.tables',
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"\btype\s*=\s*'table'\s*AND\s*",
            "TABLE_TYPE = 'BASE TABLE' AND ",
            sql,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"\bname\s+GLOB\s+'U\[0-9]\*'",
            "TABLE_NAME REGEXP '^U[0-9]+$'",
            sql,
            flags=re.IGNORECASE,
        )


class PostgreSQLDialect(SQLDialect):
    placeholder = "%s"
    paramstyle = "format"

    def __init__(self):
        super().__init__()
        try:
            import psycopg2.errors
            import psycopg2
            self.retryable_error_types = (
                psycopg2.OperationalError,
                psycopg2.errors.SerializationFailure,
            )
            self.IntegrityError = psycopg2.errors.UniqueViolation
            self.DatabaseError = psycopg2.DatabaseError
        except ImportError:
            pass

    def connect(self, dsn):
        try:
            import psycopg2
            import psycopg2.extensions
        except ImportError:
            raise ImportError(
                "psycopg2 is required for PostgreSQL support. Install: pip install psycopg2-binary"
            )
        conn = psycopg2.connect(
            host=dsn.get("host", "localhost"),
            port=int(dsn.get("port", 5432)),
            user=dsn["user"],
            password=dsn["password"],
            dbname=dsn.get("database", "touchfish_v5"),
        )
        conn.set_isolation_level(
            psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED
        )
        conn.autocommit = False
        return conn

    def lastrowid(self, cursor, rowcount: int = 0, sql_upper: str = "",
                  connection=None) -> int:
        if rowcount <= 0:
            return 0
        insert = re.match(r'\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)', sql_upper)
        if not insert:
            return 0
        table = insert.group(1).lower()
        with self._schema_lock:
            column = self.table_auto_pk_map.get(table)
        if column is None:
            return 0
        cursor.execute(
            "SELECT CURRVAL(PG_GET_SERIAL_SEQUENCE(%s, %s))",
            (table, column),
        )
        return cursor.fetchone()[0]

    def translate_sql(self, sql: str) -> str:
        sql = self._placeholderize(sql)

        # INSERT OR REPLACE → INSERT ... ON CONFLICT (pk) DO UPDATE ...
        sql = self._insert_or_replace_pg(sql)

        # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
        sql = self._insert_or_ignore_pg(sql)

        # DDL: INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
        sql = re.sub(
            r'\bINT(?:EGER)?\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
            'SERIAL PRIMARY KEY',
            sql,
            flags=re.IGNORECASE,
        )

        # COLLATE NOCASE → case-insensitive comparisons (PG has no built-in CI collation)
        sql = re.sub(
            r'([\w.]+)\s*=\s*(%s)\s+COLLATE\s+NOCASE\b',
            r'LOWER(\1) = LOWER(\2)',
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r'([\w.]+)\s+LIKE\s+(%s)\s+COLLATE\s+NOCASE\b',
            r'\1 ILIKE \2',
            sql,
            flags=re.IGNORECASE,
        )
        # PG LIKE is case-sensitive by default (SQLite/MySQL default to CI); use ILIKE
        sql = re.sub(r'(?<!I)\bLIKE\b', 'ILIKE', sql, flags=re.IGNORECASE)

        # DDL: strip remaining COLLATE NOCASE (column definitions, constraints)
        sql = re.sub(r'\s+COLLATE\s+NOCASE\b', '', sql, flags=re.IGNORECASE)

        # Fix typo TEXT REAL → REAL
        sql = re.sub(r'\bTEXT\s+REAL\b', 'REAL', sql, flags=re.IGNORECASE)

        # REAL in PG is 4-byte single-precision; upgrade to DOUBLE PRECISION (8-byte)
        sql = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql, flags=re.IGNORECASE)

        # BOOLEAN → BOOLEAN (PG has native boolean, but lowercase)
        # PG supports BOOLEAN keyword

        # INSERT ... DEFAULT VALUES → works in PG, no change needed

        # CREATE INDEX IF NOT EXISTS → PG supports natively, no change
        # DROP INDEX IF EXISTS → PG supports natively
        # Partial indexes (WHERE clause)  PG supports natively

        # PRAGMA table_info → information_schema
        sql = self._translate_pragma(sql)

        # sqlite_master → pg_catalog
        sql = self._translate_sqlite_master(sql)

        return sql

    def _insert_or_replace_pg(self, sql: str) -> str:
        """INSERT OR REPLACE INTO t(c1,...) VALUES(...)/SELECT... → INSERT ... ON CONFLICT DO UPDATE"""
        m = re.match(
            r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]*)\)",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            return self._build_pg_upsert(m.group(1), m.group(2), "VALUES ({})".format(m.group(3)), sql)

        # Try SELECT pattern: INSERT OR REPLACE INTO t(cols) SELECT ...
        m2 = re.match(
            r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*(SELECT\b.+)",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if m2:
            return self._build_pg_upsert(m2.group(1), m2.group(2), m2.group(3), sql)

        return sql

    def _build_pg_upsert(self, table_name: str, cols_str: str, body: str,
                         _original_sql: str = "") -> str:
        table = table_name.lower()
        cols = [c.strip() for c in cols_str.split(",")]
        pk_cols = self._get_pk(table)
        if not pk_cols:
            pk_cols = [cols[0]]
        non_pk = [c for c in cols if c.lower() not in pk_cols]
        if not non_pk:
            return "INSERT INTO {} ({}) {} ON CONFLICT ({}) DO NOTHING".format(
                table_name, cols_str, body, ", ".join(pk_cols),
            )
        set_clause = ", ".join("{} = EXCLUDED.{}".format(c, c) for c in non_pk)
        return "INSERT INTO {} ({}) {} ON CONFLICT ({}) DO UPDATE SET {}".format(
            table_name, cols_str, body, ", ".join(pk_cols), set_clause,
        )

    def _insert_or_ignore_pg(self, sql: str) -> str:
        """INSERT OR IGNORE INTO ... → INSERT INTO ... ON CONFLICT DO NOTHING"""
        m = re.match(
            r"\bINSERT\s+OR\s+IGNORE\s+INTO\s+(\S.+)",
            sql,
            re.IGNORECASE,
        )
        if not m:
            return sql
        return "INSERT INTO " + m.group(1) + " ON CONFLICT DO NOTHING"

    def _translate_pragma(self, sql: str) -> str:
        m = re.match(
            r'\s*PRAGMA\s+table_info\((\w+)\)\s*',
            sql,
            re.IGNORECASE,
        )
        if not m:
            return sql
        return (
            "SELECT ordinal_position - 1 AS cid,"
            " column_name AS name, data_type AS type,"
            " CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS \"notnull\","
            " column_default AS dflt_value,"
            " CASE WHEN (SELECT COUNT(*) FROM information_schema.table_constraints"
            "   tc JOIN information_schema.key_column_usage kcu"
            "   ON tc.constraint_name = kcu.constraint_name"
            "   WHERE tc.table_name = '{}' AND kcu.column_name = c.column_name"
            "   AND tc.table_schema = CURRENT_SCHEMA()"
            "   AND kcu.table_schema = CURRENT_SCHEMA()"
            "   AND tc.constraint_type = 'PRIMARY KEY') > 0 THEN 1 ELSE 0 END AS pk"
            " FROM information_schema.columns c"
            " WHERE c.table_name = '{}' AND c.table_schema = CURRENT_SCHEMA()"
            " ORDER BY ordinal_position"
        ).format(m.group(1), m.group(1))

    def _translate_sqlite_master(self, sql: str) -> str:
        upper = sql.upper()
        if "SQLITE_MASTER" not in upper:
            return sql
        sql = re.sub(
            r'\bSELECT\s+name\s+FROM\s+sqlite_master\b',
            'SELECT tablename AS name FROM pg_catalog.pg_tables',
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            r"\bWHERE\s+type\s*=\s*'table'\s*AND\s*",
            "WHERE schemaname = CURRENT_SCHEMA() AND ",
            sql,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"\bname\s+GLOB\s+'U\[0-9]\*'",
            "tablename ~ '^U[0-9]+$'",
            sql,
        )
