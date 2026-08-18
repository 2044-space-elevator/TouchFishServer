"""
OSS2 (阿里云对象存储) 适配模块。

当服务器的 res/<port_api>/config.json 中 storage_backend 为 "oss2" 时，
普通文件与贴图文件的 blob 以
  file/<hash>.file      （普通文件）
  sticker/<hash>.file   （贴图文件）
为 Object Key 存放在指定的 OSS2 Bucket 中。

本地磁盘仅作为上传 / 下载的临时中转：上传时先存本地、再传 OSS、最后删本地；
下载时先从 OSS 拉回本地临时文件（.oss_ 前缀）、发给客户端、再删除临时文件。
"""
from __future__ import annotations

import os
import time
import json
import threading
import uuid
import glob

try:
    import oss2
    _OSS2_AVAILABLE = True
except ImportError:
    oss2 = None
    _OSS2_AVAILABLE = False

_config_cache = {}
_config_cache_ttl = 60.0
_config_lock = threading.Lock()

_download_locks = {}
_download_locks_guard = threading.Lock()


def _read_config(port_api: int) -> dict:
    """读取服务器配置（带 60 秒 TTL 缓存）。"""
    now = time.time()
    with _config_lock:
        cached = _config_cache.get(port_api)
        if cached and now - cached[0] < _config_cache_ttl:
            return cached[1]
        try:
            with open("res/{}/config.json".format(port_api), "r", encoding="utf-8") as handle:
                cfg = json.load(handle)
        except Exception:
            cfg = {}
        _config_cache[port_api] = (now, cfg)
        return cfg


def is_oss_enabled(port_api: int, cfg: dict = None) -> bool:
    """返回该服务器是否配置并启用了 OSS2 文件存储。"""
    if cfg is None:
        cfg = _read_config(port_api)
    if cfg.get("storage_backend") != "oss2":
        return False
    if not _OSS2_AVAILABLE:
        return False
    return bool(cfg.get("oss2_authid") and cfg.get("oss2_authkey")
                and cfg.get("oss2_endpoint") and cfg.get("oss2_bucket"))


def _get_bucket(port_api: int, cfg: dict = None):
    if cfg is None:
        cfg = _read_config(port_api)
    auth = oss2.Auth(cfg["oss2_authid"], cfg["oss2_authkey"])
    return oss2.Bucket(auth, cfg["oss2_endpoint"], cfg["oss2_bucket"])


def object_key(kind: str, hashes: str) -> str:
    """生成 OSS2 Object Key：file/<hash>.file 或 sticker/<hash>.file"""
    if kind not in ("file", "sticker"):
        raise ValueError("invalid kind: {}".format(kind))
    return "{}/{}.file".format(kind, hashes)


def upload_file_to_oss(port_api: int, local_path: str, kind: str, hashes: str, cfg: dict = None) -> bool:
    """将本地临时文件上传到 OSS2（上传完成后由调用方负责删除本地文件）。"""
    if not is_oss_enabled(port_api, cfg):
        return False
    if cfg is None:
        cfg = _read_config(port_api)
    if not os.path.isfile(local_path):
        return False
    bucket = _get_bucket(port_api, cfg)
    key = object_key(kind, hashes)
    try:
        bucket.put_object_from_file(key, local_path)
        return True
    except Exception as e:
        print("[WARN] OSS2 上传失败 ({}): {}".format(key, e))
        return False


def _get_download_lock(kind: str, hashes: str) -> threading.Lock:
    key = "{}/{}".format(kind, hashes)
    with _download_locks_guard:
        lock = _download_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _download_locks[key] = lock
        return lock


def download_from_oss(port_api: int, kind: str, hashes: str, local_path: str, cfg: dict = None) -> bool:
    """从 OSS2 下载对象到本地临时路径。返回是否成功。"""
    if not is_oss_enabled(port_api, cfg):
        return False
    if cfg is None:
        cfg = _read_config(port_api)
    bucket = _get_bucket(port_api, cfg)
    key = object_key(kind, hashes)
    lock = _get_download_lock(kind, hashes)
    with lock:
        if os.path.isfile(local_path):
            return True
        try:
            if not bucket.object_exists(key):
                return False
            dirname = os.path.dirname(local_path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            bucket.get_object_to_file(key, local_path)
            return True
        except Exception as e:
            print("[WARN] OSS2 下载失败 ({}): {}".format(key, e))
            return False


def get_size_from_oss(port_api: int, kind: str, hashes: str, cfg: dict = None) -> int:
    """获取 OSS2 对象大小（in Byte) ~~字节跳动~~
    未启用 OSS2、对象不存在或 head 失败时返回 0。
    """
    if not is_oss_enabled(port_api, cfg):
        return 0
    if cfg is None:
        cfg = _read_config(port_api)
    bucket = _get_bucket(port_api, cfg)
    key = object_key(kind, hashes)
    try:
        return int(bucket.head_object(key).content_length or 0)
    except Exception as e:
        print("[WARN] OSS2 head 失败 ({}): {}".format(key, e))
        return 0


def delete_from_oss(port_api: int, kind: str, hashes: str, cfg: dict = None) -> bool:
    """从 OSS2 删除对象。"""
    if not is_oss_enabled(port_api, cfg):
        return False
    if cfg is None:
        cfg = _read_config(port_api)
    bucket = _get_bucket(port_api, cfg)
    key = object_key(kind, hashes)
    try:
        bucket.delete_object(key)
        return True
    except Exception as e:
        print("[WARN] OSS2 删除失败 ({}): {}".format(key, e))
        return False


def temp_download_path(port_api: int, kind: str, hashes: str) -> str:
    """
    生成一个唯一的 OSS 下载临时文件路径。
    使用 .oss_ 前缀 + uuid，避免与本地存储的正式文件冲突，
    也避免并发下载同一 hash 时互相覆盖。
    """
    return "res/{}/{}/.oss_{}_{}.file".format(port_api, kind, uuid.uuid4().hex, hashes)


def safe_remove(path: str, retries: int = 5, delay: float = 0.2):
    """
    安全删除本地文件，带重试机制。
    解决 Windows 上 WinError 32（文件被占用）导致删除失败的问题。
    失败时不抛出异常，多次重试后仍失败则仅打印警告（交由后台清理兜底）。
    """
    for attempt in range(retries):
        try:
            if os.path.isfile(path):
                os.remove(path)
            return True
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay)
    try:
        if os.path.isfile(path):
            print("[WARN] 临时文件删除失败(多次重试): {}".format(path))
    except OSError:
        pass
    return False


def cleanup_temp_files(port_api: int, kind: str, max_age: float = 3600.0):
    """
    清理指定目录下残留的 .oss_ 临时文件。
    正常情况下临时文件用后即删；这里兜底清理因异常崩溃等未能删除的残留。
    max_age 秒数：只清理超过该时间的旧文件，避免误删正在使用的文件。
    """
    if kind not in ("file", "sticker"):
        return
    directory = "res/{}/{}".format(port_api, kind)
    if not os.path.isdir(directory):
        return
    now = time.time()
    try:
        pattern = os.path.join(directory, ".oss_*.file")
        for path in glob.glob(pattern):
            try:
                if now - os.path.getmtime(path) > max_age:
                    safe_remove(path)
            except OSError:
                pass
    except OSError:
        pass
