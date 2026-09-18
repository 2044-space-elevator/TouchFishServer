from __future__ import annotations
import io
import os
import base64
import time
import uuid
from collections import deque
from db import FileDb
import hashlib
from file_types import detect_file_type
import threading
import oss_store

try:
    from PIL import Image, ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    Image = None
    ImageOps = None
    _PIL_AVAILABLE = False

try:
    import blurhash as _blurhash
    _BLURHASH_AVAILABLE = True
except ImportError:
    _blurhash = None
    _BLURHASH_AVAILABLE = False




_THUMB_EXT = ".thumb.webp"
_MEDIA_THUMB_EDGE = 512
_MEDIA_THUMB_QUALITY = 80
_MEDIA_MAX_PIXELS = 40_000_000
_MEDIA_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
_MEDIA_QUEUE_LIMIT = 256
_MEDIA_PENDING_STALE = 600.0
_MEDIA_MISS_MEMO_TTL = 3600.0
_MEDIA_MISS_MEMO_LIMIT = 4096

_media_queue = deque()
_media_queued = set()
_media_cancelled = {}
_media_miss_memo = {}
_media_cond = threading.Condition()
_media_notifier = None
_media_worker_started = False

# blob 的发布/登记与回收删除
_HASH_LOCK_STRIPES = 64
_hash_locks = [threading.Lock() for _ in range(_HASH_LOCK_STRIPES)]


def _hash_lock(hashes) -> threading.Lock:
    text = hashes.decode("ascii", "ignore") if isinstance(hashes, (bytes, bytearray)) else str(hashes)
    try:
        index = int(text[:16], 16)
    except ValueError:
        index = 0
    return _hash_locks[index % _HASH_LOCK_STRIPES]

def sha256(data : str | bytes) -> str:
    if isinstance(data, str):
        data = bytes(data, encoding="utf-8")

    sha256_hash = hashlib.sha256()
    sha256_hash.update(data)

    return sha256_hash.hexdigest()

def init(port_api : int):
    if not os.path.exists("res/{}/file".format(port_api)):
        os.makedirs("res/{}/file".format(port_api))
    if not os.path.exists("res/{}/sticker".format(port_api)):
        os.makedirs("res/{}/sticker".format(port_api))
    if not os.path.exists(tmp_dir(port_api)):
        os.makedirs(tmp_dir(port_api))
    if not os.path.exists(thumb_dir(port_api)):
        os.makedirs(thumb_dir(port_api))
    file_cursor = FileDb("res/{}/file/file.db".format(port_api), port_api)
    file_cursor.create_file_db()


def thumb_dir(port_api : int):
    """缩略图 dir"""
    return "res/{}/thumb".format(port_api)


def thumb_path(port_api : int, hashes : str):
    return os.path.join(thumb_dir(port_api), "{}{}".format(hashes, _THUMB_EXT))


def tmp_dir(port_api : int):
    """我们也有 TMPFS！临时文件（分块暂存、拼接产物、上传暂存、OSS 下载临时）"""
    return "res/{}/tmp".format(port_api)


def file_path(port_api : int, hashes : str):
    return "res/{}/file/{}.file".format(port_api, hashes)


def _decode_upload(file_b64):
    """兼容 base64 字符串与原始 bytes"""
    if isinstance(file_b64, (bytes, bytearray)):
        return bytes(file_b64)
    return base64.b64decode(file_b64)


def _stage_bytes(port_api : int, data : bytes):
    """把内容写入临时目录的暂存文件"""
    directory = tmp_dir(port_api)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, ".stg_{}.part".format(uuid.uuid4().hex))
    with open(path, "wb") as file:
        file.write(data)
    return path


def sticker_path(port_api : int, hashes : str):
    """贴图文件独立存放目录，由 sticker.db 全职管理，不参与 file.db 自动回收"""
    return "res/{}/sticker/{}.file".format(port_api, hashes)


