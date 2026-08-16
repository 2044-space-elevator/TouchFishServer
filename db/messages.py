from db.tool import Db
import time


class MessagesDb(Db):
    def __init__(self, path: str, port_api: int, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self._create_table()
        self._migrate()
        self._create_indexes()

    @staticmethod
    def room_key_of(sender_uid: int, receiver_uid: int, group_id=None) -> str:
        """房间的规范化 key：私聊为排序后的 UminUmax，群聊为 Ggid。"""
        if group_id is not None:
            return "G{}".format(group_id)
        lo, hi = (sender_uid, receiver_uid) if sender_uid < receiver_uid \
            else (receiver_uid, sender_uid)
        return "U{}U{}".format(lo, hi)

    def _create_table(self):
        self.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                mid INTEGER PRIMARY KEY AUTOINCREMENT,
                client_mid TEXT,
                sender_uid INTEGER NOT NULL,
                receiver_uid INTEGER NOT NULL,
                group_id INTEGER,
                content TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'plain',
                file_hash TEXT,
                send_time REAL NOT NULL,
                quote INTEGER NOT NULL DEFAULT -1,
                deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at REAL,
                deleted_by INTEGER,
                file_name TEXT,
                forwarded INTEGER NOT NULL DEFAULT -1,
                room_key TEXT,
                room_seq INTEGER
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS room_preferences (
                uid INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                is_pinned INTEGER NOT NULL DEFAULT 0,
                notify_level INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (uid, room_id)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS message_mentions (
                mid INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                PRIMARY KEY (mid, uid)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS group_pinned_messages (
                pin_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                pinned_by_uid INTEGER NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(message_id, group_id)
            )
        """)

    def _migrate(self):
        """尝试修复db"""
        for col, typ in [("group_id", "INTEGER"), ("client_mid", "TEXT"),
                         ("deleted_at", "REAL"), ("deleted_by", "INTEGER"),
                           ("file_name", "TEXT"),
                           ("forwarded", "INTEGER NOT NULL DEFAULT -1"),
                           ("room_key", "TEXT"), ("room_seq", "INTEGER")]:
            try:
                self.execute("ALTER TABLE messages ADD COLUMN {} {}".format(col, typ))
            except Exception:
                pass
        self._backfill_room_sequences()

    def _backfill_room_sequences(self):
        """为旧数据回填 room_key / room_seq（按 mid 升序、每房间从 1 递增）。

        幂等：仅处理 room_seq IS NULL 的行，重复执行安全。
        """
        with self.lock:
            def operation():
                rows = self.cursor.execute(
                    "SELECT mid, sender_uid, receiver_uid, group_id "
                    "FROM messages WHERE room_seq IS NULL ORDER BY mid ASC"
                ).fetchall()
                if not rows:
                    self.conn.commit()
                    return
                seq_by_room = {}
                updates = []
                for mid, sender, receiver, gid in rows:
                    key = self.room_key_of(sender, receiver, gid)
                    seq_by_room[key] = seq_by_room.get(key, 0) + 1
                    updates.append((key, seq_by_room[key], mid))
                self.cursor.executemany(
                    "UPDATE messages SET room_key = ?, room_seq = ? WHERE mid = ?",
                    updates,
                )
                self.conn.commit()
                print("[INFO] DATABASE 回填了 {} 条消息的 room_key/room_seq".format(len(updates)))
            self._execute_with_retry(operation)

    def _create_indexes(self):
        try:
            self.execute("DROP INDEX IF EXISTS idx_messages_client_mid")
        except Exception:
            pass
        indexes = [
            """CREATE INDEX IF NOT EXISTS idx_messages_conversation
               ON messages(sender_uid, receiver_uid, send_time DESC)""",
            """CREATE INDEX IF NOT EXISTS idx_messages_receiver
               ON messages(receiver_uid, send_time DESC)""",
            """CREATE INDEX IF NOT EXISTS idx_messages_group
               ON messages(group_id, send_time DESC)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_sender_client_mid
               ON messages(sender_uid, client_mid) WHERE client_mid IS NOT NULL""",
            """CREATE INDEX IF NOT EXISTS idx_messages_room_seq
               ON messages(room_key, room_seq)""",
        ]
        for idx_sql in indexes:
            try:
                self.execute(idx_sql)
            except Exception:
                pass

    def add_message(self, sender_uid: int, receiver_uid: int, content: str,
                     content_type: str = 'plain', file_hash: str = None,
                      quote: int = -1, group_id: int = None,
                       client_mid: str = None, file_name: str = None,
                        forwarded: int = -1) -> dict:
        send_time = time.time()
        _IntegrityError = self.dialect.IntegrityError
        with self.lock:
            def operation():
                room_key = self.room_key_of(sender_uid, receiver_uid, group_id)
                try:
                    row = self.cursor.execute(
                        "SELECT MAX(room_seq) FROM messages WHERE room_key = ?",
                        (room_key,),
                    ).fetchone()
                    room_seq = (row[0] + 1) if row and row[0] is not None else 1
                    self.cursor.execute(
                        """INSERT INTO messages
                           (client_mid, sender_uid, receiver_uid, group_id, content, content_type,
                             file_hash, send_time, quote, file_name, forwarded, room_key, room_seq)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (client_mid, sender_uid, receiver_uid, group_id, content, content_type,
                          file_hash, send_time, quote, file_name, forwarded, room_key, room_seq),
                    )
                    mid = self.cursor.lastrowid
                    self.conn.commit()
                    return mid, room_seq, False
                except _IntegrityError:
                    self.conn.rollback()
                    if client_mid:
                        self.cursor.execute(
                            "SELECT mid FROM messages WHERE sender_uid = ? AND client_mid = ?",
                            (sender_uid, client_mid),
                        )
                        existing = self.cursor.fetchone()
                        if existing:
                            return existing[0], None, True
                    raise

            mid, room_seq, duplicate = self._execute_with_retry(operation)

        if duplicate:
            return {"mid": mid, "duplicate": True}
        return {
            "mid": mid,
            "client_mid": client_mid,
            "sender_uid": sender_uid,
            "receiver_uid": receiver_uid,
            "group_id": group_id,
            "content": content,
            "content_type": content_type,
            "file_hash": file_hash,
            "send_time": send_time,
            "quote": quote,
            "deleted": 0,
            "deleted_at": None,
            "deleted_by": None,
            "file_name": file_name,
            "forwarded": forwarded,
            "room_key": self.room_key_of(sender_uid, receiver_uid, group_id),
            "room_seq": room_seq,
        }

    def query_history(self, uid: int, target_uid: int,
                      before_mid: int = 0, limit: int = 50,
                      group_id: int = None) -> list:
        """返回历史消息，按 mid 倒序排列。"""
        if group_id is not None:
            where = "group_id = ?"
            params = [group_id]
        else:
            where = ("group_id IS NULL AND "
                     "((sender_uid = ? AND receiver_uid = ?) "
                     "OR (sender_uid = ? AND receiver_uid = ?))")
            params = [uid, target_uid, target_uid, uid]

        if before_mid > 0:
            where += " AND mid < ?"
            params.append(before_mid)

        sql = """SELECT mid, client_mid, sender_uid, receiver_uid, group_id,
                         content, content_type, file_hash, send_time, quote, deleted,
                          deleted_at, deleted_by, file_name, forwarded, room_key, room_seq
                  FROM messages WHERE {} ORDER BY mid DESC LIMIT ?""".format(where)
        params.append(limit)
        return self.query(sql, tuple(params))

    def query_sync(self, room_key: str, after_seq: int = 0,
                   after_mid: int = 0, limit: int = 100) -> list:
        """按房间增量同步：优先按 room_seq，其次按 mid，升序返回。

        返回 serialized rows（含 room_seq），配合 current_seq 使用。
        """
        if after_seq < 0:
            after_seq = 0
        if after_mid < 0:
            after_mid = 0
        if after_seq > 0:
            rows = self.query(
                "{} WHERE room_key = ? AND room_seq > ? "
                "ORDER BY room_seq ASC LIMIT ?".format(self._SELECT_ALL),
                (room_key, after_seq, limit),
            )
        elif after_mid > 0:
            rows = self.query(
                "{} WHERE room_key = ? AND mid > ? "
                "ORDER BY mid ASC LIMIT ?".format(self._SELECT_ALL),
                (room_key, after_mid, limit),
            )
        else:
            rows = self.query(
                "{} WHERE room_key = ? "
                "ORDER BY room_seq ASC LIMIT ?".format(self._SELECT_ALL),
                (room_key, limit),
            )
        return self.serialize_rows(rows)

    def sync_cursor_for_mid(self, room_key: str, mid: int):
        """返回房间内 mid 的 (room_seq, deleted)，跨房间或不存在时返回 None。"""
        rows = self.query(
            "SELECT room_seq, deleted FROM messages WHERE room_key = ? AND mid = ?",
            (room_key, mid),
        )
        return (rows[0][0], bool(rows[0][1])) if rows else None

    def sync_after_seq_for_mid(self, room_key: str, mid: int):
        """将旧客户端的 mid 游标转换为房间序号，不存在或跨房间时返回 None。"""
        cursor = self.sync_cursor_for_mid(room_key, mid)
        if cursor is None or cursor[0] is None:
            return None
        # recall_message moves the old mid to a new sequence; include that tombstone.
        return max(cursor[0] - 1, 0) if cursor[1] else cursor[0]

    def query_missing_sequences(self, room_key: str, sequences: list) -> list:
        """按具体房间序号（缺失缺口）取消息，升序返回 serialized rows。"""
        sequences = sorted({int(s) for s in sequences if s is not None})
        if not sequences:
            return []
        placeholders = ",".join("?" * len(sequences))
        rows = self.query(
            "{} WHERE room_key = ? AND room_seq IN ({}) "
            "ORDER BY room_seq ASC".format(self._SELECT_ALL, placeholders),
            tuple([room_key] + sequences),
        )
        return self.serialize_rows(rows)

    def current_room_seq(self, room_key: str) -> int:
        rows = self.query(
            "SELECT MAX(room_seq) FROM messages WHERE room_key = ?", (room_key,)
        )
        return rows[0][0] if rows and rows[0][0] is not None else 0

    _COLUMNS = ["mid", "client_mid", "sender_uid", "receiver_uid", "group_id",
                  "content", "content_type", "file_hash", "send_time", "quote", "deleted",
                  "deleted_at", "deleted_by", "file_name", "forwarded",
                  "room_key", "room_seq"]

    _SELECT_ALL = ("SELECT mid, client_mid, sender_uid, receiver_uid, group_id,"
                   " content, content_type, file_hash, send_time, quote, deleted,"
                   " deleted_at, deleted_by, file_name, forwarded, room_key, room_seq"
                   " FROM messages")

    @staticmethod
    def _redact_recalled(record: dict) -> dict:
        if record.get("deleted"):
            record["content"] = None
            record["file_hash"] = None
        return record

    @staticmethod
    def _quote_preview(record: dict) -> dict:
        preview = {
            "mid": record["mid"],
            "sender_uid": record["sender_uid"],
            "content_type": record["content_type"],
            "content": record["content"][:240] if record["content"] is not None else None,
            "file_hash": record["file_hash"],
            "file_name": record.get("file_name"),
            "deleted": bool(record["deleted"]),
            "deleted_at": record["deleted_at"],
        }
        return MessagesDb._redact_recalled(preview)

    @staticmethod
    def _same_conversation(message: dict, quoted: dict) -> bool:
        if message.get("group_id") is not None or quoted.get("group_id") is not None:
            return (message.get("group_id") is not None
                    and message.get("group_id") == quoted.get("group_id"))
        return {message["sender_uid"], message["receiver_uid"]} == {
            quoted["sender_uid"], quoted["receiver_uid"]
        }

    def serialize_rows(self, rows) -> list:
        records = [dict(zip(self._COLUMNS, r)) for r in rows]
        if not records:
            return records
        mids = [record["mid"] for record in records]
        placeholders = ",".join("?" * len(mids))
        mention_rows = self.query(
            "SELECT mid, uid FROM message_mentions WHERE mid IN ({})".format(placeholders),
            tuple(mids),
        )
        mentions = {}
        for mid, uid in mention_rows:
            mentions.setdefault(mid, []).append(uid)
        for record in records:
            record["mentioned_uids"] = mentions.get(record["mid"], [])
        quote_mids = sorted({
            mid for record in records
            for mid in (record["quote"], record["forwarded"]) if mid >= 0
        })
        quote_map = {}
        if quote_mids:
            placeholders = ",".join("?" * len(quote_mids))
            quote_rows = self.query(
                """SELECT mid, client_mid, sender_uid, receiver_uid, group_id,
                          content, content_type, file_hash, send_time, quote, deleted,
                           deleted_at, deleted_by, file_name, forwarded, room_key, room_seq
                   FROM messages WHERE mid IN ({})""".format(placeholders),
                tuple(quote_mids),
            )
            for row in quote_rows:
                quote_record = dict(zip(self._COLUMNS, row))
                quote_map[quote_record["mid"]] = quote_record
        for record in records:
            quoted = quote_map.get(record["quote"])
            record["quote_preview"] = (
                self._quote_preview(quoted)
                if quoted and self._same_conversation(record, quoted) else None
            )
            forwarded = quote_map.get(record["forwarded"])
            record["forward_preview"] = (
                self._quote_preview(forwarded)
                if forwarded and self._same_conversation(record, forwarded) else None
            )
            self._redact_recalled(record)
        return records

    def get_quote_preview(self, mid: int, message=None):
        rows = self.query(
            """SELECT mid, client_mid, sender_uid, receiver_uid, group_id,
                      content, content_type, file_hash, send_time, quote, deleted,
                        deleted_at, deleted_by, file_name, forwarded, room_key, room_seq
                      FROM messages WHERE mid = ?""",
            (mid,),
        )
        if not rows:
            return None
        quoted = dict(zip(self._COLUMNS, rows[0]))
        if message is not None and not self._same_conversation(message, quoted):
            return None
        return self._quote_preview(quoted)

    def get_chat_list(self, uid: int) -> list:
        """返回与每个用户的最新单聊消息。
        群聊由 API 接口单独合并。
        """
        rows = self.query(
            """SELECT partner_uid, mid, client_mid, sender_uid, content, content_type, send_time,
                       deleted, deleted_at, file_name
               FROM (
                 SELECT
                   CASE WHEN sender_uid = ? THEN receiver_uid ELSE sender_uid END AS partner_uid,
                    mid, client_mid, sender_uid, content, content_type, send_time,
                     deleted, deleted_at, file_name,
                   ROW_NUMBER() OVER (
                     PARTITION BY CASE WHEN sender_uid = ? THEN receiver_uid ELSE sender_uid END
                     ORDER BY mid DESC
                   ) AS rn
                 FROM messages
                  WHERE group_id IS NULL
                   AND (sender_uid = ? OR receiver_uid = ?)
                   AND sender_uid != receiver_uid
               ) AS ranked
               WHERE rn = 1 AND partner_uid != ?
               ORDER BY mid DESC""",
            (uid, uid, uid, uid, uid)
        )
        return [
            {
                "partner_uid": r[0],
                "group_id": None,
                "last_mid": r[1],
                "last_client_mid": r[2],
                "last_sender_uid": r[3],
                "last_content": None if r[7] else r[4],
                "last_content_type": r[5],
                "last_time": r[6],
                "last_deleted": bool(r[7]),
                "last_deleted_at": r[8],
                "last_file_name": r[9],
            }
            for r in rows
        ]

    def get_group_last_message(self, group_id: int) -> dict:
        """返回群聊的最新消息，如果没有则返回 None。"""
        rows = self.query(
            """SELECT mid, sender_uid, content, content_type, send_time, deleted, deleted_at,
                      file_name
               FROM messages
               WHERE group_id = ?
               ORDER BY mid DESC LIMIT 1""",
            (group_id,)
        )
        if not rows:
            return None
        return {
            "mid": rows[0][0],
            "sender_uid": rows[0][1],
            "content": None if rows[0][5] else rows[0][2],
            "content_type": rows[0][3],
            "send_time": rows[0][4],
            "deleted": bool(rows[0][5]),
            "deleted_at": rows[0][6],
            "file_name": rows[0][7],
        }

    def get_group_last_messages(self, group_ids: list) -> dict:
        """批量获取多个群聊的最新消息，单次查询。"""
        if not group_ids:
            return {}
        placeholders = ",".join("?" * len(group_ids))
        rows = self.query(
            """SELECT m.mid, m.sender_uid, m.content, m.content_type, m.send_time, m.group_id,
                       m.deleted, m.deleted_at, m.file_name
               FROM messages m
               INNER JOIN (
                   SELECT group_id, MAX(mid) AS max_mid
                   FROM messages
                    WHERE group_id IN ({})
                   GROUP BY group_id
               ) latest ON m.group_id = latest.group_id AND m.mid = latest.max_mid
            """.format(placeholders),
            tuple(group_ids)
        )
        return {
            r[5]: {
                "mid": r[0], "sender_uid": r[1], "content": None if r[6] else r[2],
                "content_type": r[3], "send_time": r[4], "deleted": bool(r[6]),
                "deleted_at": r[7],
                "file_name": r[8],
            }
            for r in rows
        }

    def verify_quote(self, quote_mid: int, sender_uid: int = 0,
                     target_uid: int = 0, group_id: int = None) -> bool:
        rows = self.query(
            "SELECT sender_uid, receiver_uid, group_id, deleted FROM messages WHERE mid = ?",
            (quote_mid,)
        )
        if not rows:
            return False
        r = rows[0]
        if r[3]:  # deleted
            return False
        if group_id is not None:
            return r[2] == group_id
        return (r[0] == sender_uid and r[1] == target_uid) or \
               (r[0] == target_uid and r[1] == sender_uid)

    def get_message(self, mid: int, include_recalled_original=False):
        rows = self.query(
            """SELECT mid, client_mid, sender_uid, receiver_uid, group_id,
                       content, content_type, file_hash, send_time, quote, deleted,
                        deleted_at, deleted_by, file_name, forwarded, room_key, room_seq
                      FROM messages WHERE mid = ?""",
            (mid,),
        )
        if not rows:
            return None
        record = dict(zip(self._COLUMNS, rows[0]))
        if not include_recalled_original:
            self._redact_recalled(record)
        return record

    def request_matches(self, mid : int, sender_uid : int, receiver_uid : int,
                        content : str, content_type : str, file_hash=None,
                         quote : int = -1, group_id=None, forwarded : int = -1) -> bool:
        message = self.get_message(mid, include_recalled_original=True)
        if message is None:
            return False
        return (
            message["sender_uid"] == sender_uid
            and message["receiver_uid"] == receiver_uid
            and message["group_id"] == group_id
            and message["content"] == content
            and message["content_type"] == content_type
            and message["file_hash"] == file_hash
            and message["quote"] == quote
            and message["forwarded"] == forwarded
        )

    def get_by_client_mid(self, sender_uid : int, client_mid : str):
        if not client_mid:
            return None
        rows = self.query(
            "SELECT mid FROM messages WHERE sender_uid = ? AND client_mid = ?",
            (sender_uid, client_mid),
        )
        return self.get_message(rows[0][0]) if rows else None

    def recall_message(self, mid: int, deleted_by: int) -> int:
        """撤回消息：置 deleted 并递增该房间 room_seq（离线端经 sync 收墓碑）。

        返回新的 room_seq（>0 表示成功），失败返回 0。
        """
        with self.lock:
            def operation():
                row = self.cursor.execute(
                    "SELECT room_key FROM messages WHERE mid = ? AND deleted = 0",
                    (mid,),
                ).fetchone()
                if not row:
                    self.conn.commit()
                    return 0
                room_key = row[0]
                max_row = self.cursor.execute(
                    "SELECT MAX(room_seq) FROM messages WHERE room_key = ?",
                    (room_key,),
                ).fetchone()
                room_seq = (max_row[0] + 1) if max_row and max_row[0] is not None else 1
                self.cursor.execute(
                    """UPDATE messages SET deleted = 1, deleted_at = ?, deleted_by = ?,
                       room_seq = ? WHERE mid = ? AND deleted = 0""",
                    (time.time(), deleted_by, room_seq, mid),
                )
                changed = self.cursor.rowcount > 0
                self.conn.commit()
                return room_seq if changed else 0
            return self._execute_with_retry(operation)

    def delete_message(self, mid: int) -> bool:
        return self.recall_message(mid, 0) > 0

    def count_file_references(self, file_hash: str) -> int:
        rows = self.query(
            "SELECT COUNT(*) FROM messages WHERE file_hash = ?",
            (file_hash,),
        )
        return rows[0][0] if rows else 0

    def get_file_reference_rows(self):
        return [
            (row[0], "message", row[1], row[2])
            for row in self.query(
                """SELECT file_hash, mid, sender_uid FROM messages
                   WHERE file_hash IS NOT NULL AND deleted = 0"""
            )
        ]

    def get_room_preferences(self, uid: int) -> dict:
        rows = self.query(
            "SELECT room_id, is_pinned, notify_level FROM room_preferences WHERE uid = ?",
            (uid,),
        )
        return {
            row[0]: {"is_pinned": bool(row[1]), "notify_level": int(row[2])}
            for row in rows
        }

    def get_room_preference_map(self, uids, room_id: str) -> dict:
        """批量获取多个用户对同一房间的通知偏好，单次查询。"""
        uids = [int(uid) for uid in set(uids)]
        if not uids:
            return {}
        placeholders = ",".join("?" * len(uids))
        rows = self.query(
            "SELECT uid, is_pinned, notify_level FROM room_preferences "
            "WHERE uid IN ({}) AND room_id = ?".format(placeholders),
            tuple(uids) + (room_id,),
        )
        return {
            row[0]: {"is_pinned": bool(row[1]), "notify_level": int(row[2])}
            for row in rows
        }

    def get_room_preference(self, uid: int, room_id: str) -> dict:
        rows = self.query(
            "SELECT is_pinned, notify_level FROM room_preferences WHERE uid = ? AND room_id = ?",
            (uid, room_id),
        )
        if not rows:
            return {"is_pinned": False, "notify_level": 0}
        return {"is_pinned": bool(rows[0][0]), "notify_level": int(rows[0][1])}

    def update_room_preference(self, uid: int, room_id: str,
                               is_pinned=None, notify_level=None) -> bool:
        current = self.query(
            "SELECT is_pinned, notify_level FROM room_preferences WHERE uid = ? AND room_id = ?",
            (uid, room_id),
        )
        pinned = int(bool(is_pinned)) if is_pinned is not None else (
            int(current[0][0]) if current else 0
        )
        level = int(notify_level) if notify_level is not None else (
            int(current[0][1]) if current else 0
        )
        if level not in (0, 1, 2):
            return False
        self.execute(
            """INSERT INTO room_preferences(uid, room_id, is_pinned, notify_level, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(uid, room_id) DO UPDATE SET
                 is_pinned = excluded.is_pinned,
                 notify_level = excluded.notify_level,
                 updated_at = excluded.updated_at""",
            (uid, room_id, pinned, level, time.time()),
        )
        return True

    def set_message_mentions(self, mid: int, mentioned_uids) -> None:
        values = [(mid, int(uid)) for uid in set(mentioned_uids)]
        if values:
            self.update(
                "INSERT OR IGNORE INTO message_mentions(mid, uid) VALUES (?, ?)",
                values,
            )

    def pin_message(self, message_id: int, group_id: int, pinned_by_uid: int) -> int:
        with self.lock:
            self.cursor.execute(
                """INSERT OR IGNORE INTO group_pinned_messages
                   (message_id, group_id, pinned_by_uid, created_at)
                   VALUES (?, ?, ?, ?)""",
                (message_id, group_id, pinned_by_uid, time.time()),
            )
            pin_id = self.cursor.lastrowid
            self.conn.commit()
            return pin_id

    def unpin_message(self, pin_id: int, group_id: int) -> bool:
        with self.lock:
            self.cursor.execute(
                "DELETE FROM group_pinned_messages WHERE pin_id = ? AND group_id = ?",
                (pin_id, group_id),
            )
            self.conn.commit()
            return self.cursor.rowcount > 0

    def unpin_message_by_mid(self, message_id: int, group_id: int) -> bool:
        with self.lock:
            self.cursor.execute(
                "DELETE FROM group_pinned_messages WHERE message_id = ? AND group_id = ?",
                (message_id, group_id),
            )
            self.conn.commit()
            return self.cursor.rowcount > 0

    def get_pinned_messages(self, group_id: int) -> list:
        return self.query(
            """SELECT pin_id, message_id, group_id, pinned_by_uid, created_at
               FROM group_pinned_messages
               WHERE group_id = ?
               ORDER BY created_at ASC""",
            (group_id,),
        )

    def is_message_pinned(self, message_id: int, group_id: int) -> bool:
        rows = self.query(
            "SELECT 1 FROM group_pinned_messages WHERE message_id = ? AND group_id = ?",
            (message_id, group_id),
        )
        return len(rows) > 0

    def get_pin_by_message(self, message_id: int, group_id: int):
        rows = self.query(
            """SELECT pin_id, message_id, group_id, pinned_by_uid, created_at
               FROM group_pinned_messages
               WHERE message_id = ? AND group_id = ?""",
            (message_id, group_id),
        )
        if not rows:
            return None
        record = rows[0]
        return {
            "pin_id": record[0],
            "message_id": record[1],
            "group_id": record[2],
            "pinned_by_uid": record[3],
            "created_at": record[4],
        }
