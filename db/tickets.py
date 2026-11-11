import json
import time

from db.dialect import SQLiteDialect
from db.tool import Db


TICKET_TYPES = ("report", "consult", "appeal")


TICKET_STATUSES = ("open", "processing", "resolved", "rejected")

_STATUS_TRANSITIONS = {
    "open": {"processing", "resolved", "rejected"},
    "processing": {"resolved", "rejected"},
    "resolved": set(),
    "rejected": set(),
}


TICKET_PRIORITIES = ("low", "normal", "high", "urgent")


_PRIORITY_ORDER_SQL = (
    "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 2 END"
)

# 列顺序固定
# 新增列只能追加在末尾，否则所有下标都会错位。
_TICKET_COLUMNS = (
    "id, uid, type, target_type, target_id, title, content, status, "
    "handler_uid, reply, created_at, updated_at, priority, assignee_uid, "
    "user_unread, attachments"
)

_MESSAGE_COLUMNS = "id, ticket_id, sender_uid, content, is_staff, created_at, attachments"


def _dump_hashes(hashes) -> str:
    if not hashes:
        return "[]"
    return json.dumps(list(hashes), ensure_ascii=False)


def _load_hashes(raw) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


class TicketsDb(Db):
    def __init__(self, path: str, port_api: int, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self._ensure_table()

    def _ensure_table(self):
        self.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         INTEGER NOT NULL,
    type        TEXT    NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    title       TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'open',
    handler_uid INTEGER,
    reply       TEXT,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    priority    TEXT    NOT NULL DEFAULT 'normal',
    assignee_uid INTEGER,
    user_unread INTEGER NOT NULL DEFAULT 0,
    attachments TEXT    NOT NULL DEFAULT '[]'
)
""")
        # 老库升级：早期版本没有 priority / assignee_uid / user_unread
        if isinstance(self.dialect, SQLiteDialect):
            existing = {row[1] for row in self.query("PRAGMA table_info(tickets)")}
            for column, ddl in (
                ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
                ("assignee_uid", "INTEGER"),
                ("user_unread", "INTEGER NOT NULL DEFAULT 0"),
                ("attachments", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if column not in existing:
                    self.execute(
                        "ALTER TABLE tickets ADD COLUMN {} {}".format(column, ddl)
                    )

        self.execute("""
