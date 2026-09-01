from db.tool import Db
import json
import time

_MESSAGE_EVENTS = {"message.plain", "message.file", "message.recalled"}

class NotificationsDb(Db):
    def __init__(self, path: str, port_api: int, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self._ensure_unified_table()
        self._migrate_new_columns()
        self._migrate_legacy_tables()

    def _ensure_unified_table(self):
        """统一通知表"""
        self.execute("""
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        INTEGER NOT NULL,
    time_stamp REAL    NOT NULL,
    info       TEXT    NOT NULL,
    read_at    REAL,
    kind       INTEGER NOT NULL DEFAULT 0
)
""")
        self.execute("""
CREATE TABLE IF NOT EXISTS notification_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_uid_ts "
            "ON notifications (uid, time_stamp)"
        )

    def _migrate_new_columns(self):
        """为旧 db补 read_at / kind 列，并回填历史数据（仅首次，幂等）。

        - kind: 消息事件=1，系统事件=0（旧消息事件保留在库中，仅不再展示）
        - read_at: 旧通知一律视为已读（read_at = time_stamp），未读数从 0 起步
        """
        for col, typ in [("read_at", "REAL"), ("kind", "INTEGER")]:
            try:
                self.execute("ALTER TABLE notifications ADD COLUMN {} {}".format(col, typ))
            except Exception:
                pass
        try:
            self.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_uid_kind "
                "ON notifications (uid, kind, time_stamp)"
            )
        except Exception:
            pass
        self._backfill_kind_and_read()

    def _backfill_kind_and_read(self):
        with self.lock:
            def operation():
                marker = self.cursor.execute(
                    "SELECT value FROM notification_meta WHERE key = 'v2_migrated'"
                ).fetchone()
                if marker:
                    self.conn.commit()
                    return
                rows = self.cursor.execute(
                    "SELECT id, info FROM notifications WHERE kind IS NULL"
                ).fetchall()
                if rows:
                    updates = []
                    for nid, raw_info in rows:
                        event = self._deserialize_event(raw_info).get("event", "")
                        updates.append((1 if event in _MESSAGE_EVENTS else 0, nid))
                    self.cursor.executemany(
                        "UPDATE notifications SET kind = ? WHERE id = ?", updates
                    )
                self.cursor.execute(
                    "UPDATE notifications SET read_at = time_stamp WHERE read_at IS NULL"
                )
                self.cursor.execute(
                    "INSERT OR REPLACE INTO notification_meta(key, value) "
                    "VALUES ('v2_migrated', '1')"
                )
                self.conn.commit()
                print("[INFO] DATABASE 完成通知 v2 迁移（kind/read_at 回填）")
            self._execute_with_retry(operation)

    def _migrate_legacy_tables(self):
        """将 U{uid} 格式的每用户表迁移到统一表"""
        rows = self.query(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name GLOB 'U[0-9]*'"
        )
        if not rows:
            return

        migrated = 0
        for (table_name,) in rows:
            uid_str = table_name[1:]
            try:
                uid = int(uid_str)
            except ValueError:
                continue

            old_rows = self.query("SELECT time_stamp, info FROM {}".format(table_name))

            if old_rows:
                existing_ts = {
                    ts for (ts,) in self.query(
                        "SELECT time_stamp FROM notifications WHERE uid = ?", (uid,)
                    )
                }
                to_insert = []
                for ts, info in old_rows:
                    if ts in existing_ts:
                        continue
                    event = self._deserialize_event(info)
                    to_insert.append(
                        (uid, ts, self._serialize_event(event), self._event_kind(event))
                    )
                if to_insert:
                    self.update(
                        "INSERT INTO notifications (uid, time_stamp, info, kind) VALUES (?, ?, ?, ?)",
                        to_insert,
                    )

            self.execute("DROP TABLE IF EXISTS {}".format(table_name))
            migrated += 1

        if migrated:
            print("[INFO] DATABASE 自动迁移了 {} 个旧版每用户表到统一表".format(migrated))

    def _serialize_event(self, event):
        if isinstance(event, (dict, list)):
            return json.dumps(event, ensure_ascii=False)
        return str(event)

    def _deserialize_event(self, raw_event: str):
        try:
            event = json.loads(raw_event)
            if isinstance(event, dict):
                return event
            return {"content": event}
        except json.JSONDecodeError:
            return {"content": raw_event}

    def create_user_table(self, uid: int):
        """deprecated，但是现留着"""
        pass

    def delete_user_table(self, uid: int):
        """删除用户的所有通知"""
        uid = int(uid)
        self.execute("DELETE FROM notifications WHERE uid = ?", (uid,))

    @staticmethod
    def _event_kind(event: dict) -> int:
        return 1 if event.get("event") in _MESSAGE_EVENTS else 0

    def add_event(self, uid: int, event) -> dict:
        """写入一条通知，返回 {"id": nid, "time_stamp": ts}。"""
        uid = int(uid)
        ts = time.time()
        nid = self.execute(
            "INSERT INTO notifications (uid, time_stamp, info, kind) VALUES (?, ?, ?, ?)",
            (uid, ts, self._serialize_event(event), self._event_kind(self._as_dict(event))),
        )
        return {"id": nid, "time_stamp": ts}

    def add_events(self, uid_and_events) -> list:
        """批量插入多条通知，单事务提交，返回与输入顺序一致的记录列表。

        :param uid_and_events: [(uid, event), ...]
        :return: [{"id": nid, "time_stamp": ts}, ...]
        """
        if not uid_and_events:
            return []
        now = time.time()
        values = []
        for index, (uid, event) in enumerate(uid_and_events):
            ts = now + index * 1e-6
            values.append((int(uid), ts, self._serialize_event(event), self._event_kind(self._as_dict(event))))
        with self.lock:
            def operation():
                self.cursor.executemany(
                    "INSERT INTO notifications (uid, time_stamp, info, kind) VALUES (?, ?, ?, ?)",
                    values,
                )
                self.conn.commit()
            self._execute_with_retry(operation)
        # 按 (uid, time_stamp) 回查 id（取最大 id，兼容低时间戳精度），保证与输入同序
        records = []
        for uid, ts, _, _ in values:
            rows = self.query(
                "SELECT id FROM notifications WHERE uid = ? AND time_stamp = ? "
                "ORDER BY id DESC LIMIT 1",
                (uid, ts),
            )
            nid = rows[0][0] if rows else None
            records.append({"id": nid, "time_stamp": ts})
        return records

    @staticmethod
    def _as_dict(event) -> dict:
        if isinstance(event, dict):
            return event
        try:
            parsed = json.loads(event) if isinstance(event, str) else None
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def redact_recalled_message(self, mid : int):
        """[DEPRECATED] 删除消息内容，保留引用预览。

        自 v2 起消息事件不再写入 notifications 表，此方法仅为兼容旧数据保留，
        新代码不应再调用。
        """
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT id, info FROM notifications WHERE info LIKE ? OR info LIKE ?",
                    ('%"mid": {}%'.format(int(mid)),
                     '%"mid":{}%'.format(int(mid))),
                )
                updates = []
                for notification_id, raw_info in self.cursor.fetchall():
                    event = self._deserialize_event(raw_info)
                    changed = False
                    if (event.get("event") in {"message.plain", "message.file"}
                            and event.get("mid") == int(mid)):
                        event["content"] = None
                        event.pop("file_hash", None)
                        event.pop("file", None)
                        event["deleted"] = True
                        changed = True
                    preview = event.get("quote_preview")
                    if isinstance(preview, dict) and preview.get("mid") == int(mid):
                        preview["content"] = None
                        preview["file_hash"] = None
                        preview.pop("file", None)
                        preview["deleted"] = True
                        changed = True
                    if changed:
                        updates.append((self._serialize_event(event), notification_id))
                if updates:
                    self.cursor.executemany(
                        "UPDATE notifications SET info = ? WHERE id = ?", updates)
                self.conn.commit()
                return len(updates)

            return self._execute_with_retry(operation)

    def query_events_after(self, uid: int, time_stamp, limit: int = None):
        uid = int(uid)
        sql = ("SELECT id, time_stamp, read_at, info FROM notifications "
               "WHERE uid = ? AND (kind IS NULL OR kind = 0) AND time_stamp > ? "
               "ORDER BY time_stamp ASC")
        params = (uid, time_stamp)
        if limit is not None:
            sql += " LIMIT ?"
            params = params + (limit,)
        return self.query(sql, params)

    def query_all_events(self, uid: int):
        return self.query_events_after(uid, 0)

    def list_events_page(self, uid: int, offset: int = 0, take: int = 50) -> tuple:
        """分页查询系统通知（kind=0），按时间倒序。返回 (rows, total)。"""
        uid = int(uid)
        total_rows = self.query(
            "SELECT COUNT(*) FROM notifications WHERE uid = ? AND (kind IS NULL OR kind = 0)",
            (uid,),
        )
        total = total_rows[0][0] if total_rows and total_rows[0][0] is not None else 0
        rows = self.query(
            "SELECT id, time_stamp, read_at, info FROM notifications "
            "WHERE uid = ? AND (kind IS NULL OR kind = 0) "
            "ORDER BY time_stamp DESC, id DESC LIMIT ? OFFSET ?",
            (uid, take, offset),
        )
        return rows, total

    def unread_count(self, uid: int) -> int:
        uid = int(uid)
        rows = self.query(
            "SELECT COUNT(*) FROM notifications "
            "WHERE uid = ? AND (kind IS NULL OR kind = 0) AND read_at IS NULL",
            (uid,),
        )
        return rows[0][0] if rows and rows[0][0] is not None else 0

    def mark_read_until(self, uid: int, time_stamp) -> int:
        """把指定时间戳及之前的系统通知全部标为已读，返回影响数。"""
        uid = int(uid)
        with self.lock:
            def operation():
                self.cursor.execute(
                    "UPDATE notifications SET read_at = ? "
                    "WHERE uid = ? AND (kind IS NULL OR kind = 0) "
                    "AND time_stamp <= ? AND read_at IS NULL",
                    (time.time(), uid, time_stamp),
                )
                changed = self.cursor.rowcount
                self.conn.commit()
                return changed
            return self._execute_with_retry(operation)

    def mark_read_ids(self, uid: int, ids) -> int:
        """把指定 id 的系统通知标为已读，返回影响数。"""
        ids = [int(i) for i in ids if str(i).lstrip('-').isdigit()]
        if not ids:
            return 0
        uid = int(uid)
        placeholders = ",".join("?" * len(ids))
        with self.lock:
            def operation():
                self.cursor.execute(
                    "UPDATE notifications SET read_at = ? "
                    "WHERE uid = ? AND (kind IS NULL OR kind = 0) "
                    "AND id IN ({}) AND read_at IS NULL".format(placeholders),
                    tuple([time.time(), uid] + ids),
                )
                changed = self.cursor.rowcount
                self.conn.commit()
                return changed
            return self._execute_with_retry(operation)

    def mark_all_read(self, uid: int) -> int:
        uid = int(uid)
        with self.lock:
            def operation():
                self.cursor.execute(
                    "UPDATE notifications SET read_at = ? "
                    "WHERE uid = ? AND (kind IS NULL OR kind = 0) AND read_at IS NULL",
                    (time.time(), uid),
                )
                changed = self.cursor.rowcount
                self.conn.commit()
                return changed
            return self._execute_with_retry(operation)

    def list_events_after(self, uid: int, time_stamp):
        events = []
        for row in self.query_events_after(uid, time_stamp):
            nid, item_ts, read_at, raw_event = row[0], row[1], row[2], row[3]
            event = self._deserialize_event(raw_event)
            event["time_stamp"] = item_ts
            event["id"] = nid
            event["read_at"] = read_at
            events.append(event)
        return events

    def list_all_events(self, uid: int):
        return self.list_events_after(uid, 0)

    def serialize_rows(self, rows):
        serialized = []
        for row in rows:
            nid, ts, read_at, raw_event = row[0], row[1], row[2], row[3]
            event = self._deserialize_event(raw_event)
            serialized.append({
                "id": nid,
                "time_stamp": ts,
                "read_at": read_at,
                "info": event,
            })
        return serialized

    def delete_events_before(self, uid: int, time_stamp) -> bool:
        uid = int(uid)
        self.execute(
            "DELETE FROM notifications WHERE uid = ? AND time_stamp <= ?",
            (uid, time_stamp),
        )
        return True

    def delete_events_by_sender(self, uid: int, event: str, sender: int) -> int:
        """删除某用户收到的指定事件通知（按发送者过滤），返回删除条数。

        用于好友申请被处理（通过/拒绝）后清理对应的 friend.request 通知，
        避免客户端重复展示已处理的申请。
        """
        uid = int(uid)
        sender = int(sender)
        rows = self.query(
            "SELECT id, info FROM notifications WHERE uid = ? AND (kind IS NULL OR kind = 0)",
            (uid,),
        )
        target_ids = []
        for nid, raw_info in rows:
            info = self._deserialize_event(raw_info)
            if info.get("event") != event:
                continue
            s = info.get("sender")
            if s == sender or s == str(sender) or s == "U{}".format(sender):
                target_ids.append(nid)
        if target_ids:
            placeholders = ",".join("?" * len(target_ids))
            self.execute(
                "DELETE FROM notifications WHERE id IN ({})".format(placeholders),
                tuple(target_ids),
            )
        return len(target_ids)

    def delete_all_events(self, uid: int) -> bool:
        uid = int(uid)
        self.execute("DELETE FROM notifications WHERE uid = ?", (uid,))
        return True
