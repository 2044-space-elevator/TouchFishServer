from flask import Flask, send_file, redirect, request as flask_request, g as flask_g
from flask import render_template_string, Response
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import register_tool
import captcha_service
import base64
import binascii
import os
import secrets
import hmac
import ipaddress
from db import *
from db.user import (
    USERS_COL_PUBLIC_EMAIL,
    USERS_COL_SHOW_READ_STATUS,
    USERS_COL_SHOW_ONLINE_STATUS,
)
import avatar
import file

import announcements
import jwt_tool
import jwt_keys
from crypto import generate_rsa_keys, return_app_route
from rate_limiter import RateLimiter
from mention_utils import resolve_mentioned_uids, should_alert, alert_from_preference
import time
import threading
from config_utils import get_features, normalize_default_join_targets, feature_gate, merge_features, SETTINGS_SPEC
from json_store import read_json, update_json
from datetime import datetime
from file_types import IMAGE_TYPES, detect_file_type, is_sticker_type
import oss_store
import external_auth_providers
from sync_limits import SYNC_MAX_LIMIT, parse_sync_missing_sequences
from error_codes import legacy_bool_text

def bool_res() -> tuple:
    return (legacy_bool_text(False), legacy_bool_text(True))

# 端点级内存限流：按 (uid, bucket) 计数，各端点各占一个桶
_rate_limit_lock = threading.Lock()
_rate_limit_hits: dict = {}

def check_rate_limit(uid, bucket: str, max_per_window: int,
                     window: float = 10.0) -> bool:
    """按 (uid, bucket) 限流
    """
    now = time.time()
    key = (uid, bucket)
    with _rate_limit_lock:
        hits = [t for t in _rate_limit_hits.get(key, []) if t > now - window]
        if len(hits) >= max_per_window:
            _rate_limit_hits[key] = hits
            return False
        hits.append(now)
        _rate_limit_hits[key] = hits
        if len(_rate_limit_hits) > 4096:
            for stale in [k for k, v in _rate_limit_hits.items()
                          if not v or v[-1] < now - window]:
                _rate_limit_hits.pop(stale, None)
        return True

def check_search_rate(uid, max_per_window: int = 20, window: float = 10.0) -> bool:
    """搜索端点 不准你乱搜"""
    return check_rate_limit(uid, "search", max_per_window, window)

# 配额检查 + 写入必须原子：否则两个并发上传都读到旧用量，双双通过。
# 用按 uid 分条的锁，避免所有用户共用一个全局锁。
_QUOTA_LOCK_STRIPES = 64
_quota_locks = [threading.Lock() for _ in range(_QUOTA_LOCK_STRIPES)]


def quota_lock(uid):
    try:
        index = int(uid) % _QUOTA_LOCK_STRIPES
    except (TypeError, ValueError):
        index = 0
    return _quota_locks[index]


# 单条 SQL 的占位符数量有上限（SQLite 默认 32766），公告又是按全量用户扇出的；
# 不分批的话 `IN (...)` 直接抛错，异常被副作用包装吞掉 —— 一条通知都发不出去。
NOTIFY_BATCH_SIZE = 500


def normalize_notification_targets(target_uids):
    """去重 + 排序 + 丢掉无法转成 int 的项（bool 不是 uid）。"""
    unique = set()
    for value in target_uids:
        if isinstance(value, bool):
            continue
        try:
            unique.add(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(unique)


def notification_batches(uids, size: int = NOTIFY_BATCH_SIZE):
    """把规范化后的 uid 列表按批切开，每批一条 IN 查询 + 一个事务。"""
    for start in range(0, len(uids), size):
        yield uids[start:start + size]


def normalize_search_request(keyword, min_len, limit, offset=0):
    """归一化搜索请求"""
    keyword = str(keyword or "").strip()
    if min_len > 0 and len(keyword) < min_len:
        return keyword, 0, 0, "search_keyword_too_short"
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 20))
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    return keyword, limit, offset, None

# 陈年老 bug 没注意到，tf.xin 上早就修了
FILE_CACHE_MAX_AGE = 365 * 24 * 3600

# max_file_size 未配置（-1）时请求体的兜底上限：大文件请走 /file/chunked_upload
DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# secret 接口的请求体是「两层 base64」：正文里的 file_b64 是第一层（4/3），
# 整个 JSON 又被 AES 加密后再 base64（第二层，再 4/3），合计 16/9。
# 只按 4/3 估算会在大文件上误判超限（413 Payload Too Large）。
SECRET_ENVELOPE_RATIO = 16 / 9
# JSON 信封 / RSA 字段 / PKCS7 填充 / 文件名的余量
REQUEST_BODY_MARGIN = 2 * 1024 * 1024


