from db.tool import Db
import json
import time

class NotificationsDb(Db):
    def __init__(self, path: str, port_api: int, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self._ensure_unified_table()
        self._migrate_legacy_tables()

    def _ensure_unified_table(self):
        """统一通知表"""
        self.execute("""
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        INTEGER NOT NULL,
    time_stamp REAL    NOT NULL,
    info       TEXT    NOT NULL
)
""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_uid_ts "
            "ON notifications (uid, time_stamp)"
        )

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
                to_insert = [
                    (uid, ts, info)
                    for ts, info in old_rows
                    if ts not in existing_ts
                ]
                if to_insert:
                    self.update(
                        "INSERT INTO notifications (uid, time_stamp, info) VALUES (?, ?, ?)",
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
    def add_event(self, uid: int, event) -> float:
        uid = int(uid)
        ts = time.time()
        self.execute(
            "INSERT INTO notifications (uid, time_stamp, info) VALUES (?, ?, ?)",
            (uid, ts, self._serialize_event(event)),
        )
        return ts

    def add_events(self, uid_and_events) -> list:
        """批量插入多条通知，单事务提交，返回与输入顺序一致的时间戳列表。

        :param uid_and_events: [(uid, event), ...]
        """
        if not uid_and_events:
            return []
        now = time.time()
        values = []
        timestamps = []
        for index, (uid, event) in enumerate(uid_and_events):
            ts = now + index * 1e-6
            values.append((int(uid), ts, self._serialize_event(event)))
            timestamps.append(ts)
        with self.lock:
            def operation():
                self.cursor.executemany(
                    "INSERT INTO notifications (uid, time_stamp, info) VALUES (?, ?, ?)",
                    values,
                )
                self.conn.commit()
            self._execute_with_retry(operation)
        return timestamps

    def redact_recalled_message(self, mid : int):
        """删除消息内容，保留引用预览"""
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

    def query_events_after(self, uid: int, time_stamp):
        uid = int(uid)
        return self.query(
            "SELECT time_stamp, info FROM notifications "
            "WHERE uid = ? AND time_stamp > ? "
            "ORDER BY time_stamp ASC",
            (uid, time_stamp),
        )

    def query_all_events(self, uid: int):
        return self.query_events_after(uid, 0)

    def list_events_after(self, uid: int, time_stamp):
        events = []
        for item_ts, raw_event in self.query_events_after(uid, time_stamp):
            event = self._deserialize_event(raw_event)
            event["time_stamp"] = item_ts
            events.append(event)
        return events

    def list_all_events(self, uid: int):
        return self.list_events_after(uid, 0)

    def serialize_rows(self, rows):
        serialized = []
        for ts, raw_event in rows:
            event = self._deserialize_event(raw_event)
            serialized.append({"time_stamp": ts, "info": event})
        return serialized

    def delete_events_before(self, uid: int, time_stamp) -> bool:
        uid = int(uid)
        self.execute(
            "DELETE FROM notifications WHERE uid = ? AND time_stamp <= ?",
            (uid, time_stamp),
        )
        return True

    def delete_all_events(self, uid: int) -> bool:
        uid = int(uid)
        self.execute("DELETE FROM notifications WHERE uid = ?", (uid,))
        return True
