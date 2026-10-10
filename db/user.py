from __future__ import annotations
from db.tool import Db
from crypto import sha256, pwd_verify
import re
import secrets
import hashlib
import json
import time

email_regex = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
ALLOWED_USER_STATS = {"user", "banned", "admin", "root"}
_UNSET = object()

class UserDb(Db):
    def __init__(self, hasher, path : str, port_api : int, port_tcp : int, dialect=None):
        super().__init__(path, port_api, port_tcp, dialect=dialect)
        self.hasher = hasher
    
    def verify_user(self,  uid, password):
        lst = self.uid_query(uid)
        if not lst:
            return False
        if pwd_verify(self.hasher, lst[0][3], password):
            return True
        return False
    
    def validate_username(self, username : str, current_uid=None):
        if not isinstance(username, str):
            return False
        if len(username) > 20 or len(username) < 4:
            return False
        if " " in username:
            return False

        existed = self.username_query(username)
        if not existed:
            return True

        if current_uid is not None and existed[0][0] == current_uid:
            return True

        return False

    def user_create(self, username, password, create_time, email=None, stat='user'):
        """
        创建新的用户
        需注意用户名、邮箱不能重复，且长度不超过 20，不少于 4
        
        :param username: 用户名
        :param password: 密码
        :param create_time: 创建时间（使用时间戳）
        :param email: 邮箱地址
        """
        if stat not in ALLOWED_USER_STATS:
            return False
        if not isinstance(username, str) or len(username) > 20 or len(username) < 4 or " " in username:
            return False
        if email and not re.fullmatch(email_regex, email):
            return False

        pwd_hash = self.hasher.hash(password)
        try:
            with self.lock:
                def operation():
                    if not self._validate_username_locked(username):
                        return False
                    if email and not self._validate_email_locked(email):
                        return False
                    self.cursor.execute("SELECT COALESCE(MAX(uid), -1) + 1 FROM users")
                    uid = self.cursor.fetchone()[0]
                    if email:
                        self.cursor.execute(
                            "INSERT INTO users (uid, username, pwd_hash, create_time, email, stat) VALUES (?, ?, ?, ?, ?, ?)",
                            (uid, username, pwd_hash, create_time, email, stat),
                        )
                    else:
                        self.cursor.execute(
                            "INSERT INTO users (uid, username, pwd_hash, create_time, stat) VALUES (?, ?, ?, ?, ?)",
                            (uid, username, pwd_hash, create_time, stat),
                        )
                    self.conn.commit()
                    return True

                return self._execute_with_retry(operation)
        except Exception as e:
            print(e)
            return False

    def uid_query(self, uid : int):
        """
        依据 uid 查询用户基本信息
        """
        return self.query("SELECT * FROM users WHERE uid = ?",  (uid,))

    def username_query(self, username : str):
        return self.query("SELECT * FROM users WHERE username = ?",  (username,))

    def email_query(self, email : str):
        """
        根据邮箱查询用户基本信息
        """
        return self.query("SELECT * FROM users WHERE email = ?",  (email,))

    def search_users(self, keyword: str, limit: int = 20, exclude_uid=None):
        """按用户名搜索（前缀匹配优先，回退模糊），排除封禁用户
        """
        if not isinstance(keyword, str) or not keyword:
            return []
        escaped = (
            keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        params = ["%" + escaped + "%"]
        where = "WHERE stat != 'banned' AND username LIKE ? ESCAPE '\\'"
        if exclude_uid is not None:
            where += " AND uid != ?"
            params.append(int(exclude_uid))
        params.append(escaped + "%")
        params.append(int(limit))
        rows = self.query(
            "SELECT uid, username, sign FROM users " + where +
            " ORDER BY CASE WHEN username LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,"
            " username ASC LIMIT ?",
            tuple(params),
        )
        return [
            {"uid": r[0], "username": r[1], "sign": r[2] or ""}
            for r in rows
        ]

    def _fetchone_locked(self, command : str, parameters : tuple = ()):
        self.cursor.execute(command, parameters)
        return self.cursor.fetchone()

    def _validate_username_locked(self, username : str, current_uid=None):
        if not isinstance(username, str):
            return False
        if len(username) > 20 or len(username) < 4:
            return False
        if " " in username:
            return False

        existed = self._fetchone_locked("SELECT uid FROM users WHERE username = ?", (username,))
        if not existed:
            return True

        if current_uid is not None and existed[0] == current_uid:
            return True

        return False

    def _validate_email_locked(self, email : str, current_uid=None):
        if not re.fullmatch(email_regex, email):
            return False

        existed = self._fetchone_locked("SELECT uid FROM users WHERE email = ?", (email,))
        if not existed:
            return True

        if current_uid is not None and existed[0] == current_uid:
            return True

        return False

    def _build_user_update_locked(self, uid : int, username=_UNSET, password=_UNSET, email=_UNSET, stat=_UNSET, sign=_UNSET, introduction=_UNSET):
        current = self._fetchone_locked("SELECT uid, stat FROM users WHERE uid = ?", (uid,))
        if not current:
            return None, None, None

        current_stat = current[1]
        fields = []
        values = []
        next_stat = current_stat

        if username is not _UNSET:
            if not self._validate_username_locked(username, uid):
                return None, None, None
            fields.append("username = ?")
            values.append(username)

        if password is not _UNSET:
            fields.append("pwd_hash = ?")
            values.append(self.hasher.hash(password))

        if email is not _UNSET:
            normalized_email = email
            if normalized_email in [None, ""]:
                normalized_email = None
            elif not self._validate_email_locked(normalized_email, uid):
                return None, None, None
            fields.append("email = ?")
            values.append(normalized_email)

        if stat is not _UNSET:
            if stat not in ALLOWED_USER_STATS:
                return None, None, None
            fields.append("stat = ?")
            values.append(stat)
            next_stat = stat

        if sign is not _UNSET:
            fields.append("sign = ?")
            values.append(sign)

        if introduction is not _UNSET:
            fields.append("introduction = ?")
            values.append(introduction)

        if not fields:
            return current_stat, None, None

        return current_stat, next_stat, (fields, values)

    def count_users_with_stat(self, stat : str):
        ret = self.query("SELECT COUNT(*) FROM users WHERE stat = ?", (stat,))
        if not ret:
            return 0
        return ret[0][0]

    def count_users(self):
        ret = self.query("SELECT COUNT(*) FROM users")
        if not ret:
            return 0
        return ret[0][0]

    def list_users(self, limit=None, offset : int = 0):
        command = "SELECT uid, username, email, stat, create_time, sign, introduction FROM users ORDER BY uid ASC"
        if limit is None:
            return self.query(command)
        return self.query(command + " LIMIT ? OFFSET ?", (int(limit), int(offset)))

    def validate_email(self, email : str, current_uid=None):
        if not re.fullmatch(email_regex, email):
            return False

        existed = self.email_query(email)
        if not existed:
            return True

        if current_uid is not None and existed[0][0] == current_uid:
            return True

        return False

    def create_user_table(self):
        cmd = """
    CREATE TABLE IF NOT EXISTS users (
        uid INTEGER UNIQUE NOT NULL,
        username TEXT COLLATE NOCASE UNIQUE NOT NULL,
        email TEXT UNIQUE,
        pwd_hash TEXT NOT NULL,
        stat TEXT DEFAULT 'user',
        create_time REAL,
        sign TEXT,
        introduction TEXT,
        auth_version INTEGER DEFAULT 0,
        public_email INTEGER NOT NULL DEFAULT 1
    )
    """
        self.execute(cmd)
        self._migrate_auth_version()
        self._migrate_public_email()
        self.create_token_table()
        self.create_identity_table()

    def _migrate_auth_version(self):
        """为旧数据库补充 auth_version"""
        try:
            columns = [row[1] for row in self.query("PRAGMA table_info(users)")]
        except Exception:
            return
        if "auth_version" not in columns:
            try:
                self.execute("ALTER TABLE users ADD COLUMN auth_version INTEGER DEFAULT 0")
            except Exception:
                pass

    def _migrate_public_email(self):
        """为旧数据库补充 public_email（新列追加在表尾，不影响 SELECT * 的既有列序）"""
        try:
            columns = [row[1] for row in self.query("PRAGMA table_info(users)")]
        except Exception:
            return
        if "public_email" not in columns:
            try:
                self.execute(
                    "ALTER TABLE users ADD COLUMN public_email INTEGER NOT NULL DEFAULT 1"
                )
            except Exception:
                pass

    def create_token_table(self):
        cmd = """
    CREATE TABLE IF NOT EXISTS tokens (
        jti TEXT PRIMARY KEY,
        uid INTEGER NOT NULL,
        issued_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        ip TEXT,
        ua TEXT
    )
    """
        self.execute(cmd)
        self._migrate_token_columns()
        try:
            self.execute("CREATE INDEX IF NOT EXISTS idx_tokens_uid ON tokens(uid)")
        except Exception:
            pass

        self._create_session_table()

    def _create_session_table(self):
        self.execute("""
    CREATE TABLE IF NOT EXISTS auth_sessions (
        session_id TEXT PRIMARY KEY,
        uid INTEGER NOT NULL,
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        refresh_hash TEXT NOT NULL UNIQUE,
        session_version INTEGER NOT NULL DEFAULT 0,
        revoked_at REAL,
        ip TEXT,
        ua TEXT
    )
    """)
        try:
            self.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_uid ON auth_sessions(uid)")
        except Exception:
            pass
        
        self.execute("""
    CREATE TABLE IF NOT EXISTS auth_devices (
        uid INTEGER NOT NULL,
        device_id TEXT NOT NULL,
        device_name TEXT,
        platform INTEGER NOT NULL DEFAULT 0,
        label TEXT,
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        PRIMARY KEY (uid, device_id)
    )
    """)
        try:
            self.execute("CREATE INDEX IF NOT EXISTS idx_auth_devices_uid ON auth_devices(uid)")
        except Exception:
            pass
        
        self._migrate_session_columns()

    def create_identity_table(self):
        self.execute("""
    CREATE TABLE IF NOT EXISTS user_identities (
        iss TEXT NOT NULL,
        sub TEXT NOT NULL,
        uid INTEGER NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (iss, sub),
        UNIQUE (uid, iss)
    )
    """)
        try:
            self.execute("CREATE INDEX IF NOT EXISTS idx_user_identities_uid ON user_identities(uid)")
        except Exception:
            pass

    @staticmethod
    def hash_refresh_token(refresh_token):
        return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()

    def create_session(self, uid, refresh_token, created_at, expires_at, ip=None, ua=None, device_id=None, location=None):
        session_id = secrets.token_hex(16)
        try:
            self.execute(
                "INSERT INTO auth_sessions (session_id, uid, created_at, last_seen_at, expires_at, refresh_hash, ip, ua, device_id, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, uid, created_at, created_at, expires_at, self.hash_refresh_token(refresh_token), ip, ua, device_id, location),
            )
            return session_id
        except Exception as e:
            print(e)
            return None

    def get_session_by_refresh(self, refresh_token):
        refresh_hash = self.hash_refresh_token(refresh_token)
        rows = self.query(
            "SELECT session_id, uid, created_at, last_seen_at, expires_at, session_version, revoked_at, ip, ua, device_id, location FROM auth_sessions WHERE refresh_hash = ?",
            (refresh_hash,),
        )
        return rows[0] if rows else None

    def session_is_active(self, session_id, uid=None, now=None):
        now = time.time() if now is None else now
        query = "SELECT 1 FROM auth_sessions WHERE session_id = ? AND revoked_at IS NULL AND expires_at > ?"
        params = [session_id, now]
        if uid is not None:
            query += " AND uid = ?"
            params.append(uid)
        return bool(self.query(query, tuple(params)))

    def rotate_session(self, session_id, old_refresh_token, new_refresh_token, now, expires_at):
        old_hash = self.hash_refresh_token(old_refresh_token)
        new_hash = self.hash_refresh_token(new_refresh_token)
        try:
            self.execute(
                "UPDATE auth_sessions SET refresh_hash = ?, last_seen_at = ?, expires_at = ?, session_version = session_version + 1 WHERE session_id = ? AND refresh_hash = ? AND revoked_at IS NULL AND expires_at > ?",
                (new_hash, now, expires_at, session_id, old_hash, now),
            )
            return self.cursor.rowcount == 1
        except Exception as e:
            print(e)
            return False

    def revoke_session(self, session_id, uid=None, now=None):
        now = time.time() if now is None else now
        try:
            query = "UPDATE auth_sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL"
            params = [now, session_id]
            if uid is not None:
                query += " AND uid = ?"
                params.append(uid)
            self.execute(query, tuple(params))
            return True
        except Exception as e:
            print(e)
            return False

    def revoke_sessions(self, uid, now=None):
        now = time.time() if now is None else now
        try:
            self.execute("UPDATE auth_sessions SET revoked_at = ? WHERE uid = ? AND revoked_at IS NULL", (now, uid))
            return True
        except Exception as e:
            print(e)
            return False

    def list_sessions(self, uid, now=None):
        """列出某用户的全部未撤销、未过期的会话（会话管理粒度）。"""
        now = time.time() if now is None else now
        return self.query(
            """SELECT s.session_id, s.created_at, s.last_seen_at, s.expires_at, s.session_version, s.ip, s.ua, 
                      s.device_id, d.device_name, d.label, d.platform, s.location
               FROM auth_sessions s
               LEFT JOIN auth_devices d ON s.uid = d.uid AND s.device_id = d.device_id
               WHERE s.uid = ? AND s.revoked_at IS NULL AND s.expires_at > ?
               ORDER BY s.last_seen_at DESC""",
            (uid, now),
        )

    def count_active_sessions(self, uid, now=None):
        """统计某用户未撤销、未过期的会话数（会话配额）。"""
        now = time.time() if now is None else now
        ret = self.query(
            "SELECT COUNT(*) FROM auth_sessions WHERE uid = ? AND revoked_at IS NULL AND expires_at > ?",
            (uid, now),
        )
        if not ret:
            return 0
        return ret[0][0]

    def get_oldest_session(self, uid, now=None):
        """获取某用户最老的未撤销、未过期会话的 session_id。"""
        now = time.time() if now is None else now
        ret = self.query(
            "SELECT session_id FROM auth_sessions WHERE uid = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY last_seen_at ASC LIMIT 1",
            (uid, now),
        )
        return ret[0][0] if ret else None

    def touch_session(self, session_id, now=None):
        """更新会话最近活跃时间（可选）。"""
        now = time.time() if now is None else now
        try:
            self.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                (now, session_id),
            )
            return True
        except Exception as e:
            print(e)
            return False

    def session_owner(self, session_id):
        """获取会话所属 uid 和 device_id（用于权限校验）。"""
        rows = self.query(
            "SELECT uid, device_id FROM auth_sessions WHERE session_id = ?",
            (session_id,),
        )
        return rows[0] if rows else None

    def upsert_device(self, uid, device_id, device_name, platform, now):
        """创建或更新设备记录（保留已有 label）。"""
        try:
            self.execute(
                """INSERT INTO auth_devices (uid, device_id, device_name, platform, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(uid, device_id) DO UPDATE SET
                       device_name = EXCLUDED.device_name,
                       platform = EXCLUDED.platform,
                       last_seen_at = EXCLUDED.last_seen_at""",
                (uid, device_id, device_name, platform, now, now),
            )
            return True
        except Exception as e:
            print(e)
            return False

    def get_device(self, uid, device_id):
        """获取设备详情。"""
        rows = self.query(
            "SELECT device_id, device_name, platform, label, created_at, last_seen_at FROM auth_devices WHERE uid = ? AND device_id = ?",
            (uid, device_id),
        )
        return rows[0] if rows else None

    def list_devices(self, uid, now=None):
        """列出某用户的全部设备。"""
        now = time.time() if now is None else now
        return self.query(
            """SELECT d.device_id, d.device_name, d.platform, d.last_seen_at, d.label
               FROM auth_devices d
               WHERE d.uid = ?
               ORDER BY d.last_seen_at DESC""",
            (uid,),
        )

    def rename_device(self, uid, device_id, label):
        """更新设备的用户自定义标签（保留 device_name）。"""
        try:
            self.execute(
                "UPDATE auth_devices SET label = ? WHERE uid = ? AND device_id = ?",
                (label, uid, device_id),
            )
            return self.cursor.rowcount > 0
        except Exception as e:
            print(e)
            return False
    
    def update_device_label(self, uid, device_id, label):
        """更新设备的用户自定义标签（别名方法）。"""
        return self.rename_device(uid, device_id, label)

    def revoke_device_sessions(self, uid, device_id, now=None):
        """撤销某设备的全部会话。"""
        now = time.time() if now is None else now
        try:
            self.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE uid = ? AND device_id = ? AND revoked_at IS NULL",
                (now, uid, device_id),
            )
            return True
        except Exception as e:
            print(e)
            return False

    def touch_device(self, uid, device_id, now=None):
        """更新设备最近活跃时间。"""
        now = time.time() if now is None else now
        try:
            self.execute(
                "UPDATE auth_devices SET last_seen_at = ? WHERE uid = ? AND device_id = ?",
                (now, uid, device_id),
            )
            return True
        except Exception as e:
            print(e)
            return False

    def get_identity_uid(self, iss, sub):
        """按外部身份 (iss, sub) 查询绑定的本地 uid。"""
        rows = self.query("SELECT uid FROM user_identities WHERE iss = ? AND sub = ?", (iss, sub))
        return rows[0][0] if rows else None

    def bind_identity(self, iss, sub, uid):
        """绑定外部身份到本地账号。冲突（身份已被占用或 uid+iss 已有绑定）返回 False。"""
        try:
            self.execute(
                "INSERT INTO user_identities (iss, sub, uid, created_at) VALUES (?, ?, ?, ?)",
                (iss, sub, uid, time.time()),
            )
            return True
        except Exception as e:
            print(e)
            return False

    def list_identities(self, uid):
        """列出某用户已绑定的外部身份。"""
        return self.query(
            "SELECT iss, sub, created_at FROM user_identities WHERE uid = ? ORDER BY created_at",
            (uid,),
        )

    def unbind_identity(self, uid, iss):
        """解绑某用户在某 issuer 下的外部身份。"""
        existed = self.query("SELECT 1 FROM user_identities WHERE uid = ? AND iss = ?", (uid, iss))
        if not existed:
            return False
        try:
            self.execute("DELETE FROM user_identities WHERE uid = ? AND iss = ?", (uid, iss))
            return True
        except Exception as e:
            print(e)
            return False

    def _migrate_session_columns(self):
        """为旧数据库的 auth_sessions 表补充 device_id 和 location 列"""
        try:
            columns = [row[1] for row in self.query("PRAGMA table_info(auth_sessions)")]
        except Exception:
            return
        for column, definition in (("device_id", "TEXT"), ("location", "TEXT")):
            if column not in columns:
                try:
                    self.execute("ALTER TABLE auth_sessions ADD COLUMN {} {}".format(column, definition))
                except Exception:
                    pass

    def _migrate_token_columns(self):
        """为旧数据库补充 tokens（是词元吗） 表的 ip/ua 列"""
        try:
            columns = [row[1] for row in self.query("PRAGMA table_info(tokens)")]
        except Exception:
            return
        for column, definition in (("ip", "TEXT"), ("ua", "TEXT")):
            if column not in columns:
                try:
                    self.execute("ALTER TABLE tokens ADD COLUMN {} {}".format(column, definition))
                except Exception:
                    pass

    def issue_token(self, jti, uid, issued_at, expires_at, ip=None, ua=None):
        """登记已签发的 JWT（对了，要记录 REDAgent.exe 和 IP）"""
        try:
            self.execute(
                "INSERT INTO tokens (jti, uid, issued_at, expires_at, ip, ua) VALUES (?, ?, ?, ?, ?, ?)",
                (jti, uid, issued_at, expires_at, ip, ua),
            )
            return True
        except Exception as e:
            print(e)
            return False

    def list_tokens(self, uid):
        """列出某用户的全部未过期 token。"""
        return self.query(
            "SELECT jti, issued_at, expires_at, ip, ua FROM tokens WHERE uid = ? ORDER BY issued_at DESC",
            (uid,),
        )

    def token_exists(self, jti):
        """校验 token是否仍存在"""
        ret = self.query("SELECT 1 FROM tokens WHERE jti = ?", (jti,))
        return bool(ret)

    def delete_token(self, jti, uid):
        """按 jti 移除某用户的词元"""
        try:
            self.execute("DELETE FROM tokens WHERE jti = ? AND uid = ?", (jti, uid))
            return True
        except Exception as e:
            print(e)
            return False

    def count_active_tokens(self, uid, now=None):
        """统计某用户未过期的词元消耗（bushi）"""
        now = time.time() if now is None else now
        ret = self.query(
            "SELECT COUNT(*) FROM tokens WHERE uid = ? AND expires_at > ?",
            (uid, now),
        )
        if not ret:
            return 0
        return ret[0][0]

    def prune_expired_tokens(self, now=None):
        """清理已过期的 token 登记。"""
        now = time.time() if now is None else now
        try:
            self.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
            return True
        except Exception as e:
            print(e)
            return False

    def delete_tokens(self, uid):
        """删除某用户的全部 token 登记（用户删除时清理）。"""
        try:
            self.execute("DELETE FROM tokens WHERE uid = ?", (uid,))
            return True
        except Exception as e:
            print(e)
            return False

    def get_oldest_token(self, uid, now=None):
        """获取用户最老的未过期 token"""
        now = time.time() if now is None else now
        ret = self.query(
            "SELECT jti FROM tokens WHERE uid = ? AND expires_at > ? ORDER BY issued_at ASC LIMIT 1",
            (uid, now),
        )
        return ret[0][0] if ret else None

    def get_auth_version(self, uid):
        ret = self.query("SELECT auth_version FROM users WHERE uid = ?", (uid,))
        if not ret or ret[0][0] is None:
            return 0
        return int(ret[0][0])

    def bump_auth_version(self, uid):
        """使该用户已签发的全部 JWT 失效，并释放其 token 登记（避免占用配额）。"""
        try:
            self.execute(
                "UPDATE users SET auth_version = auth_version + 1 WHERE uid = ?",
                (uid,),
            )
            self.delete_tokens(uid)
            self.revoke_sessions(uid)
            return True
        except Exception as e:
            print(e)
            return False
    
    def create_friend_table(self):
        cmd = """
    CREATE TABLE IF NOT EXISTS friendship (
        user1 INTEGER NOT NULL,
        user2 INTEGER NOT NULL,
        relationship TEXT CHECK(relationship IN ('pending', 'friend', 'blocked')) DEFAULT 'pending',
        adder INTEGER NOT NULL,
        blocked_by_user1 BOOLEAN,
        blocked_by_user2 BOOLEAN,
        request_message TEXT NOT NULL DEFAULT '',
        request_at REAL NOT NULL DEFAULT 0,
        UNIQUE(user1, user2)
    )
    """
        """
        adder 是添加者的 uid
        如果 pending 后另一方拒绝成为好友，默认删除关系
        被拉黑的不再有请求成为好友的权限
        request_message/request_at 记录最新一次申请的留言与时间（重复申请就地更新）
        """
        self.execute(cmd)
        # 旧库补列（重复执行安全）
        try:
            columns = [row[1] for row in self.query("PRAGMA table_info(friendship)")]
        except Exception:
            columns = []
        for col, ddl in (
            ("request_message", "TEXT NOT NULL DEFAULT ''"),
            ("request_at", "REAL NOT NULL DEFAULT 0"),
        ):
            if col not in columns:
                try:
                    self.execute("ALTER TABLE friendship ADD COLUMN {} {}".format(col, ddl))
                except Exception:
                    pass
        # #26: 为已有数据库补建唯一索引，防止并发重复插入
        try:
            self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_friendship_pair ON friendship(user1, user2)")
        except Exception:
            pass

    def query_relationship(self, uida, uidb):
        if uida > uidb:
            uida, uidb = uidb, uida
            
        return self.query("SELECT * from friendship WHERE user1 = ? and user2 = ?", (uida, uidb))

    def is_friend(self, uida, uidb):
        relationship = self.query_relationship(uida, uidb)
        return bool(relationship and relationship[0][2] == 'friend')
    
    def change_relationship(self, uida, uidb, newrelationship):
        if newrelationship not in ['pending', 'blocked', 'friend']:
            return False

        if uida > uidb:
            uida, uidb = uidb, uida
            
        self.execute("UPDATE friendship SET relationship = ? WHERE user1 = ? and user2 = ?", (newrelationship, uida, uidb))
        return True
    
    def pending_friend(self, uida, uidb, adder, message: str = ''):
        """好友申请：未拉黑即可申请；始终只保留一条最新 pending。

        返回：
          'created' —— 首次申请（新行）
          'updated' —— 同一人重复申请，就地更新留言/时间（不新增行）
          'flipped' —— 对方反过来申请，申请者翻转（供接收方交给原申请者）
          'blocked' —— 存在拉黑，拒绝
          None      —— 已是好友（或参数非法）
        """
        if adder != uida and adder != uidb:
            return None
        if uida == uidb:
            return None
        lo, hi = (uida, uidb) if uida < uidb else (uidb, uida)
        with self.lock:
            def operation():
                now = time.time()
                self.cursor.execute(
                    "SELECT relationship, adder, blocked_by_user1, blocked_by_user2 "
                    "FROM friendship WHERE user1 = ? AND user2 = ?",
                    (lo, hi),
                )
                current = self.cursor.fetchone()
                if current is None:
                    self.cursor.execute(
                        "INSERT INTO friendship (user1, user2, adder, relationship,"
                        " blocked_by_user1, blocked_by_user2, request_message, request_at)"
                        " VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                        (lo, hi, adder, False, False, message, now),
                    )
                    self.conn.commit()
                    return 'created'
                relationship, current_adder, blocked1, blocked2 = current
                if relationship == 'friend':
                    self.conn.commit()
                    return None
                if relationship == 'blocked' or blocked1 or blocked2:
                    self.conn.commit()
                    return 'blocked'
                result = 'updated' if current_adder == adder else 'flipped'
                self.cursor.execute(
                    "UPDATE friendship SET adder = ?, request_message = ?, request_at = ? "
                    "WHERE user1 = ? AND user2 = ?",
                    (adder, message, now, lo, hi),
                )
                self.conn.commit()
                return result
            return self._execute_with_retry(operation)

    def list_pending_requests(self, uid: int) -> list:
        """返回发给该用户的待处理好友申请（对方发起、自己尚未处理），按时间倒序。"""
        rows = self.query(
            "SELECT user1, user2, adder, request_message, request_at FROM friendship "
            "WHERE relationship = 'pending' AND (user1 = ? OR user2 = ?)",
            (uid, uid),
        )
        result = []
        for user1, user2, adder, message, request_at in rows:
            if adder == uid:
                continue  # 自己发出的申请不在收件列表
            applicant = user1 if adder == user1 else user2
            result.append({
                "uid": applicant,
                "message": message or "",
                "request_at": request_at or 0,
            })
        result.sort(key=lambda item: item["request_at"], reverse=True)
        return result

    def set_blocked(self, uida, uidb, blocker) -> bool:
        """拉黑：写 blocked 关系并标记拉黑方；已有关系（含 pending/friend）被覆盖。"""
        if uida == uidb or blocker not in (uida, uidb):
            return False
        lo, hi = (uida, uidb) if uida < uidb else (uidb, uida)
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT 1 FROM friendship WHERE user1 = ? AND user2 = ?", (lo, hi))
                exists = self.cursor.fetchone() is not None
                if exists:
                    column = "blocked_by_user1" if blocker == lo else "blocked_by_user2"
                    self.cursor.execute(
                        "UPDATE friendship SET relationship = 'blocked', {} = ? "
                        "WHERE user1 = ? AND user2 = ?".format(column),
                        (True, lo, hi),
                    )
                else:
                    self.cursor.execute(
                        "INSERT INTO friendship (user1, user2, adder, relationship,"
                        " blocked_by_user1, blocked_by_user2, request_message, request_at)"
                        " VALUES (?, ?, ?, 'blocked', ?, ?, '', 0)",
                        (lo, hi, blocker,
                         blocker == lo, blocker == hi),
                    )
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def unset_blocked(self, uida, uidb, blocker) -> bool:
        """解除拉黑：清除自己的拉黑标记；双方均无标记时删除整行（可重新申请）。

        对不存在或非 blocked 的关系是幂等空操作（返回 True）。
        """
        if uida == uidb or blocker not in (uida, uidb):
            return False
        lo, hi = (uida, uidb) if uida < uidb else (uidb, uida)
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT relationship, blocked_by_user1, blocked_by_user2 "
                    "FROM friendship WHERE user1 = ? AND user2 = ?",
                    (lo, hi),
                )
                row = self.cursor.fetchone()
                if row is None or row[0] != 'blocked':
                    self.conn.commit()
                    return True
                if blocker == lo:
                    other_flag = row[2]
                    self.cursor.execute(
                        "UPDATE friendship SET blocked_by_user1 = ? "
                        "WHERE user1 = ? AND user2 = ?",
                        (False, lo, hi),
                    )
                else:
                    other_flag = row[1]
                    self.cursor.execute(
                        "UPDATE friendship SET blocked_by_user2 = ? "
                        "WHERE user1 = ? AND user2 = ?",
                        (False, lo, hi),
                    )
                if not other_flag:
                    self.cursor.execute(
                        "DELETE FROM friendship WHERE user1 = ? AND user2 = ?",
                        (lo, hi),
                    )
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def ensure_friend(self, uida, uidb):
        """Create an accepted friendship, or leave an existing relation intact."""
        if uida == uidb:
            return False
        if uida > uidb:
            uida, uidb = uidb, uida
        with self.lock:
            def operation():
                self.cursor.execute(
                    "SELECT relationship FROM friendship WHERE user1 = ? AND user2 = ?",
                    (uida, uidb),
                )
                current = self.cursor.fetchone()
                if current and current[0] == "blocked":
                    return False
                if current:
                    self.cursor.execute(
                        "UPDATE friendship SET relationship = 'friend' WHERE user1 = ? AND user2 = ?",
                        (uida, uidb),
                    )
                else:
                    self.cursor.execute(
                        "INSERT INTO friendship (user1, user2, adder, relationship, blocked_by_user1, blocked_by_user2) VALUES (?, ?, ?, 'friend', ?, ?)",
                        (uida, uidb, uida, False, False),
                    )
                self.conn.commit()
                return True
            return self._execute_with_retry(operation)

    def delete_relationship(self, uida, uidb):
        if uida > uidb:
            uida, uidb = uidb, uida
        self.execute("DELETE FROM friendship WHERE user1 = ? AND user2 = ?", (uida, uidb))

    def change_pwd(self, uid : int, new_pwd : str):
        pwd_hash = self.hasher.hash(new_pwd)
        self.execute("UPDATE users SET pwd_hash = ? where uid = ?", (pwd_hash, uid))

    def change_username(self, oped : int, new_username : str):
        if not self.validate_username(new_username, oped):
            return False
        try:
            self.execute("UPDATE users SET username = ? where uid = ?", (new_username, oped))
            return True
        except Exception as e:
            print(e)
            return False

    def change_auth(self, oped : int, new_auth : str):
        self.execute("UPDATE users SET stat = ? where uid = ?", (new_auth, oped))
    
    def change_email(self, oped : int, new_email : str):
        if not self.validate_email(new_email, oped):
            return False
        try:
            self.execute("UPDATE users SET email = ? where uid = ?", (new_email, oped))
            return True
        except Exception as e:
            print(e)
            return False

    def update_user(self, uid : int, username=_UNSET, password=_UNSET, email=_UNSET, stat=_UNSET, sign=_UNSET, introduction=_UNSET):
        with self.lock:
            def operation():
                current_stat, next_stat, prepared = self._build_user_update_locked(
                    uid,
                    username=username,
                    password=password,
                    email=email,
                    stat=stat,
                    sign=sign,
                    introduction=introduction,
                )
                if current_stat is None or prepared is None:
                    return False

                fields, values = prepared
                values.append(uid)
                self.cursor.execute("UPDATE users SET {} where uid = ?".format(", ".join(fields)), tuple(values))
                self.conn.commit()
                return True

            try:
                return self._execute_with_retry(operation)
            except Exception as e:
                print(e)
                return False

    def update_user_with_root_guard(self, uid : int, username=_UNSET, password=_UNSET, email=_UNSET, stat=_UNSET, sign=_UNSET, introduction=_UNSET):
        with self.lock:
            def operation():
                current_stat, next_stat, prepared = self._build_user_update_locked(
                    uid,
                    username=username,
                    password=password,
                    email=email,
                    stat=stat,
                    sign=sign,
                    introduction=introduction,
                )
                if current_stat is None or prepared is None:
                    return False

                if current_stat == "root" and next_stat != "root":
                    root_count = self._fetchone_locked("SELECT COUNT(*) FROM users WHERE stat = ?", ("root",))
                    if root_count and root_count[0] <= 1:
                        return False

                fields, values = prepared
                values.append(uid)
                self.cursor.execute("UPDATE users SET {} where uid = ?".format(", ".join(fields)), tuple(values))
                self.conn.commit()
                return True

            try:
                return self._execute_with_retry(operation)
            except Exception as e:
                print(e)
                return False

    def delete_user(self, uid : int):
        with self.lock:
            def operation():
                current = self._fetchone_locked("SELECT uid FROM users WHERE uid = ?", (uid,))
                if not current:
                    return False
                self.cursor.execute("DELETE FROM friendship WHERE user1 = ? OR user2 = ? OR adder = ?", (uid, uid, uid))
                self.cursor.execute("DELETE FROM user_identities WHERE uid = ?", (uid,))
                self.cursor.execute("DELETE FROM users WHERE uid = ?", (uid,))
                self.conn.commit()
                return True

            try:
                return self._execute_with_retry(operation)
            except Exception as e:
                print(e)
                return False

    def delete_user_with_root_guard(self, uid : int):
        with self.lock:
            def operation():
                current = self._fetchone_locked("SELECT stat FROM users WHERE uid = ?", (uid,))
                if not current:
                    return False

                if current[0] == "root":
                    root_count = self._fetchone_locked("SELECT COUNT(*) FROM users WHERE stat = ?", ("root",))
                    if root_count and root_count[0] <= 1:
                        return False

                self.cursor.execute("DELETE FROM friendship WHERE user1 = ? OR user2 = ? OR adder = ?", (uid, uid, uid))
                self.cursor.execute("DELETE FROM user_identities WHERE uid = ?", (uid,))
                self.cursor.execute("DELETE FROM users WHERE uid = ?", (uid,))
                self.conn.commit()
                return True

            try:
                return self._execute_with_retry(operation)
            except Exception as e:
                print(e)
                return False
    
    def change_sign(self, oped : int, new_sign : str):
        self.execute("UPDATE users SET sign = ? where uid = ?", (new_sign, oped))

    def set_public_email(self, uid : int, public : bool) -> bool:
        """设置是否对外公开邮箱（默认公开）。"""
        self.execute(
            "UPDATE users SET public_email = ? WHERE uid = ?",
            (1 if public else 0, uid),
        )
        return True

    def change_introduction(self, oped : int, new_intro : str):
        self.execute('UPDATE users SET introduction = ? where uid = ?', (new_intro, oped))

    def get_session_stats(self):
        """
        获取会话统计信息
        """
        with self.lock:
            try:
                # 总会话数
                total = self._fetchone_locked(
                    "SELECT COUNT(*) FROM auth_sessions"
                )
                total_sessions = total[0] if total else 0
                
                # 活跃会话数（未撤销且未过期）
                now = int(time.time())
                active = self._fetchone_locked(
                    "SELECT COUNT(*) FROM auth_sessions "
                    "WHERE revoked_at IS NULL AND expires_at > ?",
                    (now,)
                )
                active_sessions = active[0] if active else 0
                
                # 每用户活跃会话数统计
                user_sessions = self._fetchall_locked(
                    "SELECT uid, COUNT(*) as count FROM auth_sessions "
                    "WHERE revoked_at IS NULL AND expires_at > ? "
                    "GROUP BY uid",
                    (now,)
                )
                
                # 假设配额为5（从配置读取更好，这里简化）
                max_sessions = 5
                users_at_limit = sum(1 for _, count in user_sessions if count >= max_sessions)
                
                # 平均每用户会话数
                total_users = len(user_sessions)
                avg_sessions = (sum(count for _, count in user_sessions) / total_users) if total_users > 0 else 0.0
                
                return {
                    'total_sessions': total_sessions,
                    'active_sessions': active_sessions,
                    'users_at_limit': users_at_limit,
                    'avg_sessions_per_user': round(avg_sessions, 2),
                }
            except Exception as e:
                print(f"get_session_stats error: {e}")
                return {
                    'total_sessions': 0,
                    'active_sessions': 0,
                    'users_at_limit': 0,
                    'avg_sessions_per_user': 0.0,
                }
