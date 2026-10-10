from db.tool import Db
import json
import time


GROUP_MEMBERS_MIGRATION = "group_members_v1"


class GroupDb(Db):
    def __init__(self, path: str, port_api: int, config_reader=None, dialect=None):
        super().__init__(path, port_api, -1, dialect=dialect)
        self._config_reader = config_reader or self._default_config_reader
        self.create_group_table()

    def _default_config_reader(self):
        with open("res/{}/config.json".format(self.api_pt), "r+") as f:
            return json.load(f)

    def create_group_table(self):
        """
        essence 是精华，初始值为 []，存储 mid 时间戳
        """
        self.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                gid INTEGER UNIQUE NOT NULL,
                creater INTEGER NOT NULL,
                groupname TEXT,
                members TEXT,
                admins TEXT,
                enter_hint TEXT,
                introduction TEXT,
                allow_direct_join INTEGER NOT NULL DEFAULT 0,
                require_review INTEGER NOT NULL DEFAULT 1,
                essence TEXT,
                public_messages INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS join_requests (
                rid INTEGER PRIMARY KEY AUTOINCREMENT,
                gid INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                inviter_uid INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                request_time REAL NOT NULL,
                message TEXT NOT NULL DEFAULT ''
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS group_id_sequence (
                id INTEGER PRIMARY KEY AUTOINCREMENT
            )
        """)
        self.execute("""
            INSERT OR IGNORE INTO group_id_sequence(id)
            SELECT MAX(gid) FROM groups
            WHERE EXISTS(SELECT 1 FROM groups)
              AND NOT EXISTS(SELECT 1 FROM group_id_sequence)
        """)
        self._migrate()

    def _parse_essence(self, raw) -> list:
        if not raw:
            return []
        try:
            result = json.loads(raw)
            return result if isinstance(result, list) else []
        except (ValueError, TypeError):
            return []

    def add_essence(self, gid, mid):
        with self.lock:
            def _add_essence():
                self.cursor.execute("SELECT essence FROM groups WHERE gid = ?", (gid,))
                row = self.cursor.fetchone()
                if not row:
                    return False
                essence_list = self._parse_essence(row[0])
                if mid in essence_list:
                    return False
                essence_list.append(mid)
                essence_list.sort(reverse=True)
                self.cursor.execute("UPDATE groups SET essence = ? WHERE gid = ?", (json.dumps(essence_list), gid))
                self.conn.commit()
                return True
            return self._execute_with_retry(_add_essence)

    def remove_essence(self, gid, mid):
        with self.lock:
            def _remove_essence():
                self.cursor.execute("SELECT essence FROM groups WHERE gid = ?", (gid,))
                row = self.cursor.fetchone()
                if not row:
                    return False
                essence_list = self._parse_essence(row[0])
                if mid not in essence_list:
                    return False
                essence_list.remove(mid)
                self.cursor.execute("UPDATE groups SET essence = ? WHERE gid = ?", (json.dumps(essence_list), gid))
                self.conn.commit()
                return True
            return self._execute_with_retry(_remove_essence)

    def query_essence(self, gid):
        row = self.query("SELECT essence FROM groups WHERE gid = ?", (gid,))
        if not row:
            return []
        return self._parse_essence(row[0][0])

    def _migrate(self):
        try:
            self.execute("ALTER TABLE groups ADD COLUMN allow_direct_join INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            self.execute("ALTER TABLE groups ADD COLUMN require_review INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
        try:
            self.execute("ALTER TABLE groups ADD COLUMN essence TEXT DEFAULT '[]'")
        except Exception:
            pass
        try:
            self.execute("ALTER TABLE groups ADD COLUMN essence_enabled INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
        try:
            self.execute("ALTER TABLE groups ADD COLUMN public_messages INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            self.execute("ALTER TABLE join_requests ADD COLUMN message TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        self.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                gid INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                role INTEGER NOT NULL DEFAULT 0 CHECK(role IN (0, 1, 2)),
                join_time REAL NOT NULL,
                PRIMARY KEY (gid, uid)
            )
        """)
        self.execute("CREATE INDEX IF NOT EXISTS idx_group_members_uid ON group_members(uid, gid)")
        self.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at REAL NOT NULL
            )
        """)
        self._migrate_legacy_members()

    def _migrate_legacy_members(self):
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = ?",
                    (GROUP_MEMBERS_MIGRATION,),
                )
                if self.cursor.fetchone() is not None:
                    return False

                self.cursor.execute("SELECT gid, creater, members, admins FROM groups")
                for gid, creater, members_raw, admins_raw in self.cursor.fetchall():
                    try:
                        members = {int(uid) for uid in json.loads(members_raw or "[]")}
                    except (TypeError, ValueError):
                        members = set()
                    try:
                        admins = {int(uid) for uid in json.loads(admins_raw or "[]")}
                    except (TypeError, ValueError):
                        admins = set()

                    creater = int(creater)
                    all_members = members | admins | {creater}
                    for member_uid in all_members:
                        role = 2 if member_uid == creater else (1 if member_uid in admins else 0)
                        self.cursor.execute(
                            "INSERT OR REPLACE INTO group_members (gid, uid, role, join_time) VALUES (?, ?, ?, ?)",
                            (gid, member_uid, role, 0),
                        )

                self.cursor.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    (GROUP_MEMBERS_MIGRATION, time.time()),
                )
                self.conn.commit()
                return True

            return self._execute_with_retry(operation)

    def _hydrate_group_rows(self, rows):
        if not rows:
            return []
        gids = [row[0] for row in rows]
        placeholders = ",".join("?" * len(gids))
        params = tuple(gids)

        member_rows = self.query(
            f"SELECT gid, uid FROM group_members WHERE gid IN ({placeholders}) ORDER BY gid, join_time ASC, uid ASC",
            params,
        )
        member_map = {}
        for gid, uid in member_rows:
            member_map.setdefault(gid, []).append(uid)

        admin_rows = self.query(
            f"SELECT gid, uid FROM group_members WHERE gid IN ({placeholders}) AND role = 1 ORDER BY gid, join_time ASC, uid ASC",
            params,
        )
        admin_map = {}
        for gid, uid in admin_rows:
            admin_map.setdefault(gid, []).append(uid)

        hydrated = []
        for row in rows:
            gid = row[0]
            r = list(row)
            r[3] = json.dumps(member_map.get(gid, []))
            r[4] = json.dumps(admin_map.get(gid, []))
            hydrated.append(tuple(r))
        return hydrated

    def query_gid(self, gid: int):
        rows = self.query("SELECT * FROM groups WHERE gid = ?", (gid,))
        return self._hydrate_group_rows(rows)

    def query_creater(self, uid: int):
        rows = self.query("SELECT * FROM groups WHERE creater = ?", (uid,))
        return self._hydrate_group_rows(rows)

    def groupname_search(self, groupname: str):
        """[deprecated] 群名搜索。
        """
        if not isinstance(groupname, str):
            return []
        escaped = (
            groupname.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        rows = self.query(
            "SELECT * FROM groups WHERE groupname LIKE ? ESCAPE '\\' "
            "ORDER BY gid ASC LIMIT 50",
            ('%' + escaped + '%',),
        )
        results = []
        for row in rows:
            r = list(row)
            r[3] = "[]"
            r[4] = "[]"
            results.append(r)
        return results

    def search_public(self, keyword: str, limit: int = 20, offset: int = 0):
        """按群名搜索，仅返回公开字段

        返回 (groups, has_more)，groups 为 dict 列表。
        """
        escaped = (
            keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        prefix = escaped + "%"
        contains = "%" + escaped + "%"
        rows = self.query(
            """SELECT g.gid, g.groupname, g.introduction,
                      (SELECT COUNT(*) FROM group_members gm WHERE gm.gid = g.gid) AS member_count,
                      g.allow_direct_join, g.require_review, g.public_messages
               FROM groups g
               WHERE g.groupname LIKE ? ESCAPE '\\'
               ORDER BY CASE WHEN g.groupname LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,
                        g.gid ASC
               LIMIT ? OFFSET ?""",
            (contains, prefix, limit + 1, offset),
        )
        has_more = len(rows) > limit
        groups = [
            {
                "gid": r[0],
                "groupname": r[1],
                "introduction": r[2] or "",
                "member_count": r[3],
                "allow_direct_join": bool(r[4]),
                "require_review": bool(r[5]),
                "public_messages": bool(r[6]),
            }
            for r in rows[:limit]
        ]
        return groups, has_more

    def get_member_uids(self, gid: int) -> list:
        rows = self.query(
            "SELECT uid FROM group_members WHERE gid = ? ORDER BY join_time ASC, uid ASC",
            (gid,),
        )
        return [row[0] for row in rows]

    def get_admin_uids(self, gid: int) -> list:
        rows = self.query(
            "SELECT uid FROM group_members WHERE gid = ? AND role = 1 ORDER BY join_time ASC, uid ASC",
            (gid,),
        )
        return [row[0] for row in rows]

    def get_preview_members(self, gid: int, limit: int = 10) -> list:
        """预展示成员：所有者/管理员优先，返回 [(uid, role)]，最多 limit"""
        rows = self.query(
            """SELECT uid, role FROM group_members WHERE gid = ?
               ORDER BY (CASE role WHEN 2 THEN 0 WHEN 1 THEN 1 ELSE 2 END),
                        join_time ASC, uid ASC
               LIMIT ?""",
            (gid, limit),
        )
        return [(row[0], row[1]) for row in rows]

    def count_members(self, gid: int) -> int:
        rows = self.query(
            "SELECT COUNT(*) FROM group_members WHERE gid = ?",
            (gid,),
        )
        return rows[0][0] if rows else 0

    def is_admin(self, gid: int, uid: int) -> int:
        """0=member or non-member, 1=admin, 2=owner."""
        rows = self.query(
            "SELECT role FROM group_members WHERE gid = ? AND uid = ?",
            (gid, uid),
        )
        return rows[0][0] if rows else 0

    def is_member(self, gid: int, uid: int) -> bool:
        rows = self.query(
            "SELECT 1 FROM group_members WHERE gid = ? AND uid = ?",
            (gid, uid),
        )
        return bool(rows)

    def get_group_settings(self, gid: int) -> dict:
        row = self.query(
            """SELECT gid, creater, groupname, enter_hint, introduction,
                      allow_direct_join, require_review, essence_enabled, public_messages
               FROM groups WHERE gid = ?""",
            (gid,),
        )
        if not row:
            return {}
        r = row[0]
        return {
            "gid": r[0], "creater": r[1], "groupname": r[2],
            "enter_hint": r[3], "introduction": r[4],
            "allow_direct_join": bool(r[5]), "require_review": bool(r[6]),
            "essence_enabled": bool(r[7]) if r[7] is not None else True,
            "public_messages": bool(r[8]) if r[8] is not None else False,
        }

    def get_essence_enabled(self, gid: int) -> bool:
        row = self.query("SELECT essence_enabled FROM groups WHERE gid = ?", (gid,))
        if not row:
            return True
        val = row[0][0]
        return bool(val) if val is not None else True

    def get_user_group_rows(self, uid: int) -> list:
        rows = self.query(
            """SELECT g.* FROM groups g
               INNER JOIN group_members gm ON gm.gid = g.gid
               WHERE gm.uid = ? ORDER BY g.gid ASC""",
            (uid,),
        )
        return self._hydrate_group_rows(rows)

    def get_user_groups(self, uid: int) -> list:
        return [
            {"gid": row[0], "groupname": row[2]}
            for row in self.get_user_group_rows(uid)
        ]

    def create_group(self, creater: int, groupname: str, enter_hint: str,
                     introduction: str, allow_direct_join: bool = False,
                     require_review: bool = True) -> int:
        cfg = self._config_reader()
        limit = cfg.get("groups_limit", 30)

        with self.lock:
            def operation():
                self.cursor.execute("SELECT COUNT(*) FROM groups WHERE creater = ?", (creater,))
                if limit != -1 and self.cursor.fetchone()[0] >= limit:
                    return 0
                self.cursor.execute("INSERT INTO group_id_sequence DEFAULT VALUES")
                gid = self.cursor.lastrowid
                self.cursor.execute(
                    """INSERT INTO groups (gid, creater, groupname,
                       enter_hint, introduction, allow_direct_join, require_review, essence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (gid, creater, groupname, enter_hint, introduction,
                     int(allow_direct_join), int(require_review), "[]"),
                )
                self.cursor.execute(
                    "INSERT INTO group_members (gid, uid, role, join_time) VALUES (?, ?, 2, ?)",
                    (gid, creater, time.time()),
                )
                self.conn.commit()
                return gid

            return self._execute_with_retry(operation)

    def delete_group(self, gid: int):
        with self.lock:
            def operation():
                self.cursor.execute("DELETE FROM join_requests WHERE gid = ?", (gid,))
                self.cursor.execute("DELETE FROM group_members WHERE gid = ?", (gid,))
                self.cursor.execute("DELETE FROM groups WHERE gid = ?", (gid,))
                deleted = self.cursor.rowcount > 0
                self.conn.commit()
                return deleted

            return self._execute_with_retry(operation)

    def add_member(self, gid: int, new_member: int) -> bool:
        cfg = self._config_reader()
        limit = cfg.get("single_group_max_people", 200)
        with self.lock:
            def operation():
                self.cursor.execute("SELECT 1 FROM groups WHERE gid = ?", (gid,))
                if self.cursor.fetchone() is None:
                    return False
                self.cursor.execute(
                    "SELECT 1 FROM group_members WHERE gid = ? AND uid = ?",
                    (gid, new_member),
                )
                if self.cursor.fetchone() is not None:
                    return False
                self.cursor.execute("SELECT COUNT(*) FROM group_members WHERE gid = ?", (gid,))
                if limit != -1 and self.cursor.fetchone()[0] >= limit:
                    return False
                self.cursor.execute(
                    "INSERT INTO group_members (gid, uid, role, join_time) VALUES (?, ?, 0, ?)",
                    (gid, new_member, time.time()),
                )
                self.conn.commit()
                return True

            return self._execute_with_retry(operation)

    def remove_member(self, gid: int, member: int) -> bool:
        with self.lock:
            def operation():
                self.cursor.execute(
                    "DELETE FROM group_members WHERE gid = ? AND uid = ? AND role < 2",
                    (gid, member),
                )
                removed = self.cursor.rowcount > 0
                self.conn.commit()
                return removed

            return self._execute_with_retry(operation)

    def add_admin(self, gid: int, uid: int) -> bool:
        with self.lock:
            def operation():
                self.cursor.execute(
                    "UPDATE group_members SET role = 1 WHERE gid = ? AND uid = ? AND role = 0",
                    (gid, uid),
                )
                changed = self.cursor.rowcount > 0
                self.conn.commit()
                return changed

            return self._execute_with_retry(operation)

    def remove_admin(self, gid: int, uid: int) -> bool:
        with self.lock:
            def operation():
                self.cursor.execute(
                    "UPDATE group_members SET role = 0 WHERE gid = ? AND uid = ? AND role = 1",
                    (gid, uid),
                )
                changed = self.cursor.rowcount > 0
                self.conn.commit()
                return changed

            return self._execute_with_retry(operation)

    def transfer_owner(self, gid: int, old_owner: int, new_owner: int) -> bool:
        with self.lock:
            def operation():
                self.cursor.execute("SELECT creater FROM groups WHERE gid = ?", (gid,))
                group = self.cursor.fetchone()
                if not group or group[0] != old_owner:
                    return False
                self.cursor.execute(
                    "SELECT role FROM group_members WHERE gid = ? AND uid = ?",
                    (gid, new_owner),
                )
                if self.cursor.fetchone() is None:
                    return False
                self.cursor.execute(
                    "UPDATE group_members SET role = 1 WHERE gid = ? AND uid = ?",
                    (gid, old_owner),
                )
                self.cursor.execute(
                    "UPDATE group_members SET role = 2 WHERE gid = ? AND uid = ?",
                    (gid, new_owner),
                )
                self.cursor.execute("UPDATE groups SET creater = ? WHERE gid = ?", (new_owner, gid))
                self.conn.commit()
                return True

            return self._execute_with_retry(operation)

    def update_settings(self, gid: int, **kwargs) -> bool:
        allowed = {"groupname", "enter_hint", "introduction",
                   "allow_direct_join", "require_review", "essence_enabled",
                   "public_messages"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        if "allow_direct_join" in updates:
            updates["allow_direct_join"] = int(updates["allow_direct_join"])
        if "require_review" in updates:
            updates["require_review"] = int(updates["require_review"])
        if "essence_enabled" in updates:
            updates["essence_enabled"] = int(updates["essence_enabled"])
        if "public_messages" in updates:
            updates["public_messages"] = int(updates["public_messages"])
        set_clause = ", ".join("{} = ?".format(k) for k in updates)
        self.execute(
            "UPDATE groups SET {} WHERE gid = ?".format(set_clause),
            tuple(updates.values()) + (gid,),
        )
        return True

    def request_join(self, gid: int, uid: int, inviter_uid: int = 0,
                     message: str = '') -> int:
        """创建入群申请；已有 pending 时只更新最新留言与时间（保留原 rid，不新增行）。"""
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT rid FROM join_requests WHERE gid = ? AND uid = ? AND status = 'pending'",
                    (gid, uid),
                )
                existing = self.cursor.fetchone()
                if existing:
                    self.cursor.execute(
                        "UPDATE join_requests SET message = ?, request_time = ? WHERE rid = ?",
                        (message, time.time(), existing[0]),
                    )
                    self.conn.commit()
                    return existing[0]
                self.cursor.execute(
                    """INSERT INTO join_requests (gid, uid, inviter_uid, status, request_time, message)
                       VALUES (?, ?, ?, 'pending', ?, ?)""",
                    (gid, uid, inviter_uid, time.time(), message),
                )
                rid = self.cursor.lastrowid
                self.conn.commit()
                return rid

            return self._execute_with_retry(operation)

    def get_join_requests(self, gid: int, status: str = 'pending') -> list:
        rows = self.query(
            """SELECT rid, gid, uid, inviter_uid, status, request_time, message
               FROM join_requests WHERE gid = ? AND status = ? ORDER BY request_time DESC""",
            (gid, status),
        )
        return [
            {"rid": r[0], "gid": r[1], "uid": r[2], "inviter_uid": r[3],
             "status": r[4], "request_time": r[5], "message": r[6] or ""}
            for r in rows
        ]

    def handle_join_request(self, rid: int, approved: bool) -> bool:
        cfg = self._config_reader()
        limit = cfg.get("single_group_max_people", 200)
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT gid, uid, status FROM join_requests WHERE rid = ?",
                    (rid,),
                )
                request = self.cursor.fetchone()
                if not request or request[2] != 'pending':
                    return False
                gid, uid = request[0], request[1]
                if not approved:
                    self.cursor.execute(
                        "UPDATE join_requests SET status = 'rejected' WHERE rid = ?",
                        (rid,),
                    )
                    self.conn.commit()
                    return True

                self.cursor.execute("SELECT 1 FROM groups WHERE gid = ?", (gid,))
                if self.cursor.fetchone() is None:
                    return False
                self.cursor.execute(
                    "SELECT 1 FROM group_members WHERE gid = ? AND uid = ?",
                    (gid, uid),
                )
                if self.cursor.fetchone() is not None:
                    return False
                self.cursor.execute("SELECT COUNT(*) FROM group_members WHERE gid = ?", (gid,))
                if limit != -1 and self.cursor.fetchone()[0] >= limit:
                    return False
                self.cursor.execute(
                    "INSERT INTO group_members (gid, uid, role, join_time) VALUES (?, ?, 0, ?)",
                    (gid, uid, time.time()),
                )
                self.cursor.execute(
                    "UPDATE join_requests SET status = 'approved' WHERE rid = ?",
                    (rid,),
                )
                self.conn.commit()
                return True

            return self._execute_with_retry(operation)

    def remove_user_membership(self, uid: int):
        with self.lock:
            def operation():
                self.cursor.execute("SELECT gid FROM groups WHERE creater = ?", (uid,))
                deleted_gids = [row[0] for row in self.cursor.fetchall()]
                for gid in deleted_gids:
                    self.cursor.execute("DELETE FROM join_requests WHERE gid = ?", (gid,))
                    self.cursor.execute("DELETE FROM group_members WHERE gid = ?", (gid,))
                    self.cursor.execute("DELETE FROM groups WHERE gid = ?", (gid,))
                self.cursor.execute(
                    "DELETE FROM join_requests WHERE uid = ? OR inviter_uid = ?",
                    (uid, uid),
                )
                self.cursor.execute("DELETE FROM group_members WHERE uid = ?", (uid,))
                self.conn.commit()
                return deleted_gids

            return self._execute_with_retry(operation)
