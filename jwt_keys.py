"""
JWT 签发密钥环（本服作为签发方）。

- 管理 res/<port>/secret/jwt_keys 下的非对称签名密钥（RSA / EC）
- 为每把密钥计算稳定的 kid（RFC 7638 JWK Thumbprint）
- 输出 JWKS / OIDC discovery 文档所需的公开信息
- 支持轮换与旧钥保留

不依赖 Flask、PyJWT
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

import json_store


# 对称算法只用于兼容存量 token / 本服自签
HS_ALGORITHMS = ("HS256", "HS384", "HS512")
# RSA 家族：RS* = PKCS#1 v1.5，PS* = PSS。
RSA_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")
# EC 家族：JWS 规定 ES512 使用 P-521。
EC_ALGORITHM_CURVES = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
ASYMMETRIC_ALGORITHMS = RSA_ALGORITHMS + tuple(EC_ALGORITHM_CURVES)
ALL_ALGORITHMS = HS_ALGORITHMS + ASYMMETRIC_ALGORITHMS

_CURVE_CLASSES = {
    "P-256": ec.SECP256R1,
    "P-384": ec.SECP384R1,
    "P-521": ec.SECP521R1,
}

DEFAULT_RSA_KEY_SIZE = 2048
MIN_RSA_KEY_SIZE = 2048
MAX_RSA_KEY_SIZE = 8192
DEFAULT_RETENTION_DAYS = 30


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_uint(value: int) -> str:
    if value < 0:
        raise ValueError("negative integer cannot be encoded")
    size = max(1, (value.bit_length() + 7) // 8)
    return _b64u(value.to_bytes(size, "big"))


def normalize_algorithm(value, default="HS256"):
    """把配置/参数里的算法名归一化未知值返回 default（default 为 None 时返回 None）。"""
    if isinstance(value, str):
        algorithm = value.strip().upper()
        if algorithm in ALL_ALGORITHMS:
            return algorithm
    return default


def normalize_rsa_key_size(value):
    try:
        size = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RSA_KEY_SIZE
    if size < MIN_RSA_KEY_SIZE or size > MAX_RSA_KEY_SIZE:
        return DEFAULT_RSA_KEY_SIZE
    return size


def key_type_for_algorithm(algorithm):
    if algorithm in RSA_ALGORITHMS:
        return "RSA"
    if algorithm in EC_ALGORITHM_CURVES:
        return "EC"
    return None


def curve_name(curve):
    for name, curve_class in _CURVE_CLASSES.items():
        if isinstance(curve, curve_class):
            return name
    raise ValueError("unsupported EC curve")


def key_type_of(public_key):
    """公钥的 JWK kty（仅支持 RSA/EC）未知 None"""
    if isinstance(public_key, rsa.RSAPublicKey):
        return "RSA"
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return "EC"
    return None


def algorithm_matches_key(algorithm, public_key):
    """算法与公钥类型（以及 EC 曲线）是否匹配"""
    if algorithm in RSA_ALGORITHMS:
        return isinstance(public_key, rsa.RSAPublicKey)
    if algorithm in EC_ALGORITHM_CURVES:
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            return False
        expected = _CURVE_CLASSES[EC_ALGORITHM_CURVES[algorithm]]
        return isinstance(public_key.curve, expected)
    return False


def infer_algorithm(private_key):
    """从私钥推断默认算法（重建）"""
    if isinstance(private_key, rsa.RSAPrivateKey):
        return "RS256"
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        for algorithm, name in (("ES256", "P-256"), ("ES384", "P-384"), ("ES512", "P-521")):
            if isinstance(private_key.curve, _CURVE_CLASSES[name]):
                return algorithm
    return None


def public_jwk(public_key):
    """返回不含 kid/alg/use 的 RFC 7517 公钥 JWK"""
    if isinstance(public_key, rsa.RSAPublicKey):
        numbers = public_key.public_numbers()
        return {
            "kty": "RSA",
            "n": _b64u_uint(numbers.n),
            "e": _b64u_uint(numbers.e),
        }
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        numbers = public_key.public_numbers()
        name = curve_name(numbers.curve)
        size = (numbers.curve.key_size + 7) // 8
        return {
            "kty": "EC",
            "crv": name,
            "x": _b64u(numbers.x.to_bytes(size, "big")),
            "y": _b64u(numbers.y.to_bytes(size, "big")),
        }
    raise ValueError("unsupported public key type")


def jwk_thumbprint(jwk):
    """RFC 7638 JWK Thumbprint，作为 kid"""
    if not isinstance(jwk, dict):
        raise ValueError("jwk must be a dict")
    if jwk.get("kty") == "RSA":
        members = {"e": jwk["e"], "kty": "RSA", "n": jwk["n"]}
    elif jwk.get("kty") == "EC":
        members = {"crv": jwk["crv"], "kty": "EC", "x": jwk["x"], "y": jwk["y"]}
    else:
        raise ValueError("unsupported jwk kty")
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":"))
    return _b64u(hashlib.sha256(canonical.encode("utf-8")).digest())


def describe_jwk(public_key, kid=None, algorithm=None, use="sig"):
    """构造对外发布的 JWK（含 kid/alg/use）"""
    jwk = public_jwk(public_key)
    if algorithm:
        jwk["alg"] = algorithm
    if use:
        jwk["use"] = use
    if kid:
        jwk["kid"] = kid
    return jwk


def load_private_key(path):
    with open(path, "rb") as file:
        data = file.read()
    return serialization.load_pem_private_key(data, password=None)


class KeyStore:
    """res/<port>/secret/jwt_keys 下的签名密钥环"""

    INDEX_FILE = "index.json"
    KEY_SUFFIX = ".pem"

    def __init__(self, secret_dir, *, rsa_key_size=DEFAULT_RSA_KEY_SIZE):
        self.rsa_key_size = normalize_rsa_key_size(rsa_key_size)
        self.directory = os.path.join(secret_dir, "jwt_keys")
        os.makedirs(self.directory, exist_ok=True)
        self.index_path = os.path.join(self.directory, self.INDEX_FILE)
        self._lock = threading.RLock()
        self._index = None
        self._index_stat = None
        # 启动时清一次超保留期的旧钥
        # 失败不影响启动。
        try:
            self.prune()
        except Exception:  # noqa: BLE001
            pass

    # Windows Search Index（bushi）

    def _empty_index(self):
        return {"version": 1, "keys": []}

    def _index_file_stat(self):
        try:
            stat = os.stat(self.index_path)
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def _load_index_locked(self):
        if self._index is not None:
            if self._index_file_stat() == self._index_stat:
                return self._index
            # 索引被外部进程改了
            self._index = None
        index = None
        try:
            with open(self.index_path, "r", encoding="utf-8") as file:
                raw = json.load(file)
            raw_keys = raw.get("keys") if isinstance(raw, dict) else None
            if isinstance(raw_keys, list):
                entries = []
                seen = set()
                for item in raw_keys:
                    if not isinstance(item, dict):
                        continue
                    kid = item.get("kid")
                    algorithm = normalize_algorithm(item.get("alg"), default=None)
                    if not isinstance(kid, str) or not kid or algorithm is None:
                        continue
                    if algorithm not in ASYMMETRIC_ALGORITHMS or kid in seen:
                        continue
                    seen.add(kid)
                    retired_at = item.get("retired_at")
                    try:
                        retired_at = float(retired_at) if retired_at is not None else None
                    except (TypeError, ValueError):
                        retired_at = None
                    try:
                        created_at = float(item.get("created_at") or 0)
                    except (TypeError, ValueError):
                        created_at = 0.0
                    entries.append({
                        "kid": kid,
                        "alg": algorithm,
                        "file": item.get("file") if isinstance(item.get("file"), str)
                        else kid + self.KEY_SUFFIX,
                        "created_at": created_at,
                        "retired_at": retired_at,
                        "active": bool(item.get("active")),
                    })
                index = {"version": 1, "keys": entries}
        except FileNotFoundError:
            index = None
        except Exception as error:
            print("[WARN] JWT 密钥索引损坏，尝试从 PEM 重建: {}".format(error))
            index = None
        if index is None:
            index = self._rebuild_index_locked()
        elif not index["keys"] and self._has_key_files_locked():
            # 索引被清空但目录里还有 PEM：按 PEM 重建
            index = self._rebuild_index_locked()
        self._index = index
        self._index_stat = self._index_file_stat()
        return index

    def _has_key_files_locked(self):
        try:
            return any(
                name.endswith(self.KEY_SUFFIX)
                for name in os.listdir(self.directory)
            )
        except OSError:
            return False

    def _rebuild_index_locked(self):
        """索引缺失/损坏时，从目录里的 PEM 恢复（算法推断。"""
        entries = []
        try:
            names = sorted(os.listdir(self.directory))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(self.KEY_SUFFIX):
                continue
            kid = name[:-len(self.KEY_SUFFIX)]
            path = os.path.join(self.directory, name)
            try:
                private_key = load_private_key(path)
            except Exception as error:
                print("[WARN] 跳过无法解析的 JWT 私钥 {}: {}".format(name, error))
                continue
            algorithm = infer_algorithm(private_key)
            if algorithm not in ASYMMETRIC_ALGORITHMS:
                continue
            try:
                created_at = os.path.getmtime(path)
            except OSError:
                created_at = time.time()
            entries.append({
                "kid": kid,
                "alg": algorithm,
                "file": name,
                "created_at": created_at,
                "retired_at": None,
                "active": False,
            })
        groups = {}
        for entry in entries:
            groups.setdefault(entry["alg"], []).append(entry)
        for group in groups.values():
            group.sort(key=lambda item: item["created_at"], reverse=True)
            group[0]["active"] = True
        index = {"version": 1, "keys": entries}
        self._index = index
        try:
            self._write_index_locked()
        except Exception as error:
            print("[WARN] 写入 JWT 密钥索引失败: {}".format(error))
        self._index_stat = self._index_file_stat()
        return index

    def _write_index_locked(self):
        index = self._index or self._empty_index()
        payload = {"version": 1, "keys": []}
        for entry in index["keys"]:
            retired_at = entry.get("retired_at")
            payload["keys"].append({
                "kid": entry["kid"],
                "alg": entry["alg"],
                "file": entry.get("file") or entry["kid"] + self.KEY_SUFFIX,
                "created_at": float(entry.get("created_at") or 0),
                "retired_at": float(retired_at) if retired_at is not None else None,
                "active": bool(entry.get("active")),
            })
        json_store.write_json(self.index_path, payload)
        self._index_stat = self._index_file_stat()

    def _key_path(self, entry):
        name = entry.get("file") or (entry["kid"] + self.KEY_SUFFIX)
        return os.path.join(self.directory, name)

    def _load_entry_locked(self, entry):
        if entry.get("_private") is not None and entry.get("_public") is not None:
            return entry
        now = time.time()
        failed_at = entry.get("_failed_at")
        if failed_at is not None and now - failed_at < 60:
            # 密钥文件暂时不可用时，60 秒内不重复尝试/刷日志
            return None
        path = self._key_path(entry)
        try:
            private_key = load_private_key(path)
        except Exception as error:
            entry["_failed_at"] = now
            print("[WARN] 加载 JWT 私钥失败 {}: {}".format(entry.get("kid"), error))
            return None
        entry.pop("_failed_at", None)
        public_key = private_key.public_key()
        if not algorithm_matches_key(entry.get("alg"), public_key):
            print("[WARN] JWT 私钥 {} 与算法 {} 不匹配".format(entry.get("kid"), entry.get("alg")))
            return None
        entry["_private"] = private_key
        entry["_public"] = public_key
        return entry

    # ---- 生成 / 轮换 ----

    def _generate_locked(self, algorithm):
        index = self._load_index_locked()
        for _ in range(3):
            if algorithm in RSA_ALGORITHMS:
                private_key = rsa.generate_private_key(
                    public_exponent=65537, key_size=self.rsa_key_size
                )
            else:
                curve = _CURVE_CLASSES[EC_ALGORITHM_CURVES[algorithm]]()
                private_key = ec.generate_private_key(curve)
            public_key = private_key.public_key()
            kid = jwk_thumbprint(public_jwk(public_key))
            if any(entry["kid"] == kid for entry in index["keys"]):
                continue

            pem = private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            path = os.path.join(self.directory, kid + self.KEY_SUFFIX)
            fd, temp_path = tempfile.mkstemp(
                prefix=".jwt-key-", suffix=".tmp", dir=self.directory
            )
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(pem)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp_path, path)
            except Exception:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

            entry = {
                "kid": kid,
                "alg": algorithm,
                "file": kid + self.KEY_SUFFIX,
                "created_at": time.time(),
                "retired_at": None,
                "active": True,
                "_private": private_key,
                "_public": public_key,
            }
            index["keys"].append(entry)
            self._write_index_locked()
            return entry
        raise RuntimeError("unable to generate a unique JWT signing key")

    def _active_entry_locked(self, algorithm):
        index = self._load_index_locked()
        candidates = [
            entry for entry in index["keys"]
            if entry.get("alg") == algorithm
            and entry.get("active")
            and entry.get("retired_at") is None
        ]
        candidates.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
        for entry in candidates:
            loaded = self._load_entry_locked(entry)
            if loaded is not None:
                return loaded
            entry["active"] = False
            if entry.get("retired_at") is None:
                entry["retired_at"] = time.time()
        if candidates:
            try:
                self._write_index_locked()
            except Exception:
                pass
        return None

    def signing_key(self, algorithm):
        """返回 (private_key, kid)；必要时自动生成并持久化。"""
        algorithm = normalize_algorithm(algorithm, default=None)
        if algorithm not in ASYMMETRIC_ALGORITHMS:
            raise ValueError("unsupported JWT signing algorithm: {}".format(algorithm))
        with self._lock:
            entry = self._active_entry_locked(algorithm)
            if entry is None:
                entry = self._generate_locked(algorithm)
            return entry["_private"], entry["kid"]

    def active_kid(self, algorithm):
        algorithm = normalize_algorithm(algorithm, default=None)
        if algorithm not in ASYMMETRIC_ALGORITHMS:
            return None
        with self._lock:
            entry = self._active_entry_locked(algorithm)
            return entry["kid"] if entry is not None else None

    def rotate(self, algorithm, *, retention_days=DEFAULT_RETENTION_DAYS):
        """轮换指定算法：旧钥标记 retired_at，新钥成为 active。返回新 kid。"""
        algorithm = normalize_algorithm(algorithm, default=None)
        if algorithm not in ASYMMETRIC_ALGORITHMS:
            raise ValueError("unsupported JWT signing algorithm: {}".format(algorithm))
        with self._lock:
            index = self._load_index_locked()
            now = time.time()
            for entry in index["keys"]:
                if entry.get("alg") == algorithm and entry.get("retired_at") is None:
                    entry["active"] = False
                    entry["retired_at"] = now
            entry = self._generate_locked(algorithm)
            self._prune_locked(retention_days)
            self._write_index_locked()
            return entry["kid"]

    def prune(self, retention_days=DEFAULT_RETENTION_DAYS):
        """清理超过保留期的旧钥，返回删除数量。"""
        with self._lock:
            removed = self._prune_locked(retention_days)
            if removed:
                self._write_index_locked()
            return removed

    def _prune_locked(self, retention_days):
        try:
            days = float(retention_days)
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        if days < 0:
            days = 0
        index = self._load_index_locked()
        cutoff = time.time() - days * 86400
        kept = []
        removed = []
        for entry in index["keys"]:
            retired_at = entry.get("retired_at")
            if (retired_at is not None
                    and not entry.get("active")
                    and float(retired_at) < cutoff):
                removed.append(entry)
            else:
                kept.append(entry)
        if removed:
            index["keys"] = kept
            for entry in removed:
                try:
                    os.remove(self._key_path(entry))
                except OSError:
                    pass
        return len(removed)

    # reading public keys
    # 阿三公司使用 4 作为私钥(bushi)

    def public_keys(self, algorithm=None, kid=None):
        """返回 [(kid, public_key)]；algorithm/kid 为 None 时不过滤。"""
        algorithm = normalize_algorithm(algorithm, default=None) if algorithm else None
        with self._lock:
            index = self._load_index_locked()
            result = []
            for entry in index["keys"]:
                if algorithm and entry.get("alg") != algorithm:
                    continue
                if kid and entry.get("kid") != kid:
                    continue
                loaded = self._load_entry_locked(entry)
                if loaded is not None:
                    result.append((entry["kid"], entry["_public"]))
            return result

    def algorithms(self):
        with self._lock:
            index = self._load_index_locked()
            result = set()
            for entry in index["keys"]:
                if self._load_entry_locked(entry) is not None:
                    result.add(entry["alg"])
            return result

    def jwks(self, retention_days=DEFAULT_RETENTION_DAYS):
        try:
            days = float(retention_days)
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        if days < 0:
            days = 0
        cutoff = time.time() - days * 86400
        with self._lock:
            index = self._load_index_locked()
            keys = []
            for entry in sorted(index["keys"], key=lambda item: item.get("created_at") or 0):
                retired_at = entry.get("retired_at")
                if retired_at is not None and float(retired_at) < cutoff:
                    continue
                loaded = self._load_entry_locked(entry)
                if loaded is None:
                    continue
                keys.append(describe_jwk(
                    entry["_public"], kid=entry["kid"], algorithm=entry["alg"]
                ))
            return keys

    def list_keys(self):
        with self._lock:
            index = self._load_index_locked()
            rows = []
            for entry in sorted(index["keys"], key=lambda item: item.get("created_at") or 0):
                rows.append({
                    "kid": entry["kid"],
                    "algorithm": entry["alg"],
                    "created_at": float(entry.get("created_at") or 0),
                    "retired_at": entry.get("retired_at"),
                    "active": bool(entry.get("active")),
                })
            return rows


def build_jwks(store, retention_days=DEFAULT_RETENTION_DAYS):
    """构造 /.well-known/jwks.json 响应体。"""
    return {"keys": store.jwks(retention_days)}


def build_discovery(issuer, jwks_uri, algorithms):
    """构造 /.well-known/openid-configuration。

    这里只声明"可用于验签"的元数据（issuer + jwks_uri + 支持的算法）
    
    尚未实现 authorization/token
    """
    supported = sorted({
        algorithm for algorithm in (algorithms or [])
        if algorithm in ASYMMETRIC_ALGORITHMS
    })
    return {
        "issuer": issuer,
        "jwks_uri": jwks_uri,
        "id_token_signing_alg_values_supported": supported,
        "subject_types_supported": ["public"],
        "claims_supported": [
            "sub", "iss", "aud", "exp", "iat", "nbf", "jti", "sid", "av",
            "name", "nick", "region", "perk_level", "is_superuser",
        ],
    }