def ensure_local_blob(port_api: int, kind: str, hashes: str) -> str:
    """
    确保本地存在指定 blob 的临时副本。
    - 本地已存在：直接返回
    - 本地不存在且启用 OSS2：从 OSS2 拉取到本地
    - 本地不存在且未启用 OSS2：返回空路径
    调用方负责在不需要时删除本地副本（仅 OSS2 模式下才会拉回本地）。
    """
    target_path = file_path(port_api, hashes) if kind == "file" else sticker_path(port_api, hashes)
    if os.path.isfile(target_path):
        return target_path
    if oss_store.is_oss_enabled(port_api):
        temp_path = oss_store.temp_download_path(port_api, kind, hashes)
        if oss_store.download_from_oss(port_api, kind, hashes, temp_path):
            return temp_path
    return ""


def upload_file(port_api : int, uid : int, file_b64, file_name : str, file_cursor : FileDb,
                file_last_time : float = 72.0, known_hash : str = None):
    content = _decode_upload(file_b64)
    file_size = len(content)
    hashes = known_hash or sha256(content)
    file_type = detect_file_type(content, file_name)
    extension = os.path.splitext(file_name)[1].lower()
    media_enabled = media_features_enabled(port_api) and _media_type_supported(file_type, extension)
    media_size = _read_image_size(content) if media_enabled else None

    disk_path = file_path(port_api, hashes)
    oss_enabled = oss_store.is_oss_enabled(port_api)
    need_blob = True
    if file_cursor.file_exists(hashes):
        if oss_enabled:
            need_blob = oss_store.get_size_from_oss(port_api, "file", hashes) <= 0
        else:
            need_blob = not os.path.isfile(disk_path)

    stage_path = None
    if need_blob and not os.path.isfile(disk_path):
        stage_path = _stage_bytes(port_api, content)
    try:
        with _hash_lock(hashes):
            if not need_blob and not file_cursor.file_exists(hashes):
                need_blob = True
            if need_blob and stage_path is None and not os.path.isfile(disk_path):
                stage_path = _stage_bytes(port_api, content)
            published = False
            if stage_path is not None and not os.path.isfile(disk_path):
                try:
                    os.replace(stage_path, disk_path)
                    published = True
                except OSError:
                    if not os.path.isfile(disk_path):
                        raise
            try:
                file_cursor.register_upload(
                    uid, hashes, file_name, time.time(), file_size,
                    mime_type=file_type, extension=extension,
                )
            except Exception:
                if published:
                    try:
                        if not file_cursor.file_exists(hashes):
                            oss_store.safe_remove(disk_path)
                    except Exception:
                        pass
                raise
            if media_enabled:
                try:
                    file_cursor.upsert_media_dimensions(hashes, *(media_size or (None, None)))
                except Exception:
                    pass
    finally:
        if stage_path is not None:
            oss_store.safe_remove(stage_path)

    if oss_enabled and need_blob:
        if oss_store.upload_file_to_oss(port_api, disk_path, "file", hashes):
            oss_store.safe_remove(disk_path)
    elif oss_enabled and os.path.isfile(disk_path):
        oss_store.safe_remove(disk_path)

    if media_enabled:
        enqueue_media(port_api, hashes)

    return hashes


def upload_sticker(port_api : int, uid : int, file_b64, file_name : str, sticker_cursor,
                   known_hash : str = None):
    """
    上传贴图文件（独立于 file 体系）。

    贴图文件存放在 res/<port_api>/sticker/ 目录，
    由 sticker.db 的 sticker_files 表全职管理，
    不遵守 file_last_time 自动删除规则。
    """
    content = _decode_upload(file_b64)
    file_size = len(content)
    hashes = known_hash or sha256(content)
    file_type = detect_file_type(content, file_name)

    disk_path = sticker_path(port_api, hashes)

    oss_enabled = oss_store.is_oss_enabled(port_api)
    need_blob = True
    if sticker_cursor.sticker_file_exists(hashes):
        if oss_enabled:
            need_blob = oss_store.get_size_from_oss(port_api, "sticker", hashes) <= 0
        else:
            need_blob = not os.path.isfile(disk_path)

    stage_path = None
    if need_blob and not os.path.isfile(disk_path):
        stage_path = _stage_bytes(port_api, content)
    try:
        with _hash_lock(hashes):
            published = False
            if stage_path is not None and not os.path.isfile(disk_path):
                try:
                    os.replace(stage_path, disk_path)
                    published = True
                except OSError:
                    if not os.path.isfile(disk_path):
                        raise
            try:
                sticker_cursor.register_upload(
                    uid, hashes, file_name, time.time(), file_size,
                    mime_type=file_type,
                )
            except Exception:
                if published:
                    try:
                        if not sticker_cursor.sticker_file_exists(hashes):
                            oss_store.safe_remove(disk_path)
                    except Exception:
                        pass
                raise
    finally:
        if stage_path is not None:
            oss_store.safe_remove(stage_path)

    if oss_enabled and need_blob:
        if oss_store.upload_file_to_oss(port_api, disk_path, "sticker", hashes):
            oss_store.safe_remove(disk_path)
    elif oss_enabled and os.path.isfile(disk_path):
        oss_store.safe_remove(disk_path)

    return hashes


