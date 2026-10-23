import websockets
import contextvars
import functools
from websockets.exceptions import InvalidMessage
import time
from argon2 import PasswordHasher
import db
import json
import crypto
import asyncio
import base64
import os
import threading
import logging
from collections import defaultdict
from mention_utils import resolve_mentioned_uids, should_alert, alert_from_preference
from config_utils import get_features
import jwt_tool
import jwt_keys

async def to_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    func_call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, func_call)


async def _put_nowait(queue, item):
    """不阻塞地把 item 放进有界队列！满了就丢  调用方兜底"""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass

class _InvalidHandshakeFilter(logging.Filter):
    def filter(self, record):
        exception = record.exc_info[1] if record.exc_info else None
        return not (
            record.getMessage() == "opening handshake failed"
            and isinstance(exception, InvalidMessage)
        )


def _metadata_with_name(metadata, file_name):
    if not metadata or not file_name:
        return metadata
    result = dict(metadata)
    result["file_name"] = file_name
    result["filename"] = file_name
    result.pop("mime_type", None)
    result["file_type"] = result.get("file_type") or "unknown"
    result["extension"] = os.path.splitext(file_name)[1].lower()
    return result


def can_access_room(user_cursor, group_cursor, uid: int, room_id: str) -> bool:
    if not isinstance(room_id, str) or len(room_id) < 2:
        return False
    try:
        target_id = int(room_id[1:])
    except (TypeError, ValueError):
        return False
    if room_id.startswith('U'):
        return user_cursor.is_friend(uid, target_id)
    if room_id.startswith('G'):
        return group_cursor.is_member(target_id, uid)
    return False

