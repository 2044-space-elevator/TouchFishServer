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
        self.execute("""
            CREATE TABLE IF NOT EXISTS chunk_upload_tasks (
                file_id TEXT PRIMARY KEY,
                uid INTEGER NOT NULL,
                file_name TEXT,
                chunk_total INTEGER NOT NULL,
                expected_hash TEXT,
                status TEXT NOT NULL,
                file_hash TEXT,
                verified INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS chunk_upload_parts (
                file_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                size INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(file_id, chunk_index)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS file_media (
                hash TEXT PRIMARY KEY,
                width INTEGER,
                height INTEGER,
                blurhash TEXT,
                thumb_width INTEGER,
                thumb_height INTEGER,
                thumb_size INTEGER,
                thumb_is_original INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
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
        for col, typ in [("mime_type", "TEXT"), ("extension", "TEXT"), ("size", "INTEGER DEFAULT 0")]:
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
                       (uid, hash, file_name, upload_time, active, mime_type, extension, size)
                       VALUES (?, ?, ?, ?, TRUE, ?, ?, ?)
                       ON CONFLICT(uid, hash) DO UPDATE SET
                           file_name = excluded.file_name,
                           upload_time = excluded.upload_time,
                           active = TRUE,
                           mime_type = excluded.mime_type,
                           extension = excluded.extension,
                           size = excluded.size""",
                    (uid, hashes, file_name, upload_time, mime_type, extension, size),
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

    def has_uploader(self, hashes: str):
        rows = self.query("SELECT 1 FROM file_uploaders WHERE hash = ? LIMIT 1", (hashes,))
        return bool(rows)

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

    def should_collect(self, hashes : str, expiry_hours: float = 0.0, zero_ref_seconds: float = 1800.0):
        """复核单个 hash 是否仍满足回收"""
        now = time.time()
        cutoff = now - max(float(expiry_hours), 0) * 3600
        rows = self.query("""
            SELECT 1 FROM file f
            WHERE f.hash = ?
              AND (NOT EXISTS(SELECT 1 FROM file_uploaders u WHERE u.hash = f.hash)
               OR EXISTS(SELECT 1 FROM file_gc g WHERE g.hash = f.hash AND g.zero_references_at <= ?)
               OR EXISTS(SELECT 1 FROM file_references r WHERE r.hash = f.hash
                         GROUP BY r.hash HAVING MAX(r.last_referenced_at) <= ?))
        """, (hashes, now - zero_ref_seconds, cutoff))
        return bool(rows)

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

    # 媒体元数据与缩略图
    # independent！！！

    _MEDIA_COLUMNS = ("hash, width, height, blurhash, thumb_width, thumb_height, "
                      "thumb_size, thumb_is_original, status, created_at, updated_at")

    @staticmethod
    def _media_row(row):
        def _int_or_none(value):
            return int(value) if value is not None else None
        return {
            "hash": row[0],
            "width": _int_or_none(row[1]),
            "height": _int_or_none(row[2]),
            "blurhash": row[3],
            "thumb_width": _int_or_none(row[4]),
            "thumb_height": _int_or_none(row[5]),
            "thumb_size": _int_or_none(row[6]),
            "thumb_is_original": bool(row[7]),
            "status": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }

    def get_media(self, hashes : str):
        rows = self.query(
            "SELECT {} FROM file_media WHERE hash = ?".format(self._MEDIA_COLUMNS),
            (hashes,))
        return self._media_row(rows[0]) if rows else None

    def get_media_many(self, hashes_list):
        result = {}
        unique = list(dict.fromkeys(h for h in hashes_list if h))
        for index in range(0, len(unique), 400):
            chunk = unique[index:index + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = self.query(
                "SELECT {} FROM file_media WHERE hash IN ({})".format(self._MEDIA_COLUMNS, placeholders),
                tuple(chunk))
            for row in rows:
                result[row[0]] = self._media_row(row)
        return result

    def upsert_media_dimensions(self, hashes : str, width, height):
        """上传时同步登记宽高"""
        now = time.time()
        self.execute(
            """INSERT INTO file_media (hash, width, height, thumb_is_original, status, created_at, updated_at)
               VALUES (?, ?, ?, 0, 'pending', ?, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   width = COALESCE(file_media.width, excluded.width),
                   height = COALESCE(file_media.height, excluded.height)""",
            (hashes, width, height, now, now))

    def touch_media(self, hashes : str):
        self.execute("UPDATE file_media SET updated_at = ? WHERE hash = ?", (time.time(), hashes))

    def set_media_artifacts(self, hashes : str, blurhash, thumb_width, thumb_height,
                            thumb_size, thumb_is_original : bool):
        self.execute(
            """UPDATE file_media SET blurhash = ?, thumb_width = ?, thumb_height = ?,
                   thumb_size = ?, thumb_is_original = ?, status = 'done', updated_at = ?
               WHERE hash = ?""",
            (blurhash, thumb_width, thumb_height, thumb_size,
             1 if thumb_is_original else 0, time.time(), hashes))

    def mark_media_skipped(self, hashes : str, width=None, height=None):
        """标记为无需生成"""
        now = time.time()
        self.execute(
            """INSERT INTO file_media (hash, width, height, thumb_is_original, status, created_at, updated_at)
               VALUES (?, ?, ?, 0, 'skipped', ?, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   status = 'skipped',
                   width = COALESCE(file_media.width, excluded.width),
                   height = COALESCE(file_media.height, excluded.height),
                   updated_at = excluded.updated_at""",
            (hashes, width, height, now, now))

    def delete_media(self, hashes : str):
        self.execute("DELETE FROM file_media WHERE hash = ?", (hashes,))

    def list_orphan_media(self, limit : int = 500):
        """返回 file 表中已不存在的 Oliver Twist（bushi"""
        rows = self.query(
            """SELECT hash FROM file_media
               WHERE NOT EXISTS(SELECT 1 FROM file f WHERE f.hash = file_media.hash)
               LIMIT ?""",
            (int(limit),))
        return [row[0] for row in rows]

    def get_blob_first_seen(self, hashes : str):
        rows = self.query("SELECT MIN(created_at) FROM file_uploaders WHERE hash = ?", (hashes,))
        if not rows or rows[0][0] is None:
            return None
        return float(rows[0][0])

    # ---- 分块 pro max plus ultra ----

    def create_chunk_task(self, uid : int, file_id : str, file_name : str, chunk_total : int,
                          expected_hash : str = None, max_active : int = 5,
                          stale_seconds : float = 3600.0):
        """创建分块上传任务。返回 (error, expired_file_ids)：
        error 为 None 表示 ok！"""
        now = time.time()
        cutoff = now - stale_seconds
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT file_id FROM chunk_upload_tasks WHERE uid = ? AND status IN ('uploading', 'finalizing') AND updated_at <= ?",
                    (uid, cutoff),
                )
                expired = [row[0] for row in self.cursor.fetchall()]
                for expired_id in expired:
                    self.cursor.execute("DELETE FROM chunk_upload_parts WHERE file_id = ?", (expired_id,))
                    self.cursor.execute("DELETE FROM chunk_upload_tasks WHERE file_id = ?", (expired_id,))
                self.cursor.execute(
                    "SELECT COUNT(*) FROM chunk_upload_tasks WHERE uid = ? AND status IN ('uploading', 'finalizing')",
                    (uid,),
                )
                active = self.cursor.fetchone()[0]
                if active >= max_active:
                    self.conn.commit()
                    return "Too many concurrent uploads", expired
                self.cursor.execute(
                    """INSERT INTO chunk_upload_tasks
                       (file_id, uid, file_name, chunk_total, expected_hash, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'uploading', ?, ?)""",
                    (file_id, uid, file_name, chunk_total, expected_hash, now, now),
                )
                self.conn.commit()
                return None, expired
            return self._execute_with_retry(operation)

    def get_chunk_task(self, file_id : str):
        rows = self.query(
            """SELECT file_id, uid, file_name, chunk_total, expected_hash, status, file_hash, verified, created_at, updated_at
               FROM chunk_upload_tasks WHERE file_id = ?""",
            (file_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "file_id": row[0],
            "uid": row[1],
            "file_name": row[2],
            "chunk_total": int(row[3] or 0),
            "expected_hash": row[4],
            "status": row[5],
            "file_hash": row[6],
            "verified": bool(row[7]),
            "created_at": row[8],
            "updated_at": row[9],
        }

    def touch_chunk_task(self, file_id : str):
        self.execute("UPDATE chunk_upload_tasks SET updated_at = ? WHERE file_id = ?", (time.time(), file_id))

    def upsert_chunk_part(self, file_id : str, chunk_index : int, size : int):
        """登记分块"""
        return self.execute(
            """INSERT INTO chunk_upload_parts (file_id, chunk_index, size, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(file_id, chunk_index) DO UPDATE SET
                   size = excluded.size, updated_at = excluded.updated_at""",
            (file_id, chunk_index, size, time.time()),
        )

    def list_chunk_parts(self, file_id : str):
        rows = self.query("SELECT chunk_index, size FROM chunk_upload_parts WHERE file_id = ?", (file_id,))
        return {int(row[0]): int(row[1] or 0) for row in rows}

    def claim_chunk_finalize(self, file_id : str):
        """把任务从 uploading 抢占为 finalizing，防止并发合并"""
        with self.lock:
            def operation():
                self.cursor.execute(
                    "UPDATE chunk_upload_tasks SET status = 'finalizing', updated_at = ? WHERE file_id = ? AND status = 'uploading'",
                    (time.time(), file_id),
                )
                if self.cursor.rowcount > 0:
                    self.conn.commit()
                    return "claimed"
                self.cursor.execute("SELECT status FROM chunk_upload_tasks WHERE file_id = ?", (file_id,))
                row = self.cursor.fetchone()
                self.conn.commit()
                if row is None:
                    return "missing"
                return "done" if row[0] == "done" else "busy"
            return self._execute_with_retry(operation)

    def release_chunk_finalize(self, file_id : str):
        self.execute(
            "UPDATE chunk_upload_tasks SET status = 'uploading', updated_at = ? WHERE file_id = ? AND status = 'finalizing'",
            (time.time(), file_id),
        )

    def complete_chunk_task(self, file_id : str, file_hash : str, verified : bool):
        with self.lock:
            def operation():
                self.cursor.execute(
                    "UPDATE chunk_upload_tasks SET status = 'done', file_hash = ?, verified = ?, updated_at = ? WHERE file_id = ?",
                    (file_hash, 1 if verified else 0, time.time(), file_id),
                )
                self.cursor.execute("DELETE FROM chunk_upload_parts WHERE file_id = ?", (file_id,))
                self.conn.commit()
            return self._execute_with_retry(operation)

    def delete_chunk_task(self, file_id : str):
        with self.lock:
            def operation():
                self.cursor.execute("DELETE FROM chunk_upload_parts WHERE file_id = ?", (file_id,))
                self.cursor.execute("DELETE FROM chunk_upload_tasks WHERE file_id = ?", (file_id,))
                self.conn.commit()
            return self._execute_with_retry(operation)

    def expire_stale_chunk_tasks(self, max_age : float = 3600.0):
        """删除长时间无活动的任务（含已完成的），返回被删除的 file_id 列表。"""
        cutoff = time.time() - max_age
        with self.lock:
            def operation():
                self.cursor.execute("SELECT file_id FROM chunk_upload_tasks WHERE updated_at <= ?", (cutoff,))
                expired = [row[0] for row in self.cursor.fetchall()]
                for expired_id in expired:
                    self.cursor.execute("DELETE FROM chunk_upload_parts WHERE file_id = ?", (expired_id,))
                    self.cursor.execute("DELETE FROM chunk_upload_tasks WHERE file_id = ?", (expired_id,))
                self.conn.commit()
                return expired
            return self._execute_with_retry(operation)

    def acquire_reference(self, uid : int, hashes : str):
        """获取内容引用"""
        with self.lock:
            def operation():
                self.cursor.execute(
                    """SELECT uf.file_name, uf.extension, uf.size
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
                    "size": int(row[2] or 0) or self.get_file_size(hashes),
                }

            return self._execute_with_retry(operation)

    def acquire_forward_reference(self, hashes : str):
        """获取可转发的内容引用"""
        with self.lock:
            def operation():
                self.cursor.execute(
                    """SELECT uf.file_name, uf.extension, uf.size FROM user_file uf
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
                    "size": int(row[2] or 0) or self.get_file_size(hashes),
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
        rows = self.query(
            "SELECT COALESCE(SUM(size), 0) FROM user_file WHERE uid = ? AND active = TRUE AND size > 0",
            (uid,))
        total = int(rows[0][0] or 0) if rows else 0
        missing = self.query(
            "SELECT hash FROM user_file WHERE uid = ? AND active = TRUE AND (size IS NULL OR size <= 0)",
            (uid,))
        for (hashes,) in missing:
            path = "res/{}/file/{}.file".format(self.port_api, hashes)
            if os.path.isfile(path):
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
        return total

    def backfill_missing_sizes(self):
        """
        回填 user_file 中 size 为 0/NULL 的旧记录。
        - 优先从 OSS2 的 head_object 获取大小（OSS2 模式下本地无文件）
        - 否则从本地磁盘文件获取大小（兼容本地存储模式迁移/旧数据）
        返回回填的记录数。
        """
        import oss_store
        rows = self.query(
            "SELECT hash FROM user_file WHERE active = TRUE AND (size IS NULL OR size = 0)"
        )
        if not rows:
            return 0
        backfilled = 0
        for (hashes,) in rows:
            size = 0
            # 尝试 OSS2（仅启用 OSS2 时有效）
            if oss_store.is_oss_enabled(self.port_api):
                size = oss_store.get_size_from_oss(self.port_api, "file", hashes)
            # OSS 未启用或失败时尝试本地
            if size <= 0:
                path = "res/{}/file/{}.file".format(self.port_api, hashes)
                if os.path.isfile(path):
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
            if size > 0:
                self.execute(
                    "UPDATE user_file SET size = ? WHERE hash = ?",
                    (size, hashes),
                )
                backfilled += 1
        return backfilled

    def has_active_user_file(self, uid : int, hashes : str):
        result = self.query(
            "SELECT 1 FROM user_file WHERE uid = ? AND hash = ? AND active = TRUE",
            (uid, hashes))
        return bool(result)

    def get_file_size(self, hashes : str):
        path = "res/{}/file/{}.file".format(self.port_api, hashes)
        return os.path.getsize(path) if os.path.isfile(path) else 0

    def get_blob_info(self, hashes : str):
        """按 hash 取任一 active owner 行的元数据（秒传时把既有内容信息带给新登记方）"""
        rows = self.query(
            "SELECT file_name, extension, mime_type, size FROM user_file "
            "WHERE hash = ? AND active = TRUE ORDER BY upload_time LIMIT 1",
            (hashes,),
        )
        if not rows:
            return None
        return {
            "file_name": rows[0][0],
            "extension": rows[0][1] or "",
            "mime_type": rows[0][2],
            "size": int(rows[0][3] or 0),
        }

    def increment_ref(self, hashes : str):
        return self.file_exists(hashes)

    def get_metadata(self, hashes : str, owner_uid=None):
        if not self.file_exists(hashes):
            return None
        params = (hashes,) if owner_uid is None else (hashes, owner_uid)
        owner_filter = "" if owner_uid is None else " AND uid = ?"
        rows = self.query(
            """SELECT uf.file_name, uf.extension, uf.upload_time, uf.mime_type, uf.size,
                      fm.width, fm.height, fm.blurhash, fm.thumb_size, fm.thumb_is_original
               FROM user_file uf LEFT JOIN file_media fm ON fm.hash = uf.hash
               WHERE uf.hash = ? AND uf.active = TRUE{}
               ORDER BY uf.upload_time LIMIT 1""".format(owner_filter), params)
        row = rows[0] if rows else (hashes, "", None, "unknown", 0, None, None, None, None, None)
        has_thumb = row[8] is not None or bool(row[9])
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
            "size": int(row[4] or 0) or self.get_file_size(hashes),
            "file_type": (stored_type or fallback_type or "unknown").lstrip("."),
            "extension": row[1] or "",
            "download_url": "/file/get_file/{}".format(hashes),
            "send_time": row[2],
            "width": int(row[5]) if row[5] else None,
            "height": int(row[6]) if row[6] else None,
            "blurhash": row[7],
            "has_thumb": has_thumb,
            "thumb_url": "/file/get_thumbnail/{}".format(hashes) if has_thumb else None,
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