def instant_upload_file(port_api : int, uid : int, file_hash : str, file_name : str,
                        size : int, mime_type, extension, file_cursor : FileDb) -> bool:
    """
    秒传登记内容已存在
    """
    present = os.path.isfile(file_path(port_api, file_hash))
    if not present and oss_store.is_oss_enabled(port_api):
        present = oss_store.get_size_from_oss(port_api, "file", file_hash) > 0
    if not present or not file_cursor.file_exists(file_hash):
        return False
    media_enabled = media_features_enabled(port_api) and _media_type_supported(mime_type, extension)
    with _hash_lock(file_hash):
        if not file_cursor.file_exists(file_hash):
            return False
        try:
            file_cursor.register_upload(
                uid, file_hash, file_name, time.time(), size,
                mime_type=mime_type, extension=extension,
            )
            if media_enabled:
                try:
                    file_cursor.upsert_media_dimensions(file_hash, None, None)
                except Exception:
                    pass
        except Exception:
            return False
    if media_enabled:
        enqueue_media(port_api, file_hash)
    return True


def dereference_file(port_api : int, uid : int, hashes : str, file_cursor : FileDb, file_last_time : float = 72.0):
    return delete_user_file(port_api, uid, hashes, file_cursor)


def delete_user_file(port_api : int, uid : int, hashes : str, file_cursor : FileDb):
    with _hash_lock(hashes):
        succeeded, deleted = file_cursor.delete_owned_user_file(uid, hashes)
        if not succeeded:
            return False
        # 存储空间回收
        if deleted:
            file_cursor.delete_blob_relations(hashes)
            _remove_media_locked(port_api, hashes, file_cursor)
            if oss_store.is_oss_enabled(port_api):
                oss_store.delete_from_oss(port_api, "file", hashes)
            target_path = file_path(port_api, hashes)
            if os.path.isfile(target_path):
                oss_store.safe_remove(target_path)
    return True


def clean_user_files(port_api : int, uid : int, file_cursor : FileDb):
    rows = file_cursor.clean_sender_files(uid) or []
    for row in rows:
        hashes = row[0]
        with _hash_lock(hashes):
            if file_cursor.has_uploader(hashes):
                continue
            file_cursor.delete_blob_relations(hashes)
            _remove_media_locked(port_api, hashes, file_cursor)
            if oss_store.is_oss_enabled(port_api):
                oss_store.delete_from_oss(port_api, "file", hashes)
            target_path = file_path(port_api, hashes)
            if os.path.isfile(target_path):
                oss_store.safe_remove(target_path)
    return rows


def release_references(port_api : int, hashes, file_cursor : FileDb,
                       file_last_time : float = 72.0):
    for file_hash in hashes:
        file_cursor.decrement_ref(file_hash)
    return []


def collect_expired(port_api: int, sticker_cursor,  file_cursor: FileDb, file_last_time: float = 0.0):
    """回收过期文件（贴图不参与自动删除）"""
    deleted = []
    for hashes in file_cursor.collect_expired_hashes(file_last_time):
        if sticker_cursor.query_hash_exist(hashes):
            continue
        if sticker_cursor.sticker_file_exists(hashes):
            continue
        target_path = file_path(port_api, hashes)
        with _hash_lock(hashes):
            if not file_cursor.should_collect(hashes, file_last_time):
                continue
            file_cursor.delete_blob_relations(hashes)
            _remove_media_locked(port_api, hashes, file_cursor)
            if oss_store.is_oss_enabled(port_api):
                oss_store.delete_from_oss(port_api, "file", hashes)
            if os.path.isfile(target_path):
                if not oss_store.safe_remove(target_path):
                    continue
        deleted.append(hashes)
    return deleted


