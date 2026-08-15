from flask import Flask, send_file, request as flask_request
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import register_tool
import base64
import binascii
import os
from db import *
import avatar
import file

import announcements
from crypto import generate_rsa_keys, return_app_route
from rate_limiter import RateLimiter
from mention_utils import resolve_mentioned_uids, should_alert
import time
import threading
from json_store import read_json, update_json
from datetime import datetime
from file_types import detect_file_type, is_sticker_type

def bool_res() -> tuple: 
    return (str(time.time()) + "False", str(time.time()) + "True")


def can_recall_message(operator_uid : int, operator_auth : str, message : dict,
                       group_role : int = 0) -> bool:
    if message["sender_uid"] == operator_uid:
        return True
    if message["group_id"] is not None:
        return operator_auth in {"admin", "root"} or group_role >= 1
    return operator_auth in {"admin", "root"}

def main(port_api : int, port_tcp : int, pub_pem, pri, ImgCaptcha, user_cursor, forum_cursor, file_cursor, notification_cursor, messages_cursor, group_cursor, instant_contact, sticker_cursor=None):
    """
    pri 是 cryptography 库的私钥对象
    pub_pem 是二进制 pem 文件路径
    ImgCaptcha 是 captcha.ImageCaptcha 对象
    ~_cursor 表示 db.tool.Db 对象
    """
    app = Flask(__name__)

    @app.after_request
    def allow_cross_origin_requests(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = flask_request.headers.get(
            "Access-Control-Request-Headers", "Content-Type, Authorization"
        )
        return response

    api = return_app_route(app, pri)
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

    _config_cache = None
    def read_config():
        nonlocal _config_cache
        if _config_cache is not None:
            return dict(_config_cache)
        with locks['config']:
            cfg = read_json("res/{}/config.json".format(port_api))
            _config_cache = dict(cfg)
            return dict(_config_cache)

    group_cursor._config_reader = read_config

    _base_wsgi_app = app.wsgi_app
    def apply_proxy_fix(cfg):
        if cfg.get("reverse_proxy_enabled", False):
            x_for = int(cfg.get("proxy_count", 1))
            app.wsgi_app = ProxyFix(_base_wsgi_app, x_for=x_for, x_proto=1)
        else:
            app.wsgi_app = _base_wsgi_app

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
            sent = set()
            records = []
            for target_uid in target_uids:
                if target_uid in sent or target_uid is None:
                    continue
                sent.add(target_uid)
                record = notify_user(target_uid, event, title, content, sender, meta)
                if record is not None:
                    records.append(record)
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
        nonlocal _config_cache
        with locks['config']:
            state = {}
            def apply(cfg):
                mutator(cfg)
                state["cfg"] = dict(cfg)
            update_json("res/{}/config.json".format(port_api), apply)
            _config_cache = state["cfg"]
            return dict(_config_cache)

    def serialize_server_settings(cfg, include_manage=False):
        ret = {
            "server_name" : cfg.get("server_name", "TouchFish"),
            "port_api" : port_api,
            "port_tcp" : port_tcp,
            "captcha" : bool(cfg.get("captcha", False)),
            "file_last_time" : cfg.get("file_last_time", 72),
            "groups_limit" : cfg.get("groups_limit", 30),
            "single_group_max_people" : cfg.get("single_group_max_people", 200),
            "max_file_size" : cfg.get("max_file_size", -1),
            "max_avatar_size" : cfg.get("max_avatar_size", cfg.get("max_file_size", -1)),
            "user_storage_quota" : cfg.get("user_storage_quota", -1),
            "max_message_length" : cfg.get("max_message_length", 10000),
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
            "default_asset_urls" : {
                "logo" : "/avatar/get_logo",
                "forum" : "/avatar/get_default/forum",
                "user" : "/avatar/get_default/user",
                "group" : "/avatar/get_default/group"
            }
        }
        if include_manage:
            ret["rate_limits"] = cfg.get("rate_limits", {})
            if cfg.get("email_activate"):
                ret["verify_email"] = cfg.get("email_activate")
            ret["smtp_host"] = cfg.get("smtp_host", "")
            ret["smtp_port"] = cfg.get("smtp_port", 465)
            ret["smtp_use_ssl"] = cfg.get("smtp_use_ssl", True)
            ret["reverse_proxy_enabled"] = cfg.get("reverse_proxy_enabled", False)
            ret["proxy_count"] = cfg.get("proxy_count", 1)
        return ret

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

    def decode_base64_payload(payload):
        if not isinstance(payload, str) or not payload.strip():
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
        payload = decode_base64_payload(pic_b64)
        if payload is None:
            return False
        max_avatar_size = read_upload_limit(cfg, "max_avatar_size")
        if max_avatar_size != -1 and len(payload) > max_avatar_size:
            return False
        return True

    def validate_file_upload(filename, file_b64, cfg):
        normalized_name = normalize_upload_filename(filename)
        if normalized_name is None:
            return None, None
        payload = decode_base64_payload(file_b64)
        if payload is None:
            return None, None
        max_file_size = read_upload_limit(cfg, "max_file_size")
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

    def get_comment_thread(comments : dict, fid, pid):
        forum_comments = comments.get(str(fid))
        if not isinstance(forum_comments, dict):
            return None
        post_comments = forum_comments.get(str(pid))
        if not isinstance(post_comments, dict):
            return None
        return post_comments

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
        if not user_cursor.verify_user(uid, pwd):
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

    def file_metadata(file_hash, owner_uid=None):
        return file_cursor.get_metadata(file_hash, owner_uid=owner_uid) if file_hash else None

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
        result = []
        for row in rows:
            item = {
                "fid": row[0], "pid": row[1], "title": row[2],
                "creater": row[3], "author_uid": row[3],
                "content": row[4], "send_time": row[5],
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

    def retain_cross_db_file_references(uid, only_hash=None):
        for row in file_cursor.get_user_files(uid):
            hashes = row[0]
            if only_hash is not None and hashes != only_hash:
                continue
            reference_count = (
                messages_cursor.count_file_references(hashes)
                + forum_cursor.count_file_references(hashes)
            )
            if reference_count:
                file_cursor.ensure_content_retained(hashes, reference_count)

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

        retain_cross_db_file_references(target_uid)
        file.clean_user_files(port_api, target_uid, file_cursor)
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

        notify_user(target_uid, "auth.stat.changed", "账号状态已变更", "你的账号状态已更新为 {}。".format(new_auth), sender=uid, meta={"new_auth" : new_auth})
        return True
    
    @app.before_request
    def check_rate_limit():
        ip = flask_request.remote_addr
        endpoint = flask_request.path
        if not limiter.is_allowed(ip, endpoint):
            return "Too Many Requests", 429

    @app.route("/get_rsa_pub")
    def get_rsa_key():
        return send_file("res/{}/secret/pub.pem".format(port_api), download_name="{}.pem".format(port_api))

    @api("/auth/login", methods=["POST"])
    def login(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            return bool_res()[user_cursor.verify_user(uid, pwd)]
        except Exception:
            return bool_res()[False]
    

    @api("/auth/change_pwd", methods=["POST"])
    def change_pwd(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            new_pwd = req["new_pwd"]
            if not user_cursor.verify_user(uid, pwd):
                return bool_res()[False]
            user_cursor.change_pwd(uid, new_pwd)
            return bool_res()[True]
        except Exception:
            return bool_res()[False]

    @app.route('/auth/captcha')
    def get_captcha():
        captcha = read_config().get("captcha", False)
        if not captcha:
            return {}
        token = register_tool.generate_captcha(port_api, ImgCaptcha, locks['captcha'])
        file_path = 'res/{}/captcha/{}.png'.format(port_api, token)
        with open(file_path, "rb") as file:
            ret_b64 = file.read()
        ret_b64 = base64.b64encode(ret_b64).decode("utf-8")
        return {"pic" : ret_b64, "stamp" : token}
    
    @api("/auth/change_email_verify", methods=['POST'])
    def change_email_verify(req):
        uid = req["uid"]
        pwd = req["password"]

        if not user_cursor.verify_user(uid, pwd):
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
            "email" : info[0][2],
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
            "email" : info[0][2],
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
        if not user_cursor.verify_user(uid, pwd):
            return bool_res()[False]
        new_email = req["new_email"]
        return bool_res()[user_cursor.change_email(uid, new_email)]

    @api("/auth/register", methods=['POST'])
    def register(req):
        username = req["username"]
        password = req["password"]
        cfg = read_config()
        is_captcha = cfg.get("captcha", False)
        is_email_activate = cfg.get("email_activate", "")
        if (not isinstance(username, str) or len(username) < int(cfg.get("min_username_length", 4))
                or not isinstance(password, str) or len(password) < int(cfg.get("min_password_length", 1))):
            return bool_res()[False]
        
        if is_captcha:
            captcha_stamp = req["captcha_stamp"]
            captcha_code = req["captcha_code"]
            if not register_tool.verify_captcha(port_api, captcha_stamp, captcha_code, locks['captcha']):
                return bool_res()[False]
        
        email = None
        if "email" in req.keys():
            email = req["email"] 
        
        if is_email_activate:
            sender_email = cfg["email_activate"]
            if not email:
                return bool_res()[False]
            email_pwd = cfg["email_password"]
            if not register_tool.email_code(sender_email, port_api, email, email_pwd, locks['config'], locks['activate']):
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
                if not email in json.load(file).keys():
                    return bool_res()[True]
        if register_tool.verify_email(port_api, email, activate_code, locks['activate']):
            user_cursor.change_auth(uid, "user")
            return bool_res()[True]
        return bool_res()[False]
     

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
                return bool_res()[False]

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
            return bool_res()[False]
    
    @api("/auth/change_sign", methods=['POST'])
    def change_sign(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        new_intro = req["new_introduction"]
        # #16: 校验简介长度
        max_intro = read_config().get("max_introduction_length", 500)
        if max_intro > 0 and len(str(new_intro)) > max_intro:
            return bool_res()[False]
        user_cursor.change_introduction(uid, new_intro)
        return bool_res()[True]
    
    @api("/auth/change_captcha", methods=['POST'])
    def change_captcha(req):
        uid = req["uid"]
        pwd = req["password"]
        final_stat = req["change_to"]
        if not user_cursor.verify_user(uid, pwd):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None or user_row[4] != 'root':
            return bool_res()[False]
        update_config(lambda cfg: cfg.__setitem__("captcha", final_stat))
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
        if not user_cursor.verify_user(uid, pwd):
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
                return bool_res()[False]
            return json.dumps(serialize_server_settings(read_config(), include_manage=True), ensure_ascii=False)
        except Exception:
            return bool_res()[False]

    @api("/auth/server_settings/update", methods=['POST'])
    def update_server_settings(req):
        try:
            uid = req["uid"]
            pwd = req["password"]
            if verify_root(uid, pwd) is None:
                return bool_res()[False]

            updates = {}

            if "server_name" in req:
                server_name = req["server_name"]
                if not isinstance(server_name, str):
                    return bool_res()[False]
                server_name = server_name.strip()
                if not server_name:
                    return bool_res()[False]
                updates["server_name"] = server_name

            if "captcha" in req:
                if not isinstance(req["captcha"], bool):
                    return bool_res()[False]
                updates["captcha"] = req["captcha"]

            if "file_last_time" in req:
                updates["file_last_time"] = parse_int_setting(req["file_last_time"], minimum=0)

            if "groups_limit" in req:
                updates["groups_limit"] = parse_int_setting(req["groups_limit"], minimum=1, allow_unlimited=True)

            if "single_group_max_people" in req:
                updates["single_group_max_people"] = parse_int_setting(req["single_group_max_people"], minimum=1, allow_unlimited=True)

            if "max_file_size" in req:
                updates["max_file_size"] = parse_int_setting(req["max_file_size"], minimum=0, allow_unlimited=True)

            if "max_avatar_size" in req:
                updates["max_avatar_size"] = parse_int_setting(req["max_avatar_size"], minimum=0, allow_unlimited=True)

            if "user_storage_quota" in req:
                updates["user_storage_quota"] = parse_int_setting(req["user_storage_quota"], minimum=0, allow_unlimited=True)

            if "max_message_length" in req:
                updates["max_message_length"] = parse_int_setting(req["max_message_length"], minimum=1)

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
                    return bool_res()[False]
                updates["smtp_host"] = req["smtp_host"].strip()

            if "smtp_port" in req:
                updates["smtp_port"] = parse_int_setting(req["smtp_port"], minimum=1)

            if "smtp_use_ssl" in req:
                if not isinstance(req["smtp_use_ssl"], bool):
                    return bool_res()[False]
                updates["smtp_use_ssl"] = req["smtp_use_ssl"]

            if "reverse_proxy_enabled" in req:
                if not isinstance(req["reverse_proxy_enabled"], bool):
                    return bool_res()[False]
                updates["reverse_proxy_enabled"] = req["reverse_proxy_enabled"]

            if "proxy_count" in req:
                updates["proxy_count"] = parse_int_setting(req["proxy_count"], minimum=0)

            if not updates:
                return bool_res()[False]

            def _apply(current):
                current.update(updates)
            cfg = update_config(_apply)
            # 重新加载 ws
            if "max_message_length" in updates:
                instant_contact._load_config(cfg)
            # 反代配置变更时重新包装 wsgi 中间件
            if "reverse_proxy_enabled" in updates or "proxy_count" in updates:
                apply_proxy_fix(cfg)
            return json.dumps(serialize_server_settings(cfg, include_manage=True), ensure_ascii=False)
        except Exception:
            return bool_res()[False]

    @api("/notification/query_all", methods=['POST'])
    def query_all_notifications(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        return serialize_notifications(notification_cursor.query_all_events(uid))

    @api("/notification/query_after", methods=['POST'])
    def query_notifications_after(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        return bool_res()[notification_cursor.delete_all_events(uid)]

    @api("/auth/mention_candidates", methods=['POST'])
    def mention_candidates(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        rows = user_cursor.query(
            "SELECT uid, username FROM users WHERE stat != 'banned' ORDER BY username"
        )
        return json.dumps([
            {"uid": row[0], "username": row[1]} for row in rows if row[0] != uid
        ], ensure_ascii=False)

    @app.route("/info")
    def info():
        return serialize_server_settings(read_config())

    def _sticker_item_dict(row):
        return {
            "id": row[0], "pack_id": row[1], "slug": row[2], "name": row[3],
            "file_hash": row[4], "file_type": row[5], "order": row[6],
            "size": row[7], "mode": row[8], "created_at": row[9],
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

    def _hash_file_type(file_hash):
        target = file.file_path(port_api, file_hash)
        try:
            with open(target, "rb") as handle:
                return detect_file_type(handle.read(), "")
        except OSError:
            return "unknown"

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
        if sticker_cursor is None or not user_cursor.verify_user(uid, password):
            return json.dumps({"error": "auth_failed"})
        owned = []
        for row in sticker_cursor.list_owned(uid):
            pack = _sticker_pack_dict(row[2:], include_stickers=True)
            owned.append({"pack_id": row[0], "order": row[1], "pack": pack})
        return json.dumps(owned, ensure_ascii=False)

    @api("/sticker/created", methods=["POST"])
    def sticker_created(req):
        uid, password = req.get("uid"), req.get("password")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password):
            return json.dumps({"error": "auth_failed"})
        rows, total = sticker_cursor.list_packs(0, 100, creator_uid=uid)
        return json.dumps({"items": [_sticker_pack_dict(row) for row in rows], "total": total}, ensure_ascii=False)

    @api("/sticker/pack/create", methods=["POST"])
    def create_sticker_pack(req):
        uid, password = req.get("uid"), req.get("password")
        name, prefix = req.get("name"), req.get("prefix")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password) or not isinstance(name, str) or not name.strip() or not isinstance(prefix, str) or not prefix.strip():
            return json.dumps({"success": False, "error": "invalid_request"})
        cfg = read_config()
        local_day = datetime.now().date().isoformat()
        pack_id, error = sticker_cursor.create_pack(uid, name.strip(), str(req.get("description", "")).strip(), prefix.strip(), local_day, int(cfg.get("max_sticker_packs_per_user", 24)), int(cfg.get("daily_sticker_pack_creation_limit", -1)), _sticker_exempt(uid))
        if error:
            return json.dumps({"success": False, "error": error})
        return json.dumps({"success": True, "pack": _sticker_pack_dict(sticker_cursor.get_pack(pack_id))}, ensure_ascii=False)

    @api("/sticker/item/create", methods=["POST"])
    def create_sticker_item(req):
        uid, password = req.get("uid"), req.get("password")
        pack_id, slug, file_hash = req.get("pack_id"), req.get("slug"), req.get("file_hash")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password) or not all(isinstance(value, str) and value for value in (pack_id, slug, file_hash)):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not file_cursor.has_active_user_file(uid, file_hash):
            return json.dumps({"success": False, "error": "file_not_owned"})
        try:
            file_type = _hash_file_type(file_hash)
        except OSError:
            return json.dumps({"success": False, "error": "file_unavailable"})
        try:
            sticker_path = file.file_path(port_api, file_hash)
            sticker_bytes = open(sticker_path, "rb").read() if os.path.isfile(sticker_path) else b""
        except OSError:
            return json.dumps({"success": False, "error": "file_unavailable"})
        if not is_sticker_type(sticker_bytes):
            return json.dumps({"success": False, "error": "unsupported_sticker_type"})
        cfg = read_config()
        max_sticker_size = cfg.get("max_sticker_size", 1048576)
        if max_sticker_size != -1 and len(sticker_bytes) > max_sticker_size:
            return json.dumps({"success": False, "error": "sticker_too_large"})
        sticker_id, error = sticker_cursor.create_sticker(uid, pack_id, slug.strip(), req.get("name"), file_hash, file_type, int(req.get("size", 0)), int(req.get("mode", 0)), int(cfg.get("max_stickers_per_pack", 24)), _sticker_exempt(uid))
        if error:
            return json.dumps({"success": False, "error": error})
        item = sticker_cursor.query("SELECT id, pack_id, slug, name, file_hash, file_type, position, render_size, render_mode, created_at FROM stickers WHERE id = ?", (sticker_id,))[0]
        file_cursor.add_reference(file_hash, "sticker", sticker_id, uid)
        return json.dumps({"success": True, "sticker": _sticker_item_dict(item)}, ensure_ascii=False)

    @api("/sticker/pack/update", methods=["POST"])
    def update_sticker_pack(req):
        uid, password, pack_id = req.get("uid"), req.get("password"), req.get("pack_id")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password) or not isinstance(pack_id, str) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
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

    @api("/sticker/pack/delete", methods=["POST"])
    def delete_sticker_pack(req):
        uid, password, pack_id = req.get("uid"), req.get("password"), req.get("pack_id")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password) or not isinstance(pack_id, str) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
            return json.dumps({"success": False, "error": "forbidden"})
        hashes = sticker_cursor.delete_pack(pack_id)
        for sticker_id, file_hash in hashes:
            file_cursor.remove_reference(file_hash, "sticker", sticker_id)
        return json.dumps({"success": True})

    @api("/sticker/item/delete", methods=["POST"])
    def delete_sticker_item(req):
        uid, password, pack_id, sticker_id = req.get("uid"), req.get("password"), req.get("pack_id"), req.get("sticker_id")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password) or not all(isinstance(value, str) for value in (pack_id, sticker_id)) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
            return json.dumps({"success": False, "error": "forbidden"})
        file_hash = sticker_cursor.delete_sticker(pack_id, sticker_id)
        if file_hash is None:
            return json.dumps({"success": False, "error": "not_found"})
        file_cursor.remove_reference(file_hash, "sticker", sticker_id)
        return json.dumps({"success": True})

    @api("/sticker/item/reorder", methods=["POST"])
    def reorder_sticker_items(req):
        uid, password, pack_id, ids = req.get("uid"), req.get("password"), req.get("pack_id"), req.get("sticker_ids")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password) or not isinstance(pack_id, str) or not isinstance(ids, list) or not all(isinstance(value, str) for value in ids) or not sticker_cursor.can_manage_pack(uid, pack_id, _sticker_exempt(uid)):
            return json.dumps({"success": False, "error": "forbidden"})
        return json.dumps({"success": sticker_cursor.reorder_stickers(pack_id, ids)})

    @api("/sticker/ownership/reorder", methods=["POST"])
    def reorder_sticker_ownership(req):
        uid, password, ids = req.get("uid"), req.get("password"), req.get("pack_ids")
        if (sticker_cursor is None or not user_cursor.verify_user(uid, password)
                or not isinstance(ids, list)
                or not all(isinstance(value, str) for value in ids)):
            return json.dumps({"success": False, "error": "invalid_request"})
        return json.dumps({"success": sticker_cursor.reorder_owned(uid, ids)})

    @api("/sticker/ownership", methods=["POST"])
    def sticker_ownership(req):
        uid, password, pack_id = req.get("uid"), req.get("password"), req.get("pack_id")
        if sticker_cursor is None or not user_cursor.verify_user(uid, password) or not isinstance(pack_id, str):
            return json.dumps({"success": False, "error": "invalid_request"})
        owned = bool(req.get("owned", True))
        return json.dumps({"success": sticker_cursor.set_owned(uid, pack_id, owned)})

    @api("/forum/create_forum", methods=["POST"])
    def create_forum(req):
        uid = req["uid"]
        password = req["password"]
        forum_name = req["forum_name"]
        introduction = req["introduction"]
        request_id = req.get("request_id")
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if not user_stat in ["admin", "root"]:
            return bool_res()[False]
        with locks['queue']:
            return json.dumps(read_json("res/{}/forum/queue.json".format(port_api)), ensure_ascii=False)
    
    @api("/forum/approve_forum", methods=["POST"])
    def approve_forum(req):
        uid = req["uid"]
        password = req["password"]
        qid = req["qid"]
        if not user_cursor.verify_user(uid, password):
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

    @api("/forum/reject_forum", methods=["POST"])
    def reject_forum(req):
        uid = req["uid"]
        password = req["password"]
        qid = req["qid"]
        reason = req.get("reason")
        if not isinstance(reason, str):
            reason = ""
        reason = reason.strip()
        if not user_cursor.verify_user(uid, password):
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
    
    @api("/forum/send_post", methods=["POST"])
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
        if not user_cursor.verify_user(uid, password):
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
        acquired = []
        attachment_records = []
        for hashes in attachment_hashes:
            metadata = file_cursor.acquire_reference(uid, hashes)
            if metadata is None:
                file.release_references(port_api, acquired, file_cursor)
                return bool_res()[False]
            acquired.append(hashes)
            attachment_records.append({
                "hash": hashes,
                "display_name": metadata["file_name"],
            })
        try:
            pid = forum_cursor.send_post(fid, uid, title, content, attachment_records)
        except Exception:
            file.release_references(port_api, acquired, file_cursor)
            raise
        if pid is False:
            file.release_references(port_api, acquired, file_cursor)
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
            posts = forum_cursor.query_all_post(fid_int)
            try:
                pinned_pid = forum_cursor.get_pinned_pid(fid_int)
            except forum_cursor.dialect.DatabaseError:
                pinned_pid = None
            return json.dumps({
                "posts": posts,
                "post_rows": serialize_post_rows(posts),
                "pinned_pid": pinned_pid,
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
    
    @api("/forum/remove_forum", methods=["POST"])
    def remove_forum(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not user_cursor.verify_user(uid, password):
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
    
    @api("/forum/edit_forum", methods=["POST"])
    def forum_edit_forum(req):
        """修改论坛信息，论坛创建者或管理员"""
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not user_cursor.verify_user(uid, password):
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

    @api("/forum/remove_post", methods=['POST'])
    def remove_post(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        pid = req["pid"]
        if not user_cursor.verify_user(uid, password):
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

    @api("/forum/pin_post", methods=["POST"])
    def pin_post(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        pid = req["pid"]
        if not user_cursor.verify_user(uid, password):
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

    @api("/forum/unpin_post", methods=["POST"])
    def unpin_post(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        forum_info = query_forum(fid)
        if not forum_info:
            return bool_res()[False]
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return bool_res()[False]
        rows = forum_cursor.list_members(fid)
        return json.dumps([list(row) for row in rows])

    @api("/forum/add_member", methods=["POST"])
    def forum_add_member(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        target_uid = req["target_uid"]
        role = req.get("role", 0)
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return bool_res()[False]
        if not user_cursor.uid_query(target_uid):
            return bool_res()[False]
        # 避免被管理员背刺
        if role >= operator_role and operator_role < 100:
            return bool_res()[False]
        return bool_res()[forum_cursor.add_member(fid, target_uid, role)]

    @api("/forum/remove_member", methods=["POST"])
    def forum_remove_member(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        target_uid = req["target_uid"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return bool_res()[False]
        target_role = forum_cursor.get_member_role(fid, target_uid)
        if target_role is not None and target_role >= operator_role:
            return bool_res()[False]
        return bool_res()[forum_cursor.remove_member(fid, target_uid)]

    @api("/forum/change_member_role", methods=["POST"])
    def forum_change_member_role(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        target_uid = req["target_uid"]
        new_role = req["new_role"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        operator_role = forum_cursor.get_member_role(fid, uid)
        if operator_role is None or operator_role < 50:
            return bool_res()[False]
        target_role = forum_cursor.get_member_role(fid, target_uid)
        if target_role is not None and target_role >= operator_role and operator_role < 100:
            return bool_res()[False]
        if new_role >= operator_role and operator_role < 100:
            return bool_res()[False]
        return bool_res()[forum_cursor.change_member_role(fid, target_uid, new_role)]

    @api("/forum/join", methods=["POST"])
    def forum_join(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not user_cursor.verify_user(uid, password):
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

    @api("/forum/leave", methods=["POST"])
    def forum_leave(req):
        uid = req["uid"]
        password = req["password"]
        fid = req["fid"]
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        rows = forum_cursor.query(
            "SELECT fid, role FROM forum_members WHERE uid = ?", (uid,)
        )
        return json.dumps([list(row) for row in rows])

    @api("/forum/comment", methods=["POST"])
    def comment(req):
        uid = req["uid"]
        if not isinstance(uid, int):
            return bool_res()[False]
        password = req["password"]
        fid = req["fid"]
        pid = req["pid"]
        comment : str = req["comment"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        comment_time = str(time.time())
        def add_comment(comments):
            thread = get_comment_thread(comments, fid, pid)
            if thread is None:
                return False
            thread[comment_time] = [uid, comment]
            return True

        if not update_comments(port_api, add_comment):
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
        fid = int(fid)
        pid = int(pid)
        comments = read_comments(port_api)
        thread = get_comment_thread(comments, fid, pid)
        if thread is None:
            return {}
        return thread
    
    @api("/forum/remove_comment", methods=['POST'])
    def remove_comment(req):
        uid = req["uid"]
        if not isinstance(uid, int):
            return bool_res()[False]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        fid = req["fid"]
        pid = req["pid"]
        time_stamp = req["send_time"]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        forum_role = forum_cursor.get_member_role(fid, uid)

        removed_creator = [None]
        def remove_comment_entry(comments):
            thread = get_comment_thread(comments, fid, pid)
            if thread is None or time_stamp not in thread:
                return False
            creater = thread[time_stamp][0]
            if not (creater == uid or user_stat in ['admin', 'root'] or
                    (forum_role is not None and forum_role >= 50)):
                return False
            removed_creator[0] = creater
            del thread[time_stamp]
            return True

        if not update_comments(port_api, remove_comment_entry):
            return bool_res()[False]
        if removed_creator[0] is not None and removed_creator[0] != uid:
            notify_user(
                removed_creator[0], "forum.comment.deleted", "你的评论已被删除",
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        cfg = read_config()
        normalized_name, payload = validate_file_upload(filename, file_b64, cfg)
        if normalized_name is None:
            return bool_res()[False]
        quota = cfg.get("user_storage_quota", -1)
        if quota != -1 and payload is not None:
            new_size = len(payload)
            new_hashes = file.sha256(payload)
            current_usage = file_cursor.get_user_storage_used(uid)
            if not file_cursor.has_active_user_file(uid, new_hashes):
                if current_usage + new_size > quota:
                    return bool_res()[False]
        try:
            hashes = file.upload_file(port_api, uid, file_b64, normalized_name, file_cursor,
                                      cfg.get("file_last_time", 72))
        except Exception:
            return bool_res()[False]
        return json.dumps({
            "success" : True,
            "result" : bool_res()[True],
            "hash" : hashes,
            "download_url" : "/file/get_file/{}".format(hashes),
            "info_url" : "/file/get_file_info/{}".format(hashes),
            "file" : file_metadata(hashes, uid),
        }, ensure_ascii=False)

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
            if not user_cursor.verify_user(uid, password):
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

            result = file.chunked_upload_file(
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

            # Enforce user storage quota on successful finalization.
            if result.get("success") and result.get("file_hash"):
                quota = cfg.get("user_storage_quota", -1)
                if quota != -1:
                    hashes = result["file_hash"]
                    meta = file_metadata(hashes, uid) or {}
                    new_size = int(meta.get("size") or meta.get("file_size") or 0)
                    current_usage = file_cursor.get_user_storage_used(uid)
                    # register_upload already counted this file; approximate by
                    # checking whether usage already includes it via has_active.
                    if new_size and current_usage > quota:
                        file.dereference_file(
                            port_api, uid, hashes, file_cursor,
                            cfg.get("file_last_time", 72),
                        )
                        return json.dumps(
                            {"success": False, "error": "Storage quota exceeded"},
                            ensure_ascii=False,
                        )
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

    @api('/file/dereference_file', methods=['POST'])
    def dereference_file(req):
        uid = req["uid"]
        password = req["password"]
        hashes = req["hash"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        retain_cross_db_file_references(uid, hashes)
        file_last_time = read_config().get("file_last_time", 72)
        return bool_res()[file.dereference_file(port_api, uid, hashes, file_cursor, file_last_time)]

    @api('/file/get_user_files', methods=['POST'])
    def get_user_files(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        rows = file_cursor.get_user_files(uid)
        result = []
        for row in rows:
            hashes = row[0]
            meta = file_cursor.get_metadata(hashes) or {}
            result.append({
                "hash" : hashes,
                "file_name" : row[1],
                "upload_time" : row[2],
                "size" : meta.get("size", 0),
                "ref_count" : 0,
                "upload_user_count" : 0,
                "mime_type" : "application/octet-stream",
                "file_type" : meta.get("file_type", "unknown"),
                "extension" : row[7] or "",
                "download_url" : "/file/get_file/{}".format(hashes),
            })
        return json.dumps(result, ensure_ascii=False)

    @api('/file/get_storage_info', methods=['POST'])
    def get_storage_info(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        retain_cross_db_file_references(uid, hashes)
        return bool_res()[file.delete_user_file(port_api, uid, hashes, file_cursor)]

    @api('/file/admin_get_all_files', methods=['POST'])
    def admin_get_all_files(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat not in ['admin', 'root']:
            return bool_res()[False]
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
                "size" : meta.get("size", 0),
                "ref_count" : 0,
                "upload_user_count" : 0,
                "sender" : "",
                "mime_type" : "application/octet-stream",
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
        if not user_cursor.verify_user(uid, password):
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
        ext = os.path.splitext(download_name)[1].lower()
        mimetype_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".svg": "image/svg+xml",
            ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
            ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
            ".pdf": "application/pdf", ".zip": "application/zip",
            ".tgs": "application/octet-stream",
        }
        return send_file(
            "res/{}/file/{}.file".format(port_api, hashes),
            download_name=download_name,
            as_attachment=True,
            mimetype=mimetype_map.get(ext),
        )
    
    @api("/announcement/upload_announcement", methods=['POST'])
    def upload_announcement(req):
        uid = req["uid"]
        password = req["password"]
        content = req["content"]
        if not user_cursor.verify_user(uid, password):
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
    
    @api("/announcement/edit_announcement", methods=['POST'])
    def edit_announcement(req):
        uid = req["uid"]
        password = req["password"]
        time_stamp = req["time_stamp"]
        content = req["content"]
        if not user_cursor.verify_user(uid, password):
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
    
    @api("/announcement/delete_announcement", methods=['POST'])
    def delete_announcement(req):
        uid = req["uid"]
        password = req["password"]
        time_stamp = req["time_stamp"]
        if not user_cursor.verify_user(uid, password):
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
 

    @api("/group/create_group", methods=['POST'])
    def create_group(req):
        uid = req["uid"]
        password = req["password"]
        groupname = req["groupname"]
        introduction = req.get("introduction", "")
        enter_hint = req.get("enter_hint", "")
        allow_direct_join = bool(req.get("allow_direct_join", False))
        require_review = bool(req.get("require_review", True))
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        cfg = read_config()
        min_len = cfg.get("min_group_name_length", 1)
        max_len = cfg.get("max_group_name_length", 50)
        if not isinstance(groupname, str) or not (min_len <= len(groupname.strip()) <= max_len):
            return bool_res()[False]
        gid = group_cursor.create_group(uid, groupname, enter_hint, introduction,
                                        allow_direct_join, require_review)
        if gid:
            return json.dumps({"gid": gid})
        return bool_res()[False]
    
    @app.route("/group/group_info/<gid>")
    def group_info(gid : str): 
        if not gid.isdigit():
            return {}
        qry = group_cursor.query_gid(gid)
        if len(qry) < 1:
            return {}
        return json.dumps(list(qry[0]), ensure_ascii=False)

    @app.route("/group/groupname_search/<groupname>")
    def groupname_search(groupname : str):
        return json.dumps(group_cursor.groupname_search(groupname), ensure_ascii=False)

    @api("/group/add_admin", methods=['POST'])
    def add_admin(req):
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
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
    #     if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        if not group_cursor.is_member(gid, uid):
            return bool_res()[False]
        return json.dumps({"essence" : group_cursor.query_essence(gid), "essence_enabled": group_cursor.get_essence_enabled(gid)})

    @api("/group/remove_member", methods=['POST'])
    def remove_member(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        removed = req["removed"]
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_admin(gid, uid):
            return bool_res()[False]
        return json.dumps(group_cursor.get_group_settings(gid), ensure_ascii=False)

    @api("/group/update_settings", methods=['POST'])
    def update_group_settings(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not user_cursor.verify_user(uid, password):
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
                     "allow_direct_join", "require_review", "essence_enabled"):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        if user_row[4] == 'banned':
            return bool_res()[False]
        gid = req["gid"]
        if not group_cursor.is_member(gid, uid):
            return bool_res()[False]
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_member(gid, uid):
            return bool_res()[False]
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

    @api("/group/join", methods=['POST'])
    def join_group(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        settings = group_cursor.get_group_settings(gid)
        if not settings:
            return bool_res()[False]
        if group_cursor.is_member(gid, uid):
            return bool_res()[False]
        if settings["allow_direct_join"]:
            if not settings["require_review"]:
                succeeded = group_cursor.add_member(gid, uid)
                if succeeded:
                    return json.dumps({"pending": False})
                return bool_res()[False]
            else:
                members = group_cursor.get_member_uids(gid)
                cfg = read_config()
                limit = cfg.get("single_group_max_people", 200)
                if limit != -1 and len(members) >= limit:
                    return bool_res()[False]
                rid = group_cursor.request_join(gid, uid)
                reviewers = [settings["creater"]] + group_cursor.get_admin_uids(gid)
                run_notification_side_effect("group.join.request",
                    lambda: notify_users(reviewers, "group.join.request",
                        "新的入群申请", "{} 申请加入群 {}。".format(format_user_display(uid), settings["groupname"]),
                        sender=uid, meta={"gid": gid, "rid": rid}))
                return json.dumps({"rid": rid, "pending": True})
        return bool_res()[False]

    @api("/group/invite", methods=['POST'])
    def invite_to_group(req):
        uid = req["uid"]
        password = req["password"]
        gid = req["gid"]
        invited_uid = req["invited_uid"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_member(gid, uid):
            return bool_res()[False]
        if not user_cursor.uid_query(invited_uid):
            return bool_res()[False]
        if not user_cursor.is_friend(uid, invited_uid):
            return bool_res()[False]
        if group_cursor.is_member(gid, invited_uid):
            return bool_res()[False]
        settings = group_cursor.get_group_settings(gid)
        is_admin = group_cursor.is_admin(gid, uid) >= 1
        if not settings["require_review"] or is_admin:
            succeeded = group_cursor.add_member(gid, invited_uid)
            if succeeded:
                run_notification_side_effect("group.invited",
                    lambda: notify_user(invited_uid, "group.invited", "你已被邀请加入群聊",
                        "{} 邀请你加入群 {}。".format(format_user_display(uid), settings["groupname"]),
                        sender=uid, meta={"gid": gid}))
                return json.dumps({"pending": False})
            return bool_res()[False]
        rid = group_cursor.request_join(gid, invited_uid, inviter_uid=uid)
        reviewers = [settings["creater"]] + group_cursor.get_admin_uids(gid)
        run_notification_side_effect("group.join.request",
            lambda: notify_users(reviewers, "group.join.request",
                "新的入群申请", "{} 邀请 {} 加入群 {}，等待审核。".format(format_user_display(uid), format_user_display(invited_uid), settings["groupname"]),
                sender=uid, meta={"gid": gid, "rid": rid}))
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]
        if not group_cursor.is_admin(gid, uid):
            return bool_res()[False]
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
        if not user_cursor.verify_user(uid, password):
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
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
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
            if quote < -1 or forwarded < -1 or (quote >= 0 and forwarded >= 0):
                return bool_res()[False]
            if content_type not in ("plain", "file"):
                return bool_res()[False]
            file_hash = None
            if content_type == "file":
                file_hash = str(req.get("file_hash") or content)
                if (len(file_hash) != 64
                        or not all(char in "0123456789abcdefABCDEF" for char in file_hash)):
                    return bool_res()[False]
                content = file_hash

            if not user_cursor.verify_user(uid, password):
                return bool_res()[False]
            user_row = get_user_row(uid)
            if user_row is None:
                return bool_res()[False]
            user_stat = user_row[4]
            if user_stat == 'banned':
                return bool_res()[False]
            file_record = None

            target_uid = 0
            group_id = None
            if recipient.startswith('U'):
                target_uid = int(recipient[1:])
            elif recipient.startswith('G'):
                group_id = int(recipient[1:])
            else:
                return bool_res()[False]

            source_message = None
            if forwarded >= 0:
                if not messages_cursor.verify_quote(
                        forwarded, uid, target_uid, group_id):
                    return bool_res()[False]
                source_message = messages_cursor.get_message(forwarded)
                if source_message is None:
                    return bool_res()[False]
                content_type = source_message["content_type"]
                content = source_message["content"]
                file_hash = source_message["file_hash"]

            existing = messages_cursor.get_by_client_mid(uid, client_mid)
            if existing is not None:
                if not messages_cursor.request_matches(
                        existing["mid"], uid, target_uid, content, content_type,
                        file_hash=file_hash, quote=quote, group_id=group_id,
                        forwarded=forwarded):
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
                return bool_res()[False]
            if group_id is None and not user_cursor.is_friend(uid, target_uid):
                return bool_res()[False]

            if content_type == "plain" and len(content) > read_config().get("max_message_length", 10000):
                return bool_res()[False]

            if quote >= 0:
                if not messages_cursor.verify_quote(quote, uid, target_uid, group_id):
                    return bool_res()[False]
                if group_id is not None and not group_cursor.is_member(group_id, uid):
                    return bool_res()[False]

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
                    return bool_res()[False]
            try:
                msg_record = messages_cursor.add_message(
                    uid, target_uid, content,
                    content_type=content_type, file_hash=file_hash,
                    quote=quote, group_id=group_id, client_mid=client_mid,
                    file_name=file_record["file_name"] if file_record else None,
                    forwarded=forwarded,
                )
            except Exception:
                if file_hash:
                    file.release_references(port_api, [file_hash], file_cursor)
                raise

            if msg_record.get("duplicate"):
                if file_hash:
                    file.release_references(port_api, [file_hash], file_cursor)
                if not messages_cursor.request_matches(
                        msg_record["mid"], uid, target_uid, content, content_type,
                        file_hash=file_hash, quote=quote, group_id=group_id,
                        forwarded=forwarded):
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
                allowed_mentions = group_cursor.get_member_uids(group_id) if group_id else [target_uid]
                mentioned_uids = resolve_mentioned_uids(
                    content, user_cursor, allowed_mentions, exclude_uid=uid
                )
            else:
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

            if group_id:
                room_id = "G{}".format(group_id)
                for user in group_cursor.get_member_uids(group_id):
                    user_notif = dict(notif)
                    user_notif["mentions_me"] = user in mentioned_uids
                    user_notif["should_alert"] = user != uid and should_alert(
                        messages_cursor, user, room_id, mentioned_uids
                    )
                    instant_contact.notify_user(user, user_notif)
            else:
                recv_notif = dict(notif)
                recv_notif["room_id"] = "U{}".format(uid)
                recv_notif["mentions_me"] = target_uid in mentioned_uids
                recv_notif["should_alert"] = should_alert(
                    messages_cursor, target_uid, recv_notif["room_id"], mentioned_uids
                )
                sender_notif = dict(notif)
                sender_notif["room_id"] = "U{}".format(target_uid)
                sender_notif["mentions_me"] = False
                sender_notif["should_alert"] = False
                instant_contact.notify_user(target_uid, recv_notif)
                instant_contact.notify_user(uid, sender_notif)

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
            return bool_res()[False]

    @api("/chat/list", methods=['POST'])
    def chat_list(req):
        """返回所有聊天会话及最后一条消息和对方资料。"""
        uid = req["uid"]
        password = req["password"]
        if not user_cursor.verify_user(uid, password):
            print("[WARN] chat_list: verify_user failed for uid={}".format(uid))
            return json.dumps({"error": "auth_failed"}, ensure_ascii=False)
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]

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
                })

        result.sort(key=lambda x: x.get("last_time") or 0, reverse=True)
        return json.dumps(result, ensure_ascii=False)

    @api("/chat/preferences/update", methods=['POST'])
    def update_chat_preference(req):
        uid = req["uid"]
        password = req["password"]
        room_id = str(req.get("room_id", ""))
        if not user_cursor.verify_user(uid, password):
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
        if is_pinned is not None and not isinstance(is_pinned, bool):
            return bool_res()[False]
        if notify_level is not None:
            try:
                notify_level = int(notify_level)
            except (TypeError, ValueError):
                return bool_res()[False]
        return bool_res()[messages_cursor.update_room_preference(
            uid, room_id, is_pinned=is_pinned, notify_level=notify_level
        )]

    @api("/message/recall", methods=['POST'])
    def recall_message(req):
        try:
            uid = int(req["uid"])
            password = req["password"]
            mid = int(req["mid"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({"success": False, "error": "invalid_request"})
        if not user_cursor.verify_user(uid, password):
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

        recalled = messages_cursor.get_message(mid)
        notification_cursor.redact_recalled_message(mid)
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
                instant_contact.notify_user(recipient, direct_event)
            recipients = []
        for recipient in recipients:
            instant_contact.notify_user(recipient, event)
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
            return bool_res()[False]

        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        user_row = get_user_row(uid)
        if user_row is None:
            return bool_res()[False]
        user_stat = user_row[4]
        if user_stat == 'banned':
            return bool_res()[False]

        if group_id is not None:
            if not group_cursor.is_member(group_id, uid):
                return bool_res()[False]
        elif has_target:
            if not user_cursor.is_friend(uid, target_uid):
                return bool_res()[False]
        else:
            return bool_res()[False]

        rows = messages_cursor.query_history(uid, target_uid,
            before_mid=before_mid, limit=limit, group_id=group_id)
        records = messages_cursor.serialize_rows(rows)
        return json.dumps(enrich_message_files(records), ensure_ascii=False)

    @api("/friend/add_friend", methods=['POST'])
    def add_friend(req):
        uid = req["uid"]
        password = req["password"]
        added = req["added"]
        req_word = req["req_word"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        if not user_cursor.uid_query(added):
            return bool_res()[False]
        succeeded = user_cursor.pending_friend(uid, added, uid)
        if succeeded:
            notify_user(added, "friend.request", "新的好友申请",
                        "{} 请求添加你为好友。".format(format_user_display(uid)), sender=uid)
        return bool_res()[succeeded]

    @api("/friend/deal_ship", methods=['POST'])
    def deal_ship(req):
        uid = req["uid"]
        password = req["password"]
        dealt = req["dealt"]
        stat = req["stat"]
        if not user_cursor.verify_user(uid, password):
            return bool_res()[False]
        if stat not in ("allow", "reject"):
            return bool_res()[False]
        relationship = user_cursor.query_relationship(uid, dealt)
        if not relationship:
            return bool_res()[False]
        rel = relationship[0]
        if rel[3] != dealt:
            return bool_res()[False]
        if rel[2] not in ('pending', 'friend'):
            return bool_res()[False]
        if stat == "allow":
            succeeded = user_cursor.change_relationship(uid, dealt, 'friend')
            if succeeded:
                notify_user(dealt, "friend.accepted", "好友申请已通过",
                            "{} 已通过你的好友申请。".format(format_user_display(uid)), sender=uid)
        else:
            user_cursor.delete_relationship(uid, dealt)
            succeeded = True
        return bool_res()[succeeded]
    
    return app

# pri, pub, pri_pem, pub_pem, has = generate_rsa_keys()
# with open("res/7001/secret/pub.pem", "wb") as file:
#     file.write(pub_pem)
# usr_obj = UserDb("res/7001/db/user.db", 7001, 1145)
# app = main(7001, 1145, pub_pem, pri, usr_obj)
