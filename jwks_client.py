"""
外部签发方 JWKS / OIDC discovery CLIENT

Copyright (c) 2026 ILoveScratch2
Licensed under the MIT License.

~~好吧其实是 `deepseek-v4.1-flash-1017`~~

不依赖 Flask / PyJWT！
"""
from __future__ import annotations

import base64
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec, rsa

import jwt_keys


MAX_RESPONSE_BYTES = 256 * 1024
MIN_RSA_BITS = 2048
MIN_REFRESH_INTERVAL = 15.0
DEFAULT_TIMEOUT = 5.0
DEFAULT_CACHE_SECONDS = 600
DEFAULT_STALE_SECONDS = 86400
DEFAULT_DISCOVERY_CACHE_SECONDS = 900
USER_AGENT = "TouchFish-V5-JWKS/1.0"

_CURVES = {
    "P-256": ec.SECP256R1(),
    "P-384": ec.SECP384R1(),
    "P-521": ec.SECP521R1(),
}


class JWKSError(Exception):
    """JWKS 拉取 / 解析失败。"""


@dataclass(frozen=True)
class RemoteKey:
    kid: str | None
    kty: str
    alg: str | None
    use: str | None
    key_ops: tuple[str, ...] | None
    public_key: object

    def usable_for_signing_verification(self):
        if self.use is not None and self.use != "sig":
            return False
        if self.key_ops is not None and "verify" not in self.key_ops:
            return False
        return True


_CACHE = {}
_CACHE_LOCK = threading.RLock()
_URI_LOCKS = {}
_DISCOVERY_CACHE = {}
_LAST_WARN = {}