CREATE TABLE IF NOT EXISTS ticket_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL,
    sender_uid INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    is_staff   INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    attachments TEXT   NOT NULL DEFAULT '[]'
)
""")
        if isinstance(self.dialect, SQLiteDialect):
            message_columns = {
                row[1] for row in self.query("PRAGMA table_info(ticket_messages)")
            }
            if "attachments" not in message_columns:
                self.execute(
                    "ALTER TABLE ticket_messages ADD COLUMN attachments "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket "
            "ON ticket_messages (ticket_id, created_at)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_uid_time "
            "ON tickets (uid, created_at)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_status_time "
            "ON tickets (status, created_at)"
        )

    def create(self, uid: int, ticket_type: str, title: str, content: str,
               target_type=None, target_id=None, priority="normal",
               attachments=None) -> int:
        now = time.time()
        if priority not in TICKET_PRIORITIES:
            priority = "normal"
        # 不要在外层再套 self.lock
        # threading.Lock 不可重入
        # execute() 返回 lastrowid
        return int(self.execute(
            "INSERT INTO tickets (uid, type, target_type, target_id, title, "
            "content, status, created_at, updated_at, priority, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
            (int(uid), ticket_type, target_type,
             None if target_id is None else str(target_id),
             title, content, now, now, priority, _dump_hashes(attachments)),
        ) or 0)

    def list_for_user(self, uid: int, offset: int = 0, take: int = 50):
        rows = self.query(
            "SELECT {} FROM tickets WHERE uid = ? "
            "ORDER BY updated_at DESC LIMIT ? OFFSET ?".format(_TICKET_COLUMNS),
            (int(uid), int(take), int(offset)),
        )
        total = self.query(
            "SELECT COUNT(*) FROM tickets WHERE uid = ?", (int(uid),)
        )
        return rows, (total[0][0] if total else 0)

    def list_queue(self, status=None, offset: int = 0, take: int = 50,
                   include_closed: bool = False, sort: str = "priority",
                   ticket_type=None, priority=None, creator_uid=None):
        """
        管理端队列
        """
        clauses = []
        args = []
        if status:
            clauses.append("status = ?")
            args.append(status)
        elif not include_closed:
            clauses.append("status IN ('open', 'processing')")
        if ticket_type:
            clauses.append("type = ?")
            args.append(ticket_type)
        if priority:
            clauses.append("priority = ?")
            args.append(priority)
        if creator_uid is not None:
            clauses.append("uid = ?")
            args.append(int(creator_uid))

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order = ("updated_at DESC" if sort == "time"
                 else "{}, updated_at ASC".format(_PRIORITY_ORDER_SQL))
        rows = self.query(
            "SELECT {} FROM tickets {} ORDER BY {} LIMIT ? OFFSET ?".format(
                _TICKET_COLUMNS, where, order),
            tuple(args) + (int(take), int(offset)),
        )
        total = self.query(
            "SELECT COUNT(*) FROM tickets {}".format(where), tuple(args)
        )
        return rows, (total[0][0] if total else 0)

    def get(self, ticket_id: int):
        rows = self.query(
            "SELECT {} FROM tickets WHERE id = ?".format(_TICKET_COLUMNS),
            (int(ticket_id),),
        )
        return rows[0] if rows else None

    @staticmethod
    def load_attachments(raw) -> list:
        return _load_hashes(raw)

    def pending_count(self) -> int:
        rows = self.query(
            "SELECT COUNT(*) FROM tickets WHERE status IN ('open', 'processing')"
        )
        return rows[0][0] if rows else 0

    def user_unread_total(self, uid: int) -> int:
        rows = self.query(
            "SELECT COALESCE(SUM(user_unread), 0) FROM tickets WHERE uid = ?",
            (int(uid),),
        )
        return int(rows[0][0]) if rows else 0

    def mark_read(self, ticket_id: int) -> bool:
        self.execute(
            "UPDATE tickets SET user_unread = 0 WHERE id = ?", (int(ticket_id),)
        )
        return True

    def list_messages(self, ticket_id: int):
        return self.query(
            "SELECT {} FROM ticket_messages WHERE ticket_id = ? "
            "ORDER BY created_at ASC".format(_MESSAGE_COLUMNS),
            (int(ticket_id),),
        )

    def add_message(self, ticket_id: int, sender_uid: int, content: str,
                    is_staff: bool, attachments=None) -> bool:
        """
        追加一条消息
        """
        row = self.get(ticket_id)
        if row is None:
            return False
        if not content and not attachments:
            return False
        now = time.time()
        self.execute(
            "INSERT INTO ticket_messages "
            "(ticket_id, sender_uid, content, is_staff, created_at, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(ticket_id), int(sender_uid), content, 1 if is_staff else 0, now,
             _dump_hashes(attachments)),
        )
        if is_staff:
            self.execute(
                "UPDATE tickets SET updated_at = ?, user_unread = user_unread + 1, "
                "handler_uid = COALESCE(handler_uid, ?) WHERE id = ?",
                (now, int(sender_uid), int(ticket_id)),
            )
        else:
            extra = ""
            if row[7] in ("resolved", "rejected"):
                extra = ", status = 'open'"
            self.execute(
                "UPDATE tickets SET updated_at = ?{} WHERE id = ?".format(extra),
                (now, int(ticket_id)),
            )
        return True

    def handle(self, ticket_id: int, handler_uid: int, status: str,
               reply=None, priority=None, assignee_uid=None,
               clear_assignee: bool = False):
        """
        推进工单状态（管理
        """
        if status is not None and status not in TICKET_STATUSES:
            return False, "invalid_status"
        if priority is not None and priority not in TICKET_PRIORITIES:
            return False, "invalid_priority"
        row = self.get(ticket_id)
        if row is None:
            return False, "not_found"
        current = row[7]
        if status is not None and status != current \
                and status not in _STATUS_TRANSITIONS.get(current, set()):
            return False, "invalid_transition"

        fields = ["updated_at = ?"]
        values = [time.time()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        fields.append("handler_uid = ?")
        values.append(int(handler_uid))
        if reply:
            # reply 保留为最后一次答复
            fields.append("reply = ?")
            values.append(reply)
        if priority is not None:
            fields.append("priority = ?")
            values.append(priority)
        if clear_assignee:
            fields.append("assignee_uid = NULL")
        elif assignee_uid is not None:
            fields.append("assignee_uid = ?")
            values.append(int(assignee_uid))
        values.append(int(ticket_id))

        self.execute(
            "UPDATE tickets SET {} WHERE id = ?".format(", ".join(fields)),
            tuple(values),
        )
        return True, "ok"