def force_delete_file(port_api : int, hashes : str, file_cursor : FileDb):
    with _hash_lock(hashes):
        file_cursor.force_delete_file(hashes)
        _remove_media_locked(port_api, hashes, file_cursor)
        if oss_store.is_oss_enabled(port_api):
            oss_store.delete_from_oss(port_api, "file", hashes)
        target_path = file_path(port_api, hashes)
        if os.path.isfile(target_path):
            oss_store.safe_remove(target_path)


# Experimental: streaming chunked upload for large files (resume + integrity check).
# Task state lives in file.db (chunk_upload_tasks / chunk_upload_parts), part files in
# res/<port>/tmp/. Hardened vs the original #5 draft: DB-tracked chunks, incremental hash,
# per-file size ceiling, uid-scoped tasks, concurrent-upload limit, idempotent finalize.
def chunked_upload_file(port_api : int, uid : int, file_name : str, chunk_index : int, chunk_total : int, chunk_data_b64 : str, file_id : str = None, file_cursor : FileDb = None, expected_hash : str = None):
    """
    Stream a file in chunks to avoid loading the whole payload into RAM.

    :return: dict with success/error, plus file_id (intermediate) or file_hash (final)
    """
    MAX_CHUNK_SIZE = 10 * 1024 * 1024
    MAX_TOTAL_SIZE = 200 * 1024 * 1024

    if not isinstance(uid, int):
        return {"success": False, "error": "Invalid uid"}
    if not isinstance(chunk_index, int) or not isinstance(chunk_total, int) or chunk_index < 0 or chunk_total < 1 or chunk_index >= chunk_total:
        return {"success": False, "error": "Invalid chunk parameters"}

    if chunk_total * MAX_CHUNK_SIZE > MAX_TOTAL_SIZE:
        return {"success": False, "error": "File too large"}

    try:
        decoded_chunk = base64.b64decode(chunk_data_b64)
    except Exception:
        return {"success": False, "error": "Decode failed"}

    if len(decoded_chunk) > MAX_CHUNK_SIZE:
        return {"success": False, "error": "Chunk too large"}

    if file_cursor is None:
        raise ValueError("chunked_upload_file requires a file cursor")

    directory = tmp_dir(port_api)
    try:
        if not os.path.exists(directory):
            os.makedirs(directory)
    except Exception:
        return {"success": False, "error": "Directory creation failed"}

    if chunk_index == 0:
        file_id = sha256("{}{}{}".format(time.time(), uid, uuid.uuid4().hex))
        try:
            error, expired_ids = file_cursor.create_chunk_task(uid, file_id, file_name, chunk_total, expected_hash)
        except Exception:
            return {"success": False, "error": "Failed to record chunk info"}
        for expired_id in expired_ids:
            _remove_chunk_files(port_api, expired_id)
        if error:
            return {"success": False, "error": error}
    else:
        if not file_id:
            return {"success": False, "error": "Missing file_id"}
        try:
            task = file_cursor.get_chunk_task(file_id)
        except Exception:
            return {"success": False, "error": "Failed to read chunk info"}
        if task is None or task["uid"] != uid:
            return {"success": False, "error": "Invalid file_id"}
        if task["chunk_total"] != chunk_total:
            return {"success": False, "error": "chunk_total mismatch"}
        if task["status"] == "done" and task.get("file_hash"):
            # 客户端在响应丢失后重发？
            return {"success": True, "file_hash": task["file_hash"], "verified": bool(task["verified"])}

    try:
        with open(_chunk_path(port_api, file_id, chunk_index), "wb") as f:
            f.write(decoded_chunk)
    except Exception:
        return {"success": False, "error": "Write failed"}

    try:
        file_cursor.upsert_chunk_part(file_id, chunk_index, len(decoded_chunk))
        file_cursor.touch_chunk_task(file_id)
    except Exception:
        return {"success": False, "error": "Failed to record chunk info"}

    if chunk_index == chunk_total - 1:
        return _finalize_chunked_upload(port_api, uid, file_name, file_id, chunk_total, expected_hash, file_cursor)

    return {"success": True, "file_id": file_id}


