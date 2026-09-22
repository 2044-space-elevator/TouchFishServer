"""
JSON Web Token (JWT) 认证 Toolbox
"""
from __future__ import annotations

import os
import secrets
import time

import jwt as pyjwt

LEGACY_NOTE = "using uid and password for general authorization is deprecated, please use the jwt auth (see details in docs)"


def load_public_key(key_or_path):
    """加载 RSA/EC 公钥（PEM 字符串或文件路径）。

    返回可直接传给 pyjwt 的公钥对象；失败返回 None。
    """
    if not key_or_path:
        return None
    if isinstance(key_or_path, str) and os.path.exists(key_or_path):
        try:
            with open(key_or_path, "r", encoding="utf-8") as f:
                key_or_path = f.read()
        except Exception:
            return None
    if not isinstance(key_or_path, str):
        return None
    try:
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_public_key(key_or_path.encode("utf-8"))
        return key
    except Exception:
        return None


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


def issue_token(secret, uid, auth_version, expires_seconds, issuer, session_id=None, jti=None, audience=None, key_id=None):
    """签发 JWT。返回 (token, payload)
    似乎想要支持三方验证……？
    """
    now = int(time.time())
    payload = {
        "sub": str(uid),
        "av": int(auth_version),
        "iat": now,
        "exp": now + int(expires_seconds),
        "jti": jti or secrets.token_hex(16),
        "iss": str(issuer),
    }
    if session_id:
        payload["sid"] = str(session_id)
    if audience:
        payload["aud"] = audience
    headers = {}
    if key_id:
        headers["kid"] = key_id
    token = pyjwt.encode(payload, secret, algorithm="HS256", headers=headers)
    return token, payload


def verify_token(secret, token, expected_issuer=None, expected_audience=None, allowed_algorithms=None):
    """校验 JWT，成功返回 payload，失败返回 None。
    """
    if not isinstance(token, str) or not token:
        return None
    algorithms = allowed_algorithms or ["HS256"]
    options = {}
    if expected_issuer is not None:
        options["verify_iss"] = True
    else:
        options["verify_iss"] = False
    if expected_audience is not None:
        options["verify_aud"] = True
    else:
        options["verify_aud"] = False
    try:
        return pyjwt.decode(
            token,
            secret,
            algorithms=algorithms,
            issuer=expected_issuer,
            audience=expected_audience,
            options=options,
        )
    except Exception:
        return None


def verify_external_token(token, external_issuers):
    """按配置的外部 issuer 列表验签第三方签发的 token
    什么是 external issulers？Ask DeepSEEK（）
    """
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(external_issuers, (list, tuple)):
        return None
    for issuer_cfg in external_issuers:
        if not isinstance(issuer_cfg, dict):
            continue
        iss = issuer_cfg.get("iss")
        if not iss:
            continue
        public_key = load_public_key(issuer_cfg.get("public_key_pem") or issuer_cfg.get("public_key"))
        if public_key is None:
            continue
        algorithms = issuer_cfg.get("algorithms") or ["RS256"]
        audience = issuer_cfg.get("audience")
        try:
            options = {
                "verify_iss": True,
                "verify_aud": audience is not None,
            }
            payload = pyjwt.decode(
                token,
                public_key,
                algorithms=algorithms,
                issuer=iss,
                audience=audience,
                options=options,
            )
            return payload
        except Exception:
            continue
    return None