def reset_cache():
    """清空缓存（主要供测试使用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()
        _DISCOVERY_CACHE.clear()
        _LAST_WARN.clear()


def _lock_for(key):
    with _CACHE_LOCK:
        lock = _URI_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _URI_LOCKS[key] = lock
        return lock


def _redact_url(url):
    """日志里移除 Herobrine"""
    try:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((
            parsed.scheme, parsed.netloc, parsed.path, "", ""
        ))
    except Exception:
        return "<invalid-url>"


def _warn(key, message):
    now = time.time()
    with _CACHE_LOCK:
        last = _LAST_WARN.get(key, 0)
        if now - last < 60:
            return
        _LAST_WARN[key] = now
    print("[WARN] {}".format(message))


def _coerce_seconds(value, default, minimum=0.0, maximum=365 * 86400):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def _is_disallowed_ip(address):
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return _is_disallowed_ip(address.ipv4_mapped)
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_url(url, allow_insecure_http=False):
    """校验 JWKS / discovery URL"""
    if not isinstance(url, str) or not url.strip():
        raise JWKSError("URL is empty")
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        hostname = parsed.hostname
    except ValueError as error:
        raise JWKSError("invalid URL") from error
    if parsed.scheme not in ("https", "http"):
        raise JWKSError("unsupported URL scheme: {}".format(parsed.scheme))
    if parsed.scheme == "http" and not allow_insecure_http:
        raise JWKSError("insecure HTTP is not allowed")
    if not hostname:
        raise JWKSError("URL has no host")
    if parsed.username or parsed.password:
        raise JWKSError("URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as error:
        raise JWKSError("invalid URL port") from error
    if allow_insecure_http:
        return
    host = hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise JWKSError("loopback host is not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_disallowed_ip(literal):
            raise JWKSError("private/loopback address is not allowed")
        return
    try:
        infos = socket.getaddrinfo(
            host, port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise JWKSError("unable to resolve host") from error
    for info in infos:
        address = info[4][0]
        try:
            parsed_ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError:
            continue
        if _is_disallowed_ip(parsed_ip):
            raise JWKSError("host resolves to a private address")


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_insecure_http):
        self.allow_insecure_http = allow_insecure_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        validate_url(target, self.allow_insecure_http)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get_json(url, timeout, allow_insecure_http):
    validate_url(url, allow_insecure_http)
    opener = urllib.request.build_opener(
        _ValidatingRedirectHandler(allow_insecure_http)
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is not None and status != 200:
                raise JWKSError("HTTP status {}".format(status))
            validate_url(response.geturl(), allow_insecure_http)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except JWKSError:
        raise
    except urllib.error.HTTPError as error:
        try:
            error.close()
        except Exception:
            pass
        raise JWKSError("HTTP status {}".format(error.code)) from error
    except urllib.error.URLError as error:
        raise JWKSError("network error: {}".format(getattr(error, "reason", error))) from error
    except (TimeoutError, socket.timeout) as error:
        raise JWKSError("request timed out") from error
    except OSError as error:
        raise JWKSError("network error: {}".format(error)) from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JWKSError("response is too large")
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JWKSError("invalid JSON response") from error
    if not isinstance(document, dict):
        raise JWKSError("JSON response must be an object")
    return document


def _decode_b64u(value, name):
    if not isinstance(value, str) or not value:
        raise JWKSError("missing {}".format(name))
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as error:
        raise JWKSError("invalid base64url {}".format(name)) from error


def parse_jwk(jwk):
    """把一个 JWK 解析成 RemoteKey!！！只接受 RSA / EC 公钥。"""
    if not isinstance(jwk, dict):
        raise JWKSError("JWK must be an object")
    kty = jwk.get("kty")
    kid = jwk.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise JWKSError("JWK kid must be a string")
    alg = jwk.get("alg")
    if alg is not None and not isinstance(alg, str):
        raise JWKSError("JWK alg must be a string")
    use = jwk.get("use")
    if use is not None and not isinstance(use, str):
        raise JWKSError("JWK use must be a string")
    raw_ops = jwk.get("key_ops")
    key_ops = None
    if isinstance(raw_ops, list):
        key_ops = tuple(item for item in raw_ops if isinstance(item, str))

    if kty == "RSA":
        n_bytes = _decode_b64u(jwk.get("n"), "n")
        e_bytes = _decode_b64u(jwk.get("e"), "e")
        n = int.from_bytes(n_bytes, "big")
        e = int.from_bytes(e_bytes, "big")
        if n.bit_length() < MIN_RSA_BITS:
            raise JWKSError("RSA key is too small")
        if e < 3 or e > 2 ** 31 - 1 or e % 2 == 0:
            raise JWKSError("invalid RSA exponent")
        try:
            public_key = rsa.RSAPublicNumbers(e, n).public_key()
        except ValueError as error:
            raise JWKSError("invalid RSA key") from error
    elif kty == "EC":
        crv = jwk.get("crv")
        curve = _CURVES.get(crv)
        if curve is None:
            raise JWKSError("unsupported EC curve: {}".format(crv))
        x_bytes = _decode_b64u(jwk.get("x"), "x")
        y_bytes = _decode_b64u(jwk.get("y"), "y")
        size = (curve.key_size + 7) // 8
        if not x_bytes or not y_bytes or len(x_bytes) > size or len(y_bytes) > size:
            raise JWKSError("invalid EC coordinate length")
        x = int.from_bytes(x_bytes, "big")
        y = int.from_bytes(y_bytes, "big")
        try:
            public_key = ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
        except ValueError as error:
            raise JWKSError("invalid EC key") from error
    else:
        raise JWKSError("unsupported JWK kty: {}".format(kty))

    return RemoteKey(
        kid=kid,
        kty=kty,
        alg=alg,
        use=use,
        key_ops=key_ops,
        public_key=public_key,
    )


def parse_jwks(document):
    """解析 JWKS 文档"""
    if not isinstance(document, dict):
        raise JWKSError("JWKS must be an object")
    keys = document.get("keys")
    if not isinstance(keys, list):
        raise JWKSError("JWKS has no keys array")
    parsed = []
    failed = 0
    for item in keys:
        try:
            key = parse_jwk(item)
        except JWKSError:
            failed += 1
            continue
        if not key.usable_for_signing_verification():
            continue
        parsed.append(key)
    if failed:
        _warn(("parse", failed), "JWKS 中有 {} 个密钥无法解析，已忽略".format(failed))
    if not parsed and keys:
        raise JWKSError("JWKS contains no usable keys")
    return parsed


def fetch_jwks(uri, *, timeout=DEFAULT_TIMEOUT, cache_seconds=DEFAULT_CACHE_SECONDS,
               stale_seconds=DEFAULT_STALE_SECONDS, allow_insecure_http=False,
               force_refresh=False):
    """拉取（或读取缓存）远程 JWKS，返回 RemoteKey 列表。"""
    if not isinstance(uri, str) or not uri.strip():
        raise JWKSError("JWKS URI is empty")
    timeout = _coerce_seconds(timeout, DEFAULT_TIMEOUT, 0.5, 30.0)
    cache_seconds = _coerce_seconds(cache_seconds, DEFAULT_CACHE_SECONDS, 0.0, 7 * 86400)
    stale_seconds = _coerce_seconds(stale_seconds, DEFAULT_STALE_SECONDS, 0.0, 365 * 86400)

    lock = _lock_for(uri)
    with lock:
        now = time.time()
        with _CACHE_LOCK:
            entry = _CACHE.get(uri)
        cached_keys = entry.get("keys") if entry else None
        if cached_keys is not None:
            age = now - float(entry.get("fetched_at") or 0)
            if not force_refresh and age < cache_seconds:
                return cached_keys
            # 强制刷新单独限流但 15 秒内只允许一次
            if force_refresh and now - float(entry.get("last_force") or 0) < MIN_REFRESH_INTERVAL:
                return cached_keys
        elif entry is not None:
            if now - float(entry.get("last_attempt") or 0) < MIN_REFRESH_INTERVAL:
                raise JWKSError(entry.get("error") or "JWKS is temporarily unavailable")

        try:
            # 在真正发起网络请求前校验
            validate_url(uri, allow_insecure_http)
            document = _http_get_json(uri, timeout, allow_insecure_http)
            keys = parse_jwks(document)
        except JWKSError as error:
            with _CACHE_LOCK:
                if cached_keys is not None:
                    entry["last_attempt"] = now
                    if force_refresh:
                        entry["last_force"] = now
                    entry["error"] = str(error)
                    stale_ok = now - float(entry.get("fetched_at") or 0) <= stale_seconds
                else:
                    _CACHE[uri] = {
                        "keys": None,
                        "fetched_at": 0.0,
                        "last_attempt": now,
                        "last_force": now if force_refresh else 0.0,
                        "error": str(error),
                    }
                    stale_ok = False
            if cached_keys is not None and stale_ok:
                _warn(("stale", uri), "JWKS 拉取失败，使用缓存公钥: {} ({})".format(
                    _redact_url(uri), error))
                return cached_keys
            _warn(("fetch", uri), "JWKS 拉取失败: {} ({})".format(
                _redact_url(uri), error))
            raise
        with _CACHE_LOCK:
            _CACHE[uri] = {
                "keys": keys,
                "fetched_at": time.time(),
                "last_attempt": time.time(),
                "last_force": time.time() if force_refresh else 0.0,
                "error": None,
            }
        return keys


def discover(issuer, *, discovery_url=None, timeout=DEFAULT_TIMEOUT,
             cache_seconds=DEFAULT_DISCOVERY_CACHE_SECONDS,
             stale_seconds=DEFAULT_STALE_SECONDS,
             allow_insecure_http=False, force_refresh=False):
    """通过 OIDC discovery 解析 issuer 的 jwks_uri。"""
    if discovery_url:
        url = discovery_url
    else:
        issuer_text = issuer.strip() if isinstance(issuer, str) else ""
        if not issuer_text:
            raise JWKSError("issuer is empty")
        url = issuer_text.rstrip("/") + "/.well-known/openid-configuration"
    if not isinstance(url, str) or not url.strip():
        raise JWKSError("discovery URL is empty")
    validate_url(url, allow_insecure_http)
    timeout = _coerce_seconds(timeout, DEFAULT_TIMEOUT, 0.5, 30.0)
    cache_seconds = _coerce_seconds(cache_seconds, DEFAULT_DISCOVERY_CACHE_SECONDS, 0.0, 7 * 86400)
    stale_seconds = _coerce_seconds(stale_seconds, DEFAULT_STALE_SECONDS, 0.0, 365 * 86400)

    cache_key = "discovery:" + url
    lock = _lock_for(cache_key)
    with lock:
        now = time.time()
        with _CACHE_LOCK:
            entry = _DISCOVERY_CACHE.get(url)
        cached_uri = entry.get("jwks_uri") if entry else None
        if cached_uri:
            age = now - float(entry.get("fetched_at") or 0)
            if not force_refresh and age < cache_seconds:
                return cached_uri
            if force_refresh and now - float(entry.get("last_attempt") or 0) < MIN_REFRESH_INTERVAL:
                return cached_uri
        try:
            document = _http_get_json(url, timeout, allow_insecure_http)
            jwks_uri = document.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri.strip():
                raise JWKSError("discovery document has no jwks_uri")
            validate_url(jwks_uri, allow_insecure_http)
        except JWKSError as error:
            with _CACHE_LOCK:
                if cached_uri:
                    entry["last_attempt"] = now
                else:
                    _DISCOVERY_CACHE[url] = {
                        "jwks_uri": None,
                        "fetched_at": 0.0,
                        "last_attempt": now,
                    }
            if cached_uri and now - float(entry.get("fetched_at") or 0) <= stale_seconds:
                _warn(("discovery-stale", url), "discovery 拉取失败，使用缓存 jwks_uri: {} ({})".format(
                    _redact_url(url), error))
                return cached_uri
            raise
        with _CACHE_LOCK:
            _DISCOVERY_CACHE[url] = {
                "jwks_uri": jwks_uri,
                "fetched_at": time.time(),
                "last_attempt": time.time(),
            }
        return jwks_uri


def key_matches_algorithm(key, algorithm):
    """判断 RemoteKey 是否适用于指定 JWS 算法"""
    if not isinstance(key, RemoteKey):
        return False
    if key.alg is not None and key.alg != algorithm:
        return False
    return jwt_keys.algorithm_matches_key(algorithm, key.public_key)