def _chunk_path(port_api : int, file_id : str, chunk_index : int):
    return os.path.join(tmp_dir(port_api), ".chunk_{}_{}".format(file_id, chunk_index))


def _remove_chunk_files(port_api : int, file_id : str):
    directory = tmp_dir(port_api)
    prefix = ".chunk_{}_".format(file_id)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix):
            oss_store.safe_remove(os.path.join(directory, name))


def _finalize_chunked_upload(port_api : int, uid : int, file_name : str, file_id : str,
                             chunk_total : int, expected_hash, file_cursor : FileDb):
    """合并分块并登记文件"""
    try:
        parts = file_cursor.list_chunk_parts(file_id)
    except Exception:
        return {"success": False, "error": "Finalization failed"}
    for i in range(chunk_total):
        if i not in parts or not os.path.isfile(_chunk_path(port_api, file_id, i)):
            return {"success": False, "error": "Missing chunk {}".format(i)}

    try:
        claim = file_cursor.claim_chunk_finalize(file_id)
    except Exception:
        return {"success": False, "error": "Finalization failed"}
    if claim == "done":
        task = file_cursor.get_chunk_task(file_id)
        if task and task.get("file_hash"):
            return {"success": True, "file_hash": task["file_hash"], "verified": bool(task["verified"])}
        return {"success": False, "error": "Finalization failed"}
    if claim == "busy":
        return {"success": False, "error": "Finalization failed"}
    if claim != "claimed":
        return {"success": False, "error": "Invalid file_id"}

    combined = os.path.join(tmp_dir(port_api), ".asm_{}_{}".format(file_id, uuid.uuid4().hex))
    try:
        sha256_hash = hashlib.sha256()
        total_size = 0
        with open(combined, "wb") as out:
            for i in range(chunk_total):
                with open(_chunk_path(port_api, file_id, i), "rb") as f:
                    while True:
                        piece = f.read(1024 * 1024)
                        if not piece:
                            break
                        out.write(piece)
                        sha256_hash.update(piece)
                        total_size += len(piece)

        file_hash = sha256_hash.hexdigest()

        if expected_hash and file_hash != expected_hash:
            file_cursor.delete_chunk_task(file_id)
            _remove_chunk_files(port_api, file_id)
            if os.path.isfile(combined):
                oss_store.safe_remove(combined)
            return {"success": False, "error": "Hash verification failed"}

        final_path = file_path(port_api, file_hash)
        try:
            with open(combined, "rb") as probe:
                head = probe.read(4096)
            file_type = detect_file_type(head, file_name)
        except Exception:
            file_type = detect_file_type(b"", file_name)
        extension = os.path.splitext(file_name)[1].lower()
        media_enabled = media_features_enabled(port_api) and _media_type_supported(file_type, extension)
        media_size = _read_image_size(combined) if media_enabled else None

        oss_enabled = oss_store.is_oss_enabled(port_api)
        need_blob = True
        if file_cursor.file_exists(file_hash):
            if oss_enabled:
                need_blob = oss_store.get_size_from_oss(port_api, "file", file_hash) <= 0
            else:
                need_blob = not os.path.isfile(final_path)

        with _hash_lock(file_hash):
            if not need_blob and not file_cursor.file_exists(file_hash):
                need_blob = True
            published = False
            if need_blob and not os.path.isfile(final_path):
                try:
                    os.replace(combined, final_path)
                    published = True
                except OSError:
                    if not os.path.isfile(final_path):
                        raise
            try:
                file_cursor.register_upload(
                    uid, file_hash, file_name, time.time(), total_size,
                    mime_type=file_type, extension=extension,
                )
            except Exception:
                if published:
                    try:
                        if not file_cursor.file_exists(file_hash):
                            oss_store.safe_remove(final_path)
                    except Exception:
                        pass
                raise
            if media_enabled:
                try:
                    file_cursor.upsert_media_dimensions(file_hash, *(media_size or (None, None)))
                except Exception:
                    pass

        if os.path.isfile(combined):
            oss_store.safe_remove(combined)

        if oss_enabled and need_blob:
            if oss_store.upload_file_to_oss(port_api, final_path, "file", file_hash):
                oss_store.safe_remove(final_path)

        if media_enabled:
            enqueue_media(port_api, file_hash)

        file_cursor.complete_chunk_task(file_id, file_hash, expected_hash is not None)
        _remove_chunk_files(port_api, file_id)
        return {"success": True, "file_hash": file_hash, "verified": expected_hash is not None}
    except Exception:
        try:
            file_cursor.release_chunk_finalize(file_id)
        except Exception:
            pass
        if os.path.isfile(combined):
            oss_store.safe_remove(combined)
        return {"success": False, "error": "Finalization failed"}


