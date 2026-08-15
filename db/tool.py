import threading
import time

from db.dialect import CursorProxy, SQLiteDialect


class Db:
    def __init__(self, path_or_dsn, PORT_API, PORT_TCP,
                 WAL_mode=True, max_retries=3, dialect=None):
        self.path = path_or_dsn
        self.api_pt = PORT_API
        self.tcp_pt = PORT_TCP
        self.WAL_mode = WAL_mode
        self.max_retries = max_retries
        if dialect is None:
            dialect = SQLiteDialect()
        self.dialect = dialect
        # 写锁：串行化所有写操作。读操作在 WAL 模式下并发，不加这把锁。
        self.lock = threading.Lock()
        # 每线程一个独立连接，避免单连接 + 单锁把读也串行化。
        self._local = threading.local()
        # 初始化主线程连接（触发 WAL 等 PRAGMA）。
        _ = self.conn

    def _init_thread(self):
        conn = self.dialect.connect(self.path)
        self._local.conn = conn
        self._local.cursor = CursorProxy(conn.cursor(), self.dialect, conn)

    @property
    def conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._init_thread()
        return self._local.conn

    @property
    def cursor(self):
        if not hasattr(self._local, 'cursor') or self._local.cursor is None:
            self._init_thread()
        return self._local.cursor

    @property
    def IntegrityError(self):
        return self.dialect.IntegrityError

    def _reconnect(self):
        try:
            if hasattr(self._local, 'conn') and self._local.conn:
                self._local.conn.close()
        except Exception:
            pass
        self._local.conn = None
        self._local.cursor = None
        self._init_thread()

    def _execute_with_retry(self, db_operation, *args, **kwargs):
        error_types = self.dialect.retryable_error_types or (
            self.dialect.DatabaseError,
        )
        for attempt in range(self.max_retries):
            try:
                return db_operation(*args, **kwargs)
            except error_types:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                if attempt == self.max_retries - 1:
                    raise
                self._reconnect()
                time.sleep(0.1)
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise

    def update(self, command: str, parameters: list):
        with self.lock:
            def operation():
                self.cursor.executemany(command, parameters)
                self.conn.commit()
            self._execute_with_retry(operation)

    def query(self, command: str, parameters: tuple = None):
        def operation():
            if parameters:
                self.cursor.execute(command, parameters)
            else:
                self.cursor.execute(command)
            result = self.cursor.fetchall()
            self.conn.commit()  # 关闭读事务，释放 REPEATABLE READ 快照
            return result
        return self._execute_with_retry(operation)

    def execute(self, command: str, parameters: tuple = None):
        with self.lock:
            def operation():
                if parameters:
                    self.cursor.execute(command, parameters)
                else:
                    self.cursor.execute(command)
                lastrowid = self.cursor.lastrowid
                self.conn.commit()
                return lastrowid
            return self._execute_with_retry(operation)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if hasattr(self._local, 'conn') and self._local.conn:
                self._local.conn.close()
        except Exception:
            pass