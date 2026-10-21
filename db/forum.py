from db.tool import Db
from time import time
import os
from json_store import read_json


# 单条 SQL 的占位符数量上限
_COMMENT_QUERY_BATCH = 500


def comments_path(port_api : int):
    return "res/{}/forum/comments.json".format(port_api)


class ForumDb(Db):
    def __init__(self, path : str, port_api : int, port_tcp : int, dialect=None):
        super().__init__(path, port_api, port_tcp, dialect=dialect)
    
    def create_forum_table(self):
        cmd = """
    CREATE TABLE IF NOT EXISTS forums (
        fid INTEGER UNIQUE NOT NULL,
        forumname TEXT NOT NULL,
        creater INTEGER,
        create_time REAL,
        introduction TEXT,
        post_num INTEGER,
        pinned_pid INTEGER DEFAULT NULL
    )
    """
        cmd2 = """
    CREATE TABLE IF NOT EXISTS contents (
        fid INTEGER NOT NULL,
        pid INTEGER UNIQUE NOT NULL,
        title TEXT,
        creater INTEGER NOT NULL,
        content TEXT,
        send_time REAL
    )
    """
        self.execute(cmd)
        self.execute(cmd2)
        # 版块帖子列表按 (fid, pid) 游标分页
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_contents_fid ON contents(fid, pid)")
        self.execute("""
    CREATE TABLE IF NOT EXISTS post_attachments (
        pid INTEGER NOT NULL,
        position INTEGER NOT NULL,
        file_hash TEXT NOT NULL,
        display_name TEXT,
        PRIMARY KEY (pid, position),
        UNIQUE (pid, file_hash)
    )
    """)
        self.execute("CREATE INDEX IF NOT EXISTS idx_post_attachments_hash ON post_attachments(file_hash)")
        try:
            self.execute("ALTER TABLE post_attachments ADD COLUMN display_name TEXT")
        except Exception:
            pass
        try:
            self.execute("ALTER TABLE forums ADD COLUMN pinned_pid INTEGER DEFAULT NULL")
        except Exception:
            pass
        self.execute("""
    CREATE TABLE IF NOT EXISTS forum_members (
        fid INTEGER NOT NULL,
        uid INTEGER NOT NULL,
        role INTEGER DEFAULT 0,
        join_time REAL,
        PRIMARY KEY (fid, uid)
    )
    """)
        self._create_comments_table()
        self._migrate_comments_json()

    def _create_comments_table(self):
        self.execute("""
    CREATE TABLE IF NOT EXISTS post_comments (
        fid INTEGER NOT NULL,
        pid INTEGER NOT NULL,
        time_stamp TEXT NOT NULL,
        created_at REAL NOT NULL,
        uid INTEGER NOT NULL,
        content TEXT NOT NULL,
        PRIMARY KEY (pid, time_stamp)
    )
    """)
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_post_comments_pid "
            "ON post_comments(pid, created_at)")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_post_comments_uid "
            "ON post_comments(uid)")

    def _migrate_comments_json(self):
        """
        旧版 comments.json 导入 post_comments
        """
        path = comments_path(self.api_pt)
        if not os.path.exists(path):
            return
        try:
            data = read_json(path)
        except Exception as e:
            print("[WARN] comments.json 读取失败，暂不迁移: {}".format(e))
            return
        rows = []
        for fid_key, posts in (data or {}).items():
            if not isinstance(posts, dict):
                continue
            for pid_key, thread in posts.items():
                if not isinstance(thread, dict):
                    continue
                for time_stamp, entry in thread.items():
                    if not (isinstance(entry, list) and len(entry) >= 2):
                        continue
                    try:
                        fid_value = int(fid_key)
                        pid_value = int(pid_key)
                        created_at = float(time_stamp)
                    except (TypeError, ValueError):
                        continue
                    rows.append((
                        fid_value, pid_value, str(time_stamp), created_at,
                        int(entry[0]), str(entry[1]),
                    ))
        if rows:
            try:
                self.update(
                    "INSERT OR IGNORE INTO post_comments "
                    "(fid, pid, time_stamp, created_at, uid, content) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            except Exception as e:
                print("[WARN] comments.json 迁移失败，保留原文件: {}".format(e))
                return
        try:
            os.replace(path, path + ".migrated")
        except OSError:
            pass

    def add_comment(self, fid, pid, uid, content, time_stamp=None):
        created_at = time()
        time_stamp = str(created_at) if time_stamp is None else str(time_stamp)
        try:
            self.execute(
                "INSERT INTO post_comments "
                "(fid, pid, time_stamp, created_at, uid, content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(fid), int(pid), time_stamp, created_at, int(uid), str(content)),
            )
            return time_stamp
        except self.dialect.IntegrityError:
            return None

    def get_comments(self, fid, pid):
        return self.query(
            "SELECT time_stamp, uid, content FROM post_comments "
            "WHERE fid = ? AND pid = ? ORDER BY created_at ASC",
            (int(fid), int(pid)),
        )

    def delete_comment(self, fid, pid, time_stamp, can_delete=None):
        """删除一条评论，返回被删评论的作者 uid；不存在或无权限返回 None。

        can_delete(creator) 在同一个事务里对「读出来的作者」求值，权限判断与删除
        之间没有窗口（此前是先 get_comment_creator 再单独删除）。
        """
        time_stamp = str(time_stamp)

        def operation():
            with self.lock:
                self.cursor.execute(
                    "SELECT uid FROM post_comments "
                    "WHERE fid = ? AND pid = ? AND time_stamp = ?",
                    (int(fid), int(pid), time_stamp),
                )
                row = self.cursor.fetchone()
                if row is None:
                    self.conn.commit()
                    return None
                creator = row[0]
                if can_delete is not None and not can_delete(creator):
                    self.conn.commit()
                    return None
                self.cursor.execute(
                    "DELETE FROM post_comments "
                    "WHERE fid = ? AND pid = ? AND time_stamp = ?",
                    (int(fid), int(pid), time_stamp),
                )
                self.conn.commit()
                return creator

        return self._execute_with_retry(operation)

    def comment_summaries(self, fid, pids):
        """批量返回每个帖子的评论数 + 最后一条评论。

        需要 SQLite >= 3.25 /
        MySQL >= 8.0
        """
        pids = sorted({int(pid) for pid in pids})
        if not pids:
            return {}
        summaries = {}
        for start in range(0, len(pids), _COMMENT_QUERY_BATCH):
            batch = pids[start:start + _COMMENT_QUERY_BATCH]
            placeholders = ",".join("?" * len(batch))
            rows = self.query(
                """SELECT c.pid, c.uid, c.content, c.time_stamp,
                          (SELECT COUNT(*) FROM post_comments t
                            WHERE t.fid = c.fid AND t.pid = c.pid) AS total
                     FROM post_comments c
                    WHERE c.fid = ? AND c.pid IN ({})
                      AND c.created_at = (
                        SELECT MAX(t2.created_at) FROM post_comments t2
                         WHERE t2.fid = c.fid AND t2.pid = c.pid)
                    ORDER BY c.created_at DESC""".format(placeholders),
                tuple([int(fid)] + batch),
            )
            for pid, uid, content, time_stamp, total in rows:
                if pid in summaries:
                    continue
                summaries[pid] = {
                    "count": int(total),
                    "last": {
                        "time_stamp": time_stamp,
                        "uid": uid,
                        "content": content,
                    },
                }
        return summaries

    
    def create_forum(self, forumname, creater : int, introduction):
        """
        创建论坛
        
        :param forumname: 论坛名
        :param creater: 创建者（uid）
        :param introduction: 论坛简介
        """
        cmd = """
    CREATE TABLE IF NOT EXISTS F{} (
        time_stamp REAL,
        content TEXT
    )"""
        def insert_forum():
            self.cursor.execute("SELECT MAX(fid) from forums")
            fid = self.cursor.fetchone()[0]
            if fid == None:
                fid = 0
            else:
                fid += 1
            self.cursor.execute(
                "INSERT INTO forums (fid, forumname, creater, create_time, introduction, post_num) VALUES (?, ?, ?, ?, ?, 0)",
                (fid, forumname, creater, time(), introduction)
            )
            self.cursor.execute(cmd.format(fid))
            self.cursor.execute(
                "INSERT INTO forum_members (fid, uid, role, join_time) VALUES (?, ?, 100, ?)",
                (fid, creater, time())
            )
            self.conn.commit()
            return fid

        with self.lock:
            fid = self._execute_with_retry(insert_forum)

        return fid
    
    def query_forum_fid(self, fid):
        return self.query("SELECT * FROM forums WHERE fid = ?", (fid,))
    
    def query_forum_forumname(self, forumname):
        return self.query("SELECT * FROM forums WHERE forumname LIKE ?", ('%' + forumname + '%', ))
    
    def query_forum_creater(self, creater : int):
        return self.query("SELECT * FROM forums WHERE creater = ?", (creater,))
    
    def query_post_pid(self, fid : int, pid : int):
        return self.query("SELECT * FROM contents WHERE pid = ? and fid = ?", (pid, fid))
    
    def query_post_title(self, fid : int, title : str):
        return self.query("SELECT * FROM contents WHERE fid = ? and title LIKE ?", (fid, '%' + title + '%',))
    
    def query_post_content(self, fid : int, content : str):
        return self.query("SELECT * FROM contents WHERE fid = ? and content LIKE ?", (fid, '%' + content + '%', ))
    
    def query_post_sender(self, fid : int, sender : int):
        return self.query("SELECT * FROM contents WHERE fid = ? and creater = ?", (fid, sender, ))
    
    def query_all_post(self, fid):
        return self.query("SELECT * FROM contents WHERE fid = ?", (fid, ))

    def query_posts_page(self, fid, before_pid=None, limit=None):
        """按 pid 游标分页；limit=None 时保持旧的“返回全部”行为。

        返回 (rows, has_more)，rows 内按 pid 升序，便于客户端直接追加。
        """
        where = "fid = ?"
        params = [fid]
        if before_pid is not None:
            where += " AND pid < ?"
            params.append(int(before_pid))
        columns = "SELECT fid, pid, title, creater, content, send_time FROM contents"
        if limit is None:
            rows = self.query(
                "{} WHERE {} ORDER BY pid ASC".format(columns, where),
                tuple(params),
            )
            return rows, False
        params.append(int(limit) + 1)
        rows = self.query(
            "{} WHERE {} ORDER BY pid DESC LIMIT ?".format(columns, where),
            tuple(params),
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        return rows, has_more
    
    def query_all_forums(self):
        return self.query("SELECT * FROM forums ORDER BY post_num DESC")

    def get_file_reference_rows(self, fid=None, cleanup_uid=None, pid=None):
        where = []
        params = []
        if fid is not None:
            where.append("c.fid = ?")
            params.append(fid)
        if cleanup_uid is not None:
            where.append("(f.creater = ? OR c.creater = ?)")
            params.extend([cleanup_uid, cleanup_uid])
        if pid is not None:
            where.append("c.pid = ?")
            params.append(pid)
        suffix = " WHERE " + " AND ".join(where) if where else ""
        return [
            (row[0], "forum_post", "{}:{}".format(row[1], row[2]), row[3])
            for row in self.query(
                """SELECT a.file_hash, c.fid, c.pid, c.creater
                   FROM post_attachments a
                   JOIN contents c ON c.pid = a.pid
                   JOIN forums f ON f.fid = c.fid""" + suffix,
                tuple(params),
            )
        ]

    def search(self, query_text: str, fid=None, offset: int = 0, limit: int = 30):
        """搜索功能（xsfx requestd）"""
        escaped = "%{}%".format(query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
        forum_rows = self.query(
            "SELECT fid, forumname, introduction, post_num FROM forums WHERE forumname LIKE ? ESCAPE '\\' OR introduction LIKE ? ESCAPE '\\' ORDER BY post_num DESC LIMIT ? OFFSET ?",
            (escaped, escaped, limit, offset),
        ) if fid is None else []
        where = "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
        params = [escaped, escaped]
        if fid is not None:
            where += " AND fid = ?"
            params.append(fid)
        total = self.query("SELECT COUNT(*) FROM contents WHERE " + where, tuple(params))[0][0]
        posts = self.query(
            "SELECT fid, pid, title, creater, content, send_time FROM contents WHERE " + where + " ORDER BY send_time DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        )
        return forum_rows, posts, total

    def send_post(self, fid : int, sender : int, title : str, content : str, attachments=None):
        if len(title) > 30:
            return False
        attachments = list(attachments or [])
        def insert_post():
            self.cursor.execute("SELECT MAX(pid) from contents")
            pid = self.cursor.fetchone()[0]
            if pid == None:
                pid = 0
            else:
                pid += 1
            self.cursor.execute(
                "INSERT INTO contents (fid, pid, title, creater, content, send_time) VALUES (?, ?, ?, ?, ?, ?)",
                (fid, pid, title, sender, content, time())
            )
            for position, attachment in enumerate(attachments):
                if isinstance(attachment, dict):
                    file_hash = attachment["hash"]
                    display_name = attachment.get("display_name")
                else:
                    file_hash = attachment
                    display_name = None
                self.cursor.execute(
                    """INSERT INTO post_attachments
                       (pid, position, file_hash, display_name) VALUES (?, ?, ?, ?)""",
                    (pid, position, file_hash, display_name),
                )
            self.cursor.execute("UPDATE forums set post_num = post_num + 1 where fid = ?", (fid,))
            self.conn.commit()
            return pid

        with self.lock:
            pid = self._execute_with_retry(insert_post)

        return pid

    def get_post_attachments(self, pids) -> dict:
        pids = list(pids)
        if not pids:
            return {}
        placeholders = ",".join("?" * len(pids))
        rows = self.query(
            """SELECT pid, position, file_hash, display_name FROM post_attachments
               WHERE pid IN ({}) ORDER BY pid, position""".format(placeholders),
            tuple(pids),
        )
        result = {}
        for pid, position, file_hash, display_name in rows:
            result.setdefault(pid, []).append({
                "hash": file_hash,
                "position": position,
                "display_name": display_name,
            })
        return result

    def count_file_references(self, file_hash : str) -> int:
        rows = self.query(
            "SELECT COUNT(*) FROM post_attachments WHERE file_hash = ?",
            (file_hash,),
        )
        return rows[0][0] if rows else 0

    def get_file_reference_counts(self) -> dict:
        return {
            row[0]: row[1]
            for row in self.query(
                "SELECT file_hash, COUNT(*) FROM post_attachments GROUP BY file_hash"
            )
        }
    
    def pin_post(self, fid : int, pid : int):
        if not self.query_post_pid(fid, pid):
            return False
        self.execute("UPDATE forums SET pinned_pid = ? WHERE fid = ?", (pid, fid))
        return True

    def unpin_post(self, fid : int):
        self.execute("UPDATE forums SET pinned_pid = NULL WHERE fid = ?", (fid,))
        return True

    def get_pinned_pid(self, fid : int):
        try:
            row = self.query("SELECT pinned_pid FROM forums WHERE fid = ?", (fid,))
            if row and row[0][0] is not None:
                return row[0][0]
        except Exception:
            pass
        return None

    def add_member(self, fid : int, uid : int, role : int = 0):
        self.execute(
            "INSERT OR REPLACE INTO forum_members (fid, uid, role, join_time) VALUES (?, ?, ?, ?)",
            (fid, uid, role, time())
        )
        return True

    def remove_member(self, fid : int, uid : int):
        self.execute(
            "DELETE FROM forum_members WHERE fid = ? AND uid = ?", (fid, uid)
        )
        return True

    def change_member_role(self, fid : int, uid : int, role : int):
        self.execute(
            "UPDATE forum_members SET role = ? WHERE fid = ? AND uid = ?",
            (role, fid, uid)
        )
        return True

    def list_members(self, fid : int):
        return self.query(
            "SELECT fid, uid, role, join_time FROM forum_members WHERE fid = ? ORDER BY role DESC",
            (fid,)
        )

    def get_member_role(self, fid : int, uid : int):
        row = self.query(
            "SELECT role FROM forum_members WHERE fid = ? AND uid = ?", (fid, uid)
        )
        if row:
            return row[0][0]
        return None

    def is_member(self, fid : int, uid : int):
        return self.get_member_role(fid, uid) is not None

    def delete_forum(self, fid : int):
        with self.lock:
            def operation():
                self.cursor.execute("SELECT pid FROM contents WHERE fid = ?", (fid,))
                post_ids = [row[0] for row in self.cursor.fetchall()]
                attachment_hashes = []
                if post_ids:
                    placeholders = ",".join("?" * len(post_ids))
                    self.cursor.execute(
                        "SELECT file_hash FROM post_attachments WHERE pid IN ({})".format(placeholders),
                        tuple(post_ids),
                    )
                    attachment_hashes = [row[0] for row in self.cursor.fetchall()]
                    self.cursor.execute(
                        "DELETE FROM post_attachments WHERE pid IN ({})".format(placeholders),
                        tuple(post_ids),
                    )
                self.cursor.execute("DELETE FROM forums WHERE fid = ?", (fid,))
                self.cursor.execute("DELETE FROM contents WHERE fid = ?", (fid,))
                self.cursor.execute("DELETE FROM forum_members WHERE fid = ?", (fid,))
                self.cursor.execute("DELETE FROM post_comments WHERE fid = ?", (fid,))
                self.cursor.execute("DROP TABLE IF EXISTS F{}".format(fid))
                self.conn.commit()
                return attachment_hashes

            attachment_hashes = self._execute_with_retry(operation)
        return attachment_hashes

    def delete_post(self, fid : int, pid : int):
        if not self.query_post_pid(fid, pid):
            return []
        def do_delete():
            self.cursor.execute(
                "SELECT file_hash FROM post_attachments WHERE pid = ? ORDER BY position", (pid,))
            attachment_hashes = [row[0] for row in self.cursor.fetchall()]
            self.cursor.execute("DELETE FROM post_attachments WHERE pid = ?", (pid,))
            self.cursor.execute("DELETE FROM contents where pid = ?", (pid,))
            self.cursor.execute(
                "DELETE FROM post_comments WHERE fid = ? AND pid = ?", (fid, pid))
            self.cursor.execute(
                "UPDATE forums set post_num = CASE WHEN post_num > 0 THEN post_num - 1 ELSE 0 END where fid = ?",
                (fid,)
            )
            self.conn.commit()
            return attachment_hashes

        with self.lock:
            attachment_hashes = self._execute_with_retry(do_delete)
        return attachment_hashes

    def clean_user_content(self, uid : int, return_attachment_hashes=False):
        uid = int(uid)
        with self.lock:
            def operation():
                deleted_attachment_hashes = []
                self.cursor.execute("DELETE FROM forum_members WHERE uid = ?", (uid,))
                self.cursor.execute("SELECT fid FROM forums WHERE creater = ?", (uid,))
                deleted_forums = [row[0] for row in self.cursor.fetchall()]

                for fid in deleted_forums:
                    self.cursor.execute("SELECT pid FROM contents WHERE fid = ?", (fid,))
                    for row in self.cursor.fetchall():
                        self.cursor.execute(
                            "SELECT file_hash FROM post_attachments WHERE pid = ? ORDER BY position",
                            (row[0],),
                        )
                        deleted_attachment_hashes.extend(
                            attachment[0] for attachment in self.cursor.fetchall()
                        )
                        self.cursor.execute("DELETE FROM post_attachments WHERE pid = ?", (row[0],))
                    self.cursor.execute("DELETE FROM forums WHERE fid = ?", (fid,))
                    self.cursor.execute("DELETE FROM contents WHERE fid = ?", (fid, ))
                    self.cursor.execute("DELETE FROM forum_members WHERE fid = ?", (fid,))
                    self.cursor.execute("DELETE FROM post_comments WHERE fid = ?", (fid,))
                    self.cursor.execute("DROP TABLE IF EXISTS F{}".format(fid))

                self.cursor.execute("SELECT fid FROM forums")
                remaining_forums = [row[0] for row in self.cursor.fetchall()]
                deleted_posts = {}

                for fid in remaining_forums:
                    self.cursor.execute("SELECT pid FROM contents WHERE creater = ? and fid = ?", (uid, fid))
                    post_ids = [row[0] for row in self.cursor.fetchall()]
                    if not post_ids:
                        continue

                    deleted_posts[fid] = post_ids
                    for pid in post_ids:
                        self.cursor.execute(
                            "SELECT file_hash FROM post_attachments WHERE pid = ? ORDER BY position",
                            (pid,),
                        )
                        deleted_attachment_hashes.extend(
                            attachment[0] for attachment in self.cursor.fetchall()
                        )
                        self.cursor.execute("DELETE FROM post_attachments WHERE pid = ?", (pid,))
                        self.cursor.execute(
                            "DELETE FROM post_comments WHERE fid = ? AND pid = ?",
                            (fid, pid))
                    self.cursor.execute("DELETE FROM contents WHERE creater = ? and fid = ?", (uid, fid))
                    self.cursor.execute(
                        "UPDATE forums SET post_num = CASE WHEN post_num >= ? THEN post_num - ? ELSE 0 END WHERE fid = ?",
                        (len(post_ids), len(post_ids), fid),
                    )

                # 该用户留下的评论一并删除（帖子/版块已删的评论前面已按 fid/pid 清掉）
                self.cursor.execute("DELETE FROM post_comments WHERE uid = ?", (uid,))

                self.conn.commit()
                return deleted_forums, deleted_posts, deleted_attachment_hashes

            deleted_forums, deleted_posts, deleted_attachment_hashes = self._execute_with_retry(operation)

        if return_attachment_hashes:
            return deleted_attachment_hashes
        return True
