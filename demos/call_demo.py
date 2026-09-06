#!/usr/bin/env python3
"""
TouchFish 1v1 视频通话信令端到端验证。

用法:
    python demos/call_demo.py

在 TFS 仓库根目录运行。脚本会：
  1. 在临时端口上初始化服务器（RSA 密钥 + SQLite + 两个用户 + 好友关系）
  2. 以 --dev-server --start-api 拉起后台服务器
  3. 两个 WS 客户端完成真实加密握手
  4. 走完 invite → answer → ice → hangup 全程
  5. 逐条输出 PASS / FAIL
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 确保 import 能从仓库根目录找到 crypto / db 模块
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
os.chdir(str(_root))

# ---------- 常量 ----------

PORT_API = 19999
PORT_TCP = 19998
PUB_PATH  = f"res/{PORT_API}/secret/pub.pem"
PRI_PATH  = f"res/{PORT_API}/secret/pri.pem"
PASS1, PASS2 = "demo_alice_1", "demo_bob_2"
STUN_URLS = ["stun:stun.miwifi.com:3478"]

# ---------- 导入 TFS 工具 ----------

from crypto import aes_encrypt, aes_decrypt

# ---------- 结果统计 ----------

passed  = 0
failed  = 0
_results: list[tuple[str, bool, str]] = []

def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    tag = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    msg = f"  [{tag}] {label}" + (f" — {detail}" if detail else "")
    print(msg, flush=True)
    _results.append((label, ok, detail))


# ---------- 网络工具 ----------

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from crypto import aes_encrypt, aes_decrypt   # 复用 TFS 实现，保证格式一致


def rsa_encrypt(pub, plaintext: bytes) -> bytes:
    return pub.encrypt(
        plaintext,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )


def frame_plain(aes_key: bytes, msg: dict) -> str:
    """构造 AES 加密后的明文帧。"""
    plain = json.dumps(msg)
    iv, ct = aes_encrypt(plain, aes_key)
    return json.dumps({
        "iv": base64.b64encode(iv).decode(),
        "content": base64.b64encode(ct).decode(),
    })


def decode_frame(aes_key: bytes, raw: str) -> dict:
    """解密服务端推送的加密帧。"""
    frame = json.loads(raw)
    iv  = base64.b64decode(frame["iv"])
    ct  = base64.b64decode(frame["content"])
    return json.loads(aes_decrypt(iv, ct, aes_key))


# ---------- 调用客户端 ----------

class WsClient:
    """模拟 TouchFish 客户端，完成加密握手。"""

    def __init__(self, pub_key, uid: int, password: str):
        self._pub  = pub_key
        self.uid   = uid
        self._pwd  = password
        self.aes   = os.urandom(32)
        self.ws    = None
        self._received: list[dict] = []

    async def connect(self):
        import websockets as ws_lib
        self.ws = await ws_lib.connect(
            f"ws://127.0.0.1:{PORT_TCP}",
            max_size=1 << 20,
        )

    async def handshake(self):
        # 1) REQ.UPDATE_AES_KEY
        enc_key = rsa_encrypt(self._pub, self.aes)
        await self.ws.send(json.dumps({
            "type": "REQ.UPDATE_AES_KEY",
            "aes_key": base64.b64encode(enc_key).decode(),
        }))
        # 2) AUTH.LOGIN（legacy）
        await self.ws.send(frame_plain(self.aes, {
            "type": "AUTH.LOGIN",
            "uid": self.uid,
            "password": self._pwd,
        }))
        # 等 LOGIN_SUCCEEDED
        raw = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
        try:
            resp = decode_frame(self.aes, raw)
        except Exception:
            print(f"    [dbg] {self.uid} received non-protocol frame: {raw!r}")
            raise
        return resp.get("type") == "AUTH.LOGIN_SUCCEEDED"

    async def recv(self, timeout: float = 2.0) -> dict | None:
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            msg = decode_frame(self.aes, raw)
            self._received.append(msg)
            return msg
        except asyncio.TimeoutError:
            return None

    async def recv_until(self, mtype: str, timeout: float = 3.0,
                         match: dict | None = None) -> dict | None:
        """跳过无关帧（如发给自身的 ack），等待指定类型的下一条消息。"""
        def _matches(msg: dict | None) -> bool:
            if msg is None or msg.get("type") != mtype:
                return False
            if match:
                return all(msg.get(k) == v for k, v in match.items())
            return True

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            # 先翻内存里已收到的帧
            for i, msg in enumerate(self._received):
                if _matches(msg):
                    return self._received.pop(i)
            if self._received:
                self._received.pop(0)
                continue
            rest = deadline - asyncio.get_event_loop().time()
            if rest <= 0:
                return None
            msg = await self.recv(timeout=rest)
            if _matches(msg):
                return msg

    async def send(self, msg: dict):
        await self.ws.send(frame_plain(self.aes, msg))

    def drain_received(self) -> list[dict]:
        out = list(self._received)
        self._received.clear()
        return out

    async def close(self):
        if self.ws:
            await self.ws.close()


# ---------- 引导服务器 ----------

def bootstrap_server():
    """非交互式：生成 RSA 密钥、写 config、建库、建两个好友用户。"""
    import shutil
    import db as db_mod
    from argon2 import PasswordHasher
    from crypto import generate_rsa_keys

    # 清理旧状态，保证每次运行都是干净的
    res_dir = Path(f"res/{PORT_API}")
    if res_dir.exists():
        shutil.rmtree(res_dir)

    os.makedirs(f"res/{PORT_API}/secret", exist_ok=True)
    os.makedirs(f"res/{PORT_API}/db", exist_ok=True)
    os.makedirs(f"res/{PORT_API}/captcha", exist_ok=True)
    os.makedirs(f"res/{PORT_API}/forum", exist_ok=True)
    os.makedirs(f"res/{PORT_API}/sticker", exist_ok=True)
    os.makedirs(f"res/{PORT_API}/file", exist_ok=True)
    os.makedirs(f"res/{PORT_API}/announcement", exist_ok=True)

    # RSA
    pri, pub, pri_pem, pub_pem, pub_hash = generate_rsa_keys()
    (Path(PUB_PATH)).write_bytes(pub_pem)
    (Path(PRI_PATH)).write_bytes(pri_pem)

    # server_config.json
    sc = {"0": [str(PORT_API), str(PORT_TCP)]}
    (Path("server_config.json")).write_text(json.dumps(sc, indent=2))

    # config.json
    cfg = {
        "server_name": "TouchFish-CallDemo",
        "port_api": PORT_API,
        "port_tcp": PORT_TCP,
        "captcha": False,
        "email_activate": "",
        "email_password": "",
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "reverse_proxy_enabled": False,
        "proxy_count": 1,
        "file_last_time": 72,
        "groups_limit": 30,
        "single_group_max_people": 200,
        "default_join_targets": [],
        "rate_limits": {"default": {"requests": 600, "range": 60}},
        "max_file_size": 73400320,
        "user_storage_quota": 73400320,
        "max_user_storage_quota": 73400320,
        "max_sticker_storage_quota": 31457280,
        "legacy_auth_enabled": True,
        "jwt_expires_seconds": 604800,
        "jwt_max_per_user": 5,
    }
    (Path(f"res/{PORT_API}/config.json")).write_text(json.dumps(cfg, indent=2))
    (Path(f"res/{PORT_API}/captcha/captcha.json")).write_text("{}")
    (Path(f"res/{PORT_API}/activate.json")).write_text("{}")
    (Path(f"res/{PORT_API}/forum/queue.json")).write_text('{"queue_num": 0}')
    (Path(f"res/{PORT_API}/forum/comments.json")).write_text("{}")
    (Path(f"res/{PORT_API}/announcement/announcement.json")).write_text("{}")

    # DB
    hasher = PasswordHasher(time_cost=1, memory_cost=4096, parallelism=2, hash_len=24, salt_len=16)

    user_db = db_mod.UserDb(hasher, f"res/{PORT_API}/db/user.db", PORT_API, PORT_TCP)
    user_db.create_user_table()
    user_db.create_friend_table()
    user_db.user_create("alice", PASS1, time.time())
    user_db.user_create("bob2",  PASS2, time.time())
    alice_row = user_db.query("SELECT uid FROM users WHERE username = ?", ("alice",))
    bob_row = user_db.query("SELECT uid FROM users WHERE username = ?", ("bob2",))
    uid1 = alice_row[0][0] if alice_row else None
    uid2 = bob_row[0][0] if bob_row else None
    if uid1 is None or uid2 is None or uid1 == uid2:
        raise RuntimeError(f"failed to create users: uid1={uid1}, uid2={uid2}")
    user_db.ensure_friend(uid1, uid2)

    db_mod.ForumDb(f"res/{PORT_API}/db/forum.db", PORT_API, PORT_TCP).create_forum_table()
    db_mod.FileDb(f"res/{PORT_API}/file/file.db", PORT_API).create_file_db()
    db_mod.StickerDb(f"res/{PORT_API}/db/sticker.db", PORT_API)
    db_mod.NotificationsDb(f"res/{PORT_API}/db/notification.db", PORT_API).create_user_table(uid1)
    db_mod.NotificationsDb(f"res/{PORT_API}/db/notification.db", PORT_API).create_user_table(uid2)
    db_mod.MessagesDb(f"res/{PORT_API}/db/messages.db", PORT_API)
    db_mod.GroupDb(f"res/{PORT_API}/db/group.db", PORT_API).create_group_table()

    from avatar import init as avatar_init
    avatar_init(PORT_API)

    return uid1, uid2, pub


_server_proc: subprocess.Popen | None = None
_server_log = Path(_root) / "demos" / "_server.log"

def start_server():
    global _server_proc
    _server_log.unlink(missing_ok=True)
    with open(_server_log, "w", encoding="utf-8") as fh:
        _server_proc = subprocess.Popen(
            [sys.executable, "main.py", "--use-config", "0", "--start-api", "--dev-server"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            text=True,
        )

def dump_server_log():
    if _server_log.exists():
        content = _server_log.read_text(encoding="utf-8", errors="replace")
        print("---- server log tail ----")
        print("\n".join(content.splitlines()[-30:]))
        print("--------------------------")

def stop_server():
    global _server_proc
    if _server_proc:
        _server_proc.terminate()
        _server_proc.wait(timeout=5)
        _server_proc = None


# ---------- 主流程 ----------

async def run_demo():
    global passed, failed

    # 引导
    try:
        uid1, uid2, pub = bootstrap_server()
        check("server bootstrap (RSA + DB + 2 users + friends)", True)
    except Exception as e:
        check("server bootstrap", False, str(e))
        return

    # 启动
    start_server()
    # 等待服务器就绪：轮询 TCP 端口可连接即可
    ready = False
    for _ in range(40):
        await asyncio.sleep(0.5)
        try:
            r, w = await asyncio.open_connection("127.0.0.1", PORT_TCP)
            w.close()
            await w.wait_closed()
            ready = True
            break
        except Exception:
            pass
    check("server started", ready,
          "" if ready else "server did not come up within 20s")
    if not ready:
        stop_server()
        return

    alice = WsClient(pub, uid1, PASS1)
    bob   = WsClient(pub, uid2, PASS2)
    call_id = None

    try:
        # ---------- 握手 ----------
        await alice.connect()
        check("alice ws connect", True)
    except Exception as e:
        check("alice ws connect", False, str(e)); stop_server(); return
    try:
        ok = await alice.handshake()
        check("alice AUTH.LOGIN", ok, "expected LOGIN_SUCCEEDED")
    except Exception as e:
        check("alice AUTH.LOGIN", False, str(e)); stop_server(); return
    try:
        await bob.connect()
        ok = await bob.handshake()
        check("bob AUTH.LOGIN", ok)
    except Exception as e:
        check("bob AUTH.LOGIN", False, str(e)); stop_server(); return

    # ---------- call.invite (alice → bob) ----------
    call_id = f"{int(time.time()*1000)}-{uid1}-1234"
    alice_invite = {
        "type": "call.invite",
        "call_id": call_id,
        "target_uid": uid2,
        "payload": {"sdp": "fake-offer-sdp-12345678901234567890123456789012", "sdp_type": "offer"},
    }
    try:
        await alice.send(alice_invite)
        ack = await alice.recv_until(
            "call.ack", timeout=3.0, match={"call_id": call_id, "for": "call.invite"})
        ack_ok = (
            ack is not None
            and ack.get("call_id") == call_id
            and ack.get("for") == "call.invite"
            and ack.get("status") == "delivered"
        )
        check("call.invite → alice gets ack (delivered)", ack_ok,
              "" if ack_ok else f"got: {ack}")
    except Exception as e:
        check("call.invite → alice gets ack", False, str(e))

    # bob 应收到 invite
    try:
        invite = await bob.recv_until("call.invite", timeout=3.0)
        invite_ok = (
            invite is not None
            and invite.get("type") == "call.invite"
            and invite.get("call_id") == call_id
            and invite.get("from_uid") == uid1
            and isinstance(invite.get("payload", {}).get("sdp"), str)
        )
        check("call.invite → bob receives invite", invite_ok,
              "" if invite_ok else f"got: {invite}")
    except Exception as e:
        check("call.invite → bob receives invite", False, str(e))

    # ---------- call.answer (bob → alice) ----------
    answer_payload = {"sdp": "fake-answer-sdp-abcdef0123456789abcdef012345678901", "sdp_type": "answer"}
    bob_answer = {
        "type": "call.answer",
        "call_id": call_id,
        "target_uid": uid1,
        "payload": answer_payload,
    }
    try:
        await bob.send(bob_answer)
        ack = await bob.recv_until(
            "call.ack", timeout=3.0, match={"call_id": call_id, "for": "call.answer"})
        check("call.answer → bob gets ack", ack is not None and ack.get("status") == "delivered",
              f"got: {ack}")
    except Exception as e:
        check("call.answer → bob gets ack", False, str(e))

    # alice 应收到 answer
    try:
        answer = await alice.recv_until("call.answer", timeout=3.0)
        ans_ok = (
            answer is not None
            and answer.get("type") == "call.answer"
            and answer.get("from_uid") == uid2
            and isinstance(answer.get("payload", {}).get("sdp"), str)
        )
        check("call.answer → alice receives answer", ans_ok,
              "" if ans_ok else f"got: {answer}")
    except Exception as e:
        check("call.answer → alice receives answer", False, str(e))

    # ---------- call.ice (互换 candidates) ----------
    candidate = {
        "candidate": "candidate:1 1 udp 2130706431 192.168.1.100 50000 typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
    }
    alice_ice = {
        "type": "call.ice",
        "call_id": call_id,
        "target_uid": uid2,
        "candidate": candidate,
    }
    try:
        await alice.send(alice_ice)
        # call.ice 不需要 ack（即时中继），检查 bob 收到
        ice = await bob.recv_until("call.ice", timeout=3.0)
        ice_ok = (
            ice is not None
            and ice.get("type") == "call.ice"
            and ice.get("from_uid") == uid1
            and ice.get("candidate", {}).get("candidate", "").startswith("candidate:")
        )
        check("call.ice (alice→bob) relayed", ice_ok,
              "" if ice_ok else f"got: {ice}")
    except Exception as e:
        check("call.ice (alice→bob)", False, str(e))

    bob_ice = {
        "type": "call.ice",
        "call_id": call_id,
        "target_uid": uid1,
        "candidate": {
            "candidate": "candidate:2 1 udp 2130706431 10.0.0.1 60000 typ host",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
        },
    }
    try:
        await bob.send(bob_ice)
        ice = await alice.recv_until("call.ice", timeout=3.0)
        ice_ok = (
            ice is not None
            and ice.get("type") == "call.ice"
            and ice.get("from_uid") == uid2
        )
        check("call.ice (bob→alice) relayed", ice_ok,
              "" if ice_ok else f"got: {ice}")
    except Exception as e:
        check("call.ice (bob→alice)", False, str(e))

    # ---------- call.hangup (alice 挂断) ----------
    hangup = {
        "type": "call.hangup",
        "call_id": call_id,
        "target_uid": uid2,
        "reason": "hangup",
    }
    try:
        await alice.send(hangup)
        await asyncio.sleep(0.3)
        remote_hangup = await bob.recv_until("call.hangup", timeout=3.0)
        rh_ok = (
            remote_hangup is not None
            and remote_hangup.get("type") == "call.hangup"
            and remote_hangup.get("from_uid") == uid1
            and remote_hangup.get("reason") == "hangup"
        )
        check("call.hangup (alice→bob) relayed", rh_ok,
              "" if rh_ok else f"got: {remote_hangup}")
    except Exception as e:
        check("call.hangup", False, str(e))

    # ---------- 非好友拒绝 ----------
    fake_call_id = f"{int(time.time()*1000)}-{uid1}-9999"
    alice_bad = {
        "type": "call.invite",
        "call_id": fake_call_id,
        "target_uid": 99999,  # 不存在的用户
        "payload": {"sdp": "x" * 32, "sdp_type": "offer"},
    }
    try:
        await alice.send(alice_bad)
        bad_ack = await alice.recv_until(
            "call.ack", timeout=3.0, match={"call_id": fake_call_id})
        bad_ok = (
            bad_ack is not None
            and bad_ack.get("type") == "call.ack"
            and bad_ack.get("call_id") == fake_call_id
            and bad_ack.get("status") in ("offline", "not_friends", "invalid_target")
        )
        check("call.invite to nonexistent user → ack (not delivered)", bad_ok,
              "" if bad_ok else f"got: {bad_ack}")
    except Exception as e:
        check("call.invite to nonexistent user", False, str(e))

    # ---------- 未加密帧应被忽略 ----------
    try:
        await alice.ws.send(json.dumps({
            "type": "call.hangup",
            "call_id": "raw-should-be-ignored",
            "target_uid": uid2,
            "reason": "hangup",
        }))
        # bob 不应收到任何内容
        raw = await bob.recv(1.0)
        check("unencrypted frame is ignored by server", raw is None,
              "" if raw is None else f"unexpected: {raw}")
    except Exception as e:
        check("unencrypted frame handling", False, str(e))

    await alice.close()
    await bob.close()

    # ---------- 结果 ----------
    print(f"\n{'='*60}")
    print(f"  结果: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    if failed > 0:
        sys.exit(1)


def main():
    print("="*60)
    print("  TouchFish 1v1 视频通话信令 · 端到端验证")
    print("="*60)
    try:
        asyncio.run(run_demo())
    finally:
        stop_server()
    if failed > 0:
        dump_server_log()


if __name__ == "__main__":
    main()