class InstantConnect():
    # 单个连接队列上限
    SEND_QUEUE_LIMIT = 256

    def __init__(self, port_api, port_tcp, notification_cursor, user_cursor, messages_cursor,
                 group_cursor, file_cursor=None, jwt_secret=None, jwt_key_store=None):
        self.port_api = port_api
        self.port_tcp = port_tcp
        self.notification_cursor = notification_cursor
        self.connected_clients = dict()
        self.connected_clients[-1] = []
        self.clients_belonged = dict()
        self.clients_token = dict()
        self.clients_session = dict()
        self.send_queue = dict()
        self.user_cursor = user_cursor
        self.group_cursor = group_cursor
        self.messages_cursor = messages_cursor
        self.file_cursor = file_cursor
        self.jwt_secret = jwt_tool.load_secret(port_api) if jwt_secret is None else jwt_secret
        self.jwt_key_store = (
            jwt_keys.KeyStore("res/{}/secret".format(port_api))
            if jwt_key_store is None else jwt_key_store
        )
        self.legacy_auth_enabled = True
        self.external_issuers = []
        self._load_config()
        self.aes_key = dict()
        self.pri_key = crypto.load_pri("res/{}/secret/pri.pem".format(port_api))
        self.loop = None
        self._ws_lock = threading.Lock()
        self._ws_timestamps = defaultdict(list)
        self._ws_typing_timestamps = defaultdict(list)
        self._ws_read_timestamps = defaultdict(list)
        self._read_receipt_window = 1.5
        self._read_receipt_last = dict()
        self._read_receipt_pending = dict()
        self._read_receipt_timers = dict()
        self._read_receipt_lock = threading.Lock()
        self._presence_grace = 5.0
        self._presence_state = dict()
        self._presence_timers = dict()
        # 扇出失败（DB 抖动）后的补发：uid -> (on/offline, 已尝试次数)
        self._presence_retry = dict()
        self._presence_retry_timers = dict()
        self._presence_retry_delay = 10.0
        self._presence_retry_limit = 3
        self._presence_lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._auth_lock = threading.Lock()
        self._auth_timestamps = defaultdict(list)
        self._auth_semaphore = asyncio.Semaphore(2)
        self._start_cleanup_thread()

    def _start_cleanup_thread(self):
        def _loop():
            while True:
                time.sleep(300)
                try:
                    self._cleanup_rate_timestamps()
                except Exception:
                    pass
                try:
                    self._cleanup_read_receipts()
                except Exception:
                    pass
        t = threading.Thread(target=_loop, daemon=True, name="tfv5-ws-rate-gc")
        t.start()

    def _cleanup_read_receipts(self):
        """回收已读回执节流状态：(读取者, 房间) 键随私聊对象/会话数无上限增长。

        时间戳用 time.monotonic()（与 _read_receipt_last 同源），阈值远大于
        合并窗口，不会误删仍在节流中的键。
        """
        cutoff = time.monotonic() - max(self._read_receipt_window * 8, 120.0)
        with self._read_receipt_lock:
            for key in [k for k, ts in self._read_receipt_last.items() if ts < cutoff]:
                del self._read_receipt_last[key]
            # 定时器仍在的键由 _fire 收尾；没有定时器又没有时间戳的是卡死的残留
            for key in [k for k in self._read_receipt_pending
                        if k not in self._read_receipt_timers
                        and k not in self._read_receipt_last]:
                del self._read_receipt_pending[key]

    def _cleanup_rate_timestamps(self):
        now = time.time()
        cutoff = now - 120
        with self._ws_lock:
            for d in (self._ws_timestamps, self._ws_typing_timestamps,
                      self._ws_read_timestamps):
                expired = [k for k, v in d.items() if not v or v[-1] < cutoff]
                for k in expired:
                    del d[k]
        with self._auth_lock:
            expired = [k for k, v in self._auth_timestamps.items() if not v or v[-1] < cutoff]
            for k in expired:
                del self._auth_timestamps[k]

    def _check_ws_auth_rate(self, remote_address, limit: int = 5, window: float = 60.0) -> bool:
        ip = remote_address[0] if isinstance(remote_address, tuple) else str(remote_address)
        now = time.time()
        with self._auth_lock:
            timestamps = [
                value for value in self._auth_timestamps.get(ip, [])
                if value > now - window
            ]
            if len(timestamps) >= limit:
                self._auth_timestamps[ip] = timestamps
                return False
            timestamps.append(now)
            self._auth_timestamps[ip] = timestamps
            return True

    def _verify_ws_token(self, token: str):
        """
        校验词元（bushi）
        返回 (uid, jti, sid) 或 None。
        """
        def _check():
            # 本服签发 token：HS* 用 jwt_secret，非对称用密钥环（按 kid 选钥）
            payload = jwt_tool.verify_local_token(
                self.jwt_secret, token, self.jwt_key_store
            )
            if payload is not None:
                try:
                    uid = int(payload.get("sub"))
                except (TypeError, ValueError):
                    return None
                jti = payload.get("jti")
                sid = payload.get("sid")
                if not sid:
                    return None
                row = self.user_cursor.resolve_session_identity(uid, jti, sid)
                if row is None:
                    return None
                if int(row[1]) != int(payload.get("av", -1)):
                    return None
                return (uid, jti, sid)

            # 外部签发 token：与 HTTP 旧版直接接受路径保持一致（sub 必须是本地 uid 且 av 匹配）。
            # 推荐仍先走 /auth/external/login 换成本地会话 token 再连 WS。
            if not self.external_issuers:
                return None
            payload = jwt_tool.verify_external_token(token, self.external_issuers)
            if payload is None:
                return None
            try:
                uid = int(payload.get("sub"))
            except (TypeError, ValueError):
                return None
            if not self.user_cursor.uid_query(uid):
                return None
            auth_version = self.user_cursor.get_auth_version(uid)
            if int(payload.get("av", -1)) != int(auth_version):
                return None
            return (uid, payload.get("jti"), payload.get("sid"))
        return to_thread(_check)

    def _check_ws_rate(self, uid: int, max_per_second: int = 10, bucket: str = "msg") -> bool:
        now = time.time()
        ts_dict = {
            "typing": self._ws_typing_timestamps,
            "read": self._ws_read_timestamps,
        }.get(bucket, self._ws_timestamps)
        with self._ws_lock:
            cutoff = now - 1.0
            ts_dict[uid] = [t for t in ts_dict[uid] if t > cutoff]
            if len(ts_dict[uid]) >= max_per_second:
                return False
            ts_dict[uid].append(now)
            return True

    def _load_config(self, cfg=None):
        if cfg is None:
            cfg_path = os.path.join("res", str(self.port_api), "config.json")
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        self.max_message_length = cfg.get("max_message_length", 10000)
        self.legacy_auth_enabled = bool(cfg.get("legacy_auth_enabled", True))
        raw_issuers = cfg.get("jwt_external_issuers")
        self.external_issuers = raw_issuers if isinstance(raw_issuers, list) else []
        self.features = get_features(cfg)
        rtc_cfg = cfg.get("rtc", {})
        self.rtc_turn_enabled = bool(rtc_cfg.get("turn_enabled", False)) if isinstance(rtc_cfg, dict) else False
        self.rtc_ice_servers = rtc_cfg.get("ice_servers", []) if isinstance(rtc_cfg, dict) else []

    def refresh_config(self) -> bool:
        """重新从磁盘加载配置，返回是否成功
        """
        cfg_path = os.path.join("res", str(self.port_api), "config.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print("[WARN] 重新加载配置失败，保留原值: {}".format(e))
            return False
        self._load_config(cfg)
        return True

    def encrypt_response(self, req : dict, websocket):
        json_req = json.dumps(req)
        iv, content = crypto.aes_encrypt(json_req, self.aes_key[websocket])
        iv = base64.b64encode(iv).decode('utf-8')
        content = base64.b64encode(content).decode('utf-8')
        return json.dumps({"iv" : iv, "content" : content}) 

    def _verify_quote(self, quote_mid: int, send_to: str, sender_uid: int) -> bool:
        rows = self.messages_cursor.query(
            "SELECT sender_uid, receiver_uid, group_id, deleted, content_type "
            "FROM messages WHERE mid = ?",
            (quote_mid,)
        )
        if not rows:
            return False
        r = rows[0]
        if r[3]:  # deleted
            return False
        if r[4] == 'event':  # 事件行不是用户内容，不能引用
            return False
        if send_to[0] == 'G':
            gid = int(send_to[1:])
            if r[2] != gid:
                return False
            return self.group_cursor.is_member(gid, sender_uid)
        else:
            target_uid = int(send_to[1:])
            return (r[0] == sender_uid and r[1] == target_uid) or \
                   (r[0] == target_uid and r[1] == sender_uid)

    def _cleanup_client(self, websocket):
        last_uid = None
        with self._clients_lock:
            uid = self.clients_belonged.pop(websocket, -1)
            self.clients_token.pop(websocket, None)
            self.clients_session.pop(websocket, None)
            if uid in self.connected_clients and websocket in self.connected_clients[uid]:
                self.connected_clients[uid].remove(websocket)
                if uid != -1 and not self.connected_clients[uid]:
                    del self.connected_clients[uid]
                    last_uid = uid
            elif websocket in self.connected_clients[-1]:
                self.connected_clients[-1].remove(websocket)
            self.send_queue.pop(websocket, None)
            self.aes_key.pop(websocket, None)
        # 最后一个连接掉线时启动下线宽限（不在此加锁块内，避免与 filter_online 死锁）
        if last_uid is not None:
            self._schedule_offline(last_uid)
        return last_uid

    async def _queue_message(self, websocket, message : dict):
        queue = self.send_queue.get(websocket)
        if queue is None:
            return
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            uid = self.clients_belonged.get(websocket)
            print("[WARN] WS 发送队列已满，断开慢客户端(uid={})".format(uid))
            try:
                await websocket.close(code=1013)
            except Exception:
                pass

    def _queue_ack(self, websocket, client_mid=None, status="sent", mid=None, error=None):
        if client_mid is None or websocket not in self.send_queue or self.loop is None:
            return
        ack = {"type": "message.ack", "client_mid": client_mid, "status": status}
        if mid is not None:
            ack["mid"] = mid
        if error is not None:
            ack["error"] = error
        asyncio.run_coroutine_threadsafe(
            _put_nowait(self.send_queue[websocket], ack), self.loop)

    async def _disconnect_user(self, uid : int):
        with self._clients_lock:
            clients = list(self.connected_clients.get(uid, []))
        for websocket in clients:
            try:
                await websocket.close()
            except Exception:
                pass
            finally:
                self._cleanup_client(websocket)

    def notify_user(self, uid : int, notification : dict):
        """推送系统事件通知（写入 notifications 表，再推 NOTIFICATION.NEW）

        消息事件不再走此通道，请使用 push_message / push_messages。
        """
        notification = dict(notification)
        stored = self.notification_cursor.add_event(uid, notification)
        record = {
            "id" : stored["id"],
            "time_stamp" : stored["time_stamp"],
            "read_at" : None,
            "info" : notification
        }
        if self.loop is None:
            return record
        with self._clients_lock:
            clients = list(self.connected_clients.get(uid, []))
        for websocket in clients:
            asyncio.run_coroutine_threadsafe(
                self._queue_message(websocket, {"type" : "NOTIFICATION.NEW", "notification" : record}),
                self.loop
            )
        return record

    def notify_users(self, uid_notifications) -> list:
        """批量写入系统事件通知并推送给各自已连接的客户端，单事务提交。

        :param uid_notifications: [(uid, notification), ...]
        :return: [{id, time_stamp, read_at, info}, ...]
        """
        if not uid_notifications:
            return []
        prepared = [(int(uid), dict(notification)) for uid, notification in uid_notifications]
        stored = self.notification_cursor.add_events(prepared)
        with self._clients_lock:
            client_map = {
                uid: list(self.connected_clients.get(uid, []))
                for uid, _ in prepared
            }
        records = []
        for (uid, notification), meta in zip(prepared, stored):
            record = {
                "id" : meta["id"],
                "time_stamp" : meta["time_stamp"],
                "read_at" : None,
                "info" : notification
            }
            records.append(record)
            if self.loop is None:
                continue
            for websocket in client_map.get(uid, []):
                asyncio.run_coroutine_threadsafe(
                    self._queue_message(websocket, {"type" : "NOTIFICATION.NEW", "notification" : record}),
                    self.loop
                )
        return records

    def push_message(self, uid : int, message : dict):
        """实时推送消息事件（MESSAGE.NEW），不写入 notifications

        消息只持久化在 messages 表中，离线端通过 /message/sync 增量补拉。
        """
        if self.loop is None:
            return message
        with self._clients_lock:
            clients = list(self.connected_clients.get(uid, []))
        for websocket in clients:
            asyncio.run_coroutine_threadsafe(
                self._queue_message(websocket, {"type" : "MESSAGE.NEW", "message" : message}),
                self.loop
            )
        return message

    def push_raw(self, uid : int, message : dict, exclude_websocket=None):
        """按原样中继一条信令/消息给某用户的所有在线连接（不包 MESSAGE.NEW）

        exclude_websocket: 可选的发起连接，广播（如已读回执）时不给它回声。
        """
        if self.loop is None:
            return
        with self._clients_lock:
            clients = list(self.connected_clients.get(uid, []))
        for websocket in clients:
            if websocket is exclude_websocket:
                continue
            asyncio.run_coroutine_threadsafe(
                self._queue_message(websocket, message),
                self.loop
            )

    def filter_online(self, uids) -> list:
        """返回给定 uid 集合中当前在线的子集"""
        with self._clients_lock:
            return [int(uid) for uid in set(uids) if self.connected_clients.get(int(uid))]

    def _presence_enabled(self) -> bool:
        return bool(self.features.get("presence", True))

    def _user_shows_presence(self, uid):
        """用户是否允许向好友展示在线状态（默认展示）
        """
        try:
            flags = self.user_cursor.get_show_status_flags([uid])
        except Exception as e:
            print("[WARN] 读取在线状态开关失败(uid={}): {}".format(uid, e))
            return None
        info = flags.get(int(uid))
        if info is None:
            return False
        return bool(info.get("show_online_status", True))

    def _presence_targets(self, uid):
        """这次上下线事件推给
        """
        if self.loop is None or not self._presence_enabled():
            return []
        shows = self._user_shows_presence(uid)
        if shows is None:
            return None
        if not shows:
            return []
        try:
            friends = self.user_cursor.list_friend_uids(uid)
        except Exception as e:
            print("[WARN] 读取好友列表失败(uid={}): {}".format(uid, e))
            return None
        return self.filter_online(friends)

    def _push_presence(self, uid, online, targets):
        """把上下线事件推给 targets"""
        if not targets:
            return
        payload = {
            "type": "presence.update",
            "uid": uid,
            "status": "online" if online else "offline",
            "ts": int(time.time() * 1000),
        }
        for friend in targets:
            self.push_raw(friend, dict(payload))

    def _deliver_offline(self, uid, targets):
        """复查仍在离线并广播 offline（判定与广播原子
        """
        with self._presence_lock:
            with self._clients_lock:
                still_online = bool(self.connected_clients.get(uid))
            if still_online:
                return False
            self._presence_state.pop(uid, None)
            self._push_presence(uid, False, targets)
        return True

    def _queue_presence_retry(self, uid, online):
        if self.loop is None or not self._presence_enabled():
            return
        with self._presence_lock:
            attempts = self._presence_retry.get(uid, (online, 0))[1] + 1
            if attempts > self._presence_retry_limit:
                self._presence_retry.pop(uid, None)
                print("[WARN] 在线状态扇出重试 {} 次仍失败，放弃(uid={})".format(
                    self._presence_retry_limit, uid))
                return
            self._presence_retry[uid] = (online, attempts)
            if uid in self._presence_retry_timers:
                return
            timer = threading.Timer(
                self._presence_retry_delay, self._fire_presence_retry, (uid,))
            timer.daemon = True
            self._presence_retry_timers[uid] = timer
            timer.start()

    def _fire_presence_retry(self, uid):
        with self._presence_lock:
            self._presence_retry_timers.pop(uid, None)
            pending = self._presence_retry.get(uid)
        if pending is None:
            return
        online = pending[0]
        if bool(self.filter_online([uid])) != online:
            with self._presence_lock:
                self._presence_retry.pop(uid, None)
            return
        if online:
            if self._fanout_presence(uid, True):
                with self._presence_lock:
                    self._presence_retry.pop(uid, None)
            else:
                self._queue_presence_retry(uid, True)
            return
        targets = self._presence_targets(uid)
        if targets is None:
            self._queue_presence_retry(uid, False)
            return
        self._deliver_offline(uid, targets)
        with self._presence_lock:
            self._presence_retry.pop(uid, None)

    def _fanout_presence(self, uid, online, targets=None):
        if targets is None:
            targets = self._presence_targets(uid)
            if targets is None:
                return False
        self._push_presence(uid, online, targets)
        return True

    def _mark_online(self, uid):
        if not self._presence_enabled():
            return
        with self._presence_lock:
            timer = self._presence_timers.pop(uid, None)
            if timer is not None:
                timer.cancel()
            if self._presence_state.get(uid):
                return
            self._presence_state[uid] = True
        if not self._fanout_presence(uid, True):
            self._queue_presence_retry(uid, True)

    def _schedule_offline(self, uid):
        if not self._presence_enabled():
            return
        with self._presence_lock:
            timer = self._presence_timers.pop(uid, None)
            if timer is not None:
                timer.cancel()

            def _fire():
                targets = self._presence_targets(uid)
                with self._presence_lock:
                    self._presence_timers.pop(uid, None)
                if targets is None:
                    self._queue_presence_retry(uid, False)
                    return
                self._deliver_offline(uid, targets)

            timer = threading.Timer(self._presence_grace, _fire)
            timer.daemon = True
            self._presence_timers[uid] = timer
            timer.start()

    def presence_hidden(self, uid):
        """用户关闭在线状态时，向好友 hidden 让在线指示器立即消失。"""
        if self.loop is None or not self._presence_enabled():
            return
        try:
            friends = self.user_cursor.list_friend_uids(uid)
        except Exception as e:
            print("[WARN] 读取好友列表失败(uid={}): {}".format(uid, e))
            return
        targets = self.filter_online(friends)
        if not targets:
            return
        payload = {
            "type": "presence.update",
            "uid": uid,
            "status": "hidden",
            "ts": int(time.time() * 1000),
        }
        for friend in targets:
            self.push_raw(friend, dict(payload))

    def presence_shown(self, uid):
        """用户重新开启在线状态且当前在线时，向好友 online"""
        if self.loop is None or not self._presence_enabled():
            return
        if not self.filter_online([uid]):
            return
        if not self._fanout_presence(uid, True):
            self._queue_presence_retry(uid, True)

    def emit_private_read_receipt(self, reader_uid, peer_uid, room_key,
                                  last_mid, last_seq):
        """私聊已读回执
        """
        if not self._read_receipt_enabled():
            return
        if not self._user_shows_read_status(reader_uid):
            return
        if not last_seq or last_seq <= 0:
            return
        self._emit_read_receipt(reader_uid, peer_uid, room_key, last_mid, last_seq)

    def _read_receipt_enabled(self) -> bool:
        return bool(self.features.get('chat', {}).get('read_receipt', True))

    def _user_shows_read_status(self, uid) -> bool:
        """用户是否允许向他人展示自己已读"""
        try:
            flags = self.user_cursor.get_show_status_flags([uid])
        except Exception as e:
            print("[WARN] 读取已读展示开关失败(uid={}): {}".format(uid, e))
            return False
        return bool(flags.get(int(uid), {}).get('show_read_status', True))

    def _emit_read_receipt(self, reader_uid, peer_uid, room_key, last_mid, last_seq):
        """向私聊对方推送已读回执
        """
        key = (reader_uid, room_key)
        payload = {
            "type": "message.read_receipt",
            "room_id": "U{}".format(reader_uid),
            "uid": reader_uid,
            "last_mid": last_mid,
            "last_seq": last_seq,
            "ts": int(time.time() * 1000),
        }

        def _fire():
            with self._read_receipt_lock:
                self._read_receipt_timers.pop(key, None)
                pending = self._read_receipt_pending.pop(key, None)
                if pending is None:
                    return
                target_uid, pending_payload = pending
                self._read_receipt_last[key] = time.monotonic()
            self.push_raw(target_uid, pending_payload)

        window = self._read_receipt_window
        now = time.monotonic()
        immediate = False
        cancel_timer = None
        start_timer = None
        with self._read_receipt_lock:
            last = self._read_receipt_last.get(key)
            if last is None or now - last >= window:
                immediate = True
                self._read_receipt_last[key] = now
                cancel_timer = self._read_receipt_timers.pop(key, None)
                self._read_receipt_pending.pop(key, None)
            else:
                self._read_receipt_pending[key] = (peer_uid, payload)
                if key not in self._read_receipt_timers:
                    start_timer = threading.Timer(window, _fire)
                    start_timer.daemon = True
                    self._read_receipt_timers[key] = start_timer
        if start_timer is not None:
            start_timer.start()
        if cancel_timer is not None:
            cancel_timer.cancel()
        if immediate:
            self.push_raw(peer_uid, payload)

    def _queue_call_ack(self, websocket, call_id, request, status):
        """针对 call.* 信令的回执（相当于 call 专用版 _queue_ack）"""
        if not call_id or websocket not in self.send_queue or self.loop is None:
            return
        ack = {
            "type": "call.ack",
            "call_id": call_id,
            "for": request,
            "status": status,
        }
        asyncio.run_coroutine_threadsafe(self.send_queue[websocket].put(ack), self.loop)

    async def _handle_call_message(self, websocket, message : dict):
        """视频通话信令中继：服务端只管两个好友之间转运，不持有任何通话状态。

        call.invite / call.answer / call.ice / call.hangup 均校验好友关系与速率，
        并以 {'type': ..., 'call_id': ..., 'from_uid': <发送者>, ...} 转发给目标。
        """
        sender_uid = self.clients_belonged[websocket]
        if not self._check_ws_rate(sender_uid):
            self._queue_call_ack(websocket, message.get('call_id'), message['type'], "rate_limited")
            return
        target_uid = message.get('target_uid')
        call_id = message.get('call_id')
        if isinstance(target_uid, bool) or not isinstance(target_uid, int) or target_uid < 0:
            self._queue_call_ack(websocket, call_id, message['type'], "invalid_target")
            return
        if target_uid == sender_uid:
            self._queue_call_ack(websocket, call_id, message['type'], "invalid_target")
            return
        if not isinstance(call_id, str) or not call_id or len(call_id) > 64:
            self._queue_call_ack(websocket, call_id, message['type'], "invalid_call_id")
            return
        if not await to_thread(self.user_cursor.is_friend, sender_uid, target_uid):
            self._queue_call_ack(websocket, call_id, message['type'], "not_friends")
            return

        mtype = message['type']
        relay = {"type": mtype, "call_id": call_id, "from_uid": sender_uid}
        if mtype in ("call.invite", "call.answer"):
            payload = message.get('payload')
            if not isinstance(payload, dict) or not isinstance(payload.get('sdp'), str):
                self._queue_call_ack(websocket, call_id, mtype, "invalid_request")
                return
            candidates = payload.get('candidates')
            if candidates is not None and not isinstance(candidates, list):
                self._queue_call_ack(websocket, call_id, mtype, "invalid_request")
                return
            relay["payload"] = payload
        elif mtype == "call.ice":
            candidate = message.get('candidate')
            if not isinstance(candidate, dict) or not isinstance(candidate.get('candidate'), str):
                self._queue_call_ack(websocket, call_id, mtype, "invalid_request")
                return
            relay["candidate"] = candidate
        elif mtype == "call.hangup":
            reason = message.get('reason')
            if not isinstance(reason, str) or reason not in ("hangup", "decline", "cancel", "busy", "error"):
                self._queue_call_ack(websocket, call_id, mtype, "invalid_request")
                return
            relay["reason"] = reason
        else:
            self._queue_call_ack(websocket, call_id, mtype, "invalid_request")
            return

        online = bool(self.connected_clients.get(target_uid))
        self.push_raw(target_uid, relay)
        self._queue_call_ack(
            websocket, call_id, mtype,
            "delivered" if (online or mtype != "call.invite") else "offline",
        )

    def push_recall(self, uid : int, message : dict):
        """实时推送消息撤回事件（MESSAGE.RECALLED），不写入 notifications"""
        if self.loop is None:
            return message
        with self._clients_lock:
            clients = list(self.connected_clients.get(uid, []))
        for websocket in clients:
            asyncio.run_coroutine_threadsafe(
                self._queue_message(websocket, {"type" : "MESSAGE.RECALLED", "message" : message}),
                self.loop
            )
        return message

    def push_messages(self, uid_messages) -> list:
        """批量实时推送消息事件（MESSAGE.NEW），不写入 notifications

        :param uid_messages: [(uid, message), ...]
        """
        if not uid_messages:
            return []
        prepared = [(int(uid), dict(message)) for uid, message in uid_messages]
        if self.loop is None:
            return [message for _, message in prepared]
        with self._clients_lock:
            client_map = {
                uid: list(self.connected_clients.get(uid, []))
                for uid, _ in prepared
            }
        for uid, message in prepared:
            for websocket in client_map.get(uid, []):
                asyncio.run_coroutine_threadsafe(
                    self._queue_message(websocket, {"type" : "MESSAGE.NEW", "message" : message}),
                    self.loop
                )
        return [message for _, message in prepared]

    def disconnect_user(self, uid : int):
        if self.loop is None:
            with self._clients_lock:
                clients = list(self.connected_clients.get(uid, []))
            for websocket in clients:
                self._cleanup_client(websocket)
            return

        asyncio.run_coroutine_threadsafe(self._disconnect_user(uid), self.loop)

    async def _disconnect_jti(self, jti : str):
        with self._clients_lock:
            clients = [
                websocket
                for websocket, token in self.clients_token.items()
                if token == jti
            ]
        for websocket in clients:
            try:
                await websocket.close()
            except Exception:
                pass
            finally:
                self._cleanup_client(websocket)

    def disconnect_jti(self, jti : str):
        """按 jti 去 fuck 连接"""
        if self.loop is None:
            with self._clients_lock:
                clients = [
                    websocket
                    for websocket, token in self.clients_token.items()
                    if token == jti
                ]
            for websocket in clients:
                self._cleanup_client(websocket)
            return

        asyncio.run_coroutine_threadsafe(self._disconnect_jti(jti), self.loop)

    async def _disconnect_session(self, session_id : str):
        with self._clients_lock:
            clients = [
                websocket
                for websocket, sid in self.clients_session.items()
                if sid == session_id
            ]
        for websocket in clients:
            try:
                await websocket.close()
            except Exception:
                pass
            finally:
                self._cleanup_client(websocket)

    def disconnect_session(self, session_id : str):
        """按 session_id 断开该会话的所有 WebSocket 连接。"""
        if self.loop is None:
            with self._clients_lock:
                clients = [
                    websocket
                    for websocket, sid in self.clients_session.items()
                    if sid == session_id
                ]
            for websocket in clients:
                self._cleanup_client(websocket)
            return

        asyncio.run_coroutine_threadsafe(self._disconnect_session(session_id), self.loop)
    
    async def sender(self, websocket, queue):
        try:
            while True:
                message = await queue.get()
                await websocket.send(self.encrypt_response(message, websocket))
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            pass
    
    async def handler(self, websocket : websockets.WebSocketServerProtocol):
        with self._clients_lock:
            self.connected_clients[-1].append(websocket)
            self.clients_belonged[websocket] = -1
        try:
            if not self._check_ws_auth_rate(websocket.remote_address):
                raise ValueError("登录请求过于频繁")
            message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            message = json.loads(message)
            if message['type'] != 'REQ.UPDATE_AES_KEY':
                raise ValueError("{} 而非 REQ.UPDATE_AES_KEY".format(message.get('type')))
            message["aes_key"] = base64.b64decode(message["aes_key"])
            self.aes_key[websocket] = crypto.decrypt(self.pri_key, message["aes_key"]) 

        except Exception as e:
            self._cleanup_client(websocket)
            print("[ERR] 客户端链接 WS 服务器后没有发出 AES 密钥声明")
            return
        
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            message = json.loads(message)
            message = json.loads(crypto.aes_decrypt(base64.b64decode(message['iv']), base64.b64decode(message['content']), self.aes_key[websocket]))
            if message['type'] != 'AUTH.LOGIN':
                raise ValueError("{} 而非 AUTH.LOGIN".format(message.get('type')))
            token = message.get("token")
            verified_jti = None
            verified_sid = None
            if token:
                verified = await self._verify_ws_token(token)
                if verified is not None:
                    verified_uid, verified_jti, verified_sid = verified
                else:
                    verified_uid = None
            else:
                if not self.legacy_auth_enabled:
                    verified_uid = None
                else:
                    async with self._auth_semaphore:
                        verified = await asyncio.wait_for(
                            to_thread(self.user_cursor.verify_user, message['uid'], message['password']),
                            timeout=10.0,
                        )
                    verified_uid = message.get('uid') if verified else None
            if verified_uid is None:
                raise ValueError("UID 验证失败：{}".format(message.get('uid', 'token')))
            with self._clients_lock:
                self.connected_clients[-1].remove(websocket)
                became_online = not self.connected_clients.get(verified_uid)
                if verified_uid not in self.connected_clients.keys():
                    self.connected_clients[verified_uid] = []
                self.connected_clients[verified_uid].append(websocket)
                self.clients_belonged[websocket] = verified_uid
                self.clients_token[websocket] = verified_jti
                self.clients_session[websocket] = verified_sid
                self.send_queue[websocket] = asyncio.Queue(
                    maxsize=self.SEND_QUEUE_LIMIT)
            await websocket.send(self.encrypt_response({"type" : "AUTH.LOGIN_SUCCEEDED"}, websocket))
            if became_online:
                await to_thread(self._mark_online, verified_uid)

        except Exception as e:
            print("[ERR] 客户端链接 WS 服务器后没有登录")
            self._cleanup_client(websocket)
            return
        
        send_task = asyncio.create_task(self.sender(websocket, self.send_queue[websocket]))

        try:
            async for message in websocket:
                message = json.loads(message)
                message = json.loads(crypto.aes_decrypt(base64.b64decode(message['iv']), base64.b64decode(message['content']), self.aes_key[websocket]))
                sender_uid = self.clients_belonged[websocket]

                if message['type'] == 'PING':
                    await self._queue_message(websocket, {"type": "PONG"})
                    continue

                if message['type'] in ("message.plain", "message.file"):
                    info = await to_thread(self.user_cursor.uid_query, sender_uid)
                    if not info or info[0][4] == 'banned':
                        self._queue_ack(websocket, message.get('client_mid'), status="failed", error="banned")
                        break

                    if not self._check_ws_rate(sender_uid):
                        self._queue_ack(websocket, message.get('client_mid'), status="failed", error="rate_limited")
                        continue

                    gate_content = message.get('content')
                    gate_target = gate_content.get('send_to', '') if isinstance(gate_content, dict) else ''
                    if isinstance(gate_target, str):
                        chat_features = self.features.get('chat', {})
                        if gate_target[:1] == 'G' and not chat_features.get('group_chat', True):
                            self._queue_ack(websocket, message.get('client_mid'), status="failed", error="feature_disabled_group_chat")
                            continue
                        if gate_target[:1] == 'U' and not chat_features.get('private_chat', True):
                            self._queue_ack(websocket, message.get('client_mid'), status="failed", error="feature_disabled_private_chat")
                            continue
                if message['type'] == "message.plain":
                    content = message['content']
                    if not isinstance(content, dict):
                        self._queue_ack(websocket, message.get('client_mid'), status="failed", error="invalid_request")
                        continue
                    plain = content.get('plain', '')
                    send_to = content.get('send_to', '')
                    if not isinstance(send_to, str) or len(send_to) < 2:
                        self._queue_ack(websocket, message.get('client_mid'), status="failed", error="invalid_target")
                        continue
                    quote = int(content.get('quote', -1))
                    client_mid = message.get('client_mid')
                    if quote < -1:
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_quote")
                        continue
                    if quote >= 0 and not await to_thread(
                            self._verify_quote, quote, send_to,
                            self.clients_belonged[websocket]):
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_quote")
                        continue
                    if len(plain) > self.max_message_length:
                        self._queue_ack(websocket, client_mid, status="failed", error="message_too_long")
                        continue
                    if send_to[0] == 'U':
                        # 发送给用户
                        send_to = int(send_to[1:])
                        sender_uid = self.clients_belonged[websocket]
                        if not await to_thread(self.user_cursor.is_friend, sender_uid, send_to):
                            self._queue_ack(websocket, client_mid, status="failed", error="not_friends")
                            continue
                        msg_record = await to_thread(
                            self.messages_cursor.add_message,
                            sender_uid, send_to, plain,
                            content_type='plain', quote=quote,
                            client_mid=client_mid
                        )
                        if msg_record.get("duplicate"):
                            if not await to_thread(
                                    self.messages_cursor.request_matches,
                                    msg_record["mid"], sender_uid, send_to, plain, "plain",
                                    quote=quote):
                                self._queue_ack(
                                    websocket, client_mid, status="failed",
                                    error="client_mid_conflict",
                                )
                                continue
                            self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                            continue
                        self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                        mentioned_uids = await to_thread(
                            resolve_mentioned_uids,
                            plain, self.user_cursor, [send_to], exclude_uid=sender_uid
                        )
                        await to_thread(
                            self.messages_cursor.set_message_mentions,
                            msg_record["mid"], mentioned_uids
                        )
                        sender_str = "U{}".format(sender_uid)
                        recv_notif = {
                            "event" : "message.plain",
                            "title" : str(msg_record["send_time"]),
                            "content" : plain,
                            "sender" : sender_str,
                            "meta" : quote,
                            "mid" : msg_record["mid"],
                            "client_mid" : client_mid,
                            "room_id" : sender_str,
                            "room_seq" : msg_record.get("room_seq")
                        }
                        if quote >= 0:
                            recv_notif["quote_preview"] = await to_thread(
                                self.messages_cursor.get_quote_preview, quote, msg_record)
                        else:
                            recv_notif["quote_preview"] = None
                        recv_notif["mentioned_uids"] = mentioned_uids
                        recv_notif["mentions_me"] = send_to in mentioned_uids
                        recv_notif["should_alert"] = await to_thread(
                            should_alert, self.messages_cursor, send_to, sender_str, mentioned_uids
                        )
                        sender_notif = dict(recv_notif)
                        sender_notif["room_id"] = "U{}".format(send_to)
                        sender_notif["mentions_me"] = False
                        sender_notif["should_alert"] = False
                        await to_thread(self.push_message, send_to, recv_notif)
                        await to_thread(self.push_message, sender_uid, sender_notif)
                        # 发送即已读：这次隐式前进也要告诉对方，否则对方的推送
                        # 通道会停在旧水位（见 emit_private_read_receipt 的注释）。
                        await to_thread(
                            self.emit_private_read_receipt,
                            sender_uid, send_to,
                            self.messages_cursor.room_key_of(sender_uid, send_to),
                            msg_record["mid"], msg_record.get("room_seq") or 0,
                        )

                    elif send_to[0] == 'G':
                        gid = int(send_to[1:])
                        group = await to_thread(self.group_cursor.query_gid, gid)
                        if not group:
                            self._queue_ack(websocket, client_mid, status="failed", error="group_not_found")
                            continue
                        members = await to_thread(self.group_cursor.get_member_uids, gid)
                        sender_str = "G{}U{}".format(gid, self.clients_belonged[websocket])
                        if not self.clients_belonged[websocket] in members:
                            self._queue_ack(websocket, client_mid, status="failed", error="not_group_member")
                            continue
                        msg_record = await to_thread(
                            self.messages_cursor.add_message,
                            self.clients_belonged[websocket], 0, plain,
                            content_type='plain', quote=quote, group_id=gid,
                            client_mid=client_mid
                        )
                        if msg_record.get("duplicate"):
                            if not await to_thread(
                                    self.messages_cursor.request_matches,
                                    msg_record["mid"], self.clients_belonged[websocket], 0,
                                    plain, "plain", quote=quote, group_id=gid):
                                self._queue_ack(
                                    websocket, client_mid, status="failed",
                                    error="client_mid_conflict",
                                )
                                continue
                            self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                            continue
                        self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                        mentioned_uids = await to_thread(
                            resolve_mentioned_uids,
                            plain,
                            self.user_cursor,
                            members,
                            exclude_uid=self.clients_belonged[websocket],
                        )
                        await to_thread(
                            self.messages_cursor.set_message_mentions,
                            msg_record["mid"], mentioned_uids
                        )
                        notif_dict = {
                            "event" : "message.plain",
                            "title" : str(msg_record["send_time"]),
                            "content" : plain,
                            "sender" : sender_str,
                            "meta" : quote,
                            "mid" : msg_record["mid"],
                            "client_mid" : client_mid,
                            "room_id" : "G{}".format(gid),
                            "group_id" : gid,
                            "room_seq" : msg_record.get("room_seq")
                        }
                        if quote >= 0:
                            notif_dict["quote_preview"] = await to_thread(
                                self.messages_cursor.get_quote_preview, quote, msg_record)
                        else:
                            notif_dict["quote_preview"] = None
                        room_id = "G{}".format(gid)
                        pref_map = await to_thread(
                            self.messages_cursor.get_room_preference_map, members, room_id
                        ) if members else {}
                        sender_uid = self.clients_belonged[websocket]
                        pending = []
                        for user in members:
                            user_notif = dict(notif_dict)
                            user_notif["mentioned_uids"] = mentioned_uids
                            user_notif["mentions_me"] = user in mentioned_uids
                            user_notif["should_alert"] = (
                                user != sender_uid
                                and alert_from_preference(pref_map.get(user), user, mentioned_uids)
                            )
                            pending.append((user, user_notif))
                        try:
                            await to_thread(self.push_messages, pending)
                        except Exception as e:
                            print("[WARN] 批量推送失败(群{}): {}".format(gid, e))

                    else:
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_target")

                elif message["type"] == "message.file":
                    content = message['content']
                    if not isinstance(content, dict):
                        self._queue_ack(websocket, message.get('client_mid'), status="failed", error="invalid_request")
                        continue
                    file_hashes = content.get('file_hashes')
                    send_to = content.get('send_to')
                    try:
                        quote = int(content.get('quote', -1))
                    except (TypeError, ValueError):
                        self._queue_ack(websocket, message.get('client_mid'), status="failed", error="invalid_quote")
                        continue
                    duration = content.get('duration_ms')
                    try:
                        duration = int(duration) if duration is not None else None
                    except (TypeError, ValueError):
                        duration = None
                    client_mid = message.get('client_mid')
                    if quote < -1:
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_quote")
                        continue
                    if quote >= 0 and not await to_thread(
                            self._verify_quote, quote, send_to,
                            self.clients_belonged[websocket]):
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_quote")
                        continue
                    # hyw
                    if not isinstance(file_hashes, str) or len(file_hashes) != 64 or not all(c in '0123456789abcdefABCDEF' for c in file_hashes):
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_file_hash")
                        continue
                    if not isinstance(send_to, str) or len(send_to) < 2:
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_target")
                        continue
                    if send_to[0] == 'U':
                        # 发送给用户
                        try:
                            send_to = int(send_to[1:])
                        except ValueError:
                            self._queue_ack(websocket, client_mid, status="failed", error="invalid_target")
                            continue
                        sender_uid = self.clients_belonged[websocket]
                        if not await to_thread(self.user_cursor.is_friend, sender_uid, send_to):
                            self._queue_ack(websocket, client_mid, status="failed", error="not_friends")
                            continue
                        metadata = (
                            await to_thread(self.file_cursor.acquire_reference,
                                            sender_uid, file_hashes)
                            if self.file_cursor is not None else None)
                        if metadata is None:
                            self._queue_ack(websocket, client_mid, status="failed", error="file_not_owned")
                            continue
                        msg_record = await to_thread(
                            self.messages_cursor.add_message,
                            sender_uid, send_to, file_hashes,
                            content_type='file', file_hash=file_hashes, quote=quote,
                            client_mid=client_mid, file_name=metadata["file_name"],
                            duration=duration
                        )
                        if msg_record.get("duplicate"):
                            if not await to_thread(
                                    self.messages_cursor.request_matches,
                                    msg_record["mid"], sender_uid, send_to, file_hashes, "file",
                                    file_hash=file_hashes, quote=quote, duration=duration):
                                self._queue_ack(
                                    websocket, client_mid, status="failed",
                                    error="client_mid_conflict",
                                )
                                continue
                            self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                            continue
                        if not await to_thread(
                                self.file_cursor.add_reference,
                                file_hashes, "message", msg_record["mid"], sender_uid):
                            await to_thread(
                                self.messages_cursor.recall_message,
                                msg_record["mid"], sender_uid, emit_event=False)
                            self._queue_ack(websocket, client_mid, status="failed", error="file_reference_failed")
                            continue
                        self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                        sender_str = "U{}".format(sender_uid)
                        recv_notif = {
                            "event" : "message.file",
                            "title" : str(msg_record["send_time"]),
                            "content" : file_hashes,
                            "sender" : sender_str,
                            "meta" : {"quote": quote, "avatar_url": "/avatar/get_avatar/user/{}".format(sender_uid)},
                            "quote" : quote,
                            "mid" : msg_record["mid"],
                            "file_hash" : file_hashes,
                            "client_mid" : client_mid,
                            "room_id" : sender_str,
                            "room_seq" : msg_record.get("room_seq")
                        }
                        if quote >= 0:
                            recv_notif["quote_preview"] = await to_thread(
                                self.messages_cursor.get_quote_preview, quote, msg_record)
                        else:
                            recv_notif["quote_preview"] = None
                        if duration is not None:
                            recv_notif["duration"] = duration
                        if self.file_cursor is not None:
                            recv_notif["file"] = _metadata_with_name(
                                await to_thread(
                                    self.file_cursor.get_metadata,
                                    file_hashes, owner_uid=sender_uid),
                                metadata["file_name"],
                            )
                        recv_notif["mentioned_uids"] = []
                        recv_notif["mentions_me"] = False
                        recv_notif["should_alert"] = await to_thread(
                            should_alert, self.messages_cursor, send_to, sender_str, []
                        )
                        sender_notif = dict(recv_notif)
                        sender_notif["room_id"] = "U{}".format(send_to)
                        sender_notif["should_alert"] = False
                        await to_thread(self.push_message, send_to, recv_notif)
                        await to_thread(self.push_message, sender_uid, sender_notif)
                        # 发送即已读：这次隐式前进也要告诉对方，否则对方的推送
                        # 通道会停在旧水位（见 emit_private_read_receipt 的注释）。
                        await to_thread(
                            self.emit_private_read_receipt,
                            sender_uid, send_to,
                            self.messages_cursor.room_key_of(sender_uid, send_to),
                            msg_record["mid"], msg_record.get("room_seq") or 0,
                        )

                    elif send_to[0] == 'G':
                        try:
                            gid = int(send_to[1:])
                        except ValueError:
                            self._queue_ack(websocket, client_mid, status="failed", error="invalid_target")
                            continue
                        group = await to_thread(self.group_cursor.query_gid, gid)
                        if not group:
                            self._queue_ack(websocket, client_mid, status="failed", error="group_not_found")
                            continue
                        members = await to_thread(self.group_cursor.get_member_uids, gid)
                        sender_str = "G{}U{}".format(gid, self.clients_belonged[websocket])
                        if not self.clients_belonged[websocket] in members:
                            self._queue_ack(websocket, client_mid, status="failed", error="not_group_member")
                            continue
                        sender_uid = self.clients_belonged[websocket]
                        metadata = (
                            await to_thread(self.file_cursor.acquire_reference,
                                            sender_uid, file_hashes)
                            if self.file_cursor is not None else None)
                        if metadata is None:
                            self._queue_ack(websocket, client_mid, status="failed", error="file_not_owned")
                            continue
                        msg_record = await to_thread(
                            self.messages_cursor.add_message,
                            sender_uid, 0, file_hashes,
                            content_type='file', file_hash=file_hashes, quote=quote, group_id=gid,
                            client_mid=client_mid, file_name=metadata["file_name"],
                            duration=duration
                        )
                        if msg_record.get("duplicate"):
                            if not await to_thread(
                                    self.messages_cursor.request_matches,
                                    msg_record["mid"], sender_uid, 0, file_hashes, "file",
                                    file_hash=file_hashes, quote=quote, group_id=gid, duration=duration):
                                self._queue_ack(
                                    websocket, client_mid, status="failed",
                                    error="client_mid_conflict",
                                )
                                continue
                            self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                            continue
                        if not await to_thread(
                                self.file_cursor.add_reference,
                                file_hashes, "message", msg_record["mid"], sender_uid):
                            await to_thread(
                                self.messages_cursor.recall_message,
                                msg_record["mid"], sender_uid, emit_event=False)
                            self._queue_ack(websocket, client_mid, status="failed", error="file_reference_failed")
                            continue
                        self._queue_ack(websocket, client_mid, mid=msg_record["mid"], status="sent")
                        notif_dict = {
                            "event" : "message.file",
                            "title" : str(msg_record["send_time"]),
                            "content" : file_hashes,
                            "sender" : sender_str,
                            "meta" : {"quote": quote, "avatar_url": "/avatar/get_avatar/user/{}".format(sender_uid)},
                            "quote" : quote,
                            "mid" : msg_record["mid"],
                            "file_hash" : file_hashes,
                            "client_mid" : client_mid,
                            "room_id" : "G{}".format(gid),
                            "group_id" : gid,
                            "room_seq" : msg_record.get("room_seq")
                        }
                        if quote >= 0:
                            notif_dict["quote_preview"] = await to_thread(
                                self.messages_cursor.get_quote_preview, quote, msg_record)
                        else:
                            notif_dict["quote_preview"] = None
                        if duration is not None:
                            notif_dict["duration"] = duration
                        if self.file_cursor is not None:
                            notif_dict["file"] = _metadata_with_name(
                                await to_thread(
                                    self.file_cursor.get_metadata,
                                    file_hashes,
                                    owner_uid=self.clients_belonged[websocket],
                                ),
                                metadata["file_name"],
                            )
                        room_id = "G{}".format(gid)
                        pref_map = await to_thread(
                            self.messages_cursor.get_room_preference_map, members, room_id
                        ) if members else {}
                        sender_uid = self.clients_belonged[websocket]
                        pending = []
                        for user in members:
                            user_notif = dict(notif_dict)
                            user_notif["mentioned_uids"] = []
                            user_notif["mentions_me"] = False
                            user_notif["should_alert"] = (
                                user != sender_uid
                                and alert_from_preference(pref_map.get(user), user, [])
                            )
                            pending.append((user, user_notif))
                        try:
                            await to_thread(self.push_messages, pending)
                        except Exception as e:
                            print("[WARN] 批量推送失败(群{}): {}".format(gid, e))

                    else:
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_target")

                elif message["type"] in ("call.invite", "call.answer", "call.ice", "call.hangup"):
                    await self._handle_call_message(websocket, message)

                elif message["type"] in ("typing.start", "typing.stop"):
                    if not self._check_ws_rate(sender_uid, max_per_second=20, bucket="typing"):
                        continue
                    room_id = message["room_id"]
                    sender_uid = self.clients_belonged[websocket]
                    if not await to_thread(
                            can_access_room, self.user_cursor, self.group_cursor,
                            sender_uid, room_id):
                        continue
                    # 新增字段：scope（缺省 typing）、progress（仅 uploading）、ts（服务器兜底）
                    scope = message.get("scope", "typing")
                    if scope not in ("typing", "uploading"):
                        continue
                    progress = message.get("progress")
                    if progress is not None:
                        if not isinstance(progress, (int, float)) or not (0 <= progress <= 1):
                            continue
                    ts = message.get("ts")
                    if ts is not None:
                        if not isinstance(ts, (int, float)):
                            ts = None
                        else:
                            ts = int(ts)
                    if ts is None:
                        ts = int(time.time() * 1000)
                    if room_id.startswith('U'):
                        # 私聊：广播给接收方时 room_id 需为接收方视角
                        # 不然就会 BOOM~~
                        # 是的这个 BUG 八百年不能发现
                        target = int(room_id[1:])
                        broadcast = {
                            "type": message["type"],
                            "room_id": "U{}".format(sender_uid),
                            "uid": sender_uid,
                            "scope": scope,
                            "ts": ts,
                        }
                        if progress is not None:
                            broadcast["progress"] = progress
                        with self._clients_lock:
                            clients = list(self.connected_clients.get(target, []))
                        for ws in clients:
                            asyncio.run_coroutine_threadsafe(
                                self._queue_message(ws, broadcast), self.loop)
                    elif room_id.startswith('G'):
                        broadcast = {
                            "type": message["type"],
                            "room_id": room_id,
                            "uid": sender_uid,
                            "scope": scope,
                            "ts": ts,
                        }
                        if progress is not None:
                            broadcast["progress"] = progress
                        gid = int(room_id[1:])
                        members = await to_thread(
                            self.group_cursor.get_member_uids, gid)
                        for user in members:
                            if user != sender_uid:
                                with self._clients_lock:
                                    clients = list(self.connected_clients.get(user, []))
                                for ws in clients:
                                    asyncio.run_coroutine_threadsafe(
                                        self._queue_message(ws, broadcast), self.loop)

                elif message["type"] == "message.read":
                    sender_uid = self.clients_belonged[websocket]
                    if not self._check_ws_rate(sender_uid, max_per_second=5, bucket="read"):
                        continue
                    room_id = message.get("room_id")
                    last_mid = message.get("last_mid")
                    if (not isinstance(room_id, str) or isinstance(last_mid, bool)
                            or not isinstance(last_mid, int) or last_mid <= 0):
                        continue
                    if not await to_thread(
                            can_access_room,
                            self.user_cursor, self.group_cursor, sender_uid, room_id):
                        continue
                    if room_id.startswith('U'):
                        room_key = self.messages_cursor.room_key_of(
                            sender_uid, int(room_id[1:]))
                    else:
                        room_key = self.messages_cursor.room_key_of(
                            sender_uid, 0, group_id=int(room_id[1:]))
                    cursor = await to_thread(
                        self.messages_cursor.sync_cursor_for_mid, room_key, last_mid)
                    if cursor is None or cursor[0] is None:
                        continue
                    if not await to_thread(
                            self.messages_cursor.update_read_state,
                            sender_uid, room_key, cursor[0], last_mid):
                        continue
                    # 多端已读同步
                    self.push_raw(sender_uid, {
                        "type": "message.read",
                        "room_id": room_id,
                        "uid": sender_uid,
                        "last_mid": last_mid,
                        "last_seq": cursor[0],
                        "ts": int(time.time() * 1000),
                    }, exclude_websocket=websocket)
                    # 私聊：把已读水位回执推给对方（内部要读隐私开关 + 推送，
                    # 与其它调用点一致地挪出事件循环线程）
                    if room_id.startswith('U'):
                        await to_thread(
                            self.emit_private_read_receipt,
                            sender_uid, int(room_id[1:]), room_key,
                            last_mid, cursor[0])

        except Exception as e:
            print("[ERR] WS消息处理异常: {}".format(e))

        finally:
            send_task.cancel()
            self._cleanup_client(websocket)
            

    async def main(self):
        print("[INFO] 已严肃启动 TCP 服务器")
        self.loop = asyncio.get_running_loop()
        websocket_logger = logging.getLogger("touchfish.websocket")
        websocket_logger.addFilter(_InvalidHandshakeFilter())
        async with websockets.serve(
            self.handler, "0.0.0.0", self.port_tcp, logger=websocket_logger
        ):
            await asyncio.Future() 
