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
from mention_utils import resolve_mentioned_uids, should_alert
import jwt_tool

async def to_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    func_call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, func_call)

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


def alert_from_preference(pref, uid, mentioned_uids) -> bool:
    """根据房间偏好判断是否需要提醒。pref 为 get_room_preference 的 dict，缺省按 0 级处理。"""
    level = int((pref or {}).get("notify_level", 0))
    if level == 2:
        return False
    if level == 1:
        return uid in mentioned_uids
    return True


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
    def __init__(self, port_api, port_tcp, notification_cursor, user_cursor, messages_cursor,
                 group_cursor, file_cursor=None, jwt_secret=None):
        self.port_api = port_api
        self.port_tcp = port_tcp
        self.notification_cursor = notification_cursor
        self.connected_clients = dict()
        self.connected_clients[-1] = []
        self.clients_belonged = dict()
        self.clients_token = dict()
        self.send_queue = dict()
        self.user_cursor = user_cursor
        self.group_cursor = group_cursor
        self.messages_cursor = messages_cursor
        self.file_cursor = file_cursor
        self.jwt_secret = jwt_tool.load_secret(port_api) if jwt_secret is None else jwt_secret
        self.legacy_auth_enabled = True
        self._load_config()
        self.aes_key = dict()
        self.pri_key = crypto.load_pri("res/{}/secret/pri.pem".format(port_api))
        self.loop = None
        self._ws_lock = threading.Lock()
        self._ws_timestamps = defaultdict(list)
        self._ws_typing_timestamps = defaultdict(list)
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
        t = threading.Thread(target=_loop, daemon=True, name="tfv5-ws-rate-gc")
        t.start()

    def _cleanup_rate_timestamps(self):
        now = time.time()
        cutoff = now - 120
        with self._ws_lock:
            for d in (self._ws_timestamps, self._ws_typing_timestamps):
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
        返回 (uid, jti) 或 None。
        """
        def _check():
            payload = jwt_tool.verify_token(self.jwt_secret, token)
            if payload is None:
                return None
            try:
                uid = int(payload.get("sub"))
            except (TypeError, ValueError):
                return None
            jti = payload.get("jti")
            if not self.user_cursor.token_exists(jti):
                return None
            row = self.user_cursor.uid_query(uid)
            if not row:
                return None
            if self.user_cursor.get_auth_version(uid) != int(payload.get("av", -1)):
                return None
            return (uid, jti)
        return to_thread(_check)

    def _check_ws_rate(self, uid: int, max_per_second: int = 10, bucket: str = "msg") -> bool:
        now = time.time()
        ts_dict = self._ws_typing_timestamps if bucket == "typing" else self._ws_timestamps
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
    
    def encrypt_response(self, req : dict, websocket):
        json_req = json.dumps(req)
        iv, content = crypto.aes_encrypt(json_req, self.aes_key[websocket])
        iv = base64.b64encode(iv).decode('utf-8')
        content = base64.b64encode(content).decode('utf-8')
        return json.dumps({"iv" : iv, "content" : content}) 

    def _verify_quote(self, quote_mid: int, send_to: str, sender_uid: int) -> bool:
        rows = self.messages_cursor.query(
            "SELECT sender_uid, receiver_uid, group_id, deleted FROM messages WHERE mid = ?",
            (quote_mid,)
        )
        if not rows:
            return False
        r = rows[0]
        if r[3]:  # deleted
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
        with self._clients_lock:
            uid = self.clients_belonged.pop(websocket, -1)
            self.clients_token.pop(websocket, None)
            if uid in self.connected_clients and websocket in self.connected_clients[uid]:
                self.connected_clients[uid].remove(websocket)
                if uid != -1 and not self.connected_clients[uid]:
                    del self.connected_clients[uid]
            elif websocket in self.connected_clients[-1]:
                self.connected_clients[-1].remove(websocket)
            self.send_queue.pop(websocket, None)
            self.aes_key.pop(websocket, None)

    async def _queue_message(self, websocket, message : dict):
        queue = self.send_queue.get(websocket)
        if queue is not None:
            await queue.put(message)

    def _queue_ack(self, websocket, client_mid=None, status="sent", mid=None, error=None):
        if client_mid is None or websocket not in self.send_queue or self.loop is None:
            return
        ack = {"type": "message.ack", "client_mid": client_mid, "status": status}
        if mid is not None:
            ack["mid"] = mid
        if error is not None:
            ack["error"] = error
        asyncio.run_coroutine_threadsafe(self.send_queue[websocket].put(ack), self.loop)

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

    def push_raw(self, uid : int, message : dict):
        """按原样中继一条信令/消息给某用户的所有在线连接（不包 MESSAGE.NEW）"""
        if self.loop is None:
            return
        with self._clients_lock:
            clients = list(self.connected_clients.get(uid, []))
        for websocket in clients:
            asyncio.run_coroutine_threadsafe(
                self._queue_message(websocket, message),
                self.loop
            )

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
            if token:
                verified = await self._verify_ws_token(token)
                if verified is not None:
                    verified_uid, verified_jti = verified
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
                if verified_uid not in self.connected_clients.keys():
                    self.connected_clients[verified_uid] = []
                self.connected_clients[verified_uid].append(websocket)
                self.clients_belonged[websocket] = verified_uid
                self.clients_token[websocket] = verified_jti
            self.send_queue[websocket] = asyncio.Queue()
            await websocket.send(self.encrypt_response({"type" : "AUTH.LOGIN_SUCCEEDED"}, websocket))

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
                    if quote >= 0 and not self._verify_quote(quote, send_to, self.clients_belonged[websocket]):
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
                            if not self.messages_cursor.request_matches(
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
                        mentioned_uids = resolve_mentioned_uids(
                            plain, self.user_cursor, [send_to], exclude_uid=sender_uid
                        )
                        self.messages_cursor.set_message_mentions(
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
                        recv_notif["quote_preview"] = (
                            self.messages_cursor.get_quote_preview(quote, msg_record) if quote >= 0 else None
                        )
                        recv_notif["mentioned_uids"] = mentioned_uids
                        recv_notif["mentions_me"] = send_to in mentioned_uids
                        recv_notif["should_alert"] = should_alert(
                            self.messages_cursor, send_to, sender_str, mentioned_uids
                        )
                        sender_notif = dict(recv_notif)
                        sender_notif["room_id"] = "U{}".format(send_to)
                        sender_notif["mentions_me"] = False
                        sender_notif["should_alert"] = False
                        await to_thread(self.push_message, send_to, recv_notif)
                        await to_thread(self.push_message, sender_uid, sender_notif)

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
                            if not self.messages_cursor.request_matches(
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
                        mentioned_uids = resolve_mentioned_uids(
                            plain,
                            self.user_cursor,
                            members,
                            exclude_uid=self.clients_belonged[websocket],
                        )
                        self.messages_cursor.set_message_mentions(
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
                        notif_dict["quote_preview"] = (
                            self.messages_cursor.get_quote_preview(quote, msg_record) if quote >= 0 else None
                        )
                        room_id = "G{}".format(gid)
                        pref_map = self.messages_cursor.get_room_preference_map(members, room_id) if members else {}
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
                    client_mid = message.get('client_mid')
                    if quote < -1:
                        self._queue_ack(websocket, client_mid, status="failed", error="invalid_quote")
                        continue
                    if quote >= 0 and not self._verify_quote(quote, send_to, self.clients_belonged[websocket]):
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
                        metadata = (self.file_cursor.acquire_reference(sender_uid, file_hashes)
                                    if self.file_cursor is not None else None)
                        if metadata is None:
                            self._queue_ack(websocket, client_mid, status="failed", error="file_not_owned")
                            continue
                        try:
                            msg_record = await to_thread(
                                self.messages_cursor.add_message,
                                sender_uid, send_to, file_hashes,
                                content_type='file', file_hash=file_hashes, quote=quote,
                                client_mid=client_mid, file_name=metadata["file_name"]
                            )
                        except Exception:
                            self.file_cursor.decrement_ref(file_hashes)
                            raise
                        if msg_record.get("duplicate"):
                            if not self.messages_cursor.request_matches(
                                    msg_record["mid"], sender_uid, send_to, file_hashes, "file",
                                    file_hash=file_hashes, quote=quote):
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
                            self.messages_cursor.recall_message(msg_record["mid"], sender_uid)
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
                        recv_notif["quote_preview"] = (
                            self.messages_cursor.get_quote_preview(quote, msg_record) if quote >= 0 else None
                        )
                        if self.file_cursor is not None:
                            recv_notif["file"] = _metadata_with_name(
                                self.file_cursor.get_metadata(file_hashes, owner_uid=sender_uid),
                                metadata["file_name"],
                            )
                        recv_notif["mentioned_uids"] = []
                        recv_notif["mentions_me"] = False
                        recv_notif["should_alert"] = should_alert(
                            self.messages_cursor, send_to, sender_str, []
                        )
                        sender_notif = dict(recv_notif)
                        sender_notif["room_id"] = "U{}".format(send_to)
                        sender_notif["should_alert"] = False
                        await to_thread(self.push_message, send_to, recv_notif)
                        await to_thread(self.push_message, sender_uid, sender_notif)

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
                        metadata = (self.file_cursor.acquire_reference(sender_uid, file_hashes)
                                    if self.file_cursor is not None else None)
                        if metadata is None:
                            self._queue_ack(websocket, client_mid, status="failed", error="file_not_owned")
                            continue
                        try:
                            msg_record = await to_thread(
                                self.messages_cursor.add_message,
                                sender_uid, 0, file_hashes,
                                content_type='file', file_hash=file_hashes, quote=quote, group_id=gid,
                                client_mid=client_mid, file_name=metadata["file_name"]
                            )
                        except Exception:
                            self.file_cursor.decrement_ref(file_hashes)
                            raise
                        if msg_record.get("duplicate"):
                            if not self.messages_cursor.request_matches(
                                    msg_record["mid"], sender_uid, 0, file_hashes, "file",
                                    file_hash=file_hashes, quote=quote, group_id=gid):
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
                            self.messages_cursor.recall_message(msg_record["mid"], sender_uid)
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
                        notif_dict["quote_preview"] = (
                            self.messages_cursor.get_quote_preview(quote, msg_record) if quote >= 0 else None
                        )
                        if self.file_cursor is not None:
                            notif_dict["file"] = _metadata_with_name(
                                self.file_cursor.get_metadata(
                                    file_hashes, owner_uid=self.clients_belonged[websocket]
                                ),
                                metadata["file_name"],
                            )
                        room_id = "G{}".format(gid)
                        pref_map = self.messages_cursor.get_room_preference_map(members, room_id) if members else {}
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
                    if not can_access_room(self.user_cursor, self.group_cursor, sender_uid, room_id):
                        continue
                    broadcast = {
                        "type": message["type"],
                        "room_id": room_id,
                        "uid": sender_uid,
                    }
                    if room_id.startswith('U'):
                        target = int(room_id[1:])
                        with self._clients_lock:
                            clients = list(self.connected_clients.get(target, []))
                        for ws in clients:
                            asyncio.run_coroutine_threadsafe(
                                self._queue_message(ws, broadcast), self.loop)
                    elif room_id.startswith('G'):
                        gid = int(room_id[1:])
                        members = self.group_cursor.get_member_uids(gid)
                        for user in members:
                            if user != sender_uid:
                                with self._clients_lock:
                                    clients = list(self.connected_clients.get(user, []))
                                for ws in clients:
                                    asyncio.run_coroutine_threadsafe(
                                        self._queue_message(ws, broadcast), self.loop)

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