def sweep_stale_chunk_uploads(port_api : int, file_cursor : FileDb, max_age : float = 3600.0):
    """清理超时分块任务与其暂存文件，并 sudo rm -rf 临时目录及旧协议遗留的"""
    removed = 0
    try:
        expired_ids = file_cursor.expire_stale_chunk_tasks(max_age)
    except Exception:
        expired_ids = []
    for expired_id in expired_ids:
        _remove_chunk_files(port_api, expired_id)
        removed += 1

    now = time.time()
    active_cache = {}
    for directory, prefixes in (
        (tmp_dir(port_api), (".chunk_", ".asm_", ".stg_")),
        ("res/{}/file".format(port_api), (".tmp_",)),
    ):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.startswith(prefixes):
                continue
            if name.startswith(".chunk_"):
                file_id = name[len(".chunk_"):].rsplit("_", 1)[0]
                if file_id not in active_cache:
                    try:
                        active_cache[file_id] = file_cursor.get_chunk_task(file_id) is not None
                    except Exception:
                        active_cache[file_id] = True
                if active_cache[file_id]:
                    continue
            path = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    oss_store.safe_remove(path)
                    removed += 1
            except OSError:
                pass
    return removed


# 媒体

def media_features_enabled(port_api : int, cfg : dict = None) -> bool:
    if cfg is None:
        cfg = oss_store._read_config(port_api)
    return bool(cfg.get("media_features", True))


def media_cutover(port_api : int):
    """媒体功能启用时"""
    cfg = oss_store._read_config(port_api)
    try:
        return float(cfg.get("media_features_since"))
    except (TypeError, ValueError):
        return None


def _media_type_supported(file_type, extension) -> bool:
    stored = str(file_type or "").lower().lstrip(".")
    if stored in _MEDIA_IMAGE_TYPES:
        return True
    ext = str(extension or "").lower().lstrip(".")
    return ext in _MEDIA_IMAGE_TYPES


def _read_image_size(source):
    """读文件头取宽高"""
    if not _PIL_AVAILABLE:
        return None
    try:
        if isinstance(source, (bytes, bytearray)):
            image = Image.open(io.BytesIO(bytes(source)))
        else:
            image = Image.open(source)
        with image:
            return image.width, image.height
    except Exception:
        return None


def enqueue_media(port_api : int, hashes : str):
    """把 hash 放进生成队列"""
    with _media_cond:
        if hashes in _media_queued:
            return
        if len(_media_queue) >= _MEDIA_QUEUE_LIMIT:
            return
        _media_queue.append(hashes)
        _media_queued.add(hashes)
        _media_cond.notify()


def set_media_notifier(callback):
    """注入生成完成回调"""
    global _media_notifier
    _media_notifier = callback


def start_media_worker(port_api : int, file_cursor : FileDb):
    global _media_worker_started
    if _media_worker_started:
        return
    _media_worker_started = True
    thread = threading.Thread(target=_media_worker_loop, args=(port_api, file_cursor),
                              name="tfv5-media", daemon=True)
    thread.start()


def _media_worker_loop(port_api : int, file_cursor : FileDb):
    while True:
        hashes = None
        try:
            with _media_cond:
                while not _media_queue:
                    _media_cond.wait(timeout=60)
                    _purge_media_cancelled()
                hashes = _media_queue.popleft()
                _media_queued.discard(hashes)
                cancelled = hashes in _media_cancelled
            if cancelled:
                continue
            _process_media(port_api, hashes, file_cursor)
        except Exception as error:
            print("[WARN] 媒体生成失败 ({}): {}".format(hashes, error))


def _purge_media_cancelled():
    if len(_media_cancelled) <= 1024:
        return
    cutoff = time.time() - 3600
    for key in [k for k, ts in _media_cancelled.items() if ts < cutoff]:
        _media_cancelled.pop(key, None)


def _media_miss(hashes : str, now : float):
    if len(_media_miss_memo) >= _MEDIA_MISS_MEMO_LIMIT:
        _media_miss_memo.clear()
    _media_miss_memo[hashes] = now


def ensure_media(port_api : int, hashes : str, file_cursor : FileDb):
    """Trump Will FIX IT"""
    try:
        now = time.time()
        memo = _media_miss_memo.get(hashes)
        if memo is not None and now - memo < _MEDIA_MISS_MEMO_TTL:
            return
        if not media_features_enabled(port_api):
            return
        media = file_cursor.get_media(hashes)
        if media:
            if media["status"] == "pending" and now - float(media["updated_at"] or 0) > _MEDIA_PENDING_STALE:
                file_cursor.touch_media(hashes)
                enqueue_media(port_api, hashes)
            return
        meta = file_cursor.get_metadata(hashes)
        if meta is None:
            return
        if not _media_type_supported(meta.get("file_type"), meta.get("extension")):
            _media_miss(hashes, now)
            return
        first_seen = file_cursor.get_blob_first_seen(hashes)
        cutover = media_cutover(port_api)
        if first_seen is None or cutover is None or first_seen < cutover:
            _media_miss(hashes, now)
            return
        file_cursor.upsert_media_dimensions(hashes, None, None)
        enqueue_media(port_api, hashes)
    except Exception:
        pass


def _media_source_path(port_api : int, hashes : str):
    """返回 (可读路径, 需清理的临时路径)"""
    local_path = file_path(port_api, hashes)
    if os.path.isfile(local_path):
        return local_path, None
    if not oss_store.is_oss_enabled(port_api):
        return None, None
    temp_path = oss_store.temp_download_path(port_api, "file", hashes)
    if oss_store.download_from_oss(port_api, "file", hashes, temp_path):
        return temp_path, temp_path
    return None, None


def _build_media_assets(path):
    """生成 (width, height, blurhash, thumb_bytes, thumb_w, thumb_h, thumb_is_original)
    """
    if not _PIL_AVAILABLE:
        return None
    try:
        image = Image.open(path)
        with image:
            width, height = image.width, image.height
            if width <= 0 or height <= 0:
                return None
            if width * height > _MEDIA_MAX_PIXELS:
                return (width, height, None, None, None, None, False)
            image.load()
            blur_value = None
            if _BLURHASH_AVAILABLE:
                try:
                    blur_value = _encode_blurhash(image)
                except Exception:
                    blur_value = None
            if max(width, height) <= _MEDIA_THUMB_EDGE:
                return (width, height, blur_value, None, width, height, True)
            thumb_bytes, thumb_w, thumb_h = _encode_thumbnail(image)
            return (width, height, blur_value, thumb_bytes, thumb_w, thumb_h, False)
    except Exception:
        return None


def _encode_blurhash(image):
    rgb = image.convert("RGB")
    rgb.thumbnail((32, 32))
    width, height = rgb.size
    pixels = rgb.load()
    rows = [[pixels[x, y] for x in range(width)] for y in range(height)]
    return _blurhash.encode(rows, 4, 3)


def _encode_thumbnail(image):
    rotated = ImageOps.exif_transpose(image) or image
    if rotated.mode in ("RGBA", "LA", "P"):
        working = rotated.convert("RGBA")
    else:
        working = rotated.convert("RGB")
    working.thumbnail((_MEDIA_THUMB_EDGE, _MEDIA_THUMB_EDGE), Image.LANCZOS)
    buffer = io.BytesIO()
    working.save(buffer, format="WEBP", quality=_MEDIA_THUMB_QUALITY, method=4)
    return buffer.getvalue(), working.width, working.height


