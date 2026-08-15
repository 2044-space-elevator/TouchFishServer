from __future__ import annotations
from db.tool import Db
from crypto import sha256, pwd_verify
import re
import json

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
        introduction TEXT
    )
    """
        self.execute(cmd)
    
    def create_friend_table(self):
        cmd = """
    CREATE TABLE IF NOT EXISTS friendship (
        user1 INTEGER NOT NULL,
        user2 INTEGER NOT NULL,
        relationship TEXT CHECK(relationship IN ('pending', 'friend', 'blocked')) DEFAULT 'pending',
        adder INTEGER NOT NULL,
        blocked_by_user1 BOOLEAN,
        blocked_by_user2 BOOLEAN,
        UNIQUE(user1, user2)
    )
    """
        """
        adder 是添加者的 uid
        如果 pending 后另一方拒绝成为好友，默认删除关系
        被拉黑的不再有请求成为好友的权限
        """
        self.execute(cmd)
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
    
    def pending_friend(self, uida, uidb, adder):
        if adder != uida and adder != uidb:
            return False

        if uida == uidb:
            return False
        if self.query_relationship(uida, uidb):
            return False

        if uida > uidb:
            uida, uidb = uidb, uida

        self.execute("INSERT INTO friendship (user1, user2, adder, relationship, blocked_by_user1, blocked_by_user2) VALUES (?, ?, ?, 'pending', ?, ?)", (uida, uidb, adder, False, False))
        return True

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

    def change_introduction(self, oped : int, new_intro : str):
        self.execute('UPDATE users SET introduction = ? where uid = ?', (new_intro, oped))
