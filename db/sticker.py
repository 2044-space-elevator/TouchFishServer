"""
全新表情 Sticker 数据库
"""
from __future__ import annotations

import time
import uuid

from db.tool import Db


def escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class StickerDb(Db):
    """表情数据库"""

    def __init__(self, path: str, port_api: int, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self._create_tables()

    def _create_tables(self):
        self.execute("""
            CREATE TABLE IF NOT EXISTS sticker_packs (
                id TEXT PRIMARY KEY, creator_uid INTEGER NOT NULL,
                name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                prefix TEXT NOT NULL COLLATE NOCASE UNIQUE,
                icon_hash TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                usage_count INTEGER NOT NULL DEFAULT 0, is_deleted INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS stickers (
                id TEXT PRIMARY KEY, pack_id TEXT NOT NULL, slug TEXT NOT NULL,
                name TEXT, file_hash TEXT NOT NULL, file_type TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0, render_size INTEGER NOT NULL DEFAULT 0,
                render_mode INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                UNIQUE(pack_id, slug)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS user_sticker_packs (
                uid INTEGER NOT NULL, pack_id TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, PRIMARY KEY(uid, pack_id)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS sticker_pack_creation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
                local_day TEXT NOT NULL, created_at REAL NOT NULL
            )
        """)
        self.execute("CREATE INDEX IF NOT EXISTS idx_sticker_packs_creator ON sticker_packs(creator_uid, is_deleted)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_stickers_pack ON stickers(pack_id, position)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_sticker_creation_day ON sticker_pack_creation_log(uid, local_day)")

    def create_pack(self, uid, name, description, prefix, local_day, max_packs, daily_limit, exempt):
        now = time.time()
        with self.lock:
            def operation():
                if not exempt:
                    if max_packs != -1:
                        self.cursor.execute("SELECT COUNT(*) FROM sticker_packs WHERE creator_uid = ? AND is_deleted = 0", (uid,))
                        if self.cursor.fetchone()[0] >= max_packs:
                            return None, "pack_limit"
                    if daily_limit != -1:
                        self.cursor.execute("SELECT COUNT(*) FROM sticker_pack_creation_log WHERE uid = ? AND local_day = ?", (uid, local_day))
                        if self.cursor.fetchone()[0] >= daily_limit:
                            return None, "daily_limit"
                pack_id = uuid.uuid4().hex
                self.cursor.execute(
                    "INSERT INTO sticker_packs(id, creator_uid, name, description, prefix, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pack_id, uid, name, description, prefix, now, now),
                )
                self.cursor.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM user_sticker_packs WHERE uid = ?",
                    (uid,),
                )
                position = self.cursor.fetchone()[0]
                self.cursor.execute(
                    "INSERT INTO user_sticker_packs(uid, pack_id, position, created_at) VALUES (?, ?, ?, ?)",
                    (uid, pack_id, position, now),
                )
                self.cursor.execute(
                    "INSERT INTO sticker_pack_creation_log(uid, local_day, created_at) VALUES (?, ?, ?)",
                    (uid, local_day, now),
                )
                self.conn.commit()
                return pack_id, None
            return self._execute_with_retry(operation)

    def get_pack(self, pack_id):
        rows = self.query("SELECT id, creator_uid, name, description, prefix, icon_hash, created_at, updated_at, usage_count FROM sticker_packs WHERE id = ? AND is_deleted = 0", (pack_id,))
        return rows[0] if rows else None

    def list_packs(self, offset=0, limit=20, query_text="", order="usage", creator_uid=None):
        where = "is_deleted = 0"
        params = []
        if query_text:
            where += " AND (name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR prefix LIKE ? ESCAPE '\\')"
            escaped = "%{}%".format(escape_like(query_text))
            params.extend([escaped, escaped, escaped])
        if creator_uid is not None:
            where += " AND creator_uid = ?"
            params.append(creator_uid)
        ordering = "usage_count DESC, created_at DESC" if order == "usage" else "created_at DESC"
        total = self.query("SELECT COUNT(*) FROM sticker_packs WHERE " + where, tuple(params))[0][0]
        rows = self.query(
            "SELECT id, creator_uid, name, description, prefix, icon_hash, created_at, updated_at, usage_count FROM sticker_packs WHERE " + where + " ORDER BY " + ordering + " LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        )
        return rows, total

    def list_stickers(self, pack_id):
        return self.query("SELECT id, pack_id, slug, name, file_hash, file_type, position, render_size, render_mode, created_at FROM stickers WHERE pack_id = ? ORDER BY position, created_at", (pack_id,))

    def can_manage_pack(self, uid, pack_id, exempt=False):
        rows = self.query("SELECT creator_uid FROM sticker_packs WHERE id = ? AND is_deleted = 0", (pack_id,))
        return bool(rows and (exempt or rows[0][0] == uid))

    def update_pack(self, pack_id, name=None, description=None, prefix=None, icon_hash=None):
        fields, values = [], []
        if name is not None:
            fields.append("name = ?"); values.append(name)
        if description is not None:
            fields.append("description = ?"); values.append(description)
        if prefix is not None:
            fields.append("prefix = ?"); values.append(prefix)
        if icon_hash is not None:
            fields.append("icon_hash = ?"); values.append(icon_hash)
        if not fields:
            return False
        fields.append("updated_at = ?"); values.append(time.time()); values.append(pack_id)
        self.execute("UPDATE sticker_packs SET {} WHERE id = ? AND is_deleted = 0".format(", ".join(fields)), tuple(values))
        return True

    def delete_pack(self, pack_id):
        with self.lock:
            def operation():
                self.cursor.execute("SELECT id, file_hash FROM stickers WHERE pack_id = ?", (pack_id,))
                hashes = self.cursor.fetchall()
                self.cursor.execute("UPDATE sticker_packs SET is_deleted = 1, updated_at = ? WHERE id = ?", (time.time(), pack_id))
                self.cursor.execute("DELETE FROM user_sticker_packs WHERE pack_id = ?", (pack_id,))
                self.cursor.execute("DELETE FROM stickers WHERE pack_id = ?", (pack_id,))
                self.conn.commit()
                return hashes
            return self._execute_with_retry(operation)

    def delete_sticker(self, pack_id, sticker_id):
        with self.lock:
            def operation():
                self.cursor.execute("SELECT file_hash FROM stickers WHERE id = ? AND pack_id = ?", (sticker_id, pack_id))
                row = self.cursor.fetchone()
                if row is None:
                    return None
                self.cursor.execute("DELETE FROM stickers WHERE id = ? AND pack_id = ?", (sticker_id, pack_id))
                self.cursor.execute("UPDATE sticker_packs SET updated_at = ? WHERE id = ?", (time.time(), pack_id))
                self.conn.commit()
                return row[0]
            return self._execute_with_retry(operation)

    def reorder_stickers(self, pack_id, sticker_ids):
        with self.lock:
            def operation():
                current = [row[0] for row in self.cursor.execute("SELECT id FROM stickers WHERE pack_id = ?", (pack_id,)).fetchall()]
                if set(current) != set(sticker_ids) or len(current) != len(sticker_ids):
                    return False
                for position, sticker_id in enumerate(sticker_ids):
                    self.cursor.execute("UPDATE stickers SET position = ? WHERE id = ? AND pack_id = ?", (position, sticker_id, pack_id))
                self.cursor.execute("UPDATE sticker_packs SET updated_at = ? WHERE id = ?", (time.time(), pack_id))
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def create_sticker(self, uid, pack_id, slug, name, file_hash, file_type, render_size, render_mode, max_stickers, exempt):
        now = time.time()
        _IntegrityError = self.dialect.IntegrityError
        with self.lock:
            def operation():
                self.cursor.execute("SELECT creator_uid FROM sticker_packs WHERE id = ? AND is_deleted = 0", (pack_id,))
                pack = self.cursor.fetchone()
                if not pack:
                    return None, "not_found"
                if pack[0] != uid and not exempt:
                    return None, "forbidden"
                if not exempt and max_stickers != -1:
                    self.cursor.execute("SELECT COUNT(*) FROM stickers WHERE pack_id = ?", (pack_id,))
                    if self.cursor.fetchone()[0] >= max_stickers:
                        return None, "sticker_limit"
                self.cursor.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM stickers WHERE pack_id = ?", (pack_id,))
                position = self.cursor.fetchone()[0]
                sticker_id = uuid.uuid4().hex
                try:
                    self.cursor.execute("INSERT INTO stickers(id, pack_id, slug, name, file_hash, file_type, position, render_size, render_mode, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (sticker_id, pack_id, slug, name, file_hash, file_type, position, render_size, render_mode, now))
                except _IntegrityError:
                    self.conn.rollback()
                    return None, "slug_exists"
                self.cursor.execute("UPDATE sticker_packs SET updated_at = ? WHERE id = ?", (now, pack_id))
                self.conn.commit()
                return sticker_id, None
            return self._execute_with_retry(operation)

    def set_owned(self, uid, pack_id, owned):
        with self.lock:
            def operation():
                if owned:
                    self.cursor.execute("SELECT 1 FROM sticker_packs WHERE id = ? AND is_deleted = 0", (pack_id,))
                    if self.cursor.fetchone() is None:
                        return False
                    self.cursor.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM user_sticker_packs WHERE uid = ?", (uid,))
                    position = self.cursor.fetchone()[0]
                    self.cursor.execute("INSERT OR IGNORE INTO user_sticker_packs(uid, pack_id, position, created_at) VALUES (?, ?, ?, ?)", (uid, pack_id, position, time.time()))
                    if self.cursor.rowcount > 0:
                        self.cursor.execute("UPDATE sticker_packs SET usage_count = usage_count + 1 WHERE id = ?", (pack_id,))
                else:
                    self.cursor.execute("DELETE FROM user_sticker_packs WHERE uid = ? AND pack_id = ?", (uid, pack_id))
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def reorder_owned(self, uid, pack_ids):
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT pack_id FROM user_sticker_packs WHERE uid = ?", (uid,)
                )
                current = [row[0] for row in self.cursor.fetchall()]
                if set(current) != set(pack_ids) or len(current) != len(pack_ids):
                    return False
                for position, pack_id in enumerate(pack_ids):
                    self.cursor.execute(
                        "UPDATE user_sticker_packs SET position = ? WHERE uid = ? AND pack_id = ?",
                        (position, uid, pack_id),
                    )
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def list_owned(self, uid):
        return self.query("""SELECT o.pack_id, o.position, p.id, p.creator_uid, p.name, p.description, p.prefix, p.icon_hash, p.created_at, p.updated_at, p.usage_count
                             FROM user_sticker_packs o JOIN sticker_packs p ON p.id = o.pack_id
                             WHERE o.uid = ? AND p.is_deleted = 0 ORDER BY o.position""", (uid,))

    def query_hash_exist(self, hashes):
        return self.query("SELECT * FROM stickers WHERE file_hash = ?", (hashes, ))