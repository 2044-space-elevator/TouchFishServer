"""
JSON Web Token (JWT) 认证 Toolbox
"""
from __future__ import annotations

import os
import secrets
import time

import jwt as pyjwt

LEGACY_NOTE = "using uid and password for general authorization is deprecated, please use the jwt auth (see details in docs)"


def secret_path(port_api) -> str:
    return "res/{}/secret/jwt_secret".format(port_api)


def generate_secret_if_missing(port_api):
    """密钥文件不存在时自动生成（HS256 密钥，32B 随机海克斯）"""
    path = secret_path(port_api)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as file:
            file.write(secrets.token_hex(32))
    return path


def load_secret(port_api) -> str:
    """加载（必要时生成）JWT 签名密钥。"""
    path = generate_secret_if_missing(port_api)
    with open(path, "r", encoding="utf-8") as file:
        secret = file.read().strip()
    if not secret:
        with open(path, "w", encoding="utf-8") as file:
            file.write(secrets.token_hex(32))
        with open(path, "r", encoding="utf-8") as file:
            secret = file.read().strip()
    return secret


def issue_token(secret, uid, auth_version, expires_seconds, issuer):
    """签发 JWT。返回 (token, payload)。"""
    now = int(time.time())
    payload = {
        "sub": str(uid),
        "av": int(auth_version),
        "iat": now,
        "exp": now + int(expires_seconds),
        "jti": secrets.token_hex(16),
        "iss": str(issuer),
    }
    token = pyjwt.encode(payload, secret, algorithm="HS256")
    return token, payload


def verify_token(secret, token):
    """校验 JWT，成功返回 payload，失败返回 None。"""
    if not isinstance(token, str) or not token:
        return None
    try:
        return pyjwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None
