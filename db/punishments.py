import time

from db.tool import Db

# 处罚类型：
#   warn  警告，不影响账号
#   mute 禁言，无法写入内容
#   ban 封禁
#
PUNISHMENT_KINDS = ("warn", "mute", "ban")


_RESTRICTION_ORDER = ("ban", "mute", "warn")


class PunishmentsDb(Db):
    def __init__(self, path: str, port_api: int, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self._ensure_table()

    def _ensure_table(self):
        self.execute("""
CREATE TABLE IF NOT EXISTS punishments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    reason     TEXT    NOT NULL,
    issued_by  INTEGER NOT NULL,
    created_at REAL    NOT NULL,
    expires_at REAL,
    revoked_at REAL,
    revoked_by INTEGER
)
""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_punishments_uid_time "
            "ON punishments (uid, created_at)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_punishments_active "
            "ON punishments (uid, revoked_at, expires_at)"
        )

    def create(self, uid: int, kind: str, reason: str, issued_by: int,
               duration_seconds=None) -> int:
        if kind not in PUNISHMENT_KINDS:
            return 0
        now = time.time()
        expires_at = None
        if duration_seconds is not None and duration_seconds > 0:
            expires_at = now + float(duration_seconds)
        # 不要在外层再套 self.lock
        # execute() 内部已加锁
        return int(self.execute(
            "INSERT INTO punishments (uid, kind, reason, issued_by, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (int(uid), kind, reason, int(issued_by), now, expires_at),
        ) or 0)

    def list_for_user(self, uid: int, include_inactive: bool = True):
        rows = self.query(
            "SELECT id, uid, kind, reason, issued_by, created_at, expires_at, "
            "revoked_at, revoked_by FROM punishments WHERE uid = ? "
            "ORDER BY created_at DESC",
            (int(uid),),
        )
        if include_inactive:
            return rows
        now = time.time()
        return [
            row for row in rows
            if row[7] is None and (row[6] is None or row[6] > now)
        ]

    def get(self, punishment_id: int):
        rows = self.query(
            "SELECT id, uid, kind, reason, issued_by, created_at, expires_at, "
            "revoked_at, revoked_by FROM punishments WHERE id = ?",
            (int(punishment_id),),
        )
        return rows[0] if rows else None

    def active_of_kind(self, uid: int, kind: str):
        """
        当前生效的某类处罚
        """
        rows = self.query(
            "SELECT id, uid, kind, reason, issued_by, created_at, expires_at, "
            "revoked_at, revoked_by FROM punishments "
            "WHERE uid = ? AND kind = ? AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (int(uid), kind, time.time()),
        )
        return rows[0] if rows else None

    def active_ban(self, uid: int):
        """当前生效的封禁（登录拦截"""
        return self.active_of_kind(uid, "ban")

    def active_mute(self, uid: int):
        """当前生效的禁言（内容写入拦截"""
        return self.active_of_kind(uid, "mute")

    def active_restriction(self, uid: int):
        for kind in _RESTRICTION_ORDER:
            row = self.active_of_kind(uid, kind)
            if row is not None:
                return row
        return None

    def revoke(self, punishment_id: int, revoked_by: int) -> bool:
        self.execute(
            "UPDATE punishments SET revoked_at = ?, revoked_by = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (time.time(), int(revoked_by), int(punishment_id)),
        )
        return True

    @staticmethod
    def is_expired(row) -> bool:
        expires_at = row[6]
        return expires_at is not None and expires_at <= time.time()