def _positive_int(value):
    """把配置值收成正整数；-1（不限制）/ 缺失 / 非法值返回 None。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def request_body_limit(cfg):
    """单个请求的请求体上限。

    由「一次请求最多能带多少原始 payload」反推：单文件上传受 max_file_size、
    贴纸/头像分别受 max_sticker_size / max_avatar_size 限制，分块上传的每个
    分块受 file.MAX_CHUNK_SIZE 限制——任意一项大于上限都会变成 413。
    配置项 max_request_body_size 可显式覆盖。
    """
    override = cfg.get("max_request_body_size")
    if isinstance(override, int) and not isinstance(override, bool) and override > 0:
        return override
    # max_file_size 为 -1（不限制）时按兜底值算：大文件请走分块上传
    payloads = [
        _positive_int(cfg.get("max_file_size")) or DEFAULT_MAX_UPLOAD_BYTES,
        file.MAX_CHUNK_SIZE,
    ]
    for key in ("max_sticker_size", "max_avatar_size"):
        value = _positive_int(cfg.get(key))
        if value is not None:
            payloads.append(value)
    return int(max(payloads) * SECRET_ENVELOPE_RATIO) + REQUEST_BODY_MARGIN


FILE_MIMETYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".opus": "audio/opus", ".weba": "audio/webm",
    ".pdf": "application/pdf", ".zip": "application/zip",
    ".tgs": "application/octet-stream",
}


def mime_from_name(name):
    return FILE_MIMETYPES.get(
        os.path.splitext(name or "")[1].lower(), "application/octet-stream"
    )


def can_recall_message(operator_uid : int, operator_auth : str, message : dict,
                       group_role : int = 0) -> bool:
    if message["sender_uid"] == operator_uid:
        return True
    if message["group_id"] is not None:
        return operator_auth in {"admin", "root"} or group_role >= 1
    return operator_auth in {"admin", "root"}


def public_email_visible(row) -> bool:
    """users 表 public_email 列（位序见 db.user.USERS_COL_*），没有就是 public！"""
    index = USERS_COL_PUBLIC_EMAIL
    value = row[index] if len(row) > index else 1
    return value is None or bool(value)


def show_read_status_visible(row) -> bool:
    """users 表 show_read_status 列（位序见 db.user.USERS_COL_*），没有就是展示！"""
    index = USERS_COL_SHOW_READ_STATUS
    value = row[index] if len(row) > index else 1
    return value is None or bool(value)


def show_online_status_visible(row) -> bool:
    """users 表 show_online_status 列（位序见 db.user.USERS_COL_*），没有就是展示！"""
    index = USERS_COL_SHOW_ONLINE_STATUS
    value = row[index] if len(row) > index else 1
    return value is None or bool(value)

def main(port_api : int, port_tcp : int, pub_pem, pri, ImgCaptcha, user_cursor, forum_cursor, file_cursor, notification_cursor, messages_cursor, group_cursor, instant_contact, sticker_cursor=None, jwt_secret=None, jwt_key_store=None):
    """
    pri 是 cryptography 库的私钥对象
    pub_pem 是二进制 pem 文件路径
    ImgCaptcha 是 captcha.ImageCaptcha 对象
    ~_cursor 表示 db.tool.Db 对象
    jwt_secret 是 HS256 JWT 签名密钥（不传时自动加载/生成）
    jwt_key_store 是本服非对称签名密钥环（不传时自动创建）
    """
    app = Flask(__name__)
    jwt_secret = jwt_tool.load_secret(port_api) if jwt_secret is None else jwt_secret
    if jwt_key_store is None:
        jwt_key_store = jwt_keys.KeyStore("res/{}/secret".format(port_api))

    def resolve_auth(content):
        """
        加密请求解密后的身份解析（JWT 优先，其次旧版 uid+password）。
        返回 (ok, result)：
          ok=True: result = (identity dict 或 None, legacy bool)
          ok=False: result = 错误响应 dict
        身份解析成功后设置 flask.g.auth_identity。
        """
        token = content.get("token")
        if token:
            # 本服签发 token：HS* 用 jwt_secret，非对称用密钥环（按 kid 选钥）
            payload = jwt_tool.verify_local_token(jwt_secret, token, jwt_key_store)
            if payload is not None:
                try:
                    uid = int(payload.get("sub"))
                except (TypeError, ValueError):
                    return False, {"error": "token_expired"}
                jti = payload.get("jti")
                sid = payload.get("sid")
                if not sid:
                    # 旧版 token（无 sid）
                    return False, {"error": "token_expired"}
                # 一条 JOIN 同时校验 token 登记 / 会话状态 / 用户存在 / auth_version，
                # 避免每个请求 4 次往返。
                row = user_cursor.resolve_session_identity(uid, jti, sid)
                if row is None:
                    return False, {"error": "token_expired"}
                auth_version = int(row[1])
                if int(payload.get("av", -1)) != auth_version:
                    return False, {"error": "token_expired"}
                identity = {"uid": uid, "stat": row[0], "auth_version": auth_version, "jti": jti, "sid": sid, "external": False}
                flask_g.auth_identity = identity
                return True, (identity, False)

            # 外部签发 token
            external_issuers = read_config().get("jwt_external_issuers")
            if external_issuers:
                payload = jwt_tool.verify_external_token(token, external_issuers)
                if payload is not None:
                    try:
                        uid = int(payload.get("sub"))
                    except (TypeError, ValueError):
                        return False, {"error": "token_expired"}
                    row = user_cursor.uid_query(uid)
                    if not row:
                        return False, {"error": "token_expired"}
                    auth_version = user_cursor.get_auth_version(uid)
                    if int(payload.get("av", -1)) != auth_version:
                        return False, {"error": "token_expired"}
                    identity = {"uid": uid, "stat": row[0][4], "auth_version": auth_version, "jti": payload.get("jti"), "sid": payload.get("sid"), "external": True}
                    flask_g.auth_identity = identity
                    return True, (identity, False)

            return False, {"error": "token_expired"}

        uid = content.get("uid")
        pwd = content.get("password")
        if uid is None or pwd is None:
            return True, (None, False)
        if content.get("jwt"):
            # JWT login！login！
            return True, (None, False)
        if not read_config().get("legacy_auth_enabled", True):
            return False, {"error": "auth_failed"}
        if not user_cursor.verify_user(uid, pwd):
            return False, {"error": "auth_failed"}
        row = user_cursor.uid_query(uid)
        if not row:
            return False, {"error": "auth_failed"}
        identity = {"uid": int(uid), "stat": row[0][4]}
        flask_g.auth_identity = identity
        return True, (identity, True)

    def verify_user(uid, pwd):
        """
        请求已通过身份解析（JWT 或旧版）时返回返回返回
        for 兼容性 we still need uidpwd
        """
        identity = flask_g.get("auth_identity")
        if identity is not None and int(identity.get("uid", -1)) == int(uid):
            return True
        return user_cursor.verify_user(uid, pwd)

    @app.after_request
    def allow_cross_origin_requests(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = flask_request.headers.get(
            "Access-Control-Request-Headers", "Content-Type, Authorization"
        )
        return response

    api = return_app_route(app, pri, resolve_auth,
                           feature_checker=lambda path: feature_gate(read_config(), *path))
    limiter = RateLimiter(port_api)
    manager_auths = {"admin", "root"}
    managed_auths = {"user", "banned", "admin", "root"}
    locks = {
        'config': threading.Lock(),
        'activate': threading.Lock(),
        'queue': threading.Lock(),
        'captcha': threading.Lock(),
        'announcement': threading.Lock(),
    }

    CONFIG_PATH = "res/{}/config.json".format(port_api)
    _config_cache = None
    _config_stat = None  # (mtime_ns, size) xsfx可以手动改 JSON

    def read_config():
        nonlocal _config_cache, _config_stat
        try:
            st = os.stat(CONFIG_PATH)
            stat = (st.st_mtime_ns, st.st_size)
        except OSError:
            stat = None
        if _config_cache is not None and stat is not None and stat == _config_stat:
            return dict(_config_cache)
        with locks['config']:
            try:
                st = os.stat(CONFIG_PATH)
                stat = (st.st_mtime_ns, st.st_size)
            except OSError:
                stat = None
            if _config_cache is not None and stat is not None and stat == _config_stat:
                return dict(_config_cache)
            cfg = read_json(CONFIG_PATH)
            _config_cache = dict(cfg)
            _config_stat = stat
            return dict(_config_cache)

    # ---- JWT 签发 / JWKS 配置读取（配置在启动后仍可热改） ----

    def jwt_signing_algorithm(cfg=None):
        """当前签发算法；未知值回退 HS256，保证存量服务器行为不变。"""
        cfg = read_config() if cfg is None else cfg
        return jwt_keys.normalize_algorithm(cfg.get("jwt_signing_algorithm", "HS256"))

    def jwt_issuer(cfg=None):
        """token 的 iss；未配置时沿用端口号（与升级前一致）。"""
        cfg = read_config() if cfg is None else cfg
        value = cfg.get("jwt_issuer")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return str(port_api)

    def jwt_audience(cfg=None):
        cfg = read_config() if cfg is None else cfg
        value = cfg.get("jwt_audience")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def jwt_public_base_url(cfg=None):
        """对外基址：优先 jwt_public_base_url，否则用当前请求的 url_root（ProxyFix 已生效）。"""
        cfg = read_config() if cfg is None else cfg
        value = cfg.get("jwt_public_base_url")
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
        try:
            return (flask_request.url_root or "").rstrip("/")
        except Exception:
            return ""

    def jwt_jwks_uri(cfg=None):
        base = jwt_public_base_url(cfg)
        return (base + "/.well-known/jwks.json") if base else "/.well-known/jwks.json"

    def jwt_key_retention_days(cfg=None):
        cfg = read_config() if cfg is None else cfg
        try:
            days = int(cfg.get("jwt_key_retention_days", 30))
        except (TypeError, ValueError):
            days = 30
        return days if 0 <= days <= 3650 else 30

    def apply_request_body_limit(cfg):
        limit = request_body_limit(cfg)
        if app.config.get("MAX_CONTENT_LENGTH") != limit:
            app.config["MAX_CONTENT_LENGTH"] = limit

    apply_request_body_limit(read_config())

    def _warm_external_issuers():
        """后台预热外部 issuer 的 JWKS，避免首次外部登录阻塞在网络上。"""
        try:
            issuers = read_config().get("jwt_external_issuers") or []
            for entry in issuers:
                if isinstance(entry, dict):
                    jwt_tool.warm_external_issuer(entry)
        except Exception:
            pass

    threading.Thread(
        target=_warm_external_issuers, name="tfv5-jwks-warm", daemon=True
    ).start()

    group_cursor._config_reader = read_config

    _base_wsgi_app = app.wsgi_app
    _proxy_state = {"enabled": None, "count": None}

    def apply_proxy_fix(cfg):
        enabled = bool(cfg.get("reverse_proxy_enabled", False))
        try:
            count = max(int(cfg.get("proxy_count", 1)), 0)
        except (TypeError, ValueError):
            count = 1
        if _proxy_state["enabled"] == enabled and _proxy_state["count"] == count:
            return
        if enabled:
            app.wsgi_app = ProxyFix(_base_wsgi_app, x_for=count, x_proto=1)
        else:
            app.wsgi_app = _base_wsgi_app
        _proxy_state["enabled"] = enabled
        _proxy_state["count"] = count

    apply_proxy_fix(read_config())

    def build_notification(event : str, title : str, content : str, sender=None, meta=None):
        if isinstance(meta, dict):
            meta = dict(meta)
        elif meta is not None:
            meta = {"value": meta}
        else:
            meta = {}
        if sender is not None and "pfp" not in meta and "avatar_url" not in meta:
            raw_sender = str(sender)
            uid = raw_sender.rsplit("U", 1)[-1] if "U" in raw_sender else raw_sender
            if uid.isdigit():
                meta["avatar_url"] = "/avatar/get_avatar/user/{}".format(uid)
        return {
            "event" : event,
            "title" : title,
            "content" : content,
            "sender" : sender,
            "meta" : meta
        }

    def run_side_effect(action : str, callback):
        try:
            return callback()
        except Exception as e:
            print("[WARN] 副作用失败({}): {}".format(action, e))
            return None

    def run_notification_side_effect(action : str, callback):
        return run_side_effect("notification/{}".format(action), callback)

    def ensure_notification_table(target_uid : int):
        try:
            notification_cursor.create_user_table(target_uid)
            return True
        except Exception as e:
            print("[WARN] 创建通知表失败(uid={}): {}".format(target_uid, e))
            return False

    def notify_user(target_uid : int, event : str, title : str, content : str, sender=None, meta=None):
        def callback():
            if not user_cursor.uid_query(target_uid):
                return None
            return instant_contact.notify_user(target_uid, build_notification(event, title, content, sender, meta))
        return run_notification_side_effect("notify_user/{}".format(event), callback)

    def notify_users(target_uids, event : str, title : str, content : str, sender=None, meta=None):
        def callback():
            unique = normalize_notification_targets(target_uids)
            if not unique:
                return []
            notification = build_notification(event, title, content, sender, meta)
            records = []
            for batch in notification_batches(unique):
                placeholders = ",".join("?" * len(batch))
                existing = {
                    int(row[0]) for row in user_cursor.query(
                        "SELECT uid FROM users WHERE uid IN ({})".format(placeholders),
                        tuple(batch),
                    )
                }
                prepared = [
                    (uid, dict(notification)) for uid in batch if uid in existing
                ]
                if prepared:
                    records.extend(instant_contact.notify_users(prepared))
            return records
        records = run_notification_side_effect("notify_users/{}".format(event), callback)
        if records is None:
            return []
        return records

    def all_user_ids():
        ret = run_notification_side_effect("all_user_ids", lambda: [row[0] for row in user_cursor.query("SELECT uid FROM users")])
        if ret is None:
            return []
        return ret

    def update_config(mutator):
        nonlocal _config_cache, _config_stat
        with locks['config']:
            state = {}
            def apply(cfg):
                mutator(cfg)
                state["cfg"] = dict(cfg)
            update_json(CONFIG_PATH, apply)
            _config_cache = state["cfg"]
            try:
                st = os.stat(CONFIG_PATH)
                _config_stat = (st.st_mtime_ns, st.st_size)
            except OSError:
                _config_stat = None
            apply_request_body_limit(_config_cache)
            apply_proxy_fix(_config_cache)
            # WS 侧的功能开关与限额是启动时的快照：配置一落地就同步刷新，
            # 否则管理端关掉 presence/read_receipt 后 HTTP 已经 403，
            # WS 仍在推 presence.update / message.read_receipt。
            instant_contact.refresh_config()
            return dict(_config_cache)

    def _build_ice_servers(rtc_cfg):
        """冰服务器！（ICE）"""
        default_stun = [{
            "urls": [
                "stun:stun.epygi.com",
                "stun:stun.fitauto.ru",
            ]
        }]
        if not isinstance(rtc_cfg, dict):
            return default_stun
        if not rtc_cfg.get("turn_enabled", False):
            return default_stun

        configured = rtc_cfg.get("ice_servers")
        if not isinstance(configured, list):
            return default_stun
        result = []
        for server in configured:
            if not isinstance(server, dict):
                continue
            urls = server.get("urls")
            if isinstance(urls, str):
                urls = [urls]
            if not isinstance(urls, list):
                continue
            urls = [url for url in urls if isinstance(url, str) and url]
            if not urls:
                continue
            item = {"urls": urls}
            for key in ("username", "credential"):
                if isinstance(server.get(key), str):
                    item[key] = server[key]
            result.append(item)
        return result or default_stun

    def serialize_server_settings(cfg, include_manage=False):
        ret = {
            "server_name" : cfg.get("server_name", "TouchFish"),
            "port_api" : port_api,
            "port_tcp" : port_tcp,
            "ice_servers" : _build_ice_servers(cfg.get("rtc", {})),
            "captcha" : bool(cfg.get("captcha", False)),
            "captcha_provider" : cfg.get("captcha_provider", "image"),
            "captcha_site_key" : cfg.get("captcha_site_key", ""),
            "file_last_time" : cfg.get("file_last_time", 72),
            "media_features" : bool(cfg.get("media_features", True)),
            "groups_limit" : cfg.get("groups_limit", 30),
            "single_group_max_people" : cfg.get("single_group_max_people", 200),
            "max_file_size" : cfg.get("max_file_size", -1),
            "max_avatar_size" : cfg.get("max_avatar_size", cfg.get("max_file_size", -1)),
            "user_storage_quota" : cfg.get("user_storage_quota", -1),
            "max_user_storage_quota" : cfg.get("max_user_storage_quota", 73400320),
            "max_sticker_storage_quota" : cfg.get("max_sticker_storage_quota", 31457280),
            "default_join_targets" : normalize_default_join_targets(cfg.get("default_join_targets", [])),
            "max_message_length" : cfg.get("max_message_length", 10000),
            "max_request_message_length" : cfg.get("max_request_message_length", 200),
            "min_search_length" : cfg.get("min_search_length", 2),
            "features" : get_features(cfg),
            "min_group_name_length" : cfg.get("min_group_name_length", 1),
            "max_group_name_length" : cfg.get("max_group_name_length", 50),
            "max_sign_length" : cfg.get("max_sign_length", 100),
            "max_introduction_length" : cfg.get("max_introduction_length", 500),
            "max_post_content_length" : cfg.get("max_post_content_length", 20000),
            "min_username_length" : cfg.get("min_username_length", 4),
            "min_password_length" : cfg.get("min_password_length", 1),
            "max_sticker_packs_per_user" : cfg.get("max_sticker_packs_per_user", 24),
            "max_stickers_per_pack" : cfg.get("max_stickers_per_pack", 24),
            "daily_sticker_pack_creation_limit" : cfg.get("daily_sticker_pack_creation_limit", -1),
            "max_sticker_size" : cfg.get("max_sticker_size", 1048576),
            "email_activate" : bool(cfg.get("email_activate")),
            "legacy_auth_enabled" : bool(cfg.get("legacy_auth_enabled", True)),
            "jwt_expires_seconds" : int(cfg.get("jwt_expires_seconds", 3600)),
            "jwt_refresh_expires_seconds" : int(cfg.get("jwt_refresh_expires_seconds", 604800)),
            "jwt_max_per_user" : int(cfg.get("jwt_max_per_user", 5)),
            "jwt_signing_algorithm" : jwt_signing_algorithm(cfg),
            "jwt_issuer" : jwt_issuer(cfg),
            "jwt_jwks_uri" : jwt_jwks_uri(cfg) if cfg.get("jwt_jwks_enabled", True) else "",
            "jwt_jwks_enabled" : bool(cfg.get("jwt_jwks_enabled", True)),
            "external_login_issuers" : [
                entry.get("iss") for entry in (cfg.get("jwt_external_issuers") or [])
                if isinstance(entry, dict) and isinstance(entry.get("iss"), str) and entry.get("iss")
            ],
            # 客户端据此发现"去哪里做外部登录"。空串 = 未启用。
            "external_auth_url" : (
                cfg.get("external_auth_url", "").strip().rstrip("/")
                if isinstance(cfg.get("external_auth_url"), str) else ""
            ),
            "platform_auth_enabled" : bool(cfg.get("platform_auth_enabled", False)),
            # 认证平台支持的第三方登录方式，由服务器代拉取后下发（60s 缓存）。
            # 客户端据此渲染 GitHub 等按钮，无需自己访问认证平台。
            "external_auth_providers" : external_auth_providers.providers(
                cfg.get("external_auth_url") if isinstance(cfg.get("external_auth_url"), str) else ""
            ),
            "allow_register" : bool(cfg.get("allow_register", True)),
            "default_asset_urls" : {
                "logo" : "/avatar/get_logo",
                "forum" : "/avatar/get_default/forum",
                "user" : "/avatar/get_default/user",
                "group" : "/avatar/get_default/group"
            }
        }
        if include_manage:
            ret["rate_limits"] = cfg.get("rate_limits", {})
            ret["settings_spec"] = SETTINGS_SPEC
            ret["file_download_mode"] = oss_store.get_download_mode(port_api, cfg)
            if cfg.get("email_activate"):
                ret["verify_email"] = cfg.get("email_activate")
            ret["smtp_host"] = cfg.get("smtp_host", "")
            ret["smtp_port"] = cfg.get("smtp_port", 465)
            ret["smtp_use_ssl"] = cfg.get("smtp_use_ssl", True)
            ret["reverse_proxy_enabled"] = cfg.get("reverse_proxy_enabled", False)
            ret["proxy_count"] = cfg.get("proxy_count", 1)
            # 平台端点的非敏感项直接回显；共享令牌与 SMTP 密码同理，只回显"是否已配置"
            ret["platform_auth_issuer"] = (
                cfg.get("platform_auth_issuer", "").strip().rstrip("/")
                if isinstance(cfg.get("platform_auth_issuer"), str) else ""
            )
            ret["platform_auth_allow_ips"] = (
                cfg.get("platform_auth_allow_ips", "")
                if isinstance(cfg.get("platform_auth_allow_ips"), str) else ""
            )
            ret["platform_auth_shared_token_set"] = bool(cfg.get("platform_auth_shared_token"))
        return ret

    def validate_default_join_targets(targets):
        for target in targets:
            target_id = int(target[1:])
            if target.startswith("U"):
                if not user_cursor.uid_query(target_id):
                    raise ValueError("default friend does not exist")
            elif not group_cursor.query_gid(target_id):
                raise ValueError("default group does not exist")

    def apply_default_join_targets(uid, cfg=None):
        cfg = cfg or read_config()
        try:
            targets = normalize_default_join_targets(cfg.get("default_join_targets", []))
        except ValueError:
            return
        for target in targets:
            target_id = int(target[1:])
            try:
                if target.startswith("U"):
                    if target_id != uid and user_cursor.uid_query(target_id):
                        user_cursor.ensure_friend(uid, target_id)
                elif group_cursor.query_gid(target_id):
                    group_cursor.add_member(target_id, uid)
            except Exception as e:
                print("[WARN] default join {} 失败: {}".format(target, e))

    def parse_int_setting(value, minimum=0, allow_unlimited=False):
        if isinstance(value, bool):
            raise ValueError("bool is not a valid integer setting")
        parsed = int(value)
        if allow_unlimited and parsed == -1:
            return parsed
        if parsed < minimum:
            raise ValueError("setting is below minimum")
        return parsed

    def parse_bool_flag(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off", ""}:
                return False
        return False

    def decode_base64_payload(payload, max_bytes=None):
        if not isinstance(payload, str) or not payload.strip():
            return None
        if max_bytes is not None and max_bytes >= 0:
            if len(payload) > (max_bytes * 4 // 3) + 8:
                return None
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError, TypeError):
            return None

    def read_upload_limit(cfg, specific_key=None):
        if specific_key is not None and specific_key in cfg:
            raw_value = cfg.get(specific_key)
        else:
            raw_value = cfg.get("max_file_size", -1)
        try:
            return parse_int_setting(raw_value, minimum=0, allow_unlimited=True)
        except (TypeError, ValueError):
            return -1

    def normalize_upload_filename(filename):
        if not isinstance(filename, str):
            return None
        filename = filename.strip()
        if not filename or filename in {".", ".."} or "\x00" in filename:
            return None
        if "/" in filename or "\\" in filename:
            return None
        return filename

    def read_allowed_file_extensions(cfg):
        raw_value = cfg.get("allowed_file_extensions")
        if raw_value is None:
            return None
        if isinstance(raw_value, str):
            raw_items = raw_value.split(',')
        elif isinstance(raw_value, list):
            raw_items = raw_value
        else:
            return None
        normalized = set()
        for item in raw_items:
            ext = str(item).strip().lower()
            if not ext:
                continue
            if not ext.startswith('.'):
                ext = ".{}".format(ext)
            normalized.add(ext)
        return normalized or None

    def validate_avatar_upload(pic_b64, cfg):
        max_avatar_size = read_upload_limit(cfg, "max_avatar_size")
        payload = decode_base64_payload(pic_b64, max_avatar_size)
        if payload is None:
            return False
        if max_avatar_size != -1 and len(payload) > max_avatar_size:
            return False
        return True

    def validate_file_upload(filename, file_b64, cfg):
        normalized_name = normalize_upload_filename(filename)
        if normalized_name is None:
            return None, None
        max_file_size = read_upload_limit(cfg, "max_file_size")
        payload = decode_base64_payload(file_b64, max_file_size)
        if payload is None:
            return None, None
        if max_file_size != -1 and len(payload) > max_file_size:
            return None, None
        allowed_extensions = read_allowed_file_extensions(cfg)
        if allowed_extensions:
            _, ext = os.path.splitext(normalized_name.lower())
            if not ext or ext not in allowed_extensions:
                return None, None
        return normalized_name, payload

    def serialize_user_summary(row):
        return {
            "uid" : row[0],
            "username" : row[1],
            "email" : row[2],
            # 注意到 row[3] 不是 pwd hash
            "stat" : row[3],
            "create_time" : row[4],
            "personal_sign" : row[5],
            "introduction" : row[6]
        }

    def extract_mentioned_uids(comment : str):

        mentioned_uids = set()
        for block in comment.split():
            if not block.startswith('@') or len(block) < 2:
                continue
            username = block[1:].strip(".,!?，。！？:：;；)]】}>\"'")
            if not username:
                continue
            info = user_cursor.username_query(username)
            if info:
                mentioned_uids.add(info[0][0])
        return mentioned_uids

    def query_forum(fid):
        try:
            return forum_cursor.query_forum_fid(fid) or []
        except Exception:
            return []

    def query_post(fid, pid):
        try:
            return forum_cursor.query_post_pid(fid, pid) or []
        except Exception:
            return []

    def serialize_notifications(rows):
        return json.dumps(notification_cursor.serialize_rows(rows), ensure_ascii=False)

    def get_user_row(uid):
        info = user_cursor.uid_query(uid)
        if not info:
            return None
        return info[0]

    def get_username(uid):
        """返回用户 uid 的用户名"""
        row = get_user_row(uid)
        if row is None:
            return None
        return row[1]

    def format_user_display(uid):
        """返回 '用户名(UID)' 或 'UID' """
        username = get_username(uid)
        if username is not None:
            return "{} ({})".format(username, uid)
        return str(uid)

    def verify_manager(uid, pwd):
        if not verify_user(uid, pwd):
            return None
        operator = get_user_row(uid)
        if operator is None:
            return None
        if operator[4] not in manager_auths:
            return None
        return operator

    def verify_root(uid, pwd):
        operator = verify_manager(uid, pwd)
        if operator is None or operator[4] != "root":
            return None
        return operator

    def can_manage_auth(operator_auth : str, target_auth : str):
        if operator_auth == "root":
            return target_auth in managed_auths
        if operator_auth == "admin":
            return target_auth in {"user", "banned"}
        return False

    def resolve_managed_target(operator_auth : str, target_uid : int, next_auth=None, deleting=False):
        target = get_user_row(target_uid)
        if target is None:
            return None

        target_auth = target[4]
        if not can_manage_auth(operator_auth, target_auth):
            return None
        if next_auth is not None and not can_manage_auth(operator_auth, next_auth):
            return None
        return target

    def un_optional_managed_auth(raw_auth):
        if raw_auth is None:
            return None
        if isinstance(raw_auth, str):
            raw_auth = raw_auth.strip()
            if not raw_auth:
                return None
        return raw_auth

    def collect_managed_updates(req, next_auth=None):
        updates = {}
        if "username" in req:
            updates["username"] = req["username"]
        if "target_password" in req:
            updates["password"] = req["target_password"]
        if "email" in req:
            updates["email"] = req["email"]
        if next_auth is not None:
            updates["stat"] = next_auth
        if "sign" in req:
            updates["sign"] = req["sign"]
        if "introduction" in req:
            updates["introduction"] = req["introduction"]
        return updates

    def resolve_file_size(hashes):
        size = file_cursor.get_file_size(hashes)
        if not size and oss_store.is_oss_enabled(port_api):
            size = oss_store.get_size_from_oss(port_api, "file", hashes)
        return size

    def file_metadata(file_hash, owner_uid=None):
        meta = (
            file_cursor.get_metadata(file_hash, owner_uid=owner_uid)
            if file_hash else None
        )
        if meta and not meta.get("size") and oss_store.is_oss_enabled(port_api):
            meta["size"] = oss_store.get_size_from_oss(port_api, "file", file_hash)
        if meta and file_hash:
            # 惰性自愈：新文件缺媒体元数据 / pending 卡住时重新入队
            file.ensure_media(port_api, file_hash, file_cursor)
        return meta

    def with_display_file_name(metadata, display_name):
        if not metadata or not display_name:
            return metadata
        result = dict(metadata)
        result["file_name"] = display_name
        result["filename"] = display_name
        result.pop("mime_type", None)
        result["file_type"] = result.get("file_type") or "unknown"
        result["extension"] = os.path.splitext(display_name)[1].lower()
        return result

    def enrich_message_files(records):
        metadata_cache = {}
        for record in records:
            hashes = record.get("file_hash")
            if hashes:
                cache_key = (hashes, record.get("sender_uid"))
                metadata_cache.setdefault(
                    cache_key, file_metadata(hashes, record.get("sender_uid"))
                )
                metadata = metadata_cache[cache_key]
                if metadata and record.get("file_name"):
                    metadata = with_display_file_name(metadata, record["file_name"])
                record["file"] = metadata
            preview = record.get("quote_preview")
            if preview and preview.get("file_hash"):
                hashes = preview["file_hash"]
                cache_key = (hashes, preview.get("sender_uid"))
                metadata_cache.setdefault(
                    cache_key, file_metadata(hashes, preview.get("sender_uid"))
                )
                preview["file"] = metadata_cache[cache_key]
        return records

    def serialize_post_rows(rows):
        attachments = forum_cursor.get_post_attachments([row[1] for row in rows])
        metadata_cache = {}
        summaries = {}
        if rows:
            try:
                summaries = forum_cursor.comment_summaries(
                    rows[0][0], [row[1] for row in rows])
            except Exception as e:
                print("[WARN] 读取评论摘要失败: {}".format(e))
        result = []
        for row in rows:
            summary = summaries.get(row[1]) or {}
            item = {
                "fid": row[0], "pid": row[1], "title": row[2],
                "creater": row[3], "author_uid": row[3],
                "content": row[4], "send_time": row[5],
                # 列表卡片直接展示，客户端不必再为每张卡片单独拉评论
                "comment_count": int(summary.get("count") or 0),
                "last_comment": summary.get("last"),
            }
            post_attachments = []
            for attachment in attachments.get(row[1], []):
                hashes = attachment["hash"]
                cache_key = (hashes, row[3])
                metadata_cache.setdefault(cache_key, file_metadata(hashes, row[3]))
                metadata = metadata_cache[cache_key]
                if metadata:
                    display_name = attachment.get("display_name") or metadata.get("file_name")
                    metadata = with_display_file_name(metadata, display_name)
                    metadata["position"] = attachment["position"]
                    post_attachments.append(metadata)
            if post_attachments:
                item["attachments"] = post_attachments
            result.append(item)
        return result

    def cleanup_forum_queue(target_uid : int):
        target_uid = int(target_uid)
        with locks['queue']:
            def cleanup(queue):
                removed_keys = [
                    key for key, value in queue.items()
                    if key.isdigit() and isinstance(value, dict) and value.get("creater") == target_uid
                ]
                for key in removed_keys:
                    del queue[key]
                queue["queue_num"] = max(queue.get("queue_num", 0) - len(removed_keys), 0)
            update_json("res/{}/forum/queue.json".format(port_api), cleanup)

        return True

    def clean_deleted_user_state(target_uid : int):
        target_uid = int(target_uid)
        # 会话状态（已读水位 / 房间偏好 / @ 提及）随账号一起清掉；
        # 消息行保留，避免破坏对方的聊天历史。
        messages_cursor.purge_user_state(target_uid)
        user_groups = group_cursor.get_user_group_rows(target_uid)
        owned_gids = [row[0] for row in group_cursor.query_creater(target_uid)]
        deleted_forum_ids = [row[0] for row in forum_cursor.query_forum_creater(target_uid)]

        # 通知一下噻
        for row in user_groups:
            gid = row[0]
            try:
                members = json.loads(row[3])
            except Exception:
                continue
            group_name = row[2] or str(gid)
            if row[1] == target_uid and gid in owned_gids:
                for member in members:
                    if member != target_uid:
                        notify_user(member, "group.deleted", "群聊已解散",
                            "群 {} 已被解散。".format(group_name),
                            sender=target_uid, meta={"gid": gid})
            else:
                for member in members:
                    if member != target_uid:
                        notify_user(member, "group.member.removed", "成员已退出群聊",
                            "用户 {} 已退出群 {}。".format(target_uid, group_name),
                            sender=target_uid, meta={"gid": gid})

        avatar.clean_avatar(port_api, target_uid, "user")
        for gid in owned_gids:
            avatar.clean_avatar(port_api, gid, "group")
        for fid in deleted_forum_ids:
            avatar.clean_avatar(port_api, fid, "forum")

        file.clean_user_files(port_api, target_uid, file_cursor)
        if sticker_cursor is not None:
            for row in sticker_cursor.get_user_stickers(target_uid):
                sticker_hash = row[0]
                if sticker_cursor.delete_user_sticker(target_uid, sticker_hash):
                    sticker_cursor.delete_sticker_blob_relations(sticker_hash)
                    if oss_store.is_oss_enabled(port_api):
                        oss_store.delete_from_oss(port_api, "sticker", sticker_hash)
                    sticker_disk_path = file.sticker_path(port_api, sticker_hash)
                    if os.path.isfile(sticker_disk_path):
                        try:
                            os.remove(sticker_disk_path)
                        except OSError:
                            pass
        group_cursor.remove_user_membership(target_uid)
        deleted_references = forum_cursor.get_file_reference_rows(
            cleanup_uid=target_uid
        )
        forum_cursor.clean_user_content(target_uid)
        for hashes, source_type, source_id, _ in deleted_references:
            file_cursor.remove_reference(hashes, source_type, source_id)
        cleanup_forum_queue(target_uid)
        return True

    def perform_managed_auth_change(uid : int, pwd : str, target_uid : int, new_auth : str):
        operator = verify_manager(uid, pwd)
        if operator is None:
            return False

        target = resolve_managed_target(operator[4], target_uid, next_auth=new_auth)
        if target is None:
            return False

        if not user_cursor.update_user_with_root_guard(target_uid, stat=new_auth):
            return False

        # 吊销全部 JWT；封禁时把人家的 WebSocket 全部使用尖头靴子踢掉
        user_cursor.bump_auth_version(target_uid)
        if new_auth == "banned":
            run_side_effect(
                "disconnect_banned_user",
                lambda: instant_contact.disconnect_user(target_uid)
            )

        notify_user(target_uid, "auth.stat.changed", "账号状态已变更", "你的账号状态已更新为 {}。".format(new_auth), sender=uid, meta={"new_auth" : new_auth})
        return True
    
    # ---- 外部认证平台（TouchFish Auth）专用准入 ----
    # 该组端点只服务于服务端到服务端的调用：
    #   IP/CIDR 白名单（remote_addr 已经过 ProxyFix）
    #   + 可选共享令牌（防 X-Forwarded-For 伪造绕过白名单）
    PLATFORM_PREFIX = "/auth/platform/"

    def platform_issuer(cfg=None):
        """平台端点只接受一个 issuer：显式配置优先，否则取唯一的外部签发方。"""
        cfg = read_config() if cfg is None else cfg
        value = cfg.get("platform_auth_issuer")
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
        entries = cfg.get("jwt_external_issuers") or []
        if len(entries) == 1 and isinstance(entries[0], dict):
            iss = entries[0].get("iss")
            if isinstance(iss, str):
                return iss.strip().rstrip("/")
        return ""

    def platform_allow_ips(cfg):
        raw = cfg.get("platform_auth_allow_ips")
        items = [s.strip() for s in raw.split(",") if s.strip()] if isinstance(raw, str) else []
        return items or ["127.0.0.1", "::1"]

    def _ip_in_spec(addr, spec):
        try:
            if "/" in spec:
                return addr in ipaddress.ip_network(spec, strict=False)
            return addr == ipaddress.ip_address(spec)
        except ValueError:
            return False

    def platform_caller_allowed(cfg):
        if not bool(cfg.get("platform_auth_enabled", False)):
            return False
        try:
            addr = ipaddress.ip_address(flask_request.remote_addr or "")
        except ValueError:
            return False
        if not any(_ip_in_spec(addr, spec) for spec in platform_allow_ips(cfg)):
            return False
        expected = cfg.get("platform_auth_shared_token")
        if isinstance(expected, str) and expected:
            provided = flask_request.headers.get("X-Platform-Token", "")
            if not hmac.compare_digest(provided, expected):
                return False
        return True

    def warn_platform_security():
        """平台端点开了却没配共享令牌时给出显著警告。

        此时只靠 IP 白名单，而 reverse_proxy_enabled=true 后 remote_addr 来自
        X-Forwarded-For，可被伪造 —— 等于把 bind/unbind/status 暴露给任意人。
        启动时调用一次（read_config 已定义，可安全调用）。
        """
        try:
            cfg = read_config()
        except Exception:  # noqa: BLE001
            return
        if not bool(cfg.get("platform_auth_enabled", False)):
            return
        token = cfg.get("platform_auth_shared_token")
        if isinstance(token, str) and token:
            return
        print("[WARN] platform_auth_enabled=true 但未设置 platform_auth_shared_token："
              "当前仅靠 IP 白名单鉴权，启用反向代理后 X-Forwarded-For 可被伪造，"
              "建议配置共享令牌（openssl rand -hex 32）")

    def warn_external_login_entry():
        try:
            cfg = read_config()
        except Exception:  # noqa: BLE001
            return
        if not bool(cfg.get("platform_auth_enabled", False)):
            return
        url = cfg.get("external_auth_url")
        if not (isinstance(url, str) and url.strip()):
            issuer = cfg.get("platform_auth_issuer")
            hint = f"当前 platform_auth_issuer={issuer!r} 只用于校验令牌签发者" \
                if isinstance(issuer, str) and issuer else ""
            print("[WARN] platform_auth_enabled=true 但未设置 external_auth_url："
                  "客户端将不会显示 使用 TouchFish Auth 登录 入口。"
                  f"{hint}，客户端需要的是可访问的授权平台地址，"
                  "请在 server_settings 里填写 external_auth_url")
            return
        issuers = cfg.get("jwt_external_issuers")
        if not isinstance(issuers, list) or not issuers:
            print("[WARN] external_auth_url 已配置，但 jwt_external_issuers 为空，无法使用"
                  f"请在 jwt_external_issuers 里加一条 iss={url!r} 的配置"
                  "（jwks_uri 指向该平台的 /.well-known/jwks.json，"
                  "本地 http 调试请加入 allow_insecure_http=true）")

    warn_platform_security()
    warn_external_login_entry()

    @app.before_request
    def enforce_request_limits():
        cfg = read_config()
        apply_request_body_limit(cfg)
        apply_proxy_fix(cfg)
        limit = app.config.get("MAX_CONTENT_LENGTH")
        content_length = flask_request.content_length
        if limit and content_length is not None and content_length > limit:
            return "Payload Too Large", 413
        if flask_request.path.startswith(PLATFORM_PREFIX):
            # 先于限流判断，被拒的请求不消耗配额
            if not platform_caller_allowed(cfg):
                return "Forbidden", 403
        ip = flask_request.remote_addr
        endpoint = flask_request.path
        if not limiter.is_allowed(ip, endpoint):
            return "Too Many Requests", 429

    @app.route("/get_rsa_pub")
    def get_rsa_key():
        return send_file("res/{}/secret/pub.pem".format(port_api), download_name="{}.pem".format(port_api))

    def _wellknown_response(document, status=200):
        response = Response(
            json.dumps(document, ensure_ascii=False),
            status=status,
            mimetype="application/json",
        )
        if status == 200:
            response.headers["Cache-Control"] = "public, max-age=300"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/.well-known/jwks.json")
    @app.route("/.well-known/jwks")
    def get_jwks():
        """本服 JWT 公钥集（外部平台据此独立验签，无需共享 jwt_secret）。"""
        cfg = read_config()
        if not cfg.get("jwt_jwks_enabled", True):
            return _wellknown_response({"error": "not_found"}, status=404)
        try:
            document = jwt_keys.build_jwks(jwt_key_store, jwt_key_retention_days(cfg))
        except Exception as error:
            print("[ERR] 生成 JWKS 失败: {}".format(error))
            return _wellknown_response({"error": "server_error"}, status=500)
        return _wellknown_response(document)

    @app.route("/.well-known/openid-configuration")
    def get_openid_configuration():
        """只声明验签所需元数据，不声明尚未实现的 authorize/token 端点。"""
        cfg = read_config()
        if (not cfg.get("jwt_discovery_enabled", True)
                or not cfg.get("jwt_jwks_enabled", True)):
            # 没有可用的 JWKS 时，不发布指向 404 的 discovery 元数据
            return _wellknown_response({"error": "not_found"}, status=404)
        issuer = jwt_issuer(cfg)
        if not (isinstance(issuer, str) and issuer.startswith(("http://", "https://"))):
            # jwt_issuer 未配置时它只是端口号字符串，不是合法 issuer。
            # 与其发布一个错的 issuer 让人误配，不如不发布。
            return _wellknown_response({
                "error": "issuer_not_configured",
                "detail": "jwt_issuer 必须是绝对的 http(s) URL，例如 https://fish.example.com",
            }, status=404)
        try:
            document = jwt_keys.build_discovery(
                issuer, jwt_jwks_uri(cfg), jwt_key_store.algorithms()
            )
        except Exception as error:
            print("[ERR] 生成 OIDC discovery 失败: {}".format(error))
            return _wellknown_response({"error": "server_error"}, status=500)
        return _wellknown_response(document)

    @api("/auth/login", methods=["POST"])
    def login(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            if req.get("jwt"):
                device_id = req.get("device_id")
                device_name = req.get("device_name")
                platform = req.get("platform", 0)
                return login_with_jwt(uid, pwd, device_id, device_name, platform)
            cfg = read_config()
            if not cfg.get("legacy_auth_enabled", True):
                return bool_res()[False]
            return bool_res()[verify_user(uid, pwd)]
        except Exception:
            return bool_res()[False]

    @api("/auth/refresh", methods=["POST"])
    def refresh(req):
        """
        用 refresh token 轮换会话
        """
        try:
            refresh_token = req.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                return {"error": "invalid_request"}
            return refresh_with_jwt(refresh_token)
        except Exception:
            return {"error": "auth_failed"}

    def login_with_jwt(uid, pwd, device_id=None, device_name=None, platform=0):
        """
        JWT 登录：校验凭据后签发 access token 并建立可刷新 Session。
        """
        if not verify_user(uid, pwd):
            return {"error": "auth_failed"}
        return create_session_for_user(uid, device_id, device_name, platform)

    def create_session_for_user(uid, device_id=None, device_name=None, platform=0):
        """
        为已通过身份校验的用户签发本地会话（access + refresh）。
        超限（jwt_max_per_user）时吊销最老的会话。
        """
        cfg = read_config()
        max_sessions = int(cfg.get("jwt_max_per_user", 5))
        access_expires = int(cfg.get("jwt_expires_seconds", 3600))
        refresh_expires = int(cfg.get("jwt_refresh_expires_seconds", 604800))
        auth_version = user_cursor.get_auth_version(uid)
        user_cursor.prune_expired_tokens()
        client_ip = flask_request.remote_addr or ""
        user_agent = flask_request.user_agent.string or ""
        if len(user_agent) > 256:
            user_agent = user_agent[:256]

        # 设备信息记录
        if device_id and device_name:
            now = time.time()
            user_cursor.upsert_device(uid, device_id, device_name, platform, now)

        # 达到会话上限时吊销最老的 session
        revoked_oldest = False
        if max_sessions > 0 and user_cursor.count_active_sessions(uid) >= max_sessions:
            oldest_sid = user_cursor.get_oldest_session(uid)
            if oldest_sid:
                user_cursor.revoke_session(oldest_sid, uid)
                run_side_effect(
                    "disconnect_session_owner",
                    lambda: instant_contact.disconnect_session(oldest_sid)
                )
                notify_user(uid, "session_replaced", "会话已被替换", "由于达到登录设备数量上限，您最早的登录会话已被自动吊销。")
                revoked_oldest = True
            else:
                return {"error": "token_limit_reached"}

        # 签发 refresh token 并建立 session
        refresh_token = secrets.token_hex(32)
        now = time.time()
        session_expires = now + refresh_expires
        session_id = user_cursor.create_session(
            uid, refresh_token, now, session_expires, ip=client_ip, ua=user_agent,
            device_id=device_id, location=None
        )
        if not session_id:
            return {"error": "auth_failed"}

        try:
            token, payload = jwt_tool.issue_token(
                jwt_secret, uid, auth_version, access_expires, jwt_issuer(),
                session_id=session_id,
                audience=jwt_audience(),
                algorithm=jwt_signing_algorithm(),
                key_store=jwt_key_store,
            )
        except Exception as error:
            print("[ERR] 签发 JWT 失败: {}".format(error))
            user_cursor.revoke_session(session_id, uid)
            return {"error": "auth_failed"}
        if not user_cursor.issue_token(payload["jti"], uid, payload["iat"], payload["exp"], ip=client_ip, ua=user_agent):
            user_cursor.revoke_session(session_id, uid)
            return {"error": "auth_failed"}

        result = {
            "token": token,
            "refresh_token": refresh_token,
            "expires_in": int(access_expires),
            "refresh_expires_in": int(refresh_expires),
            "expires_at": payload["exp"],
        }
        if revoked_oldest:
            result["revoked_oldest"] = True
        return result

    def refresh_with_jwt(refresh_token):
        """
        refresh token 轮换
        """
        cfg = read_config()
        access_expires = int(cfg.get("jwt_expires_seconds", 3600))
        refresh_expires = int(cfg.get("jwt_refresh_expires_seconds", 604800))
        now = time.time()

        row = user_cursor.get_session_by_refresh(refresh_token)
        if not row:
            return {"error": "auth_failed"}
        (session_id, uid, _created, _last_seen, expires_at, _version,
         revoked_at, _ip, _ua, _device_id, _location) = row
        if revoked_at is not None or expires_at <= now:
            return {"error": "auth_failed"}
        auth_version = user_cursor.get_auth_version(uid)

        new_refresh_token = secrets.token_hex(32)
        new_expires = now + refresh_expires
        if not user_cursor.rotate_session(session_id, refresh_token, new_refresh_token, now, new_expires):
            return {"error": "auth_failed"}

        try:
            token, payload = jwt_tool.issue_token(
                jwt_secret, uid, auth_version, access_expires, jwt_issuer(),
                session_id=session_id,
                audience=jwt_audience(),
                algorithm=jwt_signing_algorithm(),
                key_store=jwt_key_store,
            )
        except Exception as error:
            print("[ERR] 签发 JWT 失败: {}".format(error))
            return {"error": "auth_failed"}
        if not user_cursor.issue_token(payload["jti"], uid, payload["iat"], payload["exp"]):
            return {"error": "auth_failed"}

        return {
            "token": token,
            "refresh_token": new_refresh_token,
            "expires_in": int(access_expires),
            "refresh_expires_in": int(refresh_expires),
            "expires_at": payload["exp"],
        }

    def verify_external_identity(external_token):
        """
        验证外部签发的 JWT（jwt_external_issuers 配置）。返回 (payload, issuer_cfg)；失败 (None, None)。
        """
        if not isinstance(external_token, str) or not external_token:
            return None, None
        external_issuers = read_config().get("jwt_external_issuers")
        if not isinstance(external_issuers, list) or not external_issuers:
            return None, None
        payload = jwt_tool.verify_external_token(external_token, external_issuers)
        if payload is None:
            return None, None
        iss = payload.get("iss")
        sub = payload.get("sub")
        if not isinstance(iss, str) or not iss or not isinstance(sub, str) or not sub:
            return None, None
        for entry in external_issuers:
            if isinstance(entry, dict) and entry.get("iss") == iss:
                return payload, entry
        return None, None

    def build_external_username(payload, sub):
        """
        自动建号用户名：claim username/preferred_username 优先，otherwise ext_<sub>
        """
        candidate = None
        for claim in ("username", "preferred_username"):
            raw = payload.get(claim)
            if isinstance(raw, str) and raw.strip():
                candidate = raw.strip()
                break
        if candidate is None:
            candidate = "ext_" + sub
        candidate = "".join("_" if ch.isspace() else ch for ch in candidate)[:20]
        if len(candidate) < 4:
            candidate = (candidate or "ext") + "_ext"
        return candidate

    def auto_create_external_user(payload, sub):
        """
        外部身份自动建号（随机密码，仅能经外部平台登录）。返回新 uid 或 None。
        """
        candidate = build_external_username(payload, sub)
        email = payload.get("email")
        if not isinstance(email, str) or not email or not user_cursor.validate_email(email):
            email = None
        uid = None
        for attempt in range(10):
            if attempt == 0:
                trial = candidate
            else:
                suffix = "_" + str(attempt)
                trial = candidate[:20 - len(suffix)] + suffix
            if not user_cursor.validate_username(trial):
                continue
            if user_cursor.user_create(trial, secrets.token_hex(32), time.time(), email, stat="user"):
                uid = user_cursor.username_query(trial)[0][0]
                break
        if uid is None:
            return None
        if not ensure_notification_table(uid):
            user_cursor.delete_user(uid)
            return None
        apply_default_join_targets(uid)
        return uid

    @api("/auth/external/login", methods=["POST"])
    def external_login(req):
        """
        用外部平台签发的 JWT 换取本地会话（access + refresh）。
        默认要求外部身份已绑定本地账号；issuer 配置 allow_auto_create 开启时可自动建号。
        """
        payload, issuer_cfg = verify_external_identity(req.get("external_token"))
        if payload is None:
            return {"error": "external_token_invalid"}
        iss = payload["iss"]
        sub = payload["sub"]
        uid = user_cursor.get_identity_uid(iss, sub)
        if uid is None:
            if not issuer_cfg.get("allow_auto_create", False):
                return {"error": "external_not_linked"}
            uid = auto_create_external_user(payload, sub)
            if uid is None:
                return {"error": "conflict"}
            # 建号后绑定；失败说明并发请求刚绑定同一身份，回滚本次建号
            if not user_cursor.bind_identity(iss, sub, uid):
                existing_uid = user_cursor.get_identity_uid(iss, sub)
                user_cursor.delete_user(uid)
                if existing_uid is None:
                    return {"error": "conflict"}
                uid = existing_uid
        row = user_cursor.uid_query(uid)
        if not row:
            return {"error": "external_token_invalid"}
        if row[0][4] == "banned":
            return {"error": "user_banned"}
        return create_session_for_user(
            uid, req.get("device_id"), req.get("device_name"), req.get("platform", 0)
        )

    @api("/auth/external/bind", methods=["POST"])
    def external_bind(req):
        """
        将外部平台身份绑定到当前本地账号（需本地会话）。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        payload, _issuer_cfg = verify_external_identity(req.get("external_token"))
        if payload is None:
            return {"error": "external_token_invalid"}
        iss = payload["iss"]
        sub = payload["sub"]
        uid = identity["uid"]
        existing = user_cursor.get_identity_uid(iss, sub)
        if existing == uid:
            return {"success": True}
        if existing is not None:
            return {"error": "external_already_linked"}
        if not user_cursor.bind_identity(iss, sub, uid):
            # 身份被抢占，或该 issuer 下已有其他绑定（UNIQUE(uid, iss)）
            return {"error": "external_already_linked"}
        return {"success": True}

    @api("/auth/external/unbind", methods=["POST"])
    def external_unbind(req):
        """
        解绑当前账号在指定 issuer 下的外部身份。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        iss = req.get("iss")
        if not isinstance(iss, str) or not iss:
            return {"error": "invalid_request"}
        if not user_cursor.unbind_identity(identity["uid"], iss):
            return {"error": "not_found"}
        return {"success": True}

    @api("/auth/external/list", methods=["POST"])
    def external_list(req):
        """
        列出当前账号已绑定的外部身份。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        identities = [
            {"iss": row[0], "sub": row[1], "created_at": row[2]}
            for row in user_cursor.list_identities(identity["uid"])
        ]
        return {"identities": identities}

    def _resolve_session_target(req, identity):
        """
        解析会话管理请求的目标用户与当前会话 id。
        返回 (uid, current_sid) 或 (None, None)（错误时由调用方判断）。
        """
        uid = identity["uid"]
        current_sid = identity.get("sid")
        target_uid = req.get("target_uid")
        if target_uid is not None:
            if not isinstance(target_uid, int):
                return None, None
            operator = verify_manager(uid, identity.get("password"))
            if operator is None:
                return None, None
            if resolve_managed_target(operator[4], target_uid) is None:
                return None, None
            uid = target_uid
            current_sid = None
        return uid, current_sid

    @api("/auth/sessions/list", methods=["POST"])
    def list_auth_sessions(req):
        """
        列出当前用户的活跃会话（session 粒度）。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        sessions = []
        for row in user_cursor.list_sessions(uid):
            session_id, created_at, last_seen_at, expires_at, _version, ip, ua, device_id, device_name, label, platform, location = row
            sessions.append({
                "session_id": session_id,
                "created_at": int(created_at),
                "last_seen_at": int(last_seen_at),
                "expires_at": int(expires_at),
                "ip": ip or "",
                "ua": ua or "",
                "device_id": device_id or "",
                "device_name": device_name or "",
                "label": label or "",
                "platform": platform or 0,
                "location": location or "",
                "is_current": session_id == current_sid,
            })
        return {
            "sessions": sessions,
            "max_per_user": int(read_config().get("jwt_max_per_user", 5)),
        }

    @api("/auth/sessions/revoke", methods=["POST"])
    def revoke_auth_session(req):
        """
        按 session_id 吊销指定会话（踢出设备）。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        session_id = req.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return {"error": "invalid_request"}
        if session_id == current_sid:
            return {"error": "current_session"}
        if not user_cursor.session_is_active(session_id, uid):
            return {"error": "not_found"}
        user_cursor.revoke_session(session_id, uid)
        run_side_effect(
            "disconnect_session_owner",
            lambda: instant_contact.disconnect_session(session_id)
        )
        return {"success": True}

    @api("/auth/sessions/revoke_all", methods=["POST"])
    def revoke_all_other_sessions(req):
        """
        吊销除当前会话外的全部会话（管理员可吊销目标用户全部会话）。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        target_uid = req.get("target_uid")
        if target_uid is not None:
            # 管理员操作目标用户：吊销其全部会话
            user_cursor.revoke_sessions(uid)
            run_side_effect(
                "disconnect_user_owner",
                lambda: instant_contact.disconnect_user(uid)
            )
            return {"success": True}
        # 普通用户：吊销除当前会话外的全部会话
        for session_id, *_rest in user_cursor.list_sessions(uid):
            if session_id == current_sid:
                continue
            user_cursor.revoke_session(session_id, uid)
            run_side_effect(
                "disconnect_session_owner",
                lambda sid=session_id: instant_contact.disconnect_session(sid)
            )
        return {"success": True}

    @api("/auth/sessions/rename", methods=["POST"])
    def rename_auth_session(req):
        """
        为指定会话的设备设置自定义标签（实则就是 rename）
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        session_id = req.get("session_id")
        label = req.get("label")
        if not isinstance(session_id, str) or not session_id:
            return {"error": "invalid_request"}
        if not isinstance(label, str):
            return {"error": "invalid_request"}
        # 验证会话所有权
        owner = user_cursor.session_owner(session_id)
        if not owner or owner[0] != uid:
            return {"error": "not_found"}
        device_id = owner[1]
        if not device_id:
            return {"error": "no_device_id"}
        # 更新设备标签
        success = user_cursor.rename_device(uid, device_id, label)
        if not success:
            return {"error": "update_failed"}
        return {"success": True}

    # d e p r e c a t e d ! ! pls !
    @api("/auth/tokens/list", methods=["POST"])
    def list_auth_tokens(req):
        """
        [deprecated] 列出活跃会话，兼容旧字段名（tokens 视图，会话粒度）。
        新客户端请使用 /auth/sessions/list。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        tokens = []
        for session_id, created_at, last_seen_at, expires_at, _version, ip, ua in user_cursor.list_sessions(uid):
            tokens.append({
                "jti": session_id,
                "session_id": session_id,
                "issued_at": int(created_at),
                "last_seen_at": int(last_seen_at),
                "expires_at": int(expires_at),
                "ip": ip or "",
                "ua": ua or "",
                "is_current": session_id == current_sid,
            })
        return {
            "tokens": tokens,
            "sessions": tokens,
            "max_per_user": int(read_config().get("jwt_max_per_user", 5)),
        }

    @api("/auth/tokens/revoke", methods=["POST"])
    def revoke_auth_token(req):
        """
        [deprecated] 按 session_id 吊销会话（兼容旧 jti 字段）。
        新客户端请使用 /auth/sessions/revoke。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        session_id = req.get("session_id") or req.get("jti")
        if not isinstance(session_id, str) or not session_id:
            return {"error": "invalid_request"}
        if session_id == current_sid:
            return {"error": "current_session"}
        if not user_cursor.session_is_active(session_id, uid):
            return {"error": "not_found"}
        user_cursor.revoke_session(session_id, uid)
        run_side_effect(
            "disconnect_session_owner",
            lambda: instant_contact.disconnect_session(session_id)
        )
        return {"success": True}

    @api("/auth/logout", methods=["POST"])
    def logout_current_token(req):
        """
        登出：吊销当前请求所使用的会话（若有），并断开该设备的 WebSocket。
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"success": True}
        sid = identity.get("sid")
        if sid:
            user_cursor.revoke_session(sid, identity["uid"])
            run_side_effect(
                "disconnect_session_owner",
                lambda: instant_contact.disconnect_session(sid)
            )
        return {"success": True}

    @api("/auth/devices/list", methods=["POST"])
    def list_devices(req):
        """
        列出用户的所有设备记录
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, _current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        devices = []
        for device_id, device_name, platform, last_seen, label in user_cursor.list_devices(uid):
            devices.append({
                "device_id": device_id,
                "device_name": device_name,
                "platform": platform,
                "last_seen": int(last_seen),
                "label": label or "",
            })
        return {"devices": devices}

    @api("/auth/devices/update_label", methods=["POST"])
    def update_device_label(req):
        """
        更新设备的用户自定义标签
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid = identity["uid"]
        device_id = req.get("device_id")
        label = req.get("label", "")
        if not isinstance(device_id, str) or not device_id:
            return {"error": "invalid_request"}
        if not isinstance(label, str):
            return {"error": "invalid_request"}
        if len(label) > 64:
            label = label[:64]
        user_cursor.update_device_label(uid, device_id, label)
        return {"success": True}

    @api("/auth/devices/revoke", methods=["POST"])
    def revoke_device(req):
        """
        吊销指定设备的所有会话
        """
        identity = flask_g.get("auth_identity")
        if identity is None:
            return {"error": "not_authenticated"}
        uid, _current_sid = _resolve_session_target(req, identity)
        if uid is None:
            return {"error": "forbidden"}
        device_id = req.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            return {"error": "invalid_request"}
        sessions_revoked = 0
        for row in user_cursor.list_sessions(uid):
            session_id = row[0]
            sess_device_id = row[7]
            if sess_device_id == device_id:
                user_cursor.revoke_session(session_id, uid)
                run_side_effect(
                    "disconnect_session_owner",
                    lambda sid=session_id: instant_contact.disconnect_session(sid)
                )
                sessions_revoked += 1
        return {"success": True, "sessions_revoked": sessions_revoked}

    @api("/auth/validate", methods=["POST"])
    def validate_token(req):
        """
        仅 JWT 路径的会话探活：验证 token 有效性并返回身份
        """
        identity = flask_g.get("auth_identity")
        if identity is None or "auth_version" not in identity:
            return {"error": "not_authenticated"}
        row = get_user_row(identity["uid"])
        if row is None:
            return {"error": "token_expired"}
        return {"valid": True, "uid": identity["uid"], "stat": row[4]}

    @api("/auth/query_self", methods=["POST"])
    def query_self(req):
        """查自己的完整资料
        """
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        info = user_cursor.uid_query(uid)
        if not info:
            return json.dumps({"success": False, "error": "not_found"})
        row = info[0]
        return json.dumps({
            "uid": row[0],
            "username": row[1],
            "email": row[2],
            "stat": row[4],
            "create_time": row[5],
            "personal_sign": row[6],
            "introduction": row[7],
            "public_email": public_email_visible(row),
            "show_read_status": show_read_status_visible(row),
            "show_online_status": show_online_status_visible(row),
        }, ensure_ascii=False)

    @api("/auth/change_pwd", methods=["POST"])
    def change_pwd(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            new_pwd = req["new_pwd"]
            if not verify_user(uid, pwd):
                return bool_res()[False]
            user_cursor.change_pwd(uid, new_pwd)
            user_cursor.bump_auth_version(uid)
            return bool_res()[True]
        except Exception:
            return bool_res()[False]

    @app.route('/auth/captcha')
    def get_captcha():
        cfg = read_config()
        captcha = cfg.get("captcha", False)
        if not captcha:
            return {}
        # 第三方验证码走 /auth/captcha/page，此端点仅服务图片验证码
        if captcha_service.is_third_party(cfg.get("captcha_provider", "image")):
            return {}
        token = register_tool.generate_captcha(port_api, ImgCaptcha, locks['captcha'])
        file_path = 'res/{}/captcha/{}.png'.format(port_api, token)
        with open(file_path, "rb") as file:
            ret_b64 = file.read()
        ret_b64 = base64.b64encode(ret_b64).decode("utf-8")
        return {"pic" : ret_b64, "stamp" : token}

    @app.route('/auth/captcha/page')
    def get_captcha_page():
        """
        验证码 HTML
       """ 
        cfg = read_config()
        if not cfg.get("captcha", False):
            return Response("Captcha is disabled.", status=404, mimetype="text/plain")
        provider = cfg.get("captcha_provider", "image")
        site_key = cfg.get("captcha_site_key", "")
        html, _, _ = captcha_service.render_page(provider, site_key)
        if html is None:
            return Response("Captcha provider not configured.", status=400, mimetype="text/plain")
        return Response(html, mimetype="text/html")

    @api("/auth/change_email_verify", methods=['POST'])
    def change_email_verify(req):
        uid = req["uid"]
        pwd = req["password"]

        if not verify_user(uid, pwd):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None or user_row[4] != 'root':
            return bool_res()[False]
        
        new_stat = req["change_to"]
        if new_stat:
            verify_email = req["verify_email"]
            email_password = req["email_password"]
            update_config(lambda cfg: cfg.update({
                "email_activate": verify_email,
                "email_password": email_password,
            }))
        else:
            update_config(lambda cfg: cfg.update({
                "email_activate": "",
                "email_password": "",
            }))
        return bool_res()[True]
        
    @app.route("/auth/uid/<uid>")
    def query_uid(uid):
        info = user_cursor.uid_query(uid)
        if not len(info):
            return {}
        ret = {
            "uid" : info[0][0],
            "username" : info[0][1],
            "email" : info[0][2] if public_email_visible(info[0]) else "",
            "stat" : info[0][4],
            "create_time" : info[0][5],
            "personal_sign" : info[0][6],
            "introduction" : info[0][7]
        }
        return ret

    @app.route("/auth/username/<username>")
    def query_username(username):
        info = user_cursor.username_query(username)
        if not len(info):
            return {}
        ret = {
            "uid" : info[0][0],
            "username" : info[0][1],
            "email" : info[0][2] if public_email_visible(info[0]) else "",
            "stat" : info[0][4],
            "create_time" : info[0][5],
            "personal_sign" : info[0][6],
            "introduction" : info[0][7]
        }
        return ret

    @api("/auth/change_email", methods=['POST'])
    def change_email(req):
        uid = req["uid"]
        pwd = req["password"]
        if not verify_user(uid, pwd):
            return bool_res()[False]
        new_email = req["new_email"]
        return bool_res()[user_cursor.change_email(uid, new_email)]

    @api("/auth/register", methods=['POST'])
    def register(req):
        username = req["username"]
        password = req["password"]
        cfg = read_config()
        if not cfg.get("allow_register", True):
            return {"error": "register_disabled"}
        is_captcha = cfg.get("captcha", False)
        is_email_activate = cfg.get("email_activate", "")
        if (not isinstance(username, str) or len(username) < int(cfg.get("min_username_length", 4))
                or not isinstance(password, str) or len(password) < int(cfg.get("min_password_length", 1))):
            return bool_res()[False]
        
        if is_captcha:
            provider = cfg.get("captcha_provider", "image")
            if captcha_service.is_third_party(provider):
                token = req.get("captcha_token", "")
                if not captcha_service.verify_token(provider, cfg.get("captcha_secret", ""), token):
                    return {"error": "captcha_invalid"}
            else:
                captcha_stamp = req["captcha_stamp"]
                captcha_code = req["captcha_code"]
                captcha_ok, _ = register_tool.verify_captcha(port_api, captcha_stamp, captcha_code, locks['captcha'])
                if not captcha_ok:
                    # 验证码错误/过期/已消费：该 stamp 已被作废，客户端需刷新后重试
                    return {"error": "captcha_invalid"}
        
        email = None
        if "email" in req.keys():
            email = req["email"] 
        
        if is_email_activate:
            sender_email = cfg["email_activate"]
            if not email:
                return bool_res()[False]
            email_pwd = cfg["email_password"]
            # 后台发送，避免 SMTP 阻塞 waitress 线程；队列满时才拒绝
            if not register_tool.email_code_async(
                    sender_email, port_api, email, email_pwd,
                    locks['config'], locks['activate']):
                return bool_res()[False]

        succeeded = user_cursor.user_create(username, password, time.time(), email)
        if not succeeded:
            return bool_res()[False]
        target_uid = user_cursor.username_query(username)[0][0]
        if is_email_activate and succeeded:
            user_cursor.change_auth(target_uid, "banned")
        if not ensure_notification_table(target_uid):
            user_cursor.delete_user(target_uid)
            return bool_res()[False]
        apply_default_join_targets(target_uid, cfg)
        return bool_res()[True]

    @api("/auth/activate", methods=["POST"])
    def activate(req):
        uid = req["uid"]
        activate_code = req["activate_code"]
        uid_row = get_user_row(uid)
        if uid_row is None:
            return bool_res()[False]
        email = uid_row[2]
        with locks['activate']:
            with open("res/{}/activate.json".format(port_api), "r+") as file:
                pending = email in json.load(file).keys()
        if not pending:
            # 没有待验证记录：已激活的账号视为成功,仍被封禁说明流程异常
            return bool_res()[uid_row[4] != "banned"]
        if register_tool.verify_email(port_api, email, activate_code, locks['activate']):
            user_cursor.change_auth(uid, "user")
            return bool_res()[True]
        return bool_res()[False]

    @api("/auth/resend_activation", methods=["POST"])
    def resend_activation(req):
        """重发激活验证码
        """
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        uid_row = get_user_row(uid)
        if uid_row is None:
            return bool_res()[False]
        if uid_row[4] != "banned":
            # 已经激活过：不需要再发，按成功处理（幂等）
            return bool_res()[True]
        email = uid_row[2]
        cfg = read_config()
        sender_email = cfg.get("email_activate", "")
        if not email or not sender_email:
            return bool_res()[False]
        return bool_res()[register_tool.email_code_async(
            sender_email, port_api, email, cfg.get("email_password", ""),
            locks['config'], locks['activate'])]

    @api("/auth/forgot_password", methods=["POST"])
    def forgot_password(req):
        """忘记密码：向用户注册邮箱发送验证码（复用邮箱验证配置）。"""
        email = req.get("email")
        if not isinstance(email, str) or not email:
            return bool_res()[False]
        cfg = read_config()
        sender_email = cfg.get("email_activate", "")
        if not sender_email:
            return bool_res()[False]
        email_pwd = cfg.get("email_password", "")
        rows = user_cursor.email_query(email)
        if not rows:
            # 邮箱不存在也统一返回 False，不泄露账号信息
            return bool_res()[False]
        try:
            if not register_tool.email_code_async(
                    sender_email, port_api, email, email_pwd,
                    locks['config'], locks['activate']):
                return bool_res()[False]
        except Exception as e:
            print("[WARN] forgot_password email send failed: {}".format(e))
            return bool_res()[False]
        return bool_res()[True]

    @api("/auth/reset_password", methods=["POST"])
    def reset_password(req):
        """忘记密码：验证邮箱验证码后重置密码，并吊销该用户全部 token。"""
        email = req.get("email")
        activate_code = req.get("activate_code")
        new_pwd = req.get("new_pwd")
        if not isinstance(email, str) or not email:
            return bool_res()[False]
        if not isinstance(new_pwd, str) or not new_pwd:
            return bool_res()[False]
        try:
            code = int(activate_code)
        except (TypeError, ValueError):
            return bool_res()[False]
        rows = user_cursor.email_query(email)
        if not rows:
            return bool_res()[False]
        uid = rows[0][0]
        if not register_tool.verify_email(port_api, email, code, locks['activate']):
            return bool_res()[False]
        user_cursor.change_pwd(uid, new_pwd)
        user_cursor.bump_auth_version(uid)
        user_cursor.delete_tokens(uid)
        return bool_res()[True]

     

    @api("/auth/change_auth", methods=["POST"])
    def change_auth(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            oped = req["change_uid"]
            new_auth = req["new_auth"]
            return bool_res()[perform_managed_auth_change(uid, pwd, oped, new_auth)]
        except Exception:
            return bool_res()[False]

    @api("/auth/manage/create", methods=["POST"])
    def manage_create_user(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            username = req["username"]
            target_password = req["target_password"]
            new_auth = req.get("new_auth", "user")
            email = req["email"] if "email" in req else None

            operator = verify_manager(uid, pwd)
            if operator is None:
                return bool_res()[False]
            if not can_manage_auth(operator[4], new_auth):
                return bool_res()[False]
            cfg = read_config()
            if (not isinstance(username, str) or len(username) < int(cfg.get("min_username_length", 4))
                    or not isinstance(target_password, str) or len(target_password) < int(cfg.get("min_password_length", 1))):
                return bool_res()[False]

            if not user_cursor.user_create(username, target_password, time.time(), email, stat=new_auth):
                return bool_res()[False]

            target = user_cursor.username_query(username)
            if not target:
                return bool_res()[False]

            target_uid = target[0][0]
            extra_updates = {}
            if "sign" in req:
                extra_updates["sign"] = req["sign"]
            if "introduction" in req:
                extra_updates["introduction"] = req["introduction"]

            if extra_updates and not user_cursor.update_user(target_uid, **extra_updates):
                user_cursor.delete_user(target_uid)
                return bool_res()[False]

            if not ensure_notification_table(target_uid):
                user_cursor.delete_user(target_uid)
                return bool_res()[False]
            apply_default_join_targets(target_uid, cfg)
            return bool_res()[True]
        except Exception:
            return bool_res()[False]

    @api("/auth/manage/update", methods=["POST"])
    def manage_update_user(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            target_uid = req["change_uid"]
            next_auth = un_optional_managed_auth(req.get("new_auth"))

            operator = verify_manager(uid, pwd)
            if operator is None:
                return bool_res()[False]

            target = resolve_managed_target(operator[4], target_uid, next_auth=next_auth)
            if target is None:
                return bool_res()[False]

            updates = collect_managed_updates(req, next_auth=next_auth)
            if not updates:
                return bool_res()[False]
            cfg = read_config()
            if "username" in updates and (
                    not isinstance(updates["username"], str)
                    or len(updates["username"]) < int(cfg.get("min_username_length", 4))):
                return bool_res()[False]
            if "password" in updates and (
                    not isinstance(updates["password"], str)
                    or len(updates["password"]) < int(cfg.get("min_password_length", 1))):
                return bool_res()[False]

            if not user_cursor.update_user_with_root_guard(target_uid, **updates):
                return bool_res()[False]

            if "password" in updates or next_auth is not None:
                user_cursor.bump_auth_version(target_uid)
            if next_auth == "banned":
                run_side_effect(
                    "disconnect_banned_user",
                    lambda: instant_contact.disconnect_user(target_uid)
                )

            if next_auth is not None:
                notify_user(target_uid, "auth.stat.changed", "账号状态已变更", "你的账号状态已更新为 {}。".format(next_auth), sender=uid, meta={"new_auth" : next_auth})
            return bool_res()[True]
        except Exception:
            return bool_res()[False]

    @api("/auth/manage/ban", methods=["POST"])
    def manage_ban_user(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            target_uid = req["change_uid"]
            return bool_res()[perform_managed_auth_change(uid, pwd, target_uid, "banned")]
        except Exception:
            return bool_res()[False]

    @api("/auth/manage/delete", methods=["POST"])
    def manage_delete_user(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            target_uid = int(req["change_uid"])

            operator = verify_manager(uid, pwd)
            if operator is None:
                return bool_res()[False]

            target = resolve_managed_target(operator[4], target_uid, deleting=True)
            if target is None:
                return bool_res()[False]

            if target[4] == "root" and user_cursor.count_users_with_stat("root") <= 1:
                return bool_res()[False]

            if not user_cursor.delete_user_with_root_guard(target_uid):
                return bool_res()[False]

            user_cursor.delete_tokens(target_uid)

            run_side_effect(
                "disconnect_deleted_user",
                lambda: instant_contact.disconnect_user(target_uid)
            )
            run_side_effect(
                "clean_deleted_user_state",
                lambda: clean_deleted_user_state(target_uid)
            )
            run_side_effect(
                "delete_user_notification_table",
                lambda: notification_cursor.delete_user_table(target_uid)
            )
            return bool_res()[True]
        except Exception:
            return bool_res()[False]

    @api("/auth/manage/list", methods=['POST'])
    def manage_list_users(req):
        try:
            uid = req["uid"]
            pwd = req["password"]

            if verify_manager(uid, pwd) is None:
                return json.dumps({"success": False, "error": "forbidden"})

            requested_page_size = req.get("page_size", 50)
            fetch_all = parse_bool_flag(req.get("fetch_all", False)) or str(requested_page_size) == "-1"
            total = user_cursor.count_users()

            if fetch_all:
                rows = user_cursor.list_users()
                return json.dumps({
                    "users" : [serialize_user_summary(row) for row in rows],
                    "pagination" : {
                        "page" : 1,
                        "page_size" : len(rows),
                        "total" : total,
                        "total_pages" : 1 if total else 0,
                        "has_more" : False
                    },
                    "fetch_all" : True
                }, ensure_ascii=False)

            page_size = parse_int_setting(requested_page_size, minimum=1)
            page_size = min(page_size, 500)
            page = parse_int_setting(req.get("page", 1), minimum=1)
            offset = (page - 1) * page_size
            rows = user_cursor.list_users(limit=page_size, offset=offset)
            total_pages = (total + page_size - 1) // page_size if total else 0

            return json.dumps({
                "users" : [serialize_user_summary(row) for row in rows],
                "pagination" : {
                    "page" : page,
                    "page_size" : page_size,
                    "total" : total,
                    "total_pages" : total_pages,
                    "has_more" : offset + len(rows) < total
                },
                "fetch_all" : False
            }, ensure_ascii=False)
        except Exception:
            return json.dumps({"success": False, "error": "unavailable"})
    
    @api("/auth/change_sign", methods=['POST'])
    def change_sign(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        new_sign = req["new_sign"]
        max_sign = read_config().get("max_sign_length", 100)
        if max_sign > 0 and len(str(new_sign)) > max_sign:
            return bool_res()[False]
        user_cursor.change_sign(uid, new_sign)
        return bool_res()[True]
    
    @api("/auth/change_introduction", methods=["POST"])
    def change_introduction(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        new_intro = req["new_introduction"]
        # #16: 校验简介长度
        max_intro = read_config().get("max_introduction_length", 500)
        if max_intro > 0 and len(str(new_intro)) > max_intro:
            return bool_res()[False]
        user_cursor.change_introduction(uid, new_intro)
        return bool_res()[True]

    @api("/auth/change_public_email", methods=["POST"])
    def change_public_email(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        public = req.get("public_email")
        if not isinstance(public, bool):
            return bool_res()[False]
        user_cursor.set_public_email(uid, public)
        return bool_res()[True]

    @api("/auth/change_privacy", methods=["POST"])
    def change_privacy(req):
        """修改隐私开关（已读状态、在线状态）。

        成功仍按 bool 约定回 `True`（与 change_pwd / change_sign 一致），但
        拒绝改成 JSON 错误对象：这里是三种完全不同的失败（鉴权、字段类型、
        一个字段都没给），压成同一个 `False` 会让客户端无法分辨，也只能靠
        `detail_error` 协商拿到规范化错误码。未声明 detail_error 的老客户端
        由 crypto.legacy_failure_body 降级回原来的纯文本 `False`，行为不变。
        """
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        changed = {}
        if "show_read_status" in req:
            value = req.get("show_read_status")
            if not isinstance(value, bool):
                return json.dumps({"success": False, "error": "invalid_request"})
            user_cursor.set_show_read_status(uid, value)
            changed["show_read_status"] = value
        if "show_online_status" in req:
            value = req.get("show_online_status")
            if not isinstance(value, bool):
                return json.dumps({"success": False, "error": "invalid_request"})
            user_cursor.set_show_online_status(uid, value)
            changed["show_online_status"] = value
        if not changed:
            return json.dumps({"success": False, "error": "invalid_request"})
        # 单用户多端同步：隐私开关变更静默推给该用户所有在线端
        try:
            payload = {"type": "PRIVACY.UPDATED"}
            payload.update(changed)
            instant_contact.push_raw(uid, payload)
        except Exception:
            pass
        # 关闭在线状态
        if changed.get("show_online_status") is False:
            try:
                instant_contact.presence_hidden(uid)
            except Exception:
                pass
        # 重新开启且在线的用户
        if changed.get("show_online_status") is True:
            try:
                instant_contact.presence_shown(uid)
            except Exception:
                pass
        return bool_res()[True]
    
    @api("/auth/change_captcha", methods=['POST'])
    def change_captcha(req):
        uid = req["uid"]
        pwd = req["password"]
        final_stat = req["change_to"]
        if not verify_user(uid, pwd):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None or user_row[4] != 'root':
            return bool_res()[False]
        provider = req.get("captcha_provider")
        if isinstance(provider, str) and provider:
            provider = provider.strip().lower()
            if provider != captcha_service.IMAGE_PROVIDER and not captcha_service.is_third_party(provider):
                return {"error": "invalid_request"}
        else:
            provider = None

        def apply(cfg):
            cfg["captcha"] = final_stat
            if provider is not None:
                cfg["captcha_provider"] = provider
            site_key = req.get("captcha_site_key")
            if isinstance(site_key, str) and site_key:
                cfg["captcha_site_key"] = site_key
            secret = req.get("captcha_secret")
            if isinstance(secret, str) and secret:
                cfg["captcha_secret"] = secret

        update_config(apply)
        return bool_res()[True]

    @api("/auth/change_rate_limits", methods=['POST'])
    def change_rate_limits(req):
        """
        更新端点速率限制配置。
        仅 root 用户可操作。
        
        请求体示例：
        {
            "uid": 0,
            "password": "xxx",
            "rate_limits": {
                "default":        {"requests": 60, "range": 60},
                "/auth/login":    {"requests": 10, "range": 60},
                "/auth/register": {"requests": 5,  "range": 300}
            }
        }
        传入 null 可清空所有速率限制。
        """
        uid = req["uid"]
        pwd = req["password"]
        if not verify_user(uid, pwd):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None or user_row[4] != 'root':
            return bool_res()[False]
        new_limits = req.get("rate_limits")
        if new_limits is not None and not isinstance(new_limits, dict):
            return bool_res()[False]
        def change(cfg):
            if new_limits is None:
                cfg.pop("rate_limits", None)
            else:
                cfg["rate_limits"] = new_limits
        update_config(change)
        limiter.reload(port_api)
        return bool_res()[True]

    @api("/auth/server_settings/query", methods=['POST'])
    def query_server_settings(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            if verify_root(uid, pwd) is None:
                return json.dumps({"success": False, "error": "forbidden"})
            return json.dumps(serialize_server_settings(read_config(), include_manage=True), ensure_ascii=False)
        except Exception:
            return json.dumps({"success": False, "error": "forbidden"})

    @api("/auth/server_settings/update", methods=['POST'])
    def update_server_settings(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            if verify_root(uid, pwd) is None:
                return json.dumps({"success": False, "error": "forbidden"})

            updates = {}

            if "server_name" in req:
                server_name = req["server_name"]
                if not isinstance(server_name, str):
                    return json.dumps({"success": False, "error": "forbidden"})
                server_name = server_name.strip()
                if not server_name:
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["server_name"] = server_name

            if "captcha" in req:
                if not isinstance(req["captcha"], bool):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["captcha"] = req["captcha"]

            if "file_last_time" in req:
                updates["file_last_time"] = parse_int_setting(req["file_last_time"], minimum=0)

            if "groups_limit" in req:
                updates["groups_limit"] = parse_int_setting(req["groups_limit"], minimum=1, allow_unlimited=True)

            if "single_group_max_people" in req:
                updates["single_group_max_people"] = parse_int_setting(req["single_group_max_people"], minimum=1, allow_unlimited=True)

            if "default_join_targets" in req:
                targets = normalize_default_join_targets(req["default_join_targets"])
                validate_default_join_targets(targets)
                updates["default_join_targets"] = targets

            if "max_file_size" in req:
                updates["max_file_size"] = parse_int_setting(req["max_file_size"], minimum=0, allow_unlimited=True)

            if "max_avatar_size" in req:
                updates["max_avatar_size"] = parse_int_setting(req["max_avatar_size"], minimum=0, allow_unlimited=True)

            if "user_storage_quota" in req:
                updates["user_storage_quota"] = parse_int_setting(req["user_storage_quota"], minimum=0, allow_unlimited=True)

            if "max_user_storage_quota" in req:
                updates["max_user_storage_quota"] = parse_int_setting(req["max_user_storage_quota"], minimum=0, allow_unlimited=True)

            if "max_sticker_storage_quota" in req:
                updates["max_sticker_storage_quota"] = parse_int_setting(req["max_sticker_storage_quota"], minimum=0, allow_unlimited=True)

            if "max_message_length" in req:
                updates["max_message_length"] = parse_int_setting(req["max_message_length"], minimum=1)

            if "max_request_message_length" in req:
                updates["max_request_message_length"] = parse_int_setting(req["max_request_message_length"], minimum=1)

            if "min_group_name_length" in req:
                updates["min_group_name_length"] = parse_int_setting(req["min_group_name_length"], minimum=1)

            if "max_group_name_length" in req:
                updates["max_group_name_length"] = parse_int_setting(req["max_group_name_length"], minimum=1)

            if "max_sign_length" in req:
                updates["max_sign_length"] = parse_int_setting(req["max_sign_length"], minimum=1, allow_unlimited=True)

            if "max_introduction_length" in req:
                updates["max_introduction_length"] = parse_int_setting(req["max_introduction_length"], minimum=1, allow_unlimited=True)

            if "max_post_content_length" in req:
                updates["max_post_content_length"] = parse_int_setting(req["max_post_content_length"], minimum=1, allow_unlimited=True)

            if "min_username_length" in req:
                updates["min_username_length"] = parse_int_setting(req["min_username_length"], minimum=4)

            if "min_password_length" in req:
                updates["min_password_length"] = parse_int_setting(req["min_password_length"], minimum=1)

            if "max_sticker_packs_per_user" in req:
                updates["max_sticker_packs_per_user"] = parse_int_setting(req["max_sticker_packs_per_user"], minimum=1, allow_unlimited=True)

            if "max_stickers_per_pack" in req:
                updates["max_stickers_per_pack"] = parse_int_setting(req["max_stickers_per_pack"], minimum=1, allow_unlimited=True)

            if "daily_sticker_pack_creation_limit" in req:
                updates["daily_sticker_pack_creation_limit"] = parse_int_setting(req["daily_sticker_pack_creation_limit"], minimum=1, allow_unlimited=True)

            if "max_sticker_size" in req:
                updates["max_sticker_size"] = parse_int_setting(req["max_sticker_size"], minimum=1, allow_unlimited=True)

            if "smtp_host" in req:
                if not isinstance(req["smtp_host"], str):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["smtp_host"] = req["smtp_host"].strip()

            if "smtp_port" in req:
                updates["smtp_port"] = parse_int_setting(req["smtp_port"], minimum=1)

            if "smtp_use_ssl" in req:
                if not isinstance(req["smtp_use_ssl"], bool):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["smtp_use_ssl"] = req["smtp_use_ssl"]

            # ---- 外部认证平台相关配置 ----
            if "allow_register" in req:
                if not isinstance(req["allow_register"], bool):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["allow_register"] = req["allow_register"]

            if "external_auth_url" in req:
                value = req["external_auth_url"]
                if not isinstance(value, str):
                    return json.dumps({"success": False, "error": "invalid_request"})
                value = value.strip().rstrip("/")
                # 只允许 http(s)，否则客户端 WebView 可能被 javascript: 之类注入
                if value and not (value.startswith("http://") or value.startswith("https://")):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["external_auth_url"] = value

            if "platform_auth_enabled" in req:
                if not isinstance(req["platform_auth_enabled"], bool):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["platform_auth_enabled"] = req["platform_auth_enabled"]

            if "platform_auth_issuer" in req:
                value = req["platform_auth_issuer"]
                if not isinstance(value, str):
                    return json.dumps({"success": False, "error": "invalid_request"})
                value = value.strip().rstrip("/")
                if value and not (value.startswith("http://") or value.startswith("https://")):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["platform_auth_issuer"] = value

            if "platform_auth_allow_ips" in req:
                value = req["platform_auth_allow_ips"]
                if not isinstance(value, str):
                    return json.dumps({"success": False, "error": "invalid_request"})
                parts = [item.strip() for item in value.split(",") if item.strip()]
                for spec in parts:
                    try:
                        ipaddress.ip_network(spec, strict=False)
                    except ValueError:
                        return json.dumps({"success": False, "error": "invalid_request"})
                updates["platform_auth_allow_ips"] = ",".join(parts)

            if "platform_auth_shared_token" in req:
                value = req["platform_auth_shared_token"]
                if not isinstance(value, str) or len(value) > 128:
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["platform_auth_shared_token"] = value.strip()

            if "reverse_proxy_enabled" in req:
                if not isinstance(req["reverse_proxy_enabled"], bool):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["reverse_proxy_enabled"] = req["reverse_proxy_enabled"]

            if "proxy_count" in req:
                updates["proxy_count"] = parse_int_setting(req["proxy_count"], minimum=0)

            if "legacy_auth_enabled" in req:
                if not isinstance(req["legacy_auth_enabled"], bool):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["legacy_auth_enabled"] = req["legacy_auth_enabled"]

            if "jwt_expires_seconds" in req:
                updates["jwt_expires_seconds"] = parse_int_setting(req["jwt_expires_seconds"], minimum=60)

            if "jwt_refresh_expires_seconds" in req:
                updates["jwt_refresh_expires_seconds"] = parse_int_setting(req["jwt_refresh_expires_seconds"], minimum=60)

            if "jwt_max_per_user" in req:
                updates["jwt_max_per_user"] = parse_int_setting(req["jwt_max_per_user"], minimum=0, allow_unlimited=True)

            if "file_download_mode" in req:
                if req["file_download_mode"] not in ("redirect", "proxy"):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["file_download_mode"] = req["file_download_mode"]

            if "media_features" in req:
                if not isinstance(req["media_features"], bool):
                    return json.dumps({"success": False, "error": "invalid_request"})
                updates["media_features"] = req["media_features"]

            if "features" in req:
                try:
                    updates["features"] = merge_features(
                        read_config().get("features"), req["features"]
                    )
                except ValueError:
                    return json.dumps({"success": False, "error": "invalid_request"})

            if not updates:
                return json.dumps({"success": False, "error": "invalid_request"})

            def _apply(current):
                current.update(updates)
            cfg = update_config(_apply)
            # 重新加载 ws
            # 无条件刷新，避免只有部分键变更时才生效的遗漏
            instant_contact._load_config(cfg)
            # 反代配置变更时重新包装 wsgi 中间件
            if "reverse_proxy_enabled" in updates or "proxy_count" in updates:
                apply_proxy_fix(cfg)
            return json.dumps(serialize_server_settings(cfg, include_manage=True), ensure_ascii=False)
        except Exception:
            return json.dumps({"success": False, "error": "unavailable"})

    @api("/auth/manage/signing_keys/list", methods=["POST"])
    def list_signing_keys(req):
        """
        列出本服 JWT 签名密钥（root）。
        返回的 kid/algorithm 与 /.well-known/jwks.json 中的公钥一一对应。
        """
        try:
            uid = req.get("uid")
            if uid is None or verify_root(uid, req.get("password")) is None:
                return {"error": "forbidden"}
            cfg = read_config()
            return {
                "keys": jwt_key_store.list_keys(),
                "signing_algorithm": jwt_signing_algorithm(cfg),
                "issuer": jwt_issuer(cfg),
                "jwks_uri": jwt_jwks_uri(cfg),
                "retention_days": jwt_key_retention_days(cfg),
            }
        except Exception as error:
            print("[ERR] 读取 JWT 签名密钥失败: {}".format(error))
            return {"error": "unavailable"}

    @api("/auth/manage/signing_keys/rotate", methods=["POST"])
    def rotate_signing_key(req):
        """
        轮换 JWT 签名密钥（root）。

        默认同时把 jwt_signing_algorithm 切到新算法（activate=true）：
        旧密钥在保留期内仍可验签并继续出现在 JWKS 中，存量 token 不会立即失效。
        activate=false 只改密钥、不改配置；新钥仍会成为该算法的 active 签发钥。
        """
        try:
            uid = req.get("uid")
            if uid is None or verify_root(uid, req.get("password")) is None:
                return {"error": "forbidden"}
            algorithm = jwt_keys.normalize_algorithm(
                req.get("algorithm", "RS256"), default=None
            )
            if algorithm not in jwt_keys.ASYMMETRIC_ALGORITHMS:
                return {"error": "invalid_request"}
            activate = req.get("activate", True)
            if not isinstance(activate, bool):
                return {"error": "invalid_request"}
            issuer = req.get("issuer")
            if issuer is not None and (not isinstance(issuer, str) or len(issuer.strip()) > 512):
                return {"error": "invalid_request"}
            previous_algorithm = jwt_signing_algorithm()
            kid = jwt_key_store.rotate(
                algorithm, retention_days=jwt_key_retention_days()
            )
            if activate:
                def apply(cfg):
                    cfg["jwt_signing_algorithm"] = algorithm
                    if isinstance(issuer, str) and issuer.strip():
                        cfg["jwt_issuer"] = issuer.strip()
                update_config(apply)
            return {
                "success": True,
                "kid": kid,
                "algorithm": algorithm,
                "previous_algorithm": previous_algorithm,
                "signing_algorithm": jwt_signing_algorithm(),
                "jwks_uri": jwt_jwks_uri(),
            }
        except Exception as error:
            print("[ERR] 轮换 JWT 签名密钥失败: {}".format(error))
            return {"error": "unavailable"}

    @api("/notification/query_all", methods=['POST'])
    def query_all_notifications(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        # 默认只返回最近 500 条
        try:
            limit = int(req.get("limit", 500))
            offset = int(req.get("offset", 0))
        except (TypeError, ValueError):
            return bool_res()[False]
        limit = max(1, min(limit, 1000))
        offset = max(offset, 0)
        return serialize_notifications(
            notification_cursor.query_all_events(uid, limit=limit, offset=offset))

    @api("/notification/query_after", methods=['POST'])
    def query_notifications_after(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        try:
            time_stamp = float(req.get("time_stamp", 0))
        except (TypeError, ValueError):
            return bool_res()[False]
        return serialize_notifications(notification_cursor.query_events_after(uid, time_stamp))

    @api("/notification/delete_before", methods=['POST'])
    def delete_notifications_before(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        try:
            time_stamp = float(req["time_stamp"])
        except (KeyError, TypeError, ValueError):
            return bool_res()[False]
        return bool_res()[notification_cursor.delete_events_before(uid, time_stamp)]

    @api("/notification/delete_all", methods=['POST'])
    def delete_all_notifications(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        return bool_res()[notification_cursor.delete_all_events(uid)]

    @api("/notification/unread_count", methods=['POST'])
    def notification_unread_count(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        return json.dumps({"count": notification_cursor.unread_count(uid)}, ensure_ascii=False)

    @api("/notification/mark_read", methods=['POST'])
    def notification_mark_read(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        try:
            time_stamp = req.get("time_stamp")
            ids = req.get("ids")
            if time_stamp is not None:
                time_stamp = float(time_stamp)
                changed = notification_cursor.mark_read_until(uid, time_stamp)
            elif ids is not None:
                changed = notification_cursor.mark_read_ids(uid, ids)
            else:
                return json.dumps({"success": False, "error": "invalid_request"})
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        return json.dumps({"success": True, "changed": changed}, ensure_ascii=False)

    @api("/notification/mark_all_read", methods=['POST'])
    def notification_mark_all_read(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        changed = notification_cursor.mark_all_read(uid)
        return json.dumps({"success": True, "changed": changed}, ensure_ascii=False)

    @api("/notification/list", methods=['POST'])
    def notification_list(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        try:
            offset = max(int(req.get("offset", 0)), 0)
            take = min(max(int(req.get("take", 50)), 1), 100)
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        rows, total = notification_cursor.list_events_page(uid, offset, take)
        return json.dumps({
            "items": notification_cursor.serialize_rows(rows),
            "total": total,
            "has_more": offset + len(rows) < total,
        }, ensure_ascii=False)

    @api("/auth/mention_candidates", methods=['POST'])
    def mention_candidates(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        rows = user_cursor.query(
            "SELECT uid, username FROM users WHERE stat != 'banned' ORDER BY username"
        )
        return json.dumps([
            {"uid": row[0], "username": row[1]} for row in rows if row[0] != uid
        ], ensure_ascii=False)

    @app.route("/info")
    def info():
        return serialize_server_settings(read_config())

    # ---- 外部认证平台专用端点 ----
    # 这三个端点只登记"谁绑了谁"，刻意不创建本地会话：
    # 绑定是纯校验 + 写映射，不应该占用用户的 jwt_max_per_user 配额，
    # 更不应该因超限吊销用户最老的会话。

    @api("/auth/platform/bind", methods=["POST"])
    def platform_bind(req):
        """校验 TouchFish 凭据并建立 (iss, sub) -> uid 映射。不建会话、不发 token。"""
        cfg = read_config()
        iss = platform_issuer(cfg)
        if not iss:
            return {"error": "platform_not_configured"}
        if req.get("iss") != iss:
            return {"error": "external_token_invalid"}
        sub = req.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > 128:
            return {"error": "invalid_request"}

        identifier = req.get("identifier")
        password = req.get("password")
        if not isinstance(password, str) or not password:
            return {"error": "invalid_request"}
        if isinstance(identifier, bool):
            return {"error": "invalid_request"}
        if isinstance(identifier, int):
            uid = identifier
        elif isinstance(identifier, str) and identifier.strip():
            text = identifier.strip()
            if text.isdigit():
                uid = int(text)
            else:
                rows = user_cursor.username_query(text)
                if not rows:
                    return {"error": "auth_failed"}
                uid = rows[0][0]
        else:
            return {"error": "invalid_request"}

        if not user_cursor.verify_user(uid, password):
            return {"error": "auth_failed"}
        row = user_cursor.uid_query(uid)
        if not row:
            return {"error": "auth_failed"}
        if row[0][4] == "banned":
            return {"error": "user_banned"}

        existing = user_cursor.get_identity_uid(iss, sub)
        if existing == uid:
            return {"success": True, "uid": uid, "username": row[0][1], "created": False}
        if existing is not None:
            return {"error": "external_already_linked"}
        if not user_cursor.bind_identity(iss, sub, uid):
            return {"error": "external_already_linked"}
        return {"success": True, "uid": uid, "username": row[0][1], "created": True}

    @api("/auth/platform/unbind", methods=["POST"])
    def platform_unbind(req):
        """按 (iss, sub) 解绑。不 bump auth_version，避免踢掉用户所有设备。"""
        cfg = read_config()
        iss = platform_issuer(cfg)
        if not iss or req.get("iss") != iss:
            return {"error": "external_token_invalid"}
        sub = req.get("sub")
        if not isinstance(sub, str) or not sub:
            return {"error": "invalid_request"}
        uid = user_cursor.get_identity_uid(iss, sub)
        if uid is None:
            return {"success": True}
        if not user_cursor.unbind_identity(uid, iss):
            return {"error": "conflict"}
        return {"success": True}

    @app.route("/auth/platform/status")
    def platform_status():
        """明文只读：平台据此判断绑定是否仍然有效，避免自己维护副本。"""
        cfg = read_config()
        iss = platform_issuer(cfg)
        sub = flask_request.args.get("sub", "")
        if not iss or not sub:
            return {"linked": False}
        uid = user_cursor.get_identity_uid(iss, sub)
        if uid is None:
            return {"linked": False}
        row = user_cursor.uid_query(uid)
        if not row:
            return {"linked": False}
        return {"linked": True, "uid": uid, "username": row[0][1], "stat": row[0][4]}

    def _sticker_item_dict(row):
        return {
            "id": row[0], "pack_id": row[1], "slug": row[2], "name": row[3],
            "file_hash": row[4], "file_type": row[5], "order": row[6],
            "size": row[7], "mode": row[8], "created_at": row[9],
            "download_url" : "/sticker/get/{}".format(row[4]),
        }

    def _sticker_pack_dict(row, include_stickers=True):
        data = {
            "id": row[0], "creator_uid": row[1], "name": row[2],
            "description": row[3], "prefix": row[4], "icon_hash": row[5],
            "created_at": row[6], "updated_at": row[7], "usage_count": row[8],
        }
        if include_stickers:
            data["stickers"] = [_sticker_item_dict(item) for item in sticker_cursor.list_stickers(row[0])]
        return data

    def _sticker_exempt(uid):
        rows = user_cursor.uid_query(uid)
        return bool(rows and rows[0][4] in manager_auths)

    def _read_blob_head(kind, hashes, length=4096):
        """读取 blob 的 (总大小, 头部字节)"""
        target = file.sticker_path(port_api, hashes) if kind == "sticker" else file.file_path(port_api, hashes)
        if os.path.isfile(target):
            try:
                with open(target, "rb") as handle:
                    return os.path.getsize(target), handle.read(length)
            except OSError:
                return None
        if oss_store.is_oss_enabled(port_api):
            return oss_store.head_and_prefix(port_api, kind, hashes, length)
        return None

    def _read_blob_bytes(kind, hashes):
        """完整读取 blob 内容（仅用于 ≤10MB 的 gzip/tgs 嗅探回退）。"""
        target = file.sticker_path(port_api, hashes) if kind == "sticker" else file.file_path(port_api, hashes)
        if os.path.isfile(target):
            try:
                with open(target, "rb") as handle:
                    return handle.read()
            except OSError:
                return None
        if oss_store.is_oss_enabled(port_api):
            return oss_store.download_bytes_from_oss(port_api, kind, hashes)
        return None

    def _blob_type_and_size(kind, hashes):
        """GET blob 类型与大小"""
        head = _read_blob_head(kind, hashes)
        if head is None:
            return None
        size, prefix = head
        file_type = detect_file_type(prefix, "")
        if file_type == "unknown" and prefix.startswith(b"\x1f\x8b") and 0 < size <= 10 * 1024 * 1024:
            content = _read_blob_bytes(kind, hashes)
            if content is not None:
                file_type = detect_file_type(content, "")
        return file_type, size

    @app.route("/sticker/market")
    def sticker_market():
        if sticker_cursor is None:
            return json.dumps({"items": [], "total": 0})
        try:
            offset = max(int(flask_request.args.get("offset", 0)), 0)
            limit = min(max(int(flask_request.args.get("limit", 20)), 1), 50)
        except ValueError:
            return json.dumps({"items": [], "total": 0})
        order = flask_request.args.get("order", "usage")
        rows, total = sticker_cursor.list_packs(offset, limit, flask_request.args.get("query", "").strip(), order)
        return json.dumps({"items": [_sticker_pack_dict(row) for row in rows], "total": total}, ensure_ascii=False)

    @app.route("/sticker/pack/<pack_id>")
    def sticker_pack(pack_id):
        if sticker_cursor is None:
            return json.dumps({"error": "unavailable"}), 404
        row = sticker_cursor.get_pack(pack_id)
        if row is None:
            return json.dumps({"error": "not_found"}), 404
        return json.dumps(_sticker_pack_dict(row), ensure_ascii=False)

    @app.route("/sticker/lookup/<identifier>")
    def sticker_lookup(identifier):
        if sticker_cursor is None or "+" not in identifier:
            return json.dumps({"error": "not_found"}), 404
        prefix, slug = identifier.split("+", 1)
        rows = sticker_cursor.query("""SELECT s.id, s.pack_id, s.slug, s.name, s.file_hash, s.file_type, s.position, s.render_size, s.render_mode, s.created_at
                                       FROM stickers s JOIN sticker_packs p ON p.id = s.pack_id
                                       WHERE p.prefix = ? COLLATE NOCASE AND s.slug = ? AND p.is_deleted = 0""", (prefix, slug))
        if not rows:
            return json.dumps({"error": "not_found"}), 404
        return json.dumps(_sticker_item_dict(rows[0]), ensure_ascii=False)

    @api("/sticker/mine", methods=["POST"])
    def sticker_mine(req):
        uid, password = req.get("uid"), req.get("password")
        if sticker_cursor is None or not verify_user(uid, password):
            return json.dumps({"error": "auth_failed"})
        owned = []
        for row in sticker_cursor.list_owned(uid):
            pack = _sticker_pack_dict(row[2:], include_stickers=True)
            owned.append({"pack_id": row[0], "order": row[1], "pack": pack})
        return json.dumps(owned, ensure_ascii=False)

    @api("/sticker/created", methods=["POST"])
    def sticker_created(req):
        uid, password = req.get("uid"), req.get("password")
        if sticker_cursor is None or not verify_user(uid, password):
            return json.dumps({"error": "auth_failed"})
        rows, total = sticker_cursor.list_packs(0, 100, creator_uid=uid)
        return json.dumps({"items": [_sticker_pack_dict(row) for row in rows], "total": total}, ensure_ascii=False)

    @api("/sticker/pack/create", methods=["POST"], feature=("sticker",))
    def create_sticker_pack(req):
        uid, password = req.get("uid"), req.get("password")
        name, prefix = req.get("name"), req.get("prefix")
        if sticker_cursor is None or not verify_user(uid, password) or not isinstance(name, str) or not name.strip() or not isinstance(prefix, str) or not prefix.strip():
            return json.dumps({"success": False, "error": "invalid_request"})
        cfg = read_config()
        local_day = datetime.now().date().isoformat()
        pack_id, error = sticker_cursor.create_pack(uid, name.strip(), str(req.get("description", "")).strip(), prefix.strip(), local_day, int(cfg.get("max_sticker_packs_per_user", 24)), int(cfg.get("daily_sticker_pack_creation_limit", -1)), _sticker_exempt(uid))
        if error:
            return json.dumps({"success": False, "error": error})
        return json.dumps({"success": True, "pack": _sticker_pack_dict(sticker_cursor.get_pack(pack_id))}, ensure_ascii=False)

    @api("/sticker/item/create", methods=["POST"], feature=("sticker",))
    def create_sticker_item(req):
        uid, password = req.get("uid"), req.get("password")
        pack_id, slug, file_hash = req.get("pack_id"), req.get("slug"), req.get("file_hash")
        if sticker_cursor is None or not verify_user(uid, password) or not all(isinstance(value, str) and value for value in (pack_id, slug, file_hash)):
            return json.dumps({"success": False, "error": "invalid_request"})
        # 贴图文件现由 sticker.db 独立管理；兼容旧的 file 上传渠道
        sticker_managed = sticker_cursor.has_active_user_sticker(uid, file_hash)
        if not sticker_managed and not file_cursor.has_active_user_file(uid, file_hash):
            return json.dumps({"success": False, "error": "file_not_owned"})
        kind = "sticker" if sticker_managed else "file"
        try:
            type_and_size = _blob_type_and_size(kind, file_hash)
        except OSError:
            type_and_size = None
        if type_and_size is None:
            return json.dumps({"success": False, "error": "file_unavailable"})
        file_type, sticker_size = type_and_size
        if file_type not in IMAGE_TYPES:
            return json.dumps({"success": False, "error": "unsupported_sticker_type"})
        cfg = read_config()
        max_sticker_size = cfg.get("max_sticker_size", 1048576)
        if max_sticker_size != -1 and sticker_size > max_sticker_size:
            return json.dumps({"success": False, "error": "sticker_too_large"})
        sticker_id, error = sticker_cursor.create_sticker(uid, pack_id, slug.strip(), req.get("name"), file_hash, file_type, int(req.get("size", 0)), int(req.get("mode", 0)), int(cfg.get("max_stickers_per_pack", 24)), _sticker_exempt(uid))
        if error:
            return json.dumps({"success": False, "error": error})
        item = sticker_cursor.query("SELECT id, pack_id, slug, name, file_hash, file_type, position, render_size, render_mode, created_at FROM stickers WHERE id = ?", (sticker_id,))[0]
        if file_cursor.file_exists(file_hash):
            file_cursor.add_reference(file_hash, "sticker", sticker_id, uid)
        return json.dumps({"success": True, "sticker": _sticker_item_dict(item)}, ensure_ascii=False)

    @api("/sticker/pack/update", methods=["POST"], feature=("sticker",))
    def update_sticker_pack(req):
        uid, password, pack_id = req.get("uid"), req.get("password"), req.get("pack_id")
        if sticker_cursor is None or not verify_user(uid, password) or not isinstance(pack_id, str) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
            return json.dumps({"success": False, "error": "forbidden"})
        name = req.get("name"); prefix = req.get("prefix"); description = req.get("description")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return json.dumps({"success": False, "error": "invalid_request"})
        if prefix is not None and (not isinstance(prefix, str) or not prefix.strip()):
            return json.dumps({"success": False, "error": "invalid_request"})
        try:
            sticker_cursor.update_pack(pack_id, name=name.strip() if isinstance(name, str) else None, description=description.strip() if isinstance(description, str) else None, prefix=prefix.strip() if isinstance(prefix, str) else None)
        except Exception:
            return json.dumps({"success": False, "error": "conflict"})
        return json.dumps({"success": True, "pack": _sticker_pack_dict(sticker_cursor.get_pack(pack_id))}, ensure_ascii=False)

    @api("/sticker/pack/delete", methods=["POST"], feature=("sticker",))
    def delete_sticker_pack(req):
        uid, password, pack_id = req.get("uid"), req.get("password"), req.get("pack_id")
        if sticker_cursor is None or not verify_user(uid, password) or not isinstance(pack_id, str) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
            return json.dumps({"success": False, "error": "forbidden"})
        hashes = sticker_cursor.delete_pack(pack_id)
        for sticker_id, file_hash in hashes:
            try:
                if file_cursor.file_exists(file_hash):
                    file_cursor.remove_reference(file_hash, "sticker", sticker_id)
            except Exception:
                pass
        return json.dumps({"success": True})

    @api("/sticker/item/delete", methods=["POST"], feature=("sticker",))
    def delete_sticker_item(req):
        uid, password, pack_id, sticker_id = req.get("uid"), req.get("password"), req.get("pack_id"), req.get("sticker_id")
        if sticker_cursor is None or not verify_user(uid, password) or not all(isinstance(value, str) for value in (pack_id, sticker_id)) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
            return json.dumps({"success": False, "error": "forbidden"})
        file_hash = sticker_cursor.delete_sticker(pack_id, sticker_id)
        if file_hash is None:
            return json.dumps({"success": False, "error": "not_found"})
        try:
            if file_cursor.file_exists(file_hash):
                file_cursor.remove_reference(file_hash, "sticker", sticker_id)
        except Exception:
            pass
        return json.dumps({"success": True})

    @api("/sticker/item/reorder", methods=["POST"], feature=("sticker",))
    def reorder_sticker_items(req):
        uid, password, pack_id, ids = req.get("uid"), req.get("password"), req.get("pack_id"), req.get("sticker_ids")
        if sticker_cursor is None or not verify_user(uid, password) or not isinstance(pack_id, str) or not isinstance(ids, list) or not all(isinstance(value, str) for value in ids) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
            return json.dumps({"success": False, "error": "forbidden"})
        return json.dumps({"success": sticker_cursor.reorder_stickers(pack_id, ids)})

    @api("/sticker/ownership/reorder", methods=["POST"], feature=("sticker",))
    def reorder_sticker_ownership(req):
        uid, password, ids = req.get("uid"), req.get("password"), req.get("pack_ids")
        if (sticker_cursor is None or not verify_user(uid, password)
                or not isinstance(ids, list)
                or not all(isinstance(value, str) for value in ids)):
            return json.dumps({"success": False, "error": "invalid_request"})
        return json.dumps({"success": sticker_cursor.reorder_owned(uid, ids)})

    @api("/sticker/ownership", methods=["POST"], feature=("sticker",))
    def sticker_ownership(req):
        uid, password, pack_id = req.get("uid"), req.get("password"), req.get("pack_id")
        if sticker_cursor is None or not verify_user(uid, password) or not isinstance(pack_id, str):
            return json.dumps({"success": False, "error": "invalid_request"})
        owned = bool(req.get("owned", True))
        return json.dumps({"success": sticker_cursor.set_owned(uid, pack_id, owned)})

    @api("/forum/create_forum", methods=["POST"], feature=("forum",))
    def create_forum(req):
        uid = req["uid"]
        password = req["password"]
        forum_name = req["forum_name"]
        introduction = req["introduction"]
        request_id = req.get("request_id")
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        with locks['queue']:
            def enqueue(queue):
                if isinstance(request_id, str) and request_id:
                    duplicate = any(
                        isinstance(entry, dict) and entry.get("creater") == uid
                        and entry.get("request_id") == request_id
                        for key, entry in queue.items() if str(key).isdigit()
                    )
                    if duplicate:
                        return None
                qid_value = queue['queue_num'] + 1
                for key in queue:
                    if str(key).isdigit():
                        qid_value = max(qid_value, int(key) + 1)
                queue["queue_num"] = qid_value
                queue[qid_value] = {
                    "creater" : uid, "forumname" : forum_name,
                    "introduction" : introduction, "request_id" : request_id,
                }
                return qid_value
            qid = update_json("res/{}/forum/queue.json".format(port_api), enqueue)
            if qid is None:
                return bool_res()[True]
        notify_user(uid, "forum.review.submitted", "论坛已提交审核",
                    "论坛 {} 已提交审核。".format(forum_name), sender=uid,
                    meta={"qid": qid, "forum_name": forum_name})
        reviewer_rows = user_cursor.query(
            "SELECT uid FROM users WHERE stat IN ('admin', 'root')"
        )
        notify_users([row[0] for row in reviewer_rows if row[0] != uid],
                     "forum.review.pending", "新的论坛审核",
                     "论坛 {} 等待审核。".format(forum_name), sender=uid,
                     meta={"qid": qid, "forum_name": forum_name})
        return bool_res()[True]
        
    @api("/forum/get_approving_forum_list", methods=['POST'])
    def get_approving_forum_list(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if not user_stat in ["admin", "root"]:
            return json.dumps({"success": False, "error": "forbidden"})
        with locks['queue']:
            return json.dumps(read_json("res/{}/forum/queue.json".format(port_api)), ensure_ascii=False)
    
    @api("/forum/approve_forum", methods=["POST"], feature=("forum",))
    def approve_forum(req):
        uid = req["uid"]
        password = req["password"]
        qid = req["qid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not user_stat in ["admin", "root"]:
            return bool_res()[False]
        with locks['queue']:
            def approve(queue):
                if str(qid) not in queue:
                    return None
                chosen = queue[str(qid)]
                chosen_type = chosen.get("type", "create")
                # 论坛名不允许重名（编辑时排除自身）
                name_rows = forum_cursor.query(
                    "SELECT fid FROM forums WHERE forumname = ?",
                    (chosen["forumname"],),
                )
                dup_exists = any(
                    row[0] != chosen.get("fid")
                    for row in name_rows
                )
                if dup_exists:
                    del queue[str(qid)]
                    queue["queue_num"] = max(queue["queue_num"] - 1, 0)
                    return False
                if chosen_type == "edit":
                    edit_fid = chosen["fid"]
                    if not forum_cursor.query_forum_fid(edit_fid):
                        del queue[str(qid)]
                        queue["queue_num"] = max(queue["queue_num"] - 1, 0)
                        return False
                    forum_cursor.execute(
                        "UPDATE forums SET forumname = ?, introduction = ? WHERE fid = ?",
                        (chosen["forumname"], chosen["introduction"], edit_fid),
                    )
                    approved_fid = edit_fid
                else:
                    approved_fid = forum_cursor.create_forum(
                        chosen["forumname"], chosen["creater"], chosen["introduction"]
                    )
                del queue[str(qid)]
                queue["queue_num"] = max(queue["queue_num"] - 1, 0)
                return chosen, chosen_type, approved_fid
            approved = update_json("res/{}/forum/queue.json".format(port_api), approve)
            if not approved:
                return bool_res()[False]
            fchosen, entry_type, fid = approved
        action_text = "编辑" if entry_type == "edit" else "创建"
        notify_user(fchosen["creater"], "forum.approved", "论坛已通过审核", "你{}的论坛 {} 已通过审核。".format(action_text, fchosen["forumname"]), sender=uid, meta={"fid" : fid, "forum_name" : fchosen["forumname"]})
        return bool_res()[True]

    @api("/forum/reject_forum", methods=["POST"], feature=("forum",))
    def reject_forum(req):
        uid = req["uid"]
        password = req["password"]
        qid = req["qid"]
        reason = req.get("reason")
        if not isinstance(reason, str):
            reason = ""
        reason = reason.strip()
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not user_stat in ["admin", "root"]:
            return bool_res()[False]
        with locks['queue']:
            def reject(queue):
                if str(qid) not in queue:
                    return None
                chosen = queue.pop(str(qid))
                queue["queue_num"] = max(queue["queue_num"] - 1, 0)
                return chosen
            fchosen = update_json("res/{}/forum/queue.json".format(port_api), reject)
            if fchosen is None:
                return bool_res()[False]
        reason_suffix = "原因：{}".format(reason) if reason else ""
        action_text = "编辑" if fchosen.get("type", "create") == "edit" else "创建"
        notify_user(fchosen["creater"], "forum.rejected", "论坛未通过审核", "你{}的论坛 {} 未通过审核。{}".format(action_text, fchosen["forumname"], reason_suffix), sender=uid, meta={"qid" : qid, "fid": fchosen.get("fid"), "forum_name" : fchosen["forumname"], "reason" : reason})
        return bool_res()[True]

    @app.route("/forum/get_forum_list")
    def get_forum_list():
        return json.dumps(forum_cursor.query_all_forums(), ensure_ascii=False)

    @app.route("/forum/search")
    def search_forum():
        query_text = flask_request.args.get("query", "").strip()
        if not query_text:
            return json.dumps({"forums": [], "posts": [], "total": 0})
        try:
            fid = flask_request.args.get("fid")
            fid = int(fid) if fid is not None else None
            offset = max(int(flask_request.args.get("offset", 0)), 0)
            limit = min(max(int(flask_request.args.get("limit", 30)), 1), 50)
        except ValueError:
            return json.dumps({"forums": [], "posts": [], "total": 0})
        forums, posts, total = forum_cursor.search(query_text, fid, offset, limit)
        return json.dumps({
            "forums": [{"fid": row[0], "forum_name": row[1], "introduction": row[2], "post_num": row[3]} for row in forums],
            "posts": [{"fid": row[0], "pid": row[1], "title": row[2], "sender_uid": row[3], "content": row[4], "send_time": row[5]} for row in posts],
            "total": total,
        }, ensure_ascii=False)
    
    @api("/forum/send_post", methods=["POST"], feature=("forum",))
    def send_post(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not isinstance(fid, int):
            return bool_res()[False]
        title = req["title"]
        content = req["content"]
        if not isinstance(title, str):
            title = ""
        if not isinstance(content, str):
            content = ""
        if not title.strip() and not content.strip():
            return bool_res()[False]
        raw_attachments = req.get("attachments", req.get("attachment_hashes", []))
        if raw_attachments is None:
            raw_attachments = []
        if not isinstance(raw_attachments, list):
            return bool_res()[False]
        if len(raw_attachments) > read_config().get("max_post_attachments", 20):
            return bool_res()[False]
        attachment_hashes = []
        for item in raw_attachments:
            hashes = item.get("hash") if isinstance(item, dict) else item
            if (not isinstance(hashes, str) or len(hashes) != 64
                    or not all(char in "0123456789abcdefABCDEF" for char in hashes)
                    or hashes in attachment_hashes):
                return bool_res()[False]
            attachment_hashes.append(hashes)
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not query_forum(fid):
            return bool_res()[False]
        max_post = read_config().get("max_post_content_length", 20000)
        if max_post > 0 and len(str(content)) > max_post:
            return bool_res()[False]
        attachment_records = []
        for hashes in attachment_hashes:
            metadata = file_cursor.acquire_reference(uid, hashes)
            if metadata is None:
                return bool_res()[False]
            attachment_records.append({
                "hash": hashes,
                "display_name": metadata["file_name"],
            })
        pid = forum_cursor.send_post(fid, uid, title, content, attachment_records)
        if pid is False:
            return bool_res()[False]
        for hashes in attachment_hashes:
            file_cursor.add_reference(hashes, "forum_post", "{}:{}".format(fid, pid), uid)
        forum_info = forum_cursor.query_forum_fid(fid)
        forum_name = forum_info[0][1] if forum_info else str(fid)
        mentioned_uids = resolve_mentioned_uids(
            "{} {}".format(title, content), user_cursor, exclude_uid=uid
        )
        sender_name = user_row[1]
        for mentioned_uid in mentioned_uids:
            notify_user(
                mentioned_uid, "forum.post.mentioned", "你在帖子中被提及",
                "{} 在论坛 {} 的帖子《{}》中提到了你。".format(
                    sender_name, forum_name, title
                ), sender=uid, meta={"fid": fid, "pid": pid}
            )
        return bool_res()[True]


    @app.route("/forum/get_post_list/<fid>")
    def get_post_list(fid : str):
        if not fid.isdigit():
            return {}
        try:
            fid_int = int(fid)
            before_raw = flask_request.args.get("before_pid")
            limit_raw = flask_request.args.get("limit")
            before_pid = int(before_raw) if before_raw not in (None, "") else None
            limit = int(limit_raw) if limit_raw not in (None, "") else None
        except (TypeError, ValueError):
            return {}
        if limit is not None:
            limit = max(1, min(limit, 100))
        try:
            posts, has_more = forum_cursor.query_posts_page(
                fid_int, before_pid=before_pid, limit=limit)
            try:
                pinned_pid = forum_cursor.get_pinned_pid(fid_int)
            except forum_cursor.dialect.DatabaseError:
                pinned_pid = None
            return json.dumps({
                # 只返回富化后的结构：此前 posts 与 post_rows 各带一份正文，
                # 响应体直接翻倍。
                "post_rows": serialize_post_rows(posts),
                "pinned_pid": pinned_pid,
                "has_more": has_more,
                "next_before_pid": posts[0][1] if posts and has_more else None,
            }, ensure_ascii=False)
        except forum_cursor.dialect.DatabaseError:
            return {}

    @app.route("/forum/get_post/<fid>/<pid>")
    def get_post_detail(fid : str, pid : str):
        if not fid.isdigit() or not pid.isdigit():
            return {}
        rows = forum_cursor.query_post_pid(int(fid), int(pid))
        if not rows:
            return {}
        return json.dumps(serialize_post_rows(rows)[0], ensure_ascii=False)
    
    @api("/forum/remove_forum", methods=["POST"], feature=("forum",))
    def remove_forum(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        forum_info = query_forum(fid)
        if not forum_info:
            return bool_res()[False]
        creater = forum_info[0][2]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not (user_stat in ["admin", "root"] or uid == creater):
            return bool_res()[False]
        try:
            avatar.clean_avatar(port_api, fid, "forum")
        except Exception:
            return bool_res()[False]
        attachment_references = forum_cursor.get_file_reference_rows(fid=fid)
        forum_cursor.delete_forum(fid)
        for hashes, source_type, source_id, _ in attachment_references:
            file_cursor.remove_reference(hashes, source_type, source_id)
        return bool_res()[True]
    
    @api("/forum/edit_forum", methods=["POST"], feature=("forum",))
    def forum_edit_forum(req):
        """修改论坛信息，论坛创建者或管理员"""
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        forum_info = query_forum(fid)
        if not forum_info:
            return bool_res()[False]
        creater = forum_info[0][2]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not (user_stat in ["admin", "root"] or uid == creater):
            return bool_res()[False]
        forum_name = req.get("forum_name", forum_info[0][1])
        introduction = req.get("introduction", forum_info[0][4] or "")
        with locks['queue']:
            def enqueue(queue):
                qid_value = queue['queue_num'] + 1
                for key in queue:
                    if str(key).isdigit():
                        qid_value = max(qid_value, int(key) + 1)
                queue["queue_num"] = qid_value
                queue[qid_value] = {
                    "type" : "edit", "fid" : fid, "creater" : uid,
                    "forumname" : forum_name, "introduction" : introduction,
                }
                return qid_value
            qid = update_json("res/{}/forum/queue.json".format(port_api), enqueue)
        notify_user(uid, "forum.review.submitted", "论坛编辑已提交审核",
                    "论坛 {} 的编辑已提交审核。".format(forum_name), sender=uid,
                    meta={"qid": qid, "fid": fid, "forum_name": forum_name})
        reviewer_rows = user_cursor.query(
            "SELECT uid FROM users WHERE stat IN ('admin', 'root')"
        )
        notify_users([row[0] for row in reviewer_rows if row[0] != uid],
                     "forum.review.pending", "新的论坛编辑审核",
                     "论坛 {} 的编辑等待审核。".format(forum_name), sender=uid,
                     meta={"qid": qid, "fid": fid, "forum_name": forum_name})
        return bool_res()[True]

    @api("/forum/remove_post", methods=['POST'], feature=("forum",))
    def remove_post(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        pid = req["pid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        forum_info = query_forum(fid)
        post_info = query_post(fid, pid)
        if not forum_info or not post_info:
            return bool_res()[False]
        creater = forum_info[0][2]
        creater_post = post_info[0][3] # 好像 3 是 creater
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        forum_role = forum_cursor.get_member_role(fid, uid)
        if not (user_stat in ["admin", "root"] or uid == creater_post or
                (forum_role is not None and forum_role >= 50)):
            return bool_res()[False]
        attachment_references = forum_cursor.get_file_reference_rows(fid=fid, pid=pid)
        forum_cursor.delete_post(fid, pid)
        for hashes, source_type, source_id, _ in attachment_references:
            file_cursor.remove_reference(hashes, source_type, source_id)
        if uid != creater_post:
            notify_user(
                creater_post, "forum.post.deleted", "你的帖子已被删除",
                "你的帖子《{}》已被其他用户删除。".format(post_info[0][2]),
                sender=uid, meta={"fid": fid, "pid": pid}
            )
        return bool_res()[True]

    @api("/forum/pin_post", methods=["POST"], feature=("forum",))
    def pin_post(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        pid = req["pid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        forum_info = query_forum(fid)
        if not forum_info:
            return bool_res()[False]
        creater = forum_info[0][2]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not (user_stat in ["admin", "root"] or uid == creater):
            return bool_res()[False]
        return bool_res()[forum_cursor.pin_post(fid, pid)]

    @api("/forum/unpin_post", methods=["POST"], feature=("forum",))
    def unpin_post(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        forum_info = query_forum(fid)
        if not forum_info:
            return bool_res()[False]
        creater = forum_info[0][2]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not (user_stat in ["admin", "root"] or uid == creater):
            return bool_res()[False]
        return bool_res()[forum_cursor.unpin_post(fid)]

    @api("/forum/members", methods=["POST"])
    def forum_member_list(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        forum_info = query_forum(fid)
        if not forum_info:
            return json.dumps({"success": False, "error": "not_found"})
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return json.dumps({"success": False, "error": "forbidden"})
        rows = forum_cursor.list_members(fid)
        return json.dumps([list(row) for row in rows])

    @api("/forum/add_member", methods=["POST"], feature=("forum",))
    def forum_add_member(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        target_uid = req["target_uid"]
        role = req.get("role", 0)
        try:
            role = int(role)
        except (TypeError, ValueError):
            return bool_res()[False]
        if not verify_user(uid, password):
            return bool_res()[False]
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return bool_res()[False]
        if not user_cursor.uid_query(target_uid):
            return bool_res()[False]
        # wyf 不准拉自己
        if target_uid == uid:
            return bool_res()[False]
        # 角色只能授予 0（成员）/50（管理员），且必须低于操作者；100（论坛主）不可授予
        if role not in (0, 50) or role >= operator_role:
            return bool_res()[False]
        existing_role = forum_cursor.get_member_role(fid, target_uid)
        if existing_role is not None:
            # 已是成员：不得通过拉人覆盖更高/同级角色
            if existing_role >= operator_role:
                return bool_res()[False]
            return bool_res()[forum_cursor.change_member_role(fid, target_uid, role)]
        return bool_res()[forum_cursor.add_member(fid, target_uid, role)]

    @api("/forum/remove_member", methods=["POST"], feature=("forum",))
    def forum_remove_member(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        target_uid = req["target_uid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return bool_res()[False]
        if target_uid == uid:
            return bool_res()[False]
        target_role = forum_cursor.get_member_role(fid, target_uid)
        if target_role is not None and target_role >= operator_role:
            return bool_res()[False]
        return bool_res()[forum_cursor.remove_member(fid, target_uid)]

    @api("/forum/change_member_role", methods=["POST"], feature=("forum",))
    def forum_change_member_role(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        target_uid = req["target_uid"]
        new_role = req["new_role"]
        try:
            new_role = int(new_role)
        except (TypeError, ValueError):
            return bool_res()[False]
        if not verify_user(uid, password):
            return bool_res()[False]
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return bool_res()[False]
        # 不允许改自己的角色（防止论坛主自我降级后退出，导致论坛无人管理）
        if target_uid == uid:
            return bool_res()[False]
        # 论坛主（100）不可授予/降级：任何操作者都不能设置 100
        if new_role not in (0, 50):
            return bool_res()[False]
        target_role = forum_cursor.get_member_role(fid, target_uid)
        if target_role is not None and target_role >= operator_role:
            return bool_res()[False]
        if new_role >= operator_role:
            return bool_res()[False]
        return bool_res()[forum_cursor.change_member_role(fid, target_uid, new_role)]

    @api("/forum/join", methods=["POST"], feature=("forum",))
    def forum_join(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not query_forum(fid):
            return bool_res()[False]
        if forum_cursor.is_member(fid, uid):
            return bool_res()[False]
        return bool_res()[forum_cursor.add_member(fid, uid, 0)]

    @api("/forum/leave", methods=["POST"], feature=("forum",))
    def forum_leave(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        current_role = forum_cursor.get_member_role(fid, uid)
        if current_role is None:
            return bool_res()[False]
        if current_role >= 100:
            return bool_res()[False]  # owner cannot leave, must delete forum
        return bool_res()[forum_cursor.remove_member(fid, uid)]

    @api("/forum/my_memberships", methods=["POST"])
    def forum_my_memberships(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        rows = forum_cursor.query(
            "SELECT fid, role FROM forum_members WHERE uid = ?", (uid,)
        )
        return json.dumps([list(row) for row in rows])

    @api("/forum/comment", methods=["POST"], feature=("forum",))
    def comment(req):
        uid = req["uid"]
        if not isinstance(uid, int):
            return bool_res()[False]
        password = req["password"]
        fid = req["fid"]
        pid = req["pid"]
        comment : str = req["comment"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        try:
            fid = int(fid)
            pid = int(pid)
        except (TypeError, ValueError):
            return bool_res()[False]
        if not forum_cursor.query_post_pid(fid, pid):
            return bool_res()[False]
        comment_time = forum_cursor.add_comment(fid, pid, uid, comment)
        if comment_time is None:
            return bool_res()[False]

        def send_comment_notifications():
            _sender_row = get_user_row(uid)
            sender_name = _sender_row[1] if _sender_row else ''
            forum_info = forum_cursor.query_forum_fid(fid)
            post_info = forum_cursor.query_post_pid(fid, pid)
            notified_uids = set()
            forum_name = forum_info[0][1] if forum_info else str(fid)
            post_title = post_info[0][2] if post_info else str(pid)
            if post_info:
                post_creater = post_info[0][3]
                if post_creater != uid:
                    notify_user(post_creater, "forum.comment.created", "你的帖子收到新评论", "{} 评论了你的帖子《{}》。".format(sender_name, post_title), sender=uid, meta={"fid" : fid, "pid" : pid, "comment_time" : comment_time})
                    notified_uids.add(post_creater)
            for mentioned_uid in resolve_mentioned_uids(
                comment, user_cursor, exclude_uid=uid
            ):
                if mentioned_uid == uid or mentioned_uid in notified_uids:
                    continue
                notify_user(mentioned_uid, "forum.comment.mentioned", "你在评论中被提及", "{} 在论坛 {} 的评论中提到了你。".format(sender_name, forum_name), sender=uid, meta={"fid" : fid, "pid" : pid, "comment_time" : comment_time})
                notified_uids.add(mentioned_uid)

        run_notification_side_effect("forum.comment", send_comment_notifications)

        return bool_res()[True]

    @app.route("/forum/get_all_comments/<fid>/<pid>")
    def get_all_comments(fid, pid):
        if not fid.isdigit() or not pid.isdigit():
            return {}
        rows = forum_cursor.get_comments(int(fid), int(pid))
        if not rows:
            return {}
        # 保持旧响应结构：{time_stamp: [uid, content]}
        return {str(row[0]): [row[1], row[2]] for row in rows}
    
    @api("/forum/remove_comment", methods=['POST'], feature=("forum",))
    def remove_comment(req):
        uid = req["uid"]
        if not isinstance(uid, int):
            return bool_res()[False]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        fid = req["fid"]
        pid = req["pid"]
        time_stamp = req["send_time"]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        try:
            fid = int(fid)
            pid = int(pid)
        except (TypeError, ValueError):
            return bool_res()[False]
        forum_role = forum_cursor.get_member_role(fid, uid)
        # 权限判断放进 delete_comment 的同一事务：先读作者、再单独删除的话，
        # 中间那个窗口里评论可能已经被换作者/删掉（TOCTOU）。
        removed_creator = forum_cursor.delete_comment(
            fid, pid, time_stamp,
            can_delete=lambda creator: (
                creator == uid
                or user_stat in ['admin', 'root']
                or (forum_role is not None and forum_role >= 50)
            ),
        )
        if removed_creator is None:
            return bool_res()[False]
        if removed_creator != uid:
            notify_user(
                removed_creator, "forum.comment.deleted", "你的评论已被删除",
                "你在帖子中的评论已被其他用户删除。", sender=uid,
                meta={"fid": fid, "pid": pid, "comment_time": time_stamp}
            )
        return bool_res()[True]

    @app.route("/avatar/get_avatar/<typ>/<tid>")
    def get_avatar(typ, tid):
        if not tid.isdigit():
            return 
        if not typ in ["forum", "user", "group"]:
            return 
        return send_file(avatar.get_avatar(port_api, tid, typ))

    @app.route("/avatar/get_default/<typ>")
    def get_default_avatar(typ):
        if not typ in ["forum", "user", "group", "logo"]:
            return
        return send_file(avatar.get_default_avatar(port_api, typ))
    
    @app.route("/avatar/get_logo")
    def get_logo():
        return send_file(avatar.get_default_avatar(port_api, "logo"))
    
    @api("/avatar/upload_forum_avatar", methods=['POST'])
    def upload_forum_avatar(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        fid = req["fid"]
        pic_b64 = req["pic"]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        forum_info = query_forum(fid)
        if not forum_info:
            return bool_res()[False]
        if not validate_avatar_upload(pic_b64, read_config()):
            return bool_res()[False]
        creater = forum_info[0][2]
        if uid == creater or user_stat in ['admin', 'root']:
            try:
                avatar.upload_avatar(port_api, fid, pic_b64, 'forum')
            except Exception:
                return bool_res()[False]
            return bool_res()[True]
        return bool_res()[False]
        
    @api("/avatar/upload_user_avatar", methods=['POST'])
    def upload_user_avatar(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        pic_b64 = req["pic"]
        if not validate_avatar_upload(pic_b64, read_config()):
            return bool_res()[False]
        try:
            avatar.upload_avatar(port_api, uid, pic_b64, 'user')
        except Exception:
            return bool_res()[False]
        return bool_res()[True]
    
    @api('/avatar/upload_group_avatar', methods=['POST'])
    def upload_group_avatar(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        pic_b64 = req["pic"]
        if not verify_user(uid, password):
            return bool_res()[False]
        if not group_cursor.is_admin(gid, uid):
            return bool_res()[False]
        if not validate_avatar_upload(pic_b64, read_config()):
            return bool_res()[False]
        try:
            avatar.upload_avatar(port_api, gid, pic_b64, 'group')
        except Exception:
            return bool_res()[False]
        return bool_res()[True]

    @api('/avatar/upload_default_avatar', methods=['POST'])
    def upload_default_avatar(req):
        uid = req["uid"]
        password = req["password"]
        pic_b64 = req["pic"]
        asset_type = req.get("type", req.get("asset_type"))
        if verify_manager(uid, password) is None:
            return bool_res()[False]
        if asset_type not in ["forum", "user", "group", "logo"]:
            return bool_res()[False]
        if not validate_avatar_upload(pic_b64, read_config()):
            return bool_res()[False]
        return bool_res()[avatar.upload_default_avatar(port_api, pic_b64, asset_type)]
    
    @api('/avatar/upload_logo', methods=['POST'])
    def upload_logo(req):
        uid = req["uid"]
        password = req["password"]
        pic_b64 = req["pic"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not user_stat in ['admin', 'root']:
            return bool_res()[False]
        if not validate_avatar_upload(pic_b64, read_config()):
            return bool_res()[False]
        return bool_res()[avatar.upload_default_avatar(port_api, pic_b64, "logo")]
        
    @api('/file/upload_file', methods=['POST'])
    def upload_file(req):
        uid = req["uid"]
        password = req["password"]
        filename = req["filename"]
        file_b64 = req["file_b64"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        cfg = read_config()
        normalized_name, payload = validate_file_upload(filename, file_b64, cfg)
        if normalized_name is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        quota = cfg.get("user_storage_quota", -1)
        max_quota = cfg.get("max_user_storage_quota", 73400320)  # 默认 70MB
        new_hashes = file.sha256(payload)
        with quota_lock(uid):
            if quota != -1 or max_quota != -1:
                if not file_cursor.has_active_user_file(uid, new_hashes):
                    new_size = len(payload)
                    current_usage = file_cursor.get_user_storage_used(uid)
                    if quota != -1 and current_usage + new_size > quota:
                        return json.dumps({"success": False, "error": "Storage quota exceeded"})
                    # 瞬时单用户文件总大小不得超过 max_user_storage_quota（默认 70MB）
                    if max_quota != -1 and current_usage + new_size > max_quota:
                        return json.dumps({"success": False, "error": "Storage quota exceeded"})
            try:
                hashes = file.upload_file(port_api, uid, payload, normalized_name, file_cursor,
                                          cfg.get("file_last_time", 72), known_hash=new_hashes)
            except Exception:
                return json.dumps({"success": False, "error": "upload_failed"})
        return json.dumps({
            "success" : True,
            "result" : bool_res()[True],
            "hash" : hashes,
            "download_url" : "/file/get_file/{}".format(hashes),
            "info_url" : "/file/get_file_info/{}".format(hashes),
            "file" : file_metadata(hashes, uid),
        }, ensure_ascii=False)

    def enforce_chunked_upload_quota(uid : int, result : dict, cfg : dict):
        """
        分块上传 finalize 之后的配额核对
        """
        if not (result.get("success") and result.get("file_hash")):
            return None
        quota = cfg.get("user_storage_quota", -1)
        max_quota = cfg.get("max_user_storage_quota", 73400320)
        if quota == -1 and max_quota == -1:
            return None
        hashes = result["file_hash"]
        meta = file_metadata(hashes, uid) or {}
        new_size = int(meta.get("size") or meta.get("file_size") or 0)
        if not new_size:
            return None
        current_usage = file_cursor.get_user_storage_used(uid)
        if ((quota != -1 and current_usage > quota)
                or (max_quota != -1 and current_usage > max_quota)):
            file.dereference_file(
                port_api, uid, hashes, file_cursor,
                cfg.get("file_last_time", 72),
            )
            return "Storage quota exceeded"
        return None

    # Experimental: streaming chunked upload (rebased + hardened from Leitarkkk #5).
    @api('/file/chunked_upload', methods=['POST'])
    def chunked_upload(req):
        """
        Stream large files in chunks.

        Request params:
        - uid, password, filename
        - chunk_index (0-based), chunk_total
        - chunk_data (base64)
        - file_id (required after first chunk)
        - expected_hash (optional SHA256 of full file)
        """
        try:
            uid = req["uid"]
            password = req["password"]
            filename = req["filename"]
            chunk_index = req["chunk_index"]
            chunk_total = req["chunk_total"]
            chunk_data = req["chunk_data"]
            file_id = req.get("file_id", None)
            expected_hash = req.get("expected_hash", None)

            if not isinstance(uid, int):
                return json.dumps({"success": False, "error": "Invalid uid"}, ensure_ascii=False)
            if not verify_user(uid, password):
                return json.dumps({"success": False, "error": "Password incorrect"}, ensure_ascii=False)
            user_row = get_user_row(uid)
            if user_row is None:
                return json.dumps({"success": False, "error": "User not found"}, ensure_ascii=False)
            if user_row[4] == 'banned':
                return json.dumps({"success": False, "error": "User banned"}, ensure_ascii=False)

            cfg = read_config()
            normalized_name = normalize_upload_filename(filename)
            if normalized_name is None:
                return json.dumps({"success": False, "error": "Invalid filename"}, ensure_ascii=False)
            allowed_extensions = read_allowed_file_extensions(cfg)
            if allowed_extensions:
                _, ext = os.path.splitext(normalized_name.lower())
                if not ext or ext not in allowed_extensions:
                    return json.dumps({"success": False, "error": "Extension not allowed"}, ensure_ascii=False)

            is_final_chunk = (
                isinstance(chunk_index, int) and isinstance(chunk_total, int)
                and chunk_total > 0 and chunk_index == chunk_total - 1
            )

            def upload_chunks():
                return file.chunked_upload_file(
                    port_api,
                    uid,
                    normalized_name,
                    chunk_index,
                    chunk_total,
                    chunk_data,
                    file_id,
                    file_cursor,
                    expected_hash,
                )

            if is_final_chunk:
                with quota_lock(uid):
                    result = upload_chunks()
                    quota_error = enforce_chunked_upload_quota(uid, result, cfg)
            else:
                result = upload_chunks()
                quota_error = None

            if quota_error:
                return json.dumps(
                    {"success": False, "error": quota_error},
                    ensure_ascii=False,
                )

            if result.get("success") and result.get("file_hash"):
                return json.dumps({
                    "success": True,
                    "file_hash": result["file_hash"],
                    "verified": result.get("verified", False),
                    "hash": result["file_hash"],
                    "download_url": "/file/get_file/{}".format(result["file_hash"]),
                    "info_url": "/file/get_file_info/{}".format(result["file_hash"]),
                    "file": file_metadata(result["file_hash"], uid),
                }, ensure_ascii=False)

            return json.dumps(result, ensure_ascii=False)
        except KeyError as e:
            return json.dumps({"success": False, "error": "Missing parameter"}, ensure_ascii=False)
        except Exception:
            return json.dumps({"success": False, "error": "Server error"}, ensure_ascii=False)

    # 高级的秒传
    @api('/file/instant_upload', methods=['POST'])
    def instant_upload(req):
        try:
            uid = req["uid"]
            password = req["password"]
            filename = req["filename"]
            file_hash = req.get("file_hash") or req.get("hash") or ""

            if not isinstance(uid, int):
                return {"success": False, "error": "Invalid uid"}
            if not verify_user(uid, password):
                return {"success": False, "error": "Password incorrect"}
            user_row = get_user_row(uid)
            if user_row is None:
                return {"success": False, "error": "User not found"}
            if user_row[4] == 'banned':
                return {"success": False, "error": "User banned"}

            normalized_name = normalize_upload_filename(filename)
            if normalized_name is None:
                return {"success": False, "error": "Invalid filename"}
            cfg = read_config()
            allowed_extensions = read_allowed_file_extensions(cfg)
            if allowed_extensions:
                _, ext = os.path.splitext(normalized_name.lower())
                if not ext or ext not in allowed_extensions:
                    return {"success": False, "error": "Extension not allowed"}

            if not isinstance(file_hash, str) or len(file_hash) != 64:
                return {"success": False, "error": "Invalid file hash"}
            if not all(c in "0123456789abcdefABCDEF" for c in file_hash):
                return {"success": False, "error": "Invalid file hash"}
            file_hash = file_hash.lower()
            present = os.path.isfile(file.file_path(port_api, file_hash))
            if not present and oss_store.is_oss_enabled(port_api):
                present = oss_store.get_size_from_oss(port_api, "file", file_hash) > 0
            if not file_cursor.file_exists(file_hash) or not present:
                return {"success": True, "instant": False}

            blob_info = file_cursor.get_blob_info(file_hash) or {}
            extension = blob_info.get("extension") or os.path.splitext(normalized_name)[1].lower()
            mime_type = blob_info.get("mime_type")
            new_size = blob_info.get("size") or resolve_file_size(file_hash)

            quota = cfg.get("user_storage_quota", -1)
            max_quota = cfg.get("max_user_storage_quota", 73400320)
            with quota_lock(uid):
                current_usage = file_cursor.get_user_storage_used(uid)
                if new_size and not file_cursor.has_active_user_file(uid, file_hash):
                    if quota != -1 and current_usage + new_size > quota:
                        return {"success": False, "error": "Storage quota exceeded"}
                    if max_quota != -1 and current_usage + new_size > max_quota:
                        return {"success": False, "error": "Storage quota exceeded"}

                ok = file.instant_upload_file(
                    port_api, uid, file_hash, normalized_name, new_size,
                    mime_type, extension, file_cursor,
                )
            if not ok:
                return {"success": True, "instant": False}
            return {
                "success": True,
                "instant": True,
                "file_hash": file_hash,
                "hash": file_hash,
                "download_url": "/file/get_file/{}".format(file_hash),
                "info_url": "/file/get_file_info/{}".format(file_hash),
                "file": file_metadata(file_hash, uid),
            }
        except KeyError:
            return {"success": False, "error": "Missing parameter"}
        except Exception:
            return {"success": False, "error": "Server error"}

    @api('/file/dereference_file', methods=['POST'])
    def dereference_file(req):
        uid = req["uid"]
        password = req["password"]
        hashes = req["hash"]
        if not verify_user(uid, password):
            return bool_res()[False]
        file_last_time = read_config().get("file_last_time", 72)
        return bool_res()[file.dereference_file(port_api, uid, hashes, file_cursor, file_last_time)]

    @api('/file/get_user_files', methods=['POST'])
    def get_user_files(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        rows = file_cursor.get_user_files(uid)
        result = []
        for row in rows:
            hashes = row[0]
            meta = file_cursor.get_metadata(hashes) or {}
            file_name = row[1] or ""
            result.append({
                "hash" : hashes,
                "file_name" : file_name,
                "upload_time" : row[2],
                "size" : meta.get("size", 0) or resolve_file_size(hashes),
                "ref_count" : 0,
                "upload_user_count" : 0,
                "mime_type" : mime_from_name(file_name or row[7] or ""),
                "file_type" : meta.get("file_type", "unknown"),
                "extension" : row[7] or "",
                "download_url" : "/file/get_file/{}".format(hashes),
                "width" : meta.get("width"),
                "height" : meta.get("height"),
                "blurhash" : meta.get("blurhash"),
                "thumb_url" : meta.get("thumb_url"),
            })
        return json.dumps(result, ensure_ascii=False)

    @api('/file/get_storage_info', methods=['POST'])
    def get_storage_info(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        cfg = read_config()
        quota = cfg.get("user_storage_quota", -1)
        used = file_cursor.get_user_storage_used(uid)
        return json.dumps({
            "quota" : quota,
            "used" : used,
            "remaining" : -1 if quota == -1 else max(quota - used, 0)
        }, ensure_ascii=False)

    @api('/file/delete_file', methods=['POST'])
    def delete_file(req):
        uid = req["uid"]
        password = req["password"]
        hashes = req["hash"]
        if not verify_user(uid, password):
            return bool_res()[False]
        return bool_res()[file.delete_user_file(port_api, uid, hashes, file_cursor)]

    @api('/file/admin_get_all_files', methods=['POST'])
    def admin_get_all_files(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat not in ['admin', 'root']:
            return json.dumps({"success": False, "error": "forbidden"})
        target_uid = req.get("target_uid")
        rows = file_cursor.get_all_user_files(target_uid)
        result = []
        for row in rows:
            username = ""
            try:
                uq = user_cursor.uid_query(row[0])
                if uq:
                    username = uq[0][1]
            except Exception:
                pass
            hashes = row[1]
            meta = file_cursor.get_metadata(hashes) or {}
            result.append({
                "uid" : row[0],
                "username" : username,
                "hash" : hashes,
                "file_name" : row[2],
                "upload_time" : row[3],
                "size" : meta.get("size", 0) or resolve_file_size(hashes),
                "ref_count" : 0,
                "upload_user_count" : 0,
                "sender" : "",
                "mime_type" : mime_from_name(row[2] or row[9] or ""),
                "file_type" : meta.get("file_type", "unknown"),
                "extension" : row[9] or "",
                "download_url" : "/file/get_file/{}".format(hashes),
            })
        return json.dumps(result, ensure_ascii=False)

    @api('/file/admin_force_delete_file', methods=['POST'])
    def admin_force_delete_file(req):
        uid = req["uid"]
        password = req["password"]
        hashes = req["hash"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat not in ['admin', 'root']:
            return bool_res()[False]
        file.force_delete_file(port_api, hashes, file_cursor)
        return bool_res()[True]

    @app.route('/file/get_file_info/<hashes>')
    def get_file_info(hashes):
        metadata = file_metadata(hashes)
        if metadata is None:
            return {}
        uploaders = file_cursor.query(
            "SELECT COUNT(*) FROM file_uploaders WHERE hash = ?", (hashes,)
        )
        metadata["upload_user_count"] = uploaders[0][0] if uploaders else 0
        return metadata

    @app.route("/file/get_file/<hashes>")
    def get_file(hashes : str):
        if not file_cursor.file_exists(hashes):
            return ("", 404)
        metadata = file_metadata(hashes) or {}
        download_name = metadata.get("file_name") or hashes
        if oss_store.is_oss_enabled(port_api):
            if oss_store.get_download_mode(port_api) == "redirect":
                url = oss_store.presigned_download_url(
                    port_api, "file", hashes, download_name,
                    content_type=mime_from_name(download_name),
                )
                if url:
                    return redirect(url, code=307)
        # OSS2 模式（代理模式）：先从 OSS 拉取到唯一临时文件，发送完成后立即删除
        if oss_store.is_oss_enabled(port_api):
            temp_path = oss_store.temp_download_path(port_api, "file", hashes)
            try:
                if not oss_store.download_from_oss(port_api, "file", hashes, temp_path):
                    return ("", 404)
                resp = send_file(
                    temp_path,
                    download_name=download_name,
                    as_attachment=True,
                    mimetype=mime_from_name(download_name),
                    max_age=FILE_CACHE_MAX_AGE,
                )
                # 发送完成后立即删除本地临时文件（带重试，避免 WinError 32）
                @resp.call_on_close
                def _cleanup_temp():
                    oss_store.safe_remove(temp_path)
                return resp
            except Exception:
                oss_store.safe_remove(temp_path)
                raise
        return send_file(
            "res/{}/file/{}.file".format(port_api, hashes),
            download_name=download_name,
            as_attachment=True,
            mimetype=mime_from_name(download_name),
            max_age=FILE_CACHE_MAX_AGE,
        )

    @app.route("/file/get_thumbnail/<hashes>")
    def get_thumbnail(hashes : str):
        """缩略图"""
        media = file_cursor.get_media(hashes)
        if media is None or media["status"] != "done":
            return ("", 404)
        if media["thumb_is_original"]:
            # 原图不超过缩略图规格：直接复用原图
            return get_file(hashes)
        if not media["thumb_size"]:
            return ("", 404)
        download_name = "{}.thumb.webp".format(hashes)
        if oss_store.is_oss_enabled(port_api):
            if oss_store.get_download_mode(port_api) == "redirect":
                url = oss_store.presigned_download_url(
                    port_api, "thumb", hashes, download_name, content_type="image/webp")
                if url:
                    return redirect(url, code=307)
            temp_path = oss_store.temp_download_path(port_api, "thumb", hashes)
            try:
                if not oss_store.download_from_oss(port_api, "thumb", hashes, temp_path):
                    return ("", 404)
                resp = send_file(
                    temp_path,
                    download_name=download_name,
                    as_attachment=False,
                    mimetype="image/webp",
                    max_age=FILE_CACHE_MAX_AGE,
                )
                @resp.call_on_close
                def _cleanup_thumb_temp():
                    oss_store.safe_remove(temp_path)
                return resp
            except Exception:
                oss_store.safe_remove(temp_path)
                raise
        target_path = file.thumb_path(port_api, hashes)
        if not os.path.isfile(target_path):
            return ("", 404)
        return send_file(
            target_path,
            download_name=download_name,
            as_attachment=False,
            mimetype="image/webp",
            max_age=FILE_CACHE_MAX_AGE,
        )

    @api('/sticker/upload', methods=['POST'])
    def upload_sticker(req):
        """
        上传贴图文件（独立于 file 体系）。
        贴图文件存放于 res/<port_api>/sticker/，由 sticker.db 全职管理。
        不遵守 file_last_time 自动删除规则。
        """
        uid = req["uid"]
        password = req["password"]
        filename = req["filename"]
        file_b64 = req["file_b64"]
        if sticker_cursor is None:
            return json.dumps({"success": False, "error": "unavailable"}, ensure_ascii=False)
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"}, ensure_ascii=False)
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "user_not_found"}, ensure_ascii=False)
        if user_row[4] == 'banned':
            return json.dumps({"success": False, "error": "user_banned"}, ensure_ascii=False)
        normalized_name = normalize_upload_filename(filename)
        if normalized_name is None:
            return json.dumps({"success": False, "error": "invalid_filename"}, ensure_ascii=False)
        cfg = read_config()
        max_sticker_size = cfg.get("max_sticker_size", 1048576)
        payload = decode_base64_payload(file_b64, max_sticker_size)
        if payload is None:
            return json.dumps({"success": False, "error": "invalid_base64"}, ensure_ascii=False)
        if not is_sticker_type(payload, normalized_name):
            return json.dumps({"success": False, "error": "unsupported_sticker_type"}, ensure_ascii=False)
        if max_sticker_size != -1 and len(payload) > max_sticker_size:
            return json.dumps({"success": False, "error": "sticker_too_large"}, ensure_ascii=False)
        # 瞬时贴图总大小不得超过 max_sticker_storage_quota（默认 30MB）
        max_sticker_quota = cfg.get("max_sticker_storage_quota", 31457280)
        new_hashes = file.sha256(payload)
        if max_sticker_quota != -1:
            if not sticker_cursor.has_active_user_sticker(uid, new_hashes):
                current_sticker_usage = sticker_cursor.get_user_sticker_used(uid)
                if current_sticker_usage + len(payload) > max_sticker_quota:
                    return json.dumps({"success": False, "error": "sticker_storage_quota_exceeded"}, ensure_ascii=False)
        try:
            hashes = file.upload_sticker(port_api, uid, payload, normalized_name, sticker_cursor, known_hash=new_hashes)
        except Exception:
            return json.dumps({"success": False, "error": "upload_failed"}, ensure_ascii=False)
        info = sticker_cursor.get_sticker_file_info(hashes)
        return json.dumps({
            "success" : True,
            "hash" : hashes,
            "download_url" : "/sticker/get/{}".format(hashes),
            "sticker" : {
                "hash" : hashes,
                "file_name" : normalized_name,
                "file_type" : (info or {}).get("mime_type") or "unknown",
                "size" : (info or {}).get("size") or len(payload),
                "download_url" : "/sticker/get/{}".format(hashes),
            },
        }, ensure_ascii=False)

    @app.route("/sticker/get/<hashes>")
    def get_sticker(hashes : str):
        if sticker_cursor is None:
            return ("", 404)
        info = sticker_cursor.get_sticker_file_info(hashes)
        if info is None:
            return ("", 404)
        target_path = file.sticker_path(port_api, hashes)
        download_name = info.get("file_name") or (hashes + ".png")
        ext = os.path.splitext(download_name)[1].lower()
        mimetype_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".svg": "image/svg+xml",
            ".tgs": "application/octet-stream",
        }
        if oss_store.is_oss_enabled(port_api):
            if oss_store.get_download_mode(port_api) == "redirect":
                url = oss_store.presigned_download_url(
                    port_api, "sticker", hashes, download_name,
                    content_type=mimetype_map.get(ext),
                )
                if url:
                    return redirect(url, code=307)
        # OSS2 mode (proxy): download to unique temp file, delete immediately after sending
        if oss_store.is_oss_enabled(port_api):
            temp_path = oss_store.temp_download_path(port_api, "sticker", hashes)
            try:
                if not oss_store.download_from_oss(port_api, "sticker", hashes, temp_path):
                    return ("", 404)
                resp = send_file(
                    temp_path,
                    download_name=download_name,
                    as_attachment=True,
                    mimetype=mimetype_map.get(ext),
                    # hash 寻址内容不可变：URL 相同内容不变，一年内缓存无需校验
                    max_age=FILE_CACHE_MAX_AGE,
                )
                @resp.call_on_close
                def _cleanup_sticker_temp():
                    oss_store.safe_remove(temp_path)
                return resp
            except Exception:
                oss_store.safe_remove(temp_path)
                raise
        if not os.path.isfile(target_path):
            return ("", 404)
        return send_file(
            target_path,
            download_name=download_name,
            as_attachment=True,
            mimetype=mimetype_map.get(ext),
            # hash 寻址内容不可变：URL 相同内容不变，一年内缓存无需校验
            max_age=FILE_CACHE_MAX_AGE,
        )
    
    @api("/announcement/upload_announcement", methods=['POST'], feature=("announcement",))
    def upload_announcement(req):
        uid = req["uid"]
        password = req["password"]
        content = req["content"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not user_stat in ['admin', 'root']:
            return bool_res()[False]
        time_stamp = announcements.upload_announcement(port_api, uid, content, locks['announcement'])
        notify_users(all_user_ids(), "announcement.created", "收到新公告", content, sender=uid, meta={"time_stamp" : time_stamp})
        return bool_res()[True]
    
    @api("/announcement/edit_announcement", methods=['POST'], feature=("announcement",))
    def edit_announcement(req):
        uid = req["uid"]
        password = req["password"]
        time_stamp = req["time_stamp"]
        content = req["content"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not user_stat in ['admin', 'root']:
            return bool_res()[False]
        succeeded = announcements.edit_announcement(port_api, time_stamp, content, locks['announcement'])
        if succeeded:
            notify_users(all_user_ids(), "announcement.edited", "公告已更新", content, sender=uid, meta={"time_stamp" : time_stamp})
        return bool_res()[succeeded] 
    
    @api("/announcement/delete_announcement", methods=['POST'], feature=("announcement",))
    def delete_announcement(req):
        uid = req["uid"]
        password = req["password"]
        time_stamp = req["time_stamp"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not user_stat in ['admin', 'root']:
            return bool_res()[False]
        succeeded = announcements.delete_announcement(port_api, time_stamp, locks['announcement'])
        if succeeded:
            notify_users(all_user_ids(), "announcement.deleted", "公告已删除", "编号为 {} 的公告已被删除。".format(time_stamp), sender=uid, meta={"time_stamp" : time_stamp})
        return bool_res()[succeeded]
    
    @app.route("/announcement/query_all")
    def query_all():
        return announcements.query_all(port_api, locks['announcement'])
    
    @app.route("/announcement/query_single/<time_stamp>")
    def query_single(time_stamp : str):
        return announcements.query_single(port_api, time_stamp, locks['announcement'])
 

    @api("/group/create_group", methods=['POST'], feature=("chat", "group_create"))
    def create_group(req):
        uid = req["uid"]
        password = req["password"]
        groupname = req["groupname"]
        introduction = req.get("introduction", "")
        enter_hint = req.get("enter_hint", "")
        allow_direct_join = bool(req.get("allow_direct_join", False))
        require_review = bool(req.get("require_review", True))
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        cfg = read_config()
        min_len = cfg.get("min_group_name_length", 1)
        max_len = cfg.get("max_group_name_length", 50)
        if not isinstance(groupname, str) or not (min_len <= len(groupname.strip()) <= max_len):
            return json.dumps({"success": False, "error": "invalid_request"})
        gid = group_cursor.create_group(uid, groupname, enter_hint, introduction,
                                        allow_direct_join, require_review)
        if gid:
            return json.dumps({"gid": gid})
        return json.dumps({"success": False, "error": "unavailable"})
    
    @app.route("/group/group_info/<gid>")
    def group_info(gid : str):
        if not gid.isdigit():
            return {}
        qry = group_cursor.query_gid(gid)
        if len(qry) < 1:
            return {}
        row = list(qry[0])
        row[3] = "[]"
        row[4] = "[]"
        return json.dumps(row, ensure_ascii=False)

    @app.route("/group/groupname_search/<groupname>")
    def groupname_search(groupname : str):
        """[deprecated] 明文群名搜索：保留旧结构以兼容老客户端，成员/管理员已清空。"""
        return json.dumps(group_cursor.groupname_search(groupname), ensure_ascii=False)

    @api("/user/search", methods=['POST'])
    def user_search(req):
        """按用户名搜索用户。返回 uid/username/sign"""
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if not check_search_rate(uid):
            return json.dumps({"success": False, "error": "rate_limited"})
        keyword, limit, _offset, err = normalize_search_request(
            req.get("keyword"), read_config().get("min_search_length", 2),
            req.get("limit", 20),
        )
        if err:
            return json.dumps({"success": False, "error": err})
        results = user_cursor.search_users(keyword, limit=limit, exclude_uid=uid)
        return json.dumps(results, ensure_ascii=False)

    @api("/group/search", methods=['POST'])
    def group_search(req):
        """按群名搜索群组（分页）"""
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if not check_search_rate(uid):
            return json.dumps({"success": False, "error": "rate_limited"})
        keyword, limit, offset, err = normalize_search_request(
            req.get("keyword"), read_config().get("min_search_length", 2),
            req.get("limit", 20), req.get("offset", 0),
        )
        if err:
            return json.dumps({"success": False, "error": err})
        groups, has_more = group_cursor.search_public(keyword, limit=limit, offset=offset)
        return json.dumps(
            {"success": True, "groups": groups, "has_more": has_more},
            ensure_ascii=False,
        )

    @api("/group/preview", methods=['POST'])
    def group_preview(req):
        """群组预览：任意登录用户可查看（加入前预览）。enter_hint 仅成员可见。"""
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        if user_row[4] == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        settings = group_cursor.get_group_settings(gid)
        if not settings:
            return json.dumps({"success": False, "error": "not_found"})
        member_count = group_cursor.count_members(gid)
        preview_members = group_cursor.get_preview_members(gid, limit=10)
        name_map = {}
        if preview_members:
            placeholders = ",".join("?" * len(preview_members))
            rows = user_cursor.query(
                "SELECT uid, username FROM users WHERE uid IN ({})".format(placeholders),
                tuple(p[0] for p in preview_members),
            )
            name_map = {r[0]: r[1] for r in rows}
        members = []
        for muid, role in preview_members:
            role_name = "owner" if role == 2 else ("admin" if role == 1 else "member")
            members.append({
                "uid": muid,
                "username": name_map.get(muid, "User {}".format(muid)),
                "role": role_name,
            })
        is_member = group_cursor.is_member(gid, uid)
        result = {
            "gid": settings["gid"],
            "groupname": settings["groupname"],
            "introduction": settings["introduction"],
            "member_count": member_count,
            "is_member": is_member,
            "allow_direct_join": settings["allow_direct_join"],
            "require_review": settings["require_review"],
            "public_messages": settings["public_messages"],
            "essence_enabled": settings["essence_enabled"],
            "members": members,
        }
        if is_member:
            result["enter_hint"] = settings.get("enter_hint", "")
        return json.dumps(result, ensure_ascii=False)

    @api("/group/add_admin", methods=['POST'])
    def add_admin(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        gid = req["gid"]
        added = req["added"]
        stat = group_cursor.is_admin(gid, uid)
        if stat != 2:
            return bool_res()[False]
        succeeded = group_cursor.add_admin(gid, added)
        if succeeded:
            run_notification_side_effect(
                "group.admin.added",
                lambda: notify_user(added, "group.admin.added", "你已成为群管理员",
                    "你已被 {} 设置为群 {} 的管理员。".format(format_user_display(uid), group_cursor.query_gid(gid)[0][2] if group_cursor.query_gid(gid) else str(gid)),
                    sender=uid, meta={"gid" : gid})
            )
        return bool_res()[succeeded]
    
    # @api("/group/invite_member", methods=['POST'])
    # def invite_member(req):
    #     uid = req['uid']
    #     password = req['password']
    #     if not verify_user(uid, password):
    #         return bool_res()[False]
    # TODO 这里应该有好友检查
    #     gid = req['gid']
    #     added = req['added']
    #     if not user_cursor.uid_query(added):
    #         return bool_res()[False]
    #     return bool_res()[group_cursor.add_member(gid, added)]

    @api("/group/add_essence", methods=['POST'])
    def add_essence(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        mid = req["mid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        stat = group_cursor.is_admin(gid, uid);
        if stat < 1:
            return bool_res()[False]
        if not group_cursor.get_essence_enabled(gid):
            return bool_res()[False]
        message = messages_cursor.get_message(mid)
        if message is None or message.get("group_id") != gid:
            return bool_res()[False]
        succeed = group_cursor.add_essence(gid, mid)
        if not succeed:
            return bool_res()[False]
        sender = message["sender_uid"]
        group_info = group_cursor.query_gid(gid)
        if group_info:
            group_info = group_info[0]
            group_name = group_info[2]
            target_uids = group_cursor.get_member_uids(gid)
        else:
            return bool_res()[False]
        if succeed: 
            notify_users(target_uids, "group.essence.add", "{} 的消息被设定精华".format(group_name), "管理员 {} 将用户 {} 的消息设为精华".format(uid, sender), sender=uid, meta={"gid" : gid})

        return bool_res()[succeed]

    @api("/group/remove_essence", methods=['POST'])
    def remove_essence(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        mid = req["mid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        stat = group_cursor.is_admin(gid, uid);
        if stat < 1:
            return bool_res()[False]
        if not group_cursor.get_essence_enabled(gid):
            return bool_res()[False]
        message = messages_cursor.get_message(mid)
        if message is None or message.get("group_id") != gid:
            return bool_res()[False]
        sender = message["sender_uid"]
        succeed = group_cursor.remove_essence(gid, mid)
        if not succeed:
            return bool_res()[False]
        group_info = group_cursor.query_gid(gid)
        if group_info:
            group_info = group_info[0]
            group_name = group_info[2]
            target_uids = group_cursor.get_member_uids(gid)
        else:
            return bool_res()[False]
        if succeed: 
            notify_users(target_uids, "group.essence.remove", "{} 的消息被移除精华".format(group_name), "管理员 {} 将用户 {} 的消息移除精华".format(uid, sender), sender=uid, meta={"gid" : gid})
        return bool_res()[succeed]

    @api("/group/query_essence", methods=['POST'])
    def query_essence(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if not group_cursor.is_member(gid, uid):
            return json.dumps({"success": False, "error": "not_group_member"})
        return json.dumps({"essence" : group_cursor.query_essence(gid), "essence_enabled": group_cursor.get_essence_enabled(gid)})

    @api("/group/remove_member", methods=['POST'])
    def remove_member(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        removed = req["removed"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        oper = group_cursor.is_admin(gid, uid)
        oped = group_cursor.is_admin(gid, removed)
        if not oper > oped:
            return bool_res()[False]
        succeeded = group_cursor.remove_member(gid, removed)
        if succeeded:
            run_notification_side_effect(
                "group.member.removed",
                lambda: notify_user(removed, "group.member.removed", "你已被移出群聊",
                    "你已被 {} 移出群 {}。".format(format_user_display(uid), group_cursor.query_gid(gid)[0][2] if group_cursor.query_gid(gid) else str(gid)),
                    sender=uid, meta={"gid" : gid})
            )
        return bool_res()[succeeded]

    @api("/group/leave", methods=['POST'])
    def group_leave(req):
        """ 成员/管理员 主动退群"""
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_member(gid, uid):
            return bool_res()[False]
        # 群主不可
        if group_cursor.is_admin(gid, uid) == 2:
            return bool_res()[False]
        succeeded = group_cursor.remove_member(gid, uid)
        if succeeded:
            run_notification_side_effect(
                "group.left",
                lambda: notify_user(
                    uid, "group.left", "已退出群聊", "你已退出群聊。",
                    sender=uid, meta={"gid": gid}
                )
            )
        return bool_res()[succeeded]

    @api("/group/remove_admin", methods=['POST'])
    def remove_admin(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        removed = req["removed"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_admin(gid, uid) == 2:
            return bool_res()[False]
        succeeded = group_cursor.remove_admin(gid, removed)
        if succeeded:
            run_notification_side_effect(
                "group.admin.removed",
                lambda: notify_user(removed, "group.admin.removed", "你的管理员权限已被移除",
                    "{} 移除了你在群 {} 的管理员权限。".format(format_user_display(uid), group_cursor.query_gid(gid)[0][2] if group_cursor.query_gid(gid) else str(gid)),
                    sender=uid, meta={"gid" : gid})
            )
        return bool_res()[succeeded]

    @api("/group/delete_group", methods=['POST'])
    def delete_group(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_admin(gid, uid) == 2:
            return bool_res()[False]
        group_info = group_cursor.query_gid(gid)
        if group_info:
            group_info = group_info[0]
            group_name = group_info[2]
            target_uids = group_cursor.get_member_uids(gid)
        else:
            group_name = str(gid)
            target_uids = []
        try:
            avatar.clean_avatar(port_api, gid, "group")
        except Exception:
            return bool_res()[False]
        group_cursor.delete_group(gid)
        notify_users([target_uid for target_uid in target_uids if target_uid != uid], "group.deleted", "群聊已解散",
            "群 {} 已被 {} 解散。".format(group_name, format_user_display(uid)), sender=uid, meta={"gid" : gid})
        return bool_res()[True]

    @api("/group/transfer_owner", methods=['POST'])
    def transfer_owner(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        new_owner = req["new_owner"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_admin(gid, uid) == 2:
            return bool_res()[False]
        succeeded = group_cursor.transfer_owner(gid, uid, new_owner)
        if succeeded:
            run_notification_side_effect("group.owner.transferred",
                lambda: notify_user(new_owner, "group.owner.transferred", "你已成为群主",
                    "{} 已将群 {} 的群主转让给你。".format(format_user_display(uid), group_cursor.query_gid(gid)[0][2] if group_cursor.query_gid(gid) else str(gid)),
                    sender=uid, meta={"gid": gid}))
        return bool_res()[succeeded]

    @api("/group/settings", methods=['POST'])
    def group_settings(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        if not group_cursor.is_member(gid, uid):
            return json.dumps({"success": False, "error": "not_group_member"})
        return json.dumps(group_cursor.get_group_settings(gid), ensure_ascii=False)

    @api("/group/update_settings", methods=['POST'])
    def update_group_settings(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_admin(gid, uid) == 2:
            return bool_res()[False]
        updates = {}
        for key in ("groupname", "enter_hint", "introduction",
                     "allow_direct_join", "require_review", "essence_enabled",
                     "public_messages"):
            if key in req:
                updates[key] = req[key]
        if "groupname" in updates:
            cfg = read_config()
            min_len = cfg.get("min_group_name_length", 1)
            max_len = cfg.get("max_group_name_length", 50)
            gn = updates["groupname"]
            if not isinstance(gn, str) or not (min_len <= len(gn.strip()) <= max_len):
                return bool_res()[False]
        if not updates:
            return bool_res()[False]
        return bool_res()[group_cursor.update_settings(gid, **updates)]

    @api("/group/pin_message", methods=['POST'])
    def pin_message(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        if user_row[4] == 'banned':
            return bool_res()[False]
        gid = req["gid"]
        mid = req["mid"]
        if group_cursor.is_admin(gid, uid) < 1:
            return bool_res()[False]
        msg = messages_cursor.get_message(mid)
        if msg is None or msg.get("group_id") != gid or msg.get("deleted"):
            return bool_res()[False]
        if messages_cursor.is_message_pinned(mid, gid):
            return bool_res()[False]
        pin_id = messages_cursor.pin_message(mid, gid, uid)
        if pin_id:
            group_name = group_cursor.query_gid(gid)[0][2] if group_cursor.query_gid(gid) else str(gid)
            member_uids = group_cursor.get_member_uids(gid)
            notify_users(member_uids, "messages.pinned",
                "消息已置顶",
                "{} 在群 {} 中置顶了一条消息。".format(format_user_display(uid), group_name),
                sender=uid, meta={"gid": gid, "mid": mid, "pin_id": pin_id})
        return bool_res()[bool(pin_id)]

    @api("/group/unpin_message", methods=['POST'])
    def unpin_message(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        if user_row[4] == 'banned':
            return bool_res()[False]
        gid = req["gid"]
        pin_id = req["pin_id"]
        if group_cursor.is_admin(gid, uid) < 1:
            return bool_res()[False]
        succeeded = messages_cursor.unpin_message(pin_id, gid)
        if succeeded:
            member_uids = group_cursor.get_member_uids(gid)
            notify_users(member_uids, "messages.unpinned",
                "消息置顶已取消",
                "{} 取消了群中的一条消息置顶。".format(format_user_display(uid)),
                sender=uid, meta={"gid": gid, "pin_id": pin_id})
        return bool_res()[succeeded]

    @api("/group/pinned_messages", methods=['POST'])
    def pinned_messages(req):
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        if user_row[4] == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        gid = req["gid"]
        if not group_cursor.is_member(gid, uid):
            return json.dumps({"success": False, "error": "not_group_member"})
        pins = messages_cursor.get_pinned_messages(gid)
        results = []
        for row in pins:
            pin_id, mid, gid_val, pinned_by_uid, created_at = row
            msg = messages_cursor.get_message(mid)
            results.append({
                "pin_id": pin_id,
                "message_id": mid,
                "group_id": gid_val,
                "pinned_by_uid": pinned_by_uid,
                "created_at": created_at,
                "message": msg,
            })
        return json.dumps(results, ensure_ascii=False)

    @api("/group/members", methods=['POST'])
    def group_members(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        if not group_cursor.is_member(gid, uid):
            return json.dumps({"success": False, "error": "not_group_member"})
        member_uids = group_cursor.get_member_uids(gid)
        admin_uids = group_cursor.get_admin_uids(gid)
        settings = group_cursor.get_group_settings(gid)
        members = []
        if member_uids:
            placeholders = ",".join("?" * len(member_uids))
            rows = user_cursor.query(
                "SELECT uid, username FROM users WHERE uid IN ({})".format(placeholders),
                tuple(member_uids)
            )
            name_map = {r[0]: r[1] for r in rows}
            for muid in member_uids:
                role = "owner" if muid == settings.get("creater") else ("admin" if muid in admin_uids else "member")
                members.append({
                    "uid": muid,
                    "username": name_map.get(muid, "User {}".format(muid)),
                    "role": role,
                })
        return json.dumps({"members": members, "settings": settings}, ensure_ascii=False)

    @api("/group/join", methods=['POST'], feature=("chat", "group_chat"))
    def join_group(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        settings = group_cursor.get_group_settings(gid)
        if not settings:
            return json.dumps({"success": False, "error": "not_found"})
        if group_cursor.is_member(gid, uid):
            return json.dumps({"success": False, "error": "conflict"})
        max_len = read_config().get("max_request_message_length", 200)
        request_message = str(req.get("message", "") or "").strip()
        if max_len > 0 and len(request_message) > max_len:
            return json.dumps({"success": False, "error": "request_message_too_long"})
        if settings["allow_direct_join"]:
            if not settings["require_review"]:
                succeeded = group_cursor.add_member(gid, uid)
                if succeeded:
                    return json.dumps({"pending": False})
                return json.dumps({"success": False, "error": "unavailable"})
            else:
                members = group_cursor.get_member_uids(gid)
                cfg = read_config()
                limit = cfg.get("single_group_max_people", 200)
                if limit != -1 and len(members) >= limit:
                    return json.dumps({"success": False, "error": "conflict"})
                rid = group_cursor.request_join(gid, uid, message=request_message)
                reviewers = [settings["creater"]] + group_cursor.get_admin_uids(gid)
                run_notification_side_effect("group.join.request",
                    lambda: notify_users(reviewers, "group.join.request",
                        "新的入群申请", "{} 申请加入群 {}。".format(format_user_display(uid), settings["groupname"]),
                        sender=uid, meta={"gid": gid, "rid": rid, "message": request_message}))
                return json.dumps({"rid": rid, "pending": True})
        return json.dumps({"success": False, "error": "forbidden"})

    @api("/group/invite", methods=['POST'], feature=("chat", "group_chat"))
    def invite_to_group(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        invited_uid = req["invited_uid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        if not group_cursor.is_member(gid, uid):
            return json.dumps({"success": False, "error": "not_group_member"})
        if not user_cursor.uid_query(invited_uid):
            return json.dumps({"success": False, "error": "not_found"})
        if not user_cursor.is_friend(uid, invited_uid):
            return json.dumps({"success": False, "error": "not_friends"})
        if group_cursor.is_member(gid, invited_uid):
            return json.dumps({"success": False, "error": "conflict"})
        settings = group_cursor.get_group_settings(gid)
        max_len = read_config().get("max_request_message_length", 200)
        invite_message = str(req.get("message", "") or "").strip()
        if max_len > 0 and len(invite_message) > max_len:
            return json.dumps({"success": False, "error": "request_message_too_long"})
        is_admin = group_cursor.is_admin(gid, uid) >= 1
        if not settings["require_review"] or is_admin:
            succeeded = group_cursor.add_member(gid, invited_uid)
            if succeeded:
                run_notification_side_effect("group.invited",
                    lambda: notify_user(invited_uid, "group.invited", "你已被邀请加入群聊",
                        "{} 邀请你加入群 {}。".format(format_user_display(uid), settings["groupname"]),
                        sender=uid, meta={"gid": gid}))
                return json.dumps({"pending": False})
            return json.dumps({"success": False, "error": "unavailable"})
        rid = group_cursor.request_join(gid, invited_uid, inviter_uid=uid,
                                        message=invite_message)
        reviewers = [settings["creater"]] + group_cursor.get_admin_uids(gid)
        run_notification_side_effect("group.join.request",
            lambda: notify_users(reviewers, "group.join.request",
                "新的入群申请", "{} 邀请 {} 加入群 {}，等待审核。".format(format_user_display(uid), format_user_display(invited_uid), settings["groupname"]),
                sender=uid, meta={"gid": gid, "rid": rid, "message": invite_message}))
        run_notification_side_effect("group.invited.pending",
            lambda: notify_user(invited_uid, "group.invited", "你已被邀请加入群聊",
                "{} 邀请你加入群 {}（需审核）。".format(format_user_display(uid), settings["groupname"]),
                sender=uid, meta={"gid": gid, "rid": rid}))
        return json.dumps({"rid": rid, "pending": True})

    @api("/group/join_requests", methods=['POST'])
    def join_requests(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})
        if not group_cursor.is_admin(gid, uid):
            return json.dumps({"success": False, "error": "forbidden"})
        requests = group_cursor.get_join_requests(gid)
        if requests:
            uids = list(set(r["uid"] for r in requests) | set(r["inviter_uid"] for r in requests if r["inviter_uid"]))
            if uids:
                placeholders = ",".join("?" * len(uids))
                rows = user_cursor.query(
                    "SELECT uid, username FROM users WHERE uid IN ({})".format(placeholders),
                    tuple(uids)
                )
                name_map = {r[0]: r[1] for r in rows}
                for req_item in requests:
                    req_item["username"] = name_map.get(req_item["uid"], "User {}".format(req_item["uid"]))
                    if req_item["inviter_uid"]:
                        req_item["inviter_name"] = name_map.get(req_item["inviter_uid"], "")
        return json.dumps(requests, ensure_ascii=False)

    @api("/group/handle_join_request", methods=['POST'])
    def handle_join_request(req):
        uid = req["uid"]
        password = req["password"]
        rid = req["rid"]
        approved = bool(req.get("approved", False))
        if not verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        req_info = group_cursor.query(
            "SELECT gid FROM join_requests WHERE rid = ?", (rid,)
        )
        if not req_info:
            return bool_res()[False]
        gid = req_info[0][0]
        if not group_cursor.is_admin(gid, uid):
            return bool_res()[False]
        succeeded = group_cursor.handle_join_request(rid, approved)
        if succeeded and approved:
            settings = group_cursor.get_group_settings(gid)
            req_data = group_cursor.get_join_requests(gid, status='approved')
            for r in req_data:
                if r["rid"] == rid:
                    run_notification_side_effect("group.join.approved",
                        lambda: notify_user(r["uid"], "group.join.approved",
                            "入群申请已通过", "你加入群 {} 的申请已通过。".format(settings.get("groupname", str(gid))),
                            sender=uid, meta={"gid": gid}))
        return bool_res()[succeeded]

    @api("/friend/list", methods=['POST'])
    def friend_list(req):
        """好友列表返回"""
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        rows = user_cursor.query(
            "SELECT user1, user2, adder FROM friendship WHERE relationship = 'friend' AND (user1 = ? OR user2 = ?)",
            (uid, uid)
        )
        friend_uids = []
        for row in rows:
            friend_uid = row[0] if row[1] == uid else row[1]
            friend_uids.append(friend_uid)
        return json.dumps(friend_uids)

    @api("/message/send", methods=['POST'])
    def send_message(req):
        """API 统一发送消息（文本和文件）。返回 {mid, status:'sent'}。"""
        try:
            uid = req["uid"]
            password = req["password"]
            recipient = str(req["recipient"])
            content = str(req["content"])
            content_type = str(req.get("content_type", "plain"))
            client_mid = req.get("client_mid")
            quote = int(req.get("quote", -1))
            forwarded = int(req.get("forwarded", -1))
            duration = req.get("duration_ms")
            try:
                duration = int(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            if quote < -1 or forwarded < -1 or (quote >= 0 and forwarded >= 0):
                return json.dumps({"success": False, "error": "invalid_request"})
            if content_type not in ("plain", "file"):
                return json.dumps({"success": False, "error": "invalid_request"})
            file_hash = None
            if content_type == "file":
                file_hash = str(req.get("file_hash") or content)
                if (len(file_hash) != 64
                        or not all(char in "0123456789abcdefABCDEF" for char in file_hash)):
                    return json.dumps({"success": False, "error": "invalid_request"})
                content = file_hash

            if not verify_user(uid, password):
                return json.dumps({"success": False, "error": "auth_failed"})
            user_row = get_user_row(uid)
            if user_row is None:
                return json.dumps({"success": False, "error": "invalid_request"})
            user_stat = user_row[4]
            if user_stat == 'banned':
                return json.dumps({"success": False, "error": "banned"})
            file_record = None

            target_uid = 0
            group_id = None
            if recipient.startswith('U'):
                target_uid = int(recipient[1:])
            elif recipient.startswith('G'):
                group_id = int(recipient[1:])
            else:
                return json.dumps({"success": False, "error": "invalid_request"})

            if group_id is not None:
                if not feature_gate(read_config(), "chat", "group_chat"):
                    return {"error": "feature_disabled_group_chat"}
            elif not feature_gate(read_config(), "chat", "private_chat"):
                return {"error": "feature_disabled_private_chat"}

            source_message = None
            if forwarded >= 0:
                if not messages_cursor.verify_quote(
                        forwarded, uid, target_uid, group_id):
                    return json.dumps({"success": False, "error": "invalid_quote"})
                source_message = messages_cursor.get_message(forwarded)
                if source_message is None:
                    return json.dumps({"success": False, "error": "not_found"})
                content_type = source_message["content_type"]
                content = source_message["content"]
                file_hash = source_message["file_hash"]
                if source_message.get("duration") is not None:
                    duration = source_message["duration"]

            existing = messages_cursor.get_by_client_mid(uid, client_mid)
            if existing is not None:
                if not messages_cursor.request_matches(
                        existing["mid"], uid, target_uid, content, content_type,
                        file_hash=file_hash, quote=quote, group_id=group_id,
                        forwarded=forwarded, duration=duration):
                    return json.dumps({
                        "success": False, "error": "client_mid_conflict"
                    })
                existing["quote_preview"] = (
                    messages_cursor.get_quote_preview(existing["quote"], existing)
                    if existing["quote"] >= 0 else None
                )
                existing["forward_preview"] = (
                    messages_cursor.get_quote_preview(existing["forwarded"], existing)
                    if existing["forwarded"] >= 0 else None
                )
                enrich_message_files([existing])
                return json.dumps({
                    "mid": existing["mid"], "client_mid": client_mid,
                    "status": "sent", "message": existing,
                }, ensure_ascii=False)

            if group_id is not None and not group_cursor.is_member(group_id, uid):
                return json.dumps({"success": False, "error": "not_group_member"})
            if group_id is None and not user_cursor.is_friend(uid, target_uid):
                return json.dumps({"success": False, "error": "not_friends"})

            if content_type == "plain" and len(content) > read_config().get("max_message_length", 10000):
                return json.dumps({"success": False, "error": "message_too_long"})

            if quote >= 0:
                if not messages_cursor.verify_quote(quote, uid, target_uid, group_id):
                    return json.dumps({"success": False, "error": "invalid_quote"})
                if group_id is not None and not group_cursor.is_member(group_id, uid):
                    return json.dumps({"success": False, "error": "not_group_member"})

            if file_hash:
                if forwarded >= 0:
                    file_record = file_cursor.acquire_forward_reference(file_hash)
                    if file_record is not None:
                        file_record["file_name"] = (
                            source_message.get("file_name")
                            or file_record["file_name"]
                        )
                else:
                    file_record = file_cursor.acquire_reference(uid, file_hash)
                if file_record is None:
                    return json.dumps({"success": False, "error": "file_unavailable"})
            msg_record = messages_cursor.add_message(
                uid, target_uid, content,
                content_type=content_type, file_hash=file_hash,
                quote=quote, group_id=group_id, client_mid=client_mid,
                file_name=file_record["file_name"] if file_record else None,
                forwarded=forwarded, duration=duration,
            )

            if msg_record.get("duplicate"):
                if not messages_cursor.request_matches(
                        msg_record["mid"], uid, target_uid, content, content_type,
                        file_hash=file_hash, quote=quote, group_id=group_id,
                        forwarded=forwarded, duration=duration):
                    return json.dumps({
                        "success": False, "error": "client_mid_conflict"
                    })
                existing = messages_cursor.get_message(msg_record["mid"])
                if existing:
                    existing["quote_preview"] = (
                        messages_cursor.get_quote_preview(existing["quote"], existing)
                        if existing["quote"] >= 0 else None
                    )
                    existing["forward_preview"] = (
                        messages_cursor.get_quote_preview(
                            existing["forwarded"], existing
                        ) if existing["forwarded"] >= 0 else None
                    )
                    enrich_message_files([existing])
                return json.dumps({
                    "mid": msg_record["mid"], "client_mid": client_mid,
                    "status": "sent", "message": existing,
                }, ensure_ascii=False)

            if file_hash:
                file_cursor.add_reference(file_hash, "message", msg_record["mid"], uid)

            if content_type == "plain":
                group_members = (
                    group_cursor.get_member_uids(group_id) if group_id else [])
                allowed_mentions = group_members if group_id else [target_uid]
                mentioned_uids = resolve_mentioned_uids(
                    content, user_cursor, allowed_mentions, exclude_uid=uid
                )
            else:
                group_members = []
                mentioned_uids = []
            messages_cursor.set_message_mentions(msg_record["mid"], mentioned_uids)

            notif = build_notification(
                "message.{}".format(content_type),
                str(msg_record["send_time"]),
                content,
                sender="G{}U{}".format(group_id, uid) if group_id else "U{}".format(uid),
                meta={"quote": quote}
            )
            notif["mid"] = msg_record["mid"]
            notif["client_mid"] = client_mid
            notif["quote"] = quote
            notif["mentioned_uids"] = mentioned_uids
            notif["quote_preview"] = (
                messages_cursor.get_quote_preview(quote, msg_record) if quote >= 0 else None
            )
            notif["forwarded"] = forwarded
            notif["forward_preview"] = (
                messages_cursor.get_quote_preview(forwarded, msg_record)
                if forwarded >= 0 else None
            )
            if group_id:
                notif["group_id"] = group_id
                notif["room_id"] = "G{}".format(group_id)
            if file_hash:
                notif["file_hash"] = file_hash
                notif["file"] = file_metadata(file_hash, uid)
            if duration is not None:
                notif["duration"] = duration

            if group_id:
                room_id = "G{}".format(group_id)
                if not group_members:
                    group_members = group_cursor.get_member_uids(group_id)
                # 一条 SQL 取全部成员的房间偏好，避免按成员逐个查询（N+1）
                pref_map = messages_cursor.get_room_preference_map(
                    group_members, room_id)
                pending = []
                for user in group_members:
                    user_notif = dict(notif)
                    user_notif["room_seq"] = msg_record.get("room_seq")
                    user_notif["mentions_me"] = user in mentioned_uids
                    user_notif["should_alert"] = (
                        user != uid
                        and alert_from_preference(
                            pref_map.get(user), user, mentioned_uids)
                    )
                    pending.append((user, user_notif))
                instant_contact.push_messages(pending)
            else:
                recv_notif = dict(notif)
                recv_notif["room_id"] = "U{}".format(uid)
                recv_notif["room_seq"] = msg_record.get("room_seq")
                recv_notif["mentions_me"] = target_uid in mentioned_uids
                recv_notif["should_alert"] = should_alert(
                    messages_cursor, target_uid, recv_notif["room_id"], mentioned_uids
                )
                sender_notif = dict(notif)
                sender_notif["room_id"] = "U{}".format(target_uid)
                sender_notif["room_seq"] = msg_record.get("room_seq")
                sender_notif["mentions_me"] = False
                sender_notif["should_alert"] = False
                instant_contact.push_message(target_uid, recv_notif)
                instant_contact.push_message(uid, sender_notif)
                # 发送即已读：隐式前进也要告诉对方，否则对方的推送通道会停在旧
                # 水位（WS 路径同理，见 channel.emit_private_read_receipt）。
                instant_contact.emit_private_read_receipt(
                    uid, target_uid, messages_cursor.room_key_of(uid, target_uid),
                    msg_record["mid"], msg_record.get("room_seq") or 0)

            response_message = dict(msg_record)
            response_message["mentioned_uids"] = mentioned_uids
            response_message["quote_preview"] = notif["quote_preview"]
            response_message["forward_preview"] = notif["forward_preview"]
            enrich_message_files([response_message])
            return json.dumps({
                "mid": msg_record["mid"], "client_mid": client_mid,
                "status": "sent", "message": response_message,
            }, ensure_ascii=False)
        except Exception:
            return json.dumps({"success": False, "error": "unavailable"})

    @api("/message/redirect", methods=['POST'])
    def redirect_messages(req):
        """合并转发：把同一房间内最多 100 条文本消息结为一条
        """
        try:
            uid = req["uid"]
            password = req["password"]
            recipient = str(req["recipient"])
            mids = req.get("mids") or []
            client_mid = req.get("client_mid")

            if not isinstance(mids, list):
                return json.dumps({"success": False, "error": "invalid_request"})
            mids = sorted({int(m) for m in mids if str(m).isdigit()})
            if not mids:
                return json.dumps({"success": False, "error": "invalid_request"})
            if len(mids) > 100:
                return json.dumps({"success": False, "error": "invalid_request"})

            if not verify_user(uid, password):
                return json.dumps({"success": False, "error": "auth_failed"})
            user_row = get_user_row(uid)
            if user_row is None or user_row[4] == 'banned':
                return json.dumps({"success": False, "error": "banned"})

            if recipient.startswith('U'):
                target_uid = int(recipient[1:])
                group_id = None
            elif recipient.startswith('G'):
                target_uid = 0
                group_id = int(recipient[1:])
            else:
                return json.dumps({"success": False, "error": "invalid_request"})

            if group_id is not None:
                if not feature_gate(read_config(), "chat", "group_chat"):
                    return {"error": "feature_disabled_group_chat"}
            elif not feature_gate(read_config(), "chat", "private_chat"):
                return {"error": "feature_disabled_private_chat"}

            records = messages_cursor.get_messages_by_mids(mids)
            selected = [records[m] for m in mids if m in records]
            if len(selected) != len(mids):
                return json.dumps({"success": False, "error": "invalid_request"})

            room_keys = {r["room_key"] for r in selected}
            if len(room_keys) != 1:
                return json.dumps({"success": False, "error": "invalid_request"})
            if any(r["deleted"] for r in selected):
                return json.dumps({"success": False, "error": "invalid_request"})
            if any(r["content_type"] != "plain" for r in selected):
                return json.dumps({"success": False, "error": "invalid_request"})

            source = selected[0]
            src_group = source["group_id"]
            # 校验调用者可读源房间
            if src_group is not None:
                if not group_cursor.is_member(src_group, uid):
                    return json.dumps({"success": False, "error": "not_group_member"})
            else:
                src_pair = {source["sender_uid"], source["receiver_uid"]}
                if uid not in src_pair:
                    return json.dumps({"success": False, "error": "not_group_member"})

            # 校验调用者可写目标房间
            if group_id is not None:
                if not group_cursor.is_member(group_id, uid):
                    return json.dumps({"success": False, "error": "not_group_member"})
            else:
                if not user_cursor.is_friend(uid, target_uid):
                    return json.dumps({"success": False, "error": "not_friends"})

            group_name = ""
            if src_group is not None:
                settings = group_cursor.get_group_settings(src_group)
                group_name = settings.get("groupname", "") or ""

            snapshot = messages_cursor.build_redirect_snapshot(
                uid, mids, group_name=group_name
            )
            _name_cache = {}
            for entry in snapshot.get("messages", []):
                entry_uid = entry.get("sender_uid")
                if entry_uid is None:
                    continue
                if entry_uid not in _name_cache:
                    _name_cache[entry_uid] = get_username(entry_uid)
                sender_name = _name_cache[entry_uid]
                if sender_name:
                    entry["sender_name"] = sender_name
            snapshot_content = json.dumps(snapshot, ensure_ascii=False)

            existing = messages_cursor.get_by_client_mid(uid, client_mid) \
                if client_mid else None
            if existing is not None:
                return json.dumps({
                    "mid": existing["mid"], "client_mid": client_mid,
                    "status": "sent", "message": existing,
                }, ensure_ascii=False)

            msg_record = messages_cursor.add_message(
                uid, target_uid, snapshot_content,
                content_type='redirect', group_id=group_id,
                client_mid=client_mid,
                forwarded=selected[-1]["mid"] if selected else -1,
            )
            if msg_record.get("duplicate"):
                existing = messages_cursor.get_message(msg_record["mid"])
                return json.dumps({
                    "mid": msg_record["mid"], "client_mid": client_mid,
                    "status": "sent", "message": existing,
                }, ensure_ascii=False)

            # 推送
            notif = build_notification(
                "message.redirect", str(msg_record["send_time"]),
                snapshot_content,
                sender="G{}U{}".format(group_id, uid) if group_id else "U{}".format(uid),
                meta={"quote": -1},
            )
            notif["mid"] = msg_record["mid"]
            notif["client_mid"] = client_mid
            notif["content_type"] = "redirect"
            notif["quote"] = -1
            notif["forwarded"] = msg_record["forwarded"]
            notif["mentioned_uids"] = []

            if group_id:
                pending = []
                for user in group_cursor.get_member_uids(group_id):
                    user_notif = dict(notif)
                    user_notif["room_seq"] = msg_record.get("room_seq")
                    user_notif["mentions_me"] = False
                    user_notif["should_alert"] = user != uid
                    pending.append((user, user_notif))
                instant_contact.push_messages(pending)
            else:
                recv_notif = dict(notif)
                recv_notif["room_id"] = "U{}".format(uid)
                recv_notif["room_seq"] = msg_record.get("room_seq")
                recv_notif["mentions_me"] = False
                recv_notif["should_alert"] = True
                sender_notif = dict(notif)
                sender_notif["room_id"] = "U{}".format(target_uid)
                sender_notif["room_seq"] = msg_record.get("room_seq")
                sender_notif["mentions_me"] = False
                sender_notif["should_alert"] = False
                instant_contact.push_message(target_uid, recv_notif)
                instant_contact.push_message(uid, sender_notif)
                instant_contact.emit_private_read_receipt(
                    uid, target_uid, messages_cursor.room_key_of(uid, target_uid),
                    msg_record["mid"], msg_record.get("room_seq") or 0)

            response_message = dict(msg_record)
            response_message["content_type"] = "redirect"
            return json.dumps({
                "mid": msg_record["mid"], "client_mid": client_mid,
                "status": "sent", "message": response_message,
            }, ensure_ascii=False)
        except Exception:
            return json.dumps({"success": False, "error": "unavailable"})

    @api("/chat/list", methods=['POST'])
    def chat_list(req):
        """返回所有聊天会话及最后一条消息和对方资料。"""
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            print("[WARN] chat_list: verify_user failed for uid={}".format(uid))
            return json.dumps({"error": "auth_failed"}, ensure_ascii=False)
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})

        try:
            chat_rows = messages_cursor.get_chat_list(uid)
        except Exception as e:
            print("[WARN] chat_list: get_chat_list failed for uid={}: {}".format(uid, e))
            chat_rows = []
        partner_map = {}
        for c in chat_rows:
            partner_map[c["partner_uid"]] = c

        friend_uids = set()
        friend_rows = user_cursor.query(
            """SELECT CASE WHEN user1 = ? THEN user2 ELSE user1 END
               FROM friendship WHERE relationship = 'friend' AND (user1 = ? OR user2 = ?)""",
            (uid, uid, uid)
        )
        for r in friend_rows:
            fuid = r[0]
            friend_uids.add(fuid)
            if fuid not in partner_map:
                partner_map[fuid] = {
                    "partner_uid": fuid,
                    "group_id": None,
                    "last_mid": None, "last_sender_uid": None,
                    "last_content": None, "last_content_type": None, "last_time": None,
                }

        group_rows = group_cursor.get_user_group_rows(uid)
        group_ids = [row[0] for row in group_rows]
        last_msgs = messages_cursor.get_group_last_messages(group_ids) if group_ids else {}
        for row in group_rows:
            gid = row[0]
            groupname = row[2]
            last = last_msgs.get(gid)
            partner_map[-gid] = {
                "partner_uid": gid,
                "group_id": gid,
                "groupname": groupname,
                "last_mid": last["mid"] if last else None,
                "last_sender_uid": last["sender_uid"] if last else None,
                "last_content": last["content"] if last else None,
                "last_content_type": last["content_type"] if last else None,
                "last_time": last["send_time"] if last else None,
                "last_seq": last.get("last_seq") if last else None,
                "last_deleted": bool(last.get("deleted")) if last else False,
                "last_deleted_at": last.get("deleted_at") if last else None,
                "last_file_name": last.get("file_name") if last else None,
            }

        direct_puids = [k for k in partner_map.keys() if k >= 0]
        username_map = {}
        if direct_puids:
            placeholders = ",".join("?" * len(direct_puids))
            uname_rows = user_cursor.query(
                "SELECT uid, username FROM users WHERE uid IN ({})".format(placeholders),
                tuple(direct_puids)
            )
            username_map = {r[0]: r[1] for r in uname_rows}

        preferences = messages_cursor.get_room_preferences(uid)
        result = []
        for key, chat in partner_map.items():
            if key < 0:
                gid = chat["group_id"]
                room_id = "G{}".format(gid)
                pref = preferences.get(room_id)
                result.append({
                    "room_id": room_id,
                    "room_type": "group",
                    "partner_uid": gid,
                    "username": chat.get("groupname", "Group {}".format(gid)),
                    "avatar": "/avatar/get_avatar/group/{}".format(gid),
                    "last_content": chat.get("last_content"),
                    "last_content_type": chat.get("last_content_type"),
                    "last_time": chat.get("last_time"),
                    "last_sender_uid": chat.get("last_sender_uid"),
                    "last_mid": chat.get("last_mid"),
                    "last_seq": chat.get("last_seq"),
                    "last_deleted": bool(chat.get("last_deleted", False)),
                    "last_deleted_at": chat.get("last_deleted_at"),
                    "last_file": (
                        with_display_file_name(
                            file_metadata(chat.get("last_content"), chat.get("last_sender_uid")),
                            chat.get("last_file_name"),
                        )
                        if chat.get("last_content_type") == "file"
                        and not chat.get("last_deleted") else None
                    ),
                    "is_friend": False,
                    "is_pinned": pref.get("is_pinned") if pref else None,
                    "notify_level": pref.get("notify_level") if pref else None,
                    "alias": pref.get("alias") if pref else None,
                    "description": pref.get("description") if pref else None,
                })
            else:
                room_id = "U{}".format(key)
                pref = preferences.get(room_id)
                result.append({
                    "room_id": room_id,
                    "room_type": "direct",
                    "partner_uid": key,
                    "username": username_map.get(key, "User {}".format(key)),
                    "avatar": "/avatar/get_avatar/user/{}".format(key),
                    "last_content": chat.get("last_content"),
                    "last_content_type": chat.get("last_content_type"),
                    "last_time": chat.get("last_time"),
                    "last_sender_uid": chat.get("last_sender_uid"),
                    "last_mid": chat.get("last_mid"),
                    "last_seq": chat.get("last_seq"),
                    "last_deleted": bool(chat.get("last_deleted", False)),
                    "last_deleted_at": chat.get("last_deleted_at"),
                    "last_file": (
                        with_display_file_name(
                            file_metadata(chat.get("last_content"), chat.get("last_sender_uid")),
                            chat.get("last_file_name"),
                        )
                        if chat.get("last_content_type") == "file"
                        and not chat.get("last_deleted") else None
                    ),
                    "is_friend": key in friend_uids,
                    "is_pinned": pref.get("is_pinned") if pref else None,
                    "notify_level": pref.get("notify_level") if pref else None,
                    "alias": pref.get("alias") if pref else None,
                    "description": pref.get("description") if pref else None,
                })

        result.sort(key=lambda x: x.get("last_time") or 0, reverse=True)

        # 服务端未读
        def _room_key_for(item):
            if item["room_type"] == "group":
                return messages_cursor.room_key_of(uid, 0, group_id=item["partner_uid"])
            return messages_cursor.room_key_of(uid, item["partner_uid"])

        room_keys = [_room_key_for(item) for item in result]
        try:
            messages_cursor.init_read_watermarks(uid, room_keys)
            notify_level_map = {
                _room_key_for(item): int(item.get("notify_level") or 0)
                for item in result
            }
            unread_map = messages_cursor.bulk_unread_counts(
                uid, room_keys, notify_level_map)
        except Exception as e:
            print("[WARN] chat_list: unread count failed for uid={}: {}".format(uid, e))
            unread_map = {}
        for item in result:
            item["unread_count"] = unread_map.get(_room_key_for(item), 0)

        # 可选分页：默认全量
        try:
            limit = int(req.get("limit", 0) or 0)
            offset = int(req.get("offset", 0) or 0)
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if offset < 0 or limit < 0:
            return json.dumps({"success": False, "error": "invalid_request"})
        if offset > 0:
            result = result[offset:]
        if limit > 0:
            result = result[:limit]
        return json.dumps(result, ensure_ascii=False)

    @api("/chat/preferences/update", methods=['POST'])
    def update_chat_preference(req):
        uid = req["uid"]
        password = req["password"]
        room_id = str(req.get("room_id", ""))
        if not verify_user(uid, password):
            return bool_res()[False]
        if len(room_id) < 2:
            return bool_res()[False]
        try:
            target_id = int(room_id[1:])
        except (TypeError, ValueError):
            return bool_res()[False]
        if room_id.startswith("U"):
            allowed = user_cursor.is_friend(uid, target_id)
        elif room_id.startswith("G"):
            allowed = group_cursor.is_member(target_id, uid)
        else:
            allowed = False
        if not allowed:
            return bool_res()[False]
        is_pinned = req.get("is_pinned") if "is_pinned" in req else None
        notify_level = req.get("notify_level") if "notify_level" in req else None
        alias = req.get("alias") if "alias" in req else None
        description = req.get("description") if "description" in req else None
        if is_pinned is not None and not isinstance(is_pinned, bool):
            return bool_res()[False]
        if notify_level is not None:
            try:
                notify_level = int(notify_level)
            except (TypeError, ValueError):
                return bool_res()[False]
        if alias is not None and (not isinstance(alias, str) or len(alias) > 100):
            return bool_res()[False]
        if description is not None and (not isinstance(description, str) or len(description) > 1000):
            return bool_res()[False]
        updated = messages_cursor.update_room_preference(
            uid, room_id, is_pinned=is_pinned, notify_level=notify_level,
            alias=alias, description=description
        )
        if updated:
            # 单用户多端同步：偏好变更静默推给该用户所有在线端（含发起端，应用幂等）
            try:
                current = messages_cursor.get_room_preference(uid, room_id)
                instant_contact.push_raw(uid, {
                    "type": "PREFERENCES.UPDATED",
                    "scope": "room",
                    "room_id": room_id,
                    "is_pinned": current["is_pinned"],
                    "notify_level": current["notify_level"],
                    "alias": current["alias"],
                    "description": current["description"],
                })
            except Exception:
                pass
        return bool_res()[updated]

    @api("/message/recall", methods=['POST'])
    def recall_message(req):
        try:
            uid = int(req["uid"])
            password = req["password"]
            mid = int(req["mid"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        operator = get_user_row(uid)
        message = messages_cursor.get_message(mid, include_recalled_original=True)
        if operator is None or message is None:
            return json.dumps({"success": False, "error": "not_found"})
        if message["deleted"]:
            return json.dumps({"success": False, "error": "already_recalled"})

        group_role = (group_cursor.is_admin(message["group_id"], uid)
                      if message["group_id"] is not None else 0)
        allowed = can_recall_message(uid, operator[4], message, group_role)
        if not allowed:
            return json.dumps({"success": False, "error": "forbidden"})
        if not messages_cursor.recall_message(mid, uid):
            return json.dumps({"success": False, "error": "already_recalled"})
        if message.get("file_hash"):
            file_cursor.remove_reference(message["file_hash"], "message", mid)
        # 撤回的若是群置顶消息，同步取消置顶，避免置顶残留
        if message.get("group_id") is not None:
            messages_cursor.unpin_message_by_mid(mid, message["group_id"])

        recalled = messages_cursor.get_message(mid)
        event = {
            "event": "message.recalled",
            "title": str(recalled["deleted_at"]),
            "content": None,
            "sender": uid,
            "mid": mid,
            "deleted": True,
            "deleted_at": recalled["deleted_at"],
            "deleted_by": uid,
            "group_id": recalled["group_id"],
            "room_seq": recalled.get("room_seq"),
            "room_id": (
                "G{}".format(recalled["group_id"])
                if recalled["group_id"] is not None
                else None
            ),
        }
        if recalled["group_id"] is not None:
            recipients = group_cursor.get_member_uids(recalled["group_id"])
        else:
            recipients = {recalled["sender_uid"], recalled["receiver_uid"]}
            for recipient in recipients:
                other_uid = (recalled["receiver_uid"]
                             if recipient == recalled["sender_uid"]
                             else recalled["sender_uid"])
                direct_event = dict(event)
                direct_event["room_id"] = "U{}".format(other_uid)
                instant_contact.push_recall(recipient, direct_event)
            recipients = []
        for recipient in recipients:
            instant_contact.push_recall(recipient, event)
        return json.dumps({
            "success": True,
            "message": recalled,
        }, ensure_ascii=False)

    @api("/message/recalled_original", methods=['POST'])
    def recalled_message_original(req):
        try:
            uid = int(req["uid"])
            password = req["password"]
            mid = int(req["mid"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if verify_root(uid, password) is None:
            return json.dumps({"success": False, "error": "forbidden"})
        message = messages_cursor.get_message(mid, include_recalled_original=True)
        if message is None or not message["deleted"]:
            return json.dumps({"success": False, "error": "not_found"})
        message["quote_preview"] = (
            messages_cursor.get_quote_preview(message["quote"], message)
            if message["quote"] >= 0 else None
        )
        if message.get("file_hash"):
            message["file"] = with_display_file_name(
                file_metadata(message["file_hash"], message["sender_uid"]),
                message.get("file_name"),
            )
        return json.dumps({"success": True, "message": message}, ensure_ascii=False)

    @api("/message/history", methods=['POST'])
    def message_history(req):
        """获取历史消息"""
        uid = req["uid"]
        password = req["password"]
        try:
            has_target = req.get("target_uid") is not None
            target_uid = int(req.get("target_uid", 0)) if has_target else 0
            group_id = req.get("group_id")
            before_mid = int(req.get("before_mid", 0))
            limit = max(1, min(int(req.get("limit", 50)), 200))
            if group_id is not None:
                group_id = int(group_id)
        except (ValueError, TypeError):
            return json.dumps({"success": False, "error": "invalid_request"})

        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None:
            return json.dumps({"success": False, "error": "invalid_request"})
        user_stat = user_row[4]
        if user_stat == 'banned':
            return json.dumps({"success": False, "error": "banned"})

        if group_id is not None:
            if not group_cursor.is_member(group_id, uid):
                settings = group_cursor.get_group_settings(group_id)
                if not settings or not settings.get("public_messages"):
                    return json.dumps({"success": False, "error": "not_group_member"})
                limit = min(limit, 50)
        elif has_target:
            if not user_cursor.is_friend(uid, target_uid):
                return json.dumps({"success": False, "error": "not_friends"})
        else:
            return json.dumps({"success": False, "error": "not_friends"})

        rows = messages_cursor.query_history(uid, target_uid,
            before_mid=before_mid, limit=limit, group_id=group_id)
        records = messages_cursor.serialize_rows(rows)
        return json.dumps(enrich_message_files(records), ensure_ascii=False)

    @api("/message/sync", methods=['POST'])
    def message_sync(req):
        """增量同步某房间消息（含缺口补拉）。

        请求体：
            room_id: "U<uid>" 或 "G<gid>"
            last_seq: 上次同步到的房间序号（可选，与 last_mid 二选一）
            last_mid: 上次同步到的 mid（旧客户端迁移用，可选）
            missing_sequences: [seq, ...]（可选，精确缺口）
            missing_sequence_ranges: [{"start_seq": s, "end_seq": e}, ...]（可选）
            capabilities: ["event_stream_v1", ...]（可选；声明后返回事件行，
                未声明时事件行被过滤，被过滤的 seq 表现为永久空洞）
            limit: 每批上限（默认 100，最大 200）

        返回：
            {"messages": [...], "current_seq": int, "has_more": bool}
        """
        try:
            uid = int(req["uid"])
            password = req["password"]
            room_id = str(req["room_id"])
            last_seq = int(req.get("last_seq", 0))
            last_mid = int(req.get("last_mid", 0))
            limit = max(1, min(int(req.get("limit", 100)), SYNC_MAX_LIMIT))
            capabilities = req.get("capabilities")
            include_events = (
                isinstance(capabilities, list)
                and "event_stream_v1" in capabilities
            )
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        user_row = get_user_row(uid)
        if user_row is None or user_row[4] == 'banned':
            return json.dumps({"success": False, "error": "banned"})

        if room_id.startswith('G'):
            gid = int(room_id[1:])
            if not group_cursor.is_member(gid, uid):
                return json.dumps({"success": False, "error": "not_group_member"})
            room_key = messages_cursor.room_key_of(uid, 0, group_id=gid)
        elif room_id.startswith('U'):
            target_uid = int(room_id[1:])
            if not user_cursor.is_friend(uid, target_uid):
                return json.dumps({"success": False, "error": "not_friends"})
            room_key = messages_cursor.room_key_of(uid, target_uid)
        else:
            return json.dumps({"success": False, "error": "invalid_request"})

        try:
            missing_sequences = parse_sync_missing_sequences(req)
            if missing_sequences is None:
                return json.dumps({"success": False, "error": "invalid_request"})

            after_seq = last_seq
            if after_seq < 0 or last_mid < 0:
                return json.dumps({"success": False, "error": "invalid_request"})
            if after_seq <= 0 and last_mid > 0:
                after_seq = messages_cursor.sync_after_seq_for_mid(room_key, last_mid)
                if after_seq is None:
                    return json.dumps({"success": False, "error": "not_found"})

            incremental_records = messages_cursor.query_sync(
                room_key,
                after_seq=after_seq,
                limit=limit + 1,
                include_events=include_events,
            )
            has_more = len(incremental_records) > limit
            records = incremental_records[:limit]
            if missing_sequences:
                missing_records = messages_cursor.query_missing_sequences(
                    room_key, missing_sequences, include_events=include_events)
                by_mid = {r["mid"]: r for r in records}
                for r in missing_records:
                    if r["mid"] not in by_mid:
                        records.append(r)
                        by_mid[r["mid"]] = r
                records.sort(key=lambda r: (r.get("room_seq") or 0, r.get("mid") or 0))

            current_seq = messages_cursor.current_room_seq(room_key)
            return json.dumps({
                "messages": enrich_message_files(records),
                "current_seq": current_seq,
                "has_more": has_more,
            }, ensure_ascii=False)
        except Exception as e:
            print("[WARN] message_sync failed: {}".format(e))
            return json.dumps({"success": False, "error": "unavailable"})

    @api("/message/read_receipt", methods=['POST'], feature=("chat", "read_receipt"))
    def read_receipt(req):
        """按需查询某条消息的已读情况（不推送、不持久化）。

        请求体：room_id ("U<uid>" | "G<gid>")、mid。
        私聊：返回对方是否已读到该消息（对方关闭已读则 hidden=true）。
        群聊：返回「N 人已读」计数（成员 ∩ 非发送者 ∩ 未关闭已读，不暴露他人水位）。
        """
        try:
            uid = int(req["uid"])
            password = req["password"]
            room_id = str(req["room_id"])
            mid = int(req["mid"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if len(room_id) < 2:
            return json.dumps({"success": False, "error": "invalid_request"})
        try:
            target_id = int(room_id[1:])
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})

        if room_id.startswith('U'):
            if not user_cursor.is_friend(uid, target_id):
                return json.dumps({"success": False, "error": "forbidden"})
            room_key = messages_cursor.room_key_of(uid, target_id)
            cursor = messages_cursor.sync_cursor_for_mid(room_key, mid)
            if cursor is None or cursor[0] is None:
                return json.dumps({"success": False, "error": "not_found"})
            peer_flag = user_cursor.get_show_status_flags([target_id]) \
                .get(target_id, {}).get("show_read_status", True)
            if not peer_flag:
                return json.dumps({
                    "success": True, "room_id": room_id, "mid": mid, "hidden": True,
                }, ensure_ascii=False)
            state = messages_cursor.get_read_state(target_id, room_key)
            peer_seq = state["last_read_seq"] if state else 0
            peer_mid = state["last_read_mid"] if state else 0
            return json.dumps({
                "success": True,
                "room_id": room_id,
                "mid": mid,
                "hidden": False,
                "read": peer_seq >= cursor[0],
                "reader_last_seq": peer_seq,
                "reader_last_mid": peer_mid,
            }, ensure_ascii=False)

        if room_id.startswith('G'):
            gid = target_id
            if not group_cursor.is_member(gid, uid):
                return json.dumps({"success": False, "error": "forbidden"})
            room_key = messages_cursor.room_key_of(0, 0, group_id=gid)
            cursor = messages_cursor.sync_cursor_for_mid(room_key, mid)
            if cursor is None or cursor[0] is None:
                return json.dumps({"success": False, "error": "not_found"})
            message = messages_cursor.get_message(mid, include_recalled_original=True)
            sender_uid = message.get("sender_uid") if message else None
            members = set(group_cursor.get_member_uids(gid))
            candidates = [
                s["uid"] for s in messages_cursor.read_states_for_room(room_key, cursor[0])
                if s["uid"] in members and s["uid"] != sender_uid
            ]
            flags = user_cursor.get_show_status_flags(candidates)
            count = sum(
                1 for u in candidates
                if flags.get(u, {}).get("show_read_status", True)
            )
            return json.dumps({
                "success": True,
                "room_id": room_id,
                "mid": mid,
                "hidden": False,
                "read_count": count,
            }, ensure_ascii=False)

        return json.dumps({"success": False, "error": "invalid_request"})

    @api("/friend/presence", methods=['POST'], feature=("presence",))
    def friend_presence(req):
        """批量查询好友在线状态（不推送、不持久化）。

        请求体：uid、password、可选 uids（缺省=全部好友）。
        取「请求集合 ∩ 好友」，剔除已关闭在线状态的用户，返回 {"presence": {"<uid>": "online"|"offline"}}。
        天然防止拿任意 uid 探测：非好友的 uid 不返回任何状态。
        """
        try:
            uid = int(req["uid"])
            password = req["password"]
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if not check_rate_limit(uid, "friend_presence", 30):
            return json.dumps({"success": False, "error": "rate_limited"})
        try:
            friends = set(user_cursor.list_friend_uids(uid))
        except Exception:
            friends = set()
        raw = req.get("uids")
        if raw is None:
            targets = friends
        elif isinstance(raw, list):
            try:
                targets = {int(u) for u in raw} & friends
            except (TypeError, ValueError):
                return json.dumps({"success": False, "error": "invalid_request"})
        else:
            return json.dumps({"success": False, "error": "invalid_request"})
        targets = list(targets)
        flags = user_cursor.get_show_status_flags(targets)
        online = set(instant_contact.filter_online(targets))
        presence = {
            str(u): ("online" if u in online else "offline")
            for u in targets
            if flags.get(u, {}).get("show_online_status", True)
        }
        return json.dumps({"success": True, "presence": presence}, ensure_ascii=False)

    @api("/friend/add_friend", methods=['POST'], feature=("chat", "friend_request"))
    def add_friend(req):
        uid = req["uid"]
        password = req["password"]
        added = req["added"]
        req_word = str(req.get("req_word", "") or "").strip()
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if not user_cursor.uid_query(added):
            return json.dumps({"success": False, "error": "not_found"})
        max_len = read_config().get("max_request_message_length", 200)
        if max_len > 0 and len(req_word) > max_len:
            return json.dumps({"success": False, "error": "request_message_too_long"})
        result = user_cursor.pending_friend(uid, added, uid, message=req_word)
        if result == 'blocked':
            return json.dumps({"success": False, "error": "friend_blocked"})
        if result in ('created', 'flipped'):
            if result == 'flipped':
                # 对方已有一条旧申请通知，翻转后它不再可处理，先清掉再发新的
                try:
                    notification_cursor.delete_events_by_sender(added, "friend.request", uid)
                except Exception as e:
                    print("[WARN] add_friend 清理旧申请通知失败: {}".format(e))
            notify_user(added, "friend.request", "新的好友申请",
                        "{} 请求添加你为好友。".format(format_user_display(uid)),
                        sender=uid, meta={"message": req_word})
        # 'updated' 不重发通知；'created'/'flipped' 返回成功；None（已是好友）返回失败
        return bool_res()[result is not None]

    @api("/friend/requests", methods=['POST'])
    def friend_requests(req):
        """查看发给当前用户的待处理好友申请（含最新留言）。"""
        uid = req["uid"]
        password = req["password"]
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        requests = user_cursor.list_pending_requests(uid)
        if requests:
            uids = [item["uid"] for item in requests]
            placeholders = ",".join("?" * len(uids))
            rows = user_cursor.query(
                "SELECT uid, username, sign FROM users WHERE uid IN ({})".format(placeholders),
                tuple(uids),
            )
            info_map = {r[0]: (r[1], r[2] or "") for r in rows}
            for item in requests:
                username, sign = info_map.get(item["uid"], ("User {}".format(item["uid"]), ""))
                item["username"] = username
                item["sign"] = sign
        return json.dumps(requests, ensure_ascii=False)

    @api("/friend/block", methods=['POST'])
    def block_friend(req):
        """拉黑用户"""
        uid = req["uid"]
        password = req["password"]
        target = req.get("target")
        if not verify_user(uid, password):
            return bool_res()[False]
        if target is None or not user_cursor.uid_query(target):
            return bool_res()[False]
        if not user_cursor.set_blocked(uid, target, uid):
            return bool_res()[False]
        # 双方可能残留的好友申请通知都不可再处理，清掉避免误导
        try:
            notification_cursor.delete_events_by_sender(uid, "friend.request", target)
            notification_cursor.delete_events_by_sender(target, "friend.request", uid)
        except Exception as e:
            print("[WARN] block 清理申请通知失败: {}".format(e))
        return bool_res()[True]

    @api("/friend/unblock", methods=['POST'])
    def unblock_friend(req):
        """解除拉黑"""
        uid = req["uid"]
        password = req["password"]
        target = req.get("target")
        if not verify_user(uid, password):
            return bool_res()[False]
        if target is None or not user_cursor.uid_query(target):
            return bool_res()[False]
        if not user_cursor.unset_blocked(uid, target, uid):
            return bool_res()[False]
        return bool_res()[True]

    @api("/friend/relationship", methods=['POST'])
    def friend_relationship(req):
        """查询「我」与目标的当前关系（含拉黑方向）。

        资料页据此显示「拉黑」还是「解除拉黑」：`friend_blocked` 只在申请时告诉
        我们「存在拉黑」，分不清是谁拉黑了谁。
        """
        try:
            uid = int(req["uid"])
            password = req["password"]
            target = int(req["target"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if uid == target or not user_cursor.uid_query(target):
            return json.dumps({"success": False, "error": "invalid_request"})

        relationship = 'none'
        blocked_by_me = False
        blocked_by_peer = False
        rows = user_cursor.query_relationship(uid, target)
        if rows:
            row = rows[0]
            # row: (user1, user2, relationship, adder, blocked_by_user1, blocked_by_user2)
            relationship = row[2]
            lo = row[0]
            blocked1 = bool(row[4])
            blocked2 = bool(row[5])
            blocked_by_me = blocked1 if uid == lo else blocked2
            blocked_by_peer = blocked2 if uid == lo else blocked1
        return json.dumps({
            "success": True,
            "relationship": relationship,
            "is_friend": relationship == "friend",
            "blocked_by_me": blocked_by_me,
            "blocked_by_peer": blocked_by_peer,
        }, ensure_ascii=False)

    @api("/friend/blocked", methods=['POST'])
    def blocked_list(req):
        """我拉黑的人（「拉黑列表」用，可逐条解除）。"""
        try:
            uid = int(req["uid"])
            password = req["password"]
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not verify_user(uid, password):
            return json.dumps({"success": False, "error": "auth_failed"})
        if not check_rate_limit(uid, "friend_blocked", 30):
            return json.dumps({"success": False, "error": "rate_limited"})
        return json.dumps({
            "success": True,
            "blocked": user_cursor.list_blocked_by(uid),
        }, ensure_ascii=False)

    @api("/friend/deal_ship", methods=['POST'])
    def deal_ship(req):
        uid = req["uid"]
        password = req["password"]
        dealt = req["dealt"]
        stat = req["stat"]
        if not verify_user(uid, password):
            return bool_res()[False]
        if stat not in ("allow", "reject"):
            return bool_res()[False]
        relationship = user_cursor.query_relationship(uid, dealt)
        if not relationship:
            return bool_res()[False]
        rel = relationship[0]
        # rel: (user1, user2, relationship, adder, blocked_by_user1, blocked_by_user2)
        if rel[3] != dealt:
            return bool_res()[False]
        # 只有申请接收方（非添加方）可以处理，防止申请方自行通过
        receiver = rel[1] if rel[3] == rel[0] else rel[0]
        if uid != receiver:
            return bool_res()[False]
        # 仅 pending 状态可处理；已成为好友后不允许重复同意/拒绝（拒绝会误伤已有关系）
        if rel[2] != 'pending':
            return bool_res()[False]
        if stat == "allow":
            succeeded = user_cursor.change_relationship(uid, dealt, 'friend')
            if succeeded:
                notify_user(dealt, "friend.accepted", "好友申请已通过",
                            "{} 已通过你的好友申请。".format(format_user_display(uid)), sender=uid)
        else:
            user_cursor.delete_relationship(uid, dealt)
            succeeded = True
        # 申请已处理，删除接收方的 friend.request 通知，避免客户端重复展示通过/拒绝按钮
        if succeeded:
            try:
                notification_cursor.delete_events_by_sender(uid, "friend.request", dealt)
            except Exception as e:
                print("[WARN] deal_ship 清理 friend.request 通知失败: {}".format(e))
        return bool_res()[succeeded]
    
    return app

# pri, pub, pri_pem, pub_pem, has = generate_rsa_keys()
# with open("res/7001/secret/pub.pem", "wb") as file:
#     file.write(pub_pem)
# usr_obj = UserDb("res/7001/db/user.db", 7001, 1145)
# app = main(7001, 1145, pub_pem, pri, usr_obj)
