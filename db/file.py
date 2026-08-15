from db.tool import Db
import json
import time
import os

class FileDb(Db):
    def __init__(self, path : str, port_api : int, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self.port_api = port_api

    def create_file_db(self):
        columns = [row[1] for row in self.query("PRAGMA table_info(file)")]
        if columns and columns != ["hash"]:
            self.execute("ALTER TABLE file RENAME TO file_legacy_v5")
            self.execute("CREATE TABLE file (hash TEXT PRIMARY KEY)")
            self.execute("INSERT OR IGNORE INTO file(hash) SELECT hash FROM file_legacy_v5 WHERE hash IS NOT NULL")
        elif not columns:
            self.execute("CREATE TABLE file (hash TEXT PRIMARY KEY)")
        self.create_user_file_table()
        self.execute("DROP TABLE IF EXISTS file_legacy_v5")
        self.execute("""
            CREATE TABLE IF NOT EXISTS file_uploaders (
                hash TEXT NOT NULL, uid INTEGER NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(hash, uid)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS file_references (
                hash TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
                referrer_uid INTEGER, created_at REAL NOT NULL, last_referenced_at REAL NOT NULL,
                PRIMARY KEY(hash, source_type, source_id)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS file_gc (
                hash TEXT PRIMARY KEY, zero_references_at REAL NOT NULL
            )
        """)
        self.execute("""INSERT OR IGNORE INTO file_uploaders(hash, uid, created_at)
                        SELECT hash, uid, COALESCE(upload_time, ?) FROM user_file WHERE active = TRUE""", (time.time(),))

    def create_user_file_table(self):
        cmd = """
    CREATE TABLE IF NOT EXISTS user_file (
        uid INTEGER NOT NULL,
        hash TEXT NOT NULL,
        file_name TEXT,
        upload_time REAL,
        active BOOLEAN DEFAULT TRUE,
        PRIMARY KEY (uid, hash)
    )
    """
        self.execute(cmd)
        for col, typ in [("mime_type", "TEXT"), ("extension", "TEXT")]:
            try:
                self.execute("ALTER TABLE user_file ADD COLUMN {} {}".format(col, typ))
            except Exception:
                pass

    def register_upload(self, uid : int, hashes : str, file_name : str,
                        upload_time : float, size : int = 0,
                        mime_type : str = None, extension : str = None):
        """Atomically register a blob and this user's ownership."""
        with self.lock:
            def operation():
                self.cursor.execute("SELECT 1 FROM file WHERE hash = ?", (hashes,))
                file_exists = self.cursor.fetchone() is not None
                self.cursor.execute(
                    "SELECT active FROM user_file WHERE uid = ? AND hash = ?",
                    (uid, hashes),
                )
                ownership = self.cursor.fetchone()
                already_owned = ownership is not None and bool(ownership[0])

                if not file_exists:
                    self.cursor.execute("INSERT INTO file(hash) VALUES (?)", (hashes,))

                self.cursor.execute(
                    """INSERT INTO user_file
                       (uid, hash, file_name, upload_time, active, mime_type, extension)
                       VALUES (?, ?, ?, ?, TRUE, ?, ?)
                       ON CONFLICT(uid, hash) DO UPDATE SET
                           file_name = excluded.file_name,
                           upload_time = excluded.upload_time,
                           active = TRUE,
                           mime_type = excluded.mime_type,
                           extension = excluded.extension""",
                    (uid, hashes, file_name, upload_time, mime_type, extension),
                )
                self.cursor.execute(
                    "INSERT OR IGNORE INTO file_uploaders(hash, uid, created_at) VALUES (?, ?, ?)",
                    (hashes, uid, upload_time),
                )
                self.cursor.execute(
                    "INSERT OR IGNORE INTO file_gc(hash, zero_references_at) VALUES (?, ?)",
                    (hashes, upload_time),
                )
                self.conn.commit()
                return not file_exists, already_owned

            return self._execute_with_retry(operation)

    def add_reference(self, hashes: str, source_type: str, source_id: str, referrer_uid=None):
        """添加引用（引用避免被回收）"""
        now = time.time()
        with self.lock:
            def operation():
                self.cursor.execute("SELECT 1 FROM file WHERE hash = ?", (hashes,))
                if self.cursor.fetchone() is None:
                    return False
                self.cursor.execute(
                    """INSERT INTO file_references(hash, source_type, source_id, referrer_uid, created_at, last_referenced_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(hash, source_type, source_id) DO UPDATE SET
                         referrer_uid = excluded.referrer_uid, last_referenced_at = excluded.last_referenced_at""",
                    (hashes, source_type, str(source_id), referrer_uid, now, now),
                )
                self.cursor.execute("DELETE FROM file_gc WHERE hash = ?", (hashes,))
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def remove_reference(self, hashes: str, source_type: str, source_id: str):
        now = time.time()
        with self.lock:
            def operation():
                self.cursor.execute("DELETE FROM file_references WHERE hash = ? AND source_type = ? AND source_id = ?", (hashes, source_type, str(source_id)))
                self.cursor.execute("SELECT COUNT(*) FROM file_references WHERE hash = ?", (hashes,))
                if self.cursor.fetchone()[0] == 0:
                    self.cursor.execute("INSERT OR REPLACE INTO file_gc(hash, zero_references_at) VALUES (?, ?)", (hashes, now))
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def remove_uploader(self, hashes: str, uid: int):
        with self.lock:
            def operation():
                self.cursor.execute("DELETE FROM file_uploaders WHERE hash = ? AND uid = ?", (hashes, uid))
                self.cursor.execute("SELECT COUNT(*) FROM file_uploaders WHERE hash = ?", (hashes,))
                delete_now = self.cursor.fetchone()[0] == 0
                self.conn.commit()
                return delete_now
            return self._execute_with_retry(operation)

    def collect_expired_hashes(self, expiry_hours: float, zero_ref_seconds: float = 1800.0):
        """检查过期文件"""
        now = time.time()
        cutoff = now - max(float(expiry_hours), 0) * 3600
        rows = self.query("""
            SELECT f.hash FROM file f
            WHERE NOT EXISTS(SELECT 1 FROM file_uploaders u WHERE u.hash = f.hash)
               OR EXISTS(SELECT 1 FROM file_gc g WHERE g.hash = f.hash AND g.zero_references_at <= ?)
               OR EXISTS(SELECT 1 FROM file_references r WHERE r.hash = f.hash
                         GROUP BY r.hash HAVING MAX(r.last_referenced_at) <= ?)
        """, (now - zero_ref_seconds, cutoff))
        return [row[0] for row in rows]

    def delete_blob_relations(self, hashes: str):
        with self.lock:
            def operation():
                self.cursor.execute("DELETE FROM file_references WHERE hash = ?", (hashes,))
                self.cursor.execute("DELETE FROM file_uploaders WHERE hash = ?", (hashes,))
                self.cursor.execute("DELETE FROM file_gc WHERE hash = ?", (hashes,))
                self.cursor.execute("DELETE FROM user_file WHERE hash = ?", (hashes,))
                self.cursor.execute("DELETE FROM file WHERE hash = ?", (hashes,))
                self.conn.commit()
            return self._execute_with_retry(operation)

    def acquire_reference(self, uid : int, hashes : str):
        """获取内容引用"""
        with self.lock:
            def operation():
                self.cursor.execute(
                    """SELECT uf.file_name, uf.extension
                       FROM user_file uf JOIN file f ON f.hash = uf.hash
                       WHERE uf.uid = ? AND uf.hash = ? AND uf.active = TRUE""",
                    (uid, hashes),
                )
                row = self.cursor.fetchone()
                if row is None:
                    return None
                self.conn.commit()
                return {
                    "file_name": row[0],
                    "file_type": (row[1] or "").lstrip(".") or "unknown",
                    "extension": row[1] or "",
                    "size": self.get_file_size(hashes),
                }

            return self._execute_with_retry(operation)

    def acquire_forward_reference(self, hashes : str):
        """获取可转发的内容引用"""
        with self.lock:
            def operation():
                self.cursor.execute(
                    """SELECT uf.file_name, uf.extension FROM user_file uf
                       WHERE uf.hash = ? AND uf.active = TRUE ORDER BY uf.upload_time LIMIT 1""",
                    (hashes,),
                )
                row = self.cursor.fetchone()
                if row is None:
                    return None
                self.conn.commit()
                return {
                    "file_name": row[0],
                    "file_type": (row[1] or "").lstrip(".") or "unknown",
                    "extension": row[1] or "",
                    "size": self.get_file_size(hashes),
                }

            return self._execute_with_retry(operation)

    def tag_file(self, sender : int, file_name : str, send_time : str, hashes : str,
                 size : int = 0, mime_type : str = None, extension : str = None):
        self.execute("INSERT OR IGNORE INTO file(hash) VALUES (?)", (hashes,))

    def add_user_file(self, uid : int, hashes : str, file_name : str, upload_time : float):
        with self.lock:
            def operation():
                self.cursor.execute("SELECT * FROM user_file WHERE uid = ? AND hash = ?", (uid, hashes))
                existing = self.cursor.fetchone()
                if existing:
                    self.cursor.execute(
                        """UPDATE user_file SET active = TRUE, file_name = ?, upload_time = ?
                           WHERE uid = ? AND hash = ?""",
                        (file_name, upload_time, uid, hashes),
                    )
                else:
                    self.cursor.execute(
                        "INSERT INTO user_file (uid, hash, file_name, upload_time, active) VALUES (?, ?, ?, ?, TRUE)",
                        (uid, hashes, file_name, upload_time))
                self.conn.commit()
            return self._execute_with_retry(operation)

    def deactivate_user_file(self, uid : int, hashes : str):
        self.execute("UPDATE user_file SET active = FALSE WHERE uid = ? AND hash = ?", (uid, hashes))

    def delete_owned_user_file(self, uid : int, hashes : str):
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT 1 FROM user_file WHERE uid = ? AND hash = ? AND active = TRUE",
                    (uid, hashes),
                )
                if self.cursor.fetchone() is None:
                    return False, []

                self.cursor.execute(
                    "UPDATE user_file SET active = FALSE WHERE uid = ? AND hash = ?",
                    (uid, hashes),
                )
                self.cursor.execute("DELETE FROM file_uploaders WHERE hash = ? AND uid = ?", (hashes, uid))
                self.cursor.execute("SELECT COUNT(*) FROM file_uploaders WHERE hash = ?", (hashes,))
                deleted = [(hashes,)] if self.cursor.fetchone()[0] == 0 else []
                self.conn.commit()
                return True, deleted

            return self._execute_with_retry(operation)

    def get_user_files(self, uid : int):
        return self.query(
            "SELECT uf.hash, uf.file_name, uf.upload_time, 0, 0, 0, "
            "NULL, uf.extension "
            "FROM user_file uf JOIN file f ON uf.hash = f.hash "
            "WHERE uf.uid = ? AND uf.active = TRUE",
            (uid,))

    def get_user_storage_used(self, uid : int):
        rows = self.query("SELECT hash FROM user_file WHERE uid = ? AND active = TRUE", (uid,))
        total = 0
        for (hashes,) in rows:
            path = "res/{}/file/{}.file".format(self.port_api, hashes)
            if os.path.isfile(path):
                total += os.path.getsize(path)
        return total

    def has_active_user_file(self, uid : int, hashes : str):
        result = self.query(
            "SELECT 1 FROM user_file WHERE uid = ? AND hash = ? AND active = TRUE",
            (uid, hashes))
        return bool(result)

    def get_file_size(self, hashes : str):
        path = "res/{}/file/{}.file".format(self.port_api, hashes)
        return os.path.getsize(path) if os.path.isfile(path) else 0

    def increment_ref(self, hashes : str):
        return self.file_exists(hashes)

    def get_metadata(self, hashes : str, owner_uid=None):
        if not self.file_exists(hashes):
            return None
        params = (hashes,) if owner_uid is None else (hashes, owner_uid)
        owner_filter = "" if owner_uid is None else " AND uid = ?"
        rows = self.query("SELECT file_name, extension, upload_time, mime_type FROM user_file WHERE hash = ? AND active = TRUE{} ORDER BY upload_time LIMIT 1".format(owner_filter), params)
        row = rows[0] if rows else (hashes, "", None, "unknown")
        stored_type = str(row[3] or "").lower().lstrip(".")
        if stored_type not in {"png", "jpg", "gif", "bmp", "svg", "tgs"}:
            stored_type = ""
        fallback_type = (
            row[0].rsplit(".", 1)[-1] if "." in row[0] else row[1]
        )
        return {
            "hash": hashes,
            "file_name": row[0],
            "filename": row[0],
            "size": self.get_file_size(hashes),
            "file_type": (stored_type or fallback_type or "unknown").lstrip("."),
            "extension": row[1] or "",
            "download_url": "/file/get_file/{}".format(hashes),
            "send_time": row[2],
        }

    def get_active_user_filename(self, uid : int, hashes : str):
        rows = self.query(
            """SELECT file_name FROM user_file
               WHERE uid = ? AND hash = ? AND active = TRUE""",
            (uid, hashes),
        )
        return rows[0][0] if rows else None

    def decrement_ref(self, hashes : str):
        """不要管这个，请使用 remove_reference()"""
        return self.file_exists(hashes)

    def ensure_content_retained(self, hashes : str, reference_count : int = 1):
        """不要管这个，请使用 add_reference()"""
        return self.file_exists(hashes)

    def reconcile_references(self, references=()):
        """确认引用"""
        with self.lock:
            def operation():
                self.cursor.execute("SELECT hash FROM file")
                hashes = {row[0] for row in self.cursor.fetchall()}
                self.cursor.execute("""INSERT OR IGNORE INTO file_uploaders(hash, uid, created_at)
                    SELECT hash, uid, COALESCE(upload_time, ?) FROM user_file WHERE active = TRUE""", (time.time(),))
                for hashes_value, source_type, source_id, referrer_uid in references:
                    if hashes_value not in hashes:
                        continue
                    self.cursor.execute(
                        """INSERT OR IGNORE INTO file_references
                           (hash, source_type, source_id, referrer_uid, created_at, last_referenced_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (hashes_value, source_type, str(source_id), referrer_uid, time.time(), time.time()),
                    )
                    self.cursor.execute("DELETE FROM file_gc WHERE hash = ?", (hashes_value,))
                self.conn.commit()
                return len(hashes)

            return self._execute_with_retry(operation)

    def decrement_owned_ref(self, uid : int, hashes : str):
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT 1 FROM user_file WHERE uid = ? AND hash = ? AND active = TRUE",
                    (uid, hashes),
                )
                if self.cursor.fetchone() is None:
                    return False
                changed = True
                self.conn.commit()
                return changed

            return self._execute_with_retry(operation)

    def increment_upload_user_count(self, hashes : str):
        """不要管这个，现在用不上了（file_uploaders 和 register_upload()）"""
        return self.file_exists(hashes)

    def decrement_upload_user_count(self, hashes : str):
        """不要管这个，请使用 remove_uploader()"""
        return []

    def file_exists(self, hashes : str):
        result = self.query("SELECT hash FROM file WHERE hash = ?", (hashes,))
        return bool(result)

    def lose_effect(self, file_last_time: float = 72.0):
        """不要管这个，请使用 collect_expired_hashes() 与后台回收器"""
        return []

    def query_sender_files(self, sender : int):
        return self.query("SELECT hash FROM user_file WHERE uid = ? AND active = TRUE", (sender,))

    def clean_sender_files(self, sender : int):
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT hash FROM user_file WHERE uid = ? AND active = TRUE", (sender,))
                hashes = [row[0] for row in self.cursor.fetchall()]
                self.cursor.execute(
                    "UPDATE user_file SET active = FALSE WHERE uid = ?", (sender,))
                self.cursor.execute("DELETE FROM file_uploaders WHERE uid = ?", (sender,))
                deleted = []
                for hashes_value in hashes:
                    self.cursor.execute("SELECT 1 FROM file_uploaders WHERE hash = ?", (hashes_value,))
                    if self.cursor.fetchone() is None:
                        deleted.append((hashes_value,))
                self.conn.commit()
                return deleted

            return self._execute_with_retry(operation)

    def get_all_user_files(self, uid : int = None):
        if uid is not None:
            return self.query(
                "SELECT uf.uid, uf.hash, uf.file_name, uf.upload_time, "
                "0, 0, 0, NULL, "
                "NULL, uf.extension "
                "FROM user_file uf JOIN file f ON uf.hash = f.hash "
                "WHERE uf.active = TRUE AND uf.uid = ?",
                (uid,))
        return self.query(
            "SELECT uf.uid, uf.hash, uf.file_name, uf.upload_time, "
            "0, 0, 0, NULL, "
            "NULL, uf.extension "
            "FROM user_file uf JOIN file f ON uf.hash = f.hash "
            "WHERE uf.active = TRUE")

    def force_delete_file(self, hashes : str):
        return self.delete_blob_relations(hashes)

    def return_file(self, hashes : str):
        return self.query("SELECT * FROM file WHERE hash = ?", (hashes,))
