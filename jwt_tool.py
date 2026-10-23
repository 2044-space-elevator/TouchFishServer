"""
JSON Web Token (JWT) 认证 Toolbox

- 本服自签 token：默认 HS256；配置 jwt_signing_algorithm 后可用 RS/ES/PS 非对称算法，
  通过 KeyStore 按 kid 选钥。存量 HS256 token 始终可验，保证平滑切换。
- 外部平台签发的 token：支持静态 PEM / 静态 JWK / 静态 JWKS / 远程 JWKS / OIDC discovery。
  外部 token 一律只接受非对称算法，拒绝 HS* 与 none，避免算法混淆。
"""
from __future__ import annotations

import os
import secrets
import threading
import time

import jwt as pyjwt

import jwks_client
import jwt_keys


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


def issue_token(secret, uid, auth_version, expires_seconds, issuer, session_id=None,
                jti=None, audience=None, key_id=None, *, algorithm="HS256",
                key_store=None):
    """签发 JWT。返回 (token, payload)。

    algorithm 为 HS256 时沿用 jwt_secret；为 RS/ES/PS 等非对称算法时必须提供 key_store，
    由密钥环选择 active 密钥并在 header 写入 kid。
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

    algorithm = jwt_keys.normalize_algorithm(algorithm, default="HS256")
    headers = {}
    if algorithm in jwt_keys.HS_ALGORITHMS:
        signing_key = secret
        if key_id:
            headers["kid"] = key_id
    elif algorithm in jwt_keys.ASYMMETRIC_ALGORITHMS:
        if key_store is None:
            raise ValueError("key_store is required for asymmetric JWT signing")
        signing_key, generated_kid = key_store.signing_key(algorithm)
        headers["kid"] = key_id or generated_kid
    else:
        raise ValueError("unsupported JWT signing algorithm: {}".format(algorithm))

    token = pyjwt.encode(payload, signing_key, algorithm=algorithm, headers=headers)
    return token, payload


def verify_token(secret, token, expected_issuer=None, expected_audience=None,
                 allowed_algorithms=None):
    """校验 HS256 JWT，成功返回 payload，失败返回 None。"""
    if not isinstance(token, str) or not token or token.count(".") != 2:
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


def verify_local_token(secret, token, key_store=None, allowed_algorithms=None):
    """校验本服签发的 token：HS* 用 jwt_secret，非对称用密钥环。"""
    if not isinstance(token, str) or not token or token.count(".") != 2:
        return None
    header = _unverified_header(token)
    if header is None:
        return None
    algorithm = header.get("alg")
    if not isinstance(algorithm, str):
        return None
    if algorithm in jwt_keys.HS_ALGORITHMS:
        if allowed_algorithms is not None and algorithm not in allowed_algorithms:
            return None
        if secret is None:
            return None
        return verify_token(secret, token, allowed_algorithms=[algorithm])
    if algorithm not in jwt_keys.ASYMMETRIC_ALGORITHMS or key_store is None:
        return None
    if allowed_algorithms is not None and algorithm not in allowed_algorithms:
        return None
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        kid = None
    try:
        keys = key_store.public_keys(algorithm, kid)
        if not keys and kid:
            # kid 未命中（例如手工替换密钥文件）：退回该算法全部公钥，仍由签名兜底。
            keys = key_store.public_keys(algorithm)
    except Exception:
        return None
    for _kid, public_key in keys:
        payload = _decode_with_key(token, public_key, algorithm)
        if payload is not None:
            return payload
    return None


def verify_external_token(token, external_issuers):
    """按配置的外部 issuer 验签第三方签发的 token。

    支持：
    - public_key_jwk：单把静态 JWK；
    - jwks：静态 JWKS 文档；
    - public_key_pem / public_key：静态 PEM（字符串或路径）；
    - jwks_uri：远程 JWKS（带缓存、kid 轮换刷新、stale 降级）；
    - discovery / discovery_url：从 OIDC discovery 解析 jwks_uri。

    只接受非对称算法；iss 由配置与 token 共同约束，aud/exp/nbf/leeway/required_claims
    由每个 issuer 条目控制。
    """
    if not isinstance(token, str) or not token or token.count(".") != 2:
        return None
    if not isinstance(external_issuers, (list, tuple)):
        return None
    header = _unverified_header(token)
    if header is None:
        return None
    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in jwt_keys.ASYMMETRIC_ALGORITHMS:
        return None
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        kid = None

    unverified = _unverified_claims(token)
    if not isinstance(unverified, dict):
        return None
    issuer = unverified.get("iss")
    if not isinstance(issuer, str) or not issuer:
        return None

    for issuer_cfg in external_issuers:
        if not isinstance(issuer_cfg, dict) or issuer_cfg.get("iss") != issuer:
            continue
        algorithms = _issuer_algorithms(issuer_cfg)
        if algorithm not in algorithms:
            continue
        audience = _normalize_audience(issuer_cfg.get("audience"))
        leeway = _coerce_int(issuer_cfg.get("leeway_seconds"), 30, 0, 300)
        required_claims = _normalize_required_claims(issuer_cfg.get("required_claims"))
        if _coerce_bool(issuer_cfg.get("require_exp"), False) and "exp" not in required_claims:
            required_claims = required_claims + ["exp"]

        try:
            keys = _resolve_external_keys(issuer_cfg, algorithm, force_refresh=False)
            matched = _match_external_keys(keys, algorithm, kid)
            if not matched and _external_uses_remote(issuer_cfg):
                # kid 未命中：可能是签发方刚轮换密钥，强制刷新一次（内部限流）。
                keys = _resolve_external_keys(issuer_cfg, algorithm, force_refresh=True)
                matched = _match_external_keys(keys, algorithm, kid)
            if not matched and kid:
                # 兼容 JWKS 中不带 kid 的密钥（静态单钥 pin 也走这里）。
                matched = _match_external_keys(keys, algorithm, None)
        except Exception:
            continue
        for public_key in matched:
            payload = _decode_with_key(
                token,
                public_key,
                algorithm,
                issuer=issuer,
                audience=audience,
                leeway=leeway,
                require_claims=required_claims,
            )
            if payload is not None:
                return payload
    return None


def warm_external_issuer(issuer_cfg):
    """hot hot （Plastic English）外部 issuer 的公钥（供启动后台任务使用）。"""
    if not isinstance(issuer_cfg, dict) or not issuer_cfg.get("iss"):
        return False
    algorithms = _issuer_algorithms(issuer_cfg)
    if not algorithms:
        return False
    for algorithm in algorithms:
        try:
            if _resolve_external_keys(issuer_cfg, algorithm, force_refresh=False):
                return True
        except Exception:
            continue
    return False


# internal pls

_WARN_LOCK = threading.Lock()
_WARN_TIMES = {}


def _warn_once(key, message, interval=300.0):
    """同一配置问题 5 分钟内只提示一次，避免每个请求刷屏。"""
    now = time.time()
    with _WARN_LOCK:
        last = _WARN_TIMES.get(key, 0)
        if now - last < interval:
            return
        _WARN_TIMES[key] = now
    print("[WARN] {}".format(message))


def _unverified_header(token):
    try:
        return pyjwt.get_unverified_header(token)
    except Exception:
        return None


def _unverified_claims(token):
    try:
        return pyjwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except Exception:
        return None


def _decode_with_key(token, key, algorithm, issuer=None, audience=None, leeway=0,
                     require_claims=None):
    options = {
        "verify_iss": issuer is not None,
        "verify_aud": audience is not None,
        "verify_iat": False,
    }
    claims = [claim for claim in (require_claims or []) if isinstance(claim, str) and claim]
    if claims:
        options["require"] = claims
    kwargs = {"algorithms": [algorithm], "options": options}
    if issuer is not None:
        kwargs["issuer"] = issuer
    if audience is not None:
        kwargs["audience"] = audience
    if leeway:
        kwargs["leeway"] = leeway
    try:
        return pyjwt.decode(token, key, **kwargs)
    except Exception:
        return None


def _issuer_algorithms(issuer_cfg):
    raw = issuer_cfg.get("algorithms")
    if raw is None:
        return ["RS256"]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ["RS256"]
    algorithms = []
    for item in raw:
        algorithm = jwt_keys.normalize_algorithm(item, default=None)
        if (algorithm in jwt_keys.ASYMMETRIC_ALGORITHMS
                and algorithm not in algorithms):
            algorithms.append(algorithm)
    if not algorithms:
        _warn_once(
            ("algorithms", issuer_cfg.get("iss")),
            "外部 issuer {} 的 algorithms 没有可用的非对称算法，已忽略".format(
                issuer_cfg.get("iss")),
        )
    return algorithms


def _normalize_audience(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, (list, tuple)):
        values = [item for item in raw if isinstance(item, str) and item]
        return values or None
    return None


def _normalize_required_claims(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    claims = []
    for item in raw:
        if isinstance(item, str) and item and item not in claims:
            claims.append(item)
    return claims


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "y", "on"):
            return True
        if lowered in ("0", "false", "no", "n", "off", ""):
            return False
    return default


def _coerce_int(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def _external_uses_remote(issuer_cfg):
    if (issuer_cfg.get("public_key_jwk")
            or issuer_cfg.get("public_key_pem")
            or issuer_cfg.get("public_key")
            or issuer_cfg.get("jwks")):
        return False
    return bool(issuer_cfg.get("jwks_uri")
                or _coerce_bool(issuer_cfg.get("discovery"), False))


def _remote_options(issuer_cfg):
    return {
        "timeout": issuer_cfg.get("jwks_timeout_seconds", 5),
        "cache_seconds": issuer_cfg.get("jwks_cache_seconds", 600),
        "stale_seconds": issuer_cfg.get("jwks_stale_seconds", 86400),
        "allow_insecure_http": _coerce_bool(issuer_cfg.get("allow_insecure_http"), False),
    }


def _resolve_external_keys(issuer_cfg, algorithm, force_refresh=False):
    """把 issuer 配置解析成 RemoteKey 列表；任何异常都转成空列表。"""
    jwk = issuer_cfg.get("public_key_jwk")
    if isinstance(jwk, dict):
        try:
            return [jwks_client.parse_jwk(jwk)]
        except jwks_client.JWKSError as error:
            _warn_once(
                ("jwk", issuer_cfg.get("iss")),
                "外部 issuer {} 的 public_key_jwk 无效: {}".format(
                    issuer_cfg.get("iss"), error),
            )
            return []

    jwks_document = issuer_cfg.get("jwks")
    if isinstance(jwks_document, dict):
        try:
            return jwks_client.parse_jwks(jwks_document)
        except jwks_client.JWKSError as error:
            _warn_once(
                ("jwks", issuer_cfg.get("iss")),
                "外部 issuer {} 的 jwks 无效: {}".format(
                    issuer_cfg.get("iss"), error),
            )
            return []

    pem_source = issuer_cfg.get("public_key_pem") or issuer_cfg.get("public_key")
    if pem_source:
        public_key = load_public_key(pem_source)
        if public_key is None:
            _warn_once(
                ("pem", issuer_cfg.get("iss")),
                "外部 issuer {} 的公钥 PEM 无法解析".format(issuer_cfg.get("iss")),
            )
            return []
        return [jwks_client.RemoteKey(
            kid=None,
            kty=jwt_keys.key_type_of(public_key) or "RSA",
            alg=None,
            use=None,
            key_ops=None,
            public_key=public_key,
        )]

    options = _remote_options(issuer_cfg)
    jwks_uri = issuer_cfg.get("jwks_uri")
    if not jwks_uri and _coerce_bool(issuer_cfg.get("discovery"), False):
        try:
            jwks_uri = jwks_client.discover(
                issuer_cfg.get("iss"),
                discovery_url=issuer_cfg.get("discovery_url"),
                force_refresh=force_refresh,
                **options
            )
        except jwks_client.JWKSError:
            return []
    if not jwks_uri:
        return []
    try:
        return jwks_client.fetch_jwks(
            jwks_uri, force_refresh=force_refresh, **options
        )
    except jwks_client.JWKSError:
        return []


def _match_external_keys(keys, algorithm, kid):
    candidates = []
    for key in keys:
        if not isinstance(key, jwks_client.RemoteKey):
            continue
        if not key.usable_for_signing_verification():
            continue
        if not jwks_client.key_matches_algorithm(key, algorithm):
            continue
        candidates.append(key)
    if kid:
        exact = [key for key in candidates if key.kid == kid]
        return [key.public_key for key in exact]
    return [key.public_key for key in candidates]