def _publish_thumb(port_api : int, hashes : str, data : bytes):
    """把缩略图发布到 thumb"""
    stage_path = _stage_bytes(port_api, data)
    final_path = thumb_path(port_api, hashes)
    os.makedirs(thumb_dir(port_api), exist_ok=True)
    try:
        os.replace(stage_path, final_path)
    except OSError:
        if not os.path.isfile(final_path):
            raise
    finally:
        oss_store.safe_remove(stage_path)


def _remove_thumb_file(port_api : int, hashes : str):
    with _media_cond:
        _media_cancelled[hashes] = time.time()
        _media_queued.discard(hashes)
    if oss_store.is_oss_enabled(port_api):
        oss_store.delete_from_oss(port_api, "thumb", hashes)
    path = thumb_path(port_api, hashes)
    if os.path.isfile(path):
        oss_store.safe_remove(path)


def _remove_media_locked(port_api : int, hashes : str, file_cursor : FileDb):
    """级联删除媒体行与缩略图"""
    try:
        file_cursor.delete_media(hashes)
    except Exception:
        pass
    _remove_thumb_file(port_api, hashes)


def _process_media(port_api : int, hashes : str, file_cursor : FileDb):
    if not media_features_enabled(port_api):
        return
    media = file_cursor.get_media(hashes)
    if media is None or media["status"] != "pending":
        return
    source_path, temp_path = _media_source_path(port_api, hashes)
    if source_path is None:
        # blob FXXKED
        # wait please
        return
    try:
        try:
            source_size = os.path.getsize(source_path)
        except OSError:
            source_size = 0
        assets = _build_media_assets(source_path)
    finally:
        if temp_path:
            oss_store.safe_remove(temp_path)

    if assets is None:
        try:
            file_cursor.mark_media_skipped(hashes)
        except Exception:
            pass
        return

    width, height, blur_value, thumb_bytes, thumb_w, thumb_h, thumb_is_original = assets
    oss_enabled = oss_store.is_oss_enabled(port_api)
    if thumb_is_original:
        thumb_size = int(source_size) or None
    else:
        thumb_size = len(thumb_bytes) if thumb_bytes else None

    with _hash_lock(hashes):
        with _media_cond:
            cancelled = hashes in _media_cancelled
        if cancelled or not file_cursor.file_exists(hashes):
            _remove_thumb_file(port_api, hashes)
            return
        try:
            if thumb_bytes:
                _publish_thumb(port_api, hashes, thumb_bytes)
            file_cursor.set_media_artifacts(
                hashes, blur_value, thumb_w, thumb_h, thumb_size, thumb_is_original)
        except Exception as error:
            print("[WARN] 媒体写入失败 ({}): {}".format(hashes, error))
            return

    if thumb_bytes and oss_enabled:
        path = thumb_path(port_api, hashes)
        if oss_store.upload_file_to_oss(port_api, path, "thumb", hashes):
            oss_store.safe_remove(path)

    notifier = _media_notifier
    if notifier:
        try:
            notifier(port_api, hashes)
        except Exception:
            pass


def sweep_orphan_media(port_api : int, file_cursor : FileDb, limit : int = 200,
                       max_age : float = 3600.0):
    """清理 file（可怜的孤儿呢）"""
    removed = 0
    try:
        orphans = file_cursor.list_orphan_media(limit)
    except Exception:
        orphans = []
    for hashes in orphans:
        with _hash_lock(hashes):
            _remove_media_locked(port_api, hashes, file_cursor)
        removed += 1

    directory = thumb_dir(port_api)
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for name in names[:1000]:
        if not name.endswith(_THUMB_EXT):
            continue
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) <= max_age:
                continue
        except OSError:
            continue
        hashes = name[:-len(_THUMB_EXT)]
        try:
            if file_cursor.get_media(hashes) is None:
                oss_store.safe_remove(path)
                removed += 1
        except Exception:
            pass
    return removed