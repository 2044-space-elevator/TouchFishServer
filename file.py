from __future__ import annotations
import os
import base64
import time
from db import FileDb
import hashlib
from file_types import detect_file_type
import threading
import random

_upload_lock = threading.Lock()

def sha256(data : str | bytes) -> str:
    if isinstance(data, str):
        data = bytes(data, encoding="utf-8")

    sha256_hash = hashlib.sha256()
    sha256_hash.update(data)

    return sha256_hash.hexdigest()

def init(port_api : int):
    if not os.path.exists("res/{}/file".format(port_api)):
        os.makedirs("res/{}/file".format(port_api))
    file_cursor = FileDb("res/{}/file/file.db".format(port_api), port_api)
    file_cursor.create_file_db()


def file_path(port_api : int, hashes : str):
    return "res/{}/file/{}.file".format(port_api, hashes)

def upload_file(port_api : int, uid : int, file_b64 : str, file_name : str, file_cursor : FileDb, file_last_time : float = 72.0):
    content = base64.b64decode(file_b64)
    file_size = len(content)
    hashes = sha256(content)
    file_type = detect_file_type(content, file_name)
    extension = os.path.splitext(file_name)[1].lower()

    disk_path = file_path(port_api, hashes)
    with _upload_lock:
        wrote_blob = False
        try:
            registered = file_cursor.file_exists(hashes)
            if not registered or not os.path.isfile(disk_path):
                with open(disk_path, "wb") as file:
                    wrote_blob = True
                    file.write(content)

            file_cursor.register_upload(
                uid, hashes, file_name, time.time(), file_size,
                mime_type=file_type, extension=extension,
            )
        except Exception:
            if wrote_blob:
                try:
                    registered = file_cursor.file_exists(hashes)
                except Exception:
                    registered = True
                if not registered and os.path.isfile(disk_path):
                    try:
                        os.remove(disk_path)
                    except OSError:
                        pass
            raise

    return hashes

def dereference_file(port_api : int, uid : int, hashes : str, file_cursor : FileDb, file_last_time : float = 72.0):
    return delete_user_file(port_api, uid, hashes, file_cursor)

def delete_user_file(port_api : int, uid : int, hashes : str, file_cursor : FileDb):
    succeeded, deleted = file_cursor.delete_owned_user_file(uid, hashes)
    if not succeeded:
        return False
    # 存储空间回收
    if deleted:
        file_cursor.delete_blob_relations(hashes)
        target_path = file_path(port_api, hashes)
        if os.path.isfile(target_path):
            os.remove(target_path)
    return True

def clean_user_files(port_api : int, uid : int, file_cursor : FileDb):
    rows = file_cursor.clean_sender_files(uid) or []
    for row in rows:
        file_cursor.delete_blob_relations(row[0])
        target_path = file_path(port_api, row[0])
        if os.path.isfile(target_path):
            os.remove(target_path)
    return rows


def release_references(port_api : int, hashes, file_cursor : FileDb,
                       file_last_time : float = 72.0):
    for file_hash in hashes:
        file_cursor.decrement_ref(file_hash)
    return []


def collect_expired(port_api: int, sticker_cursor,  file_cursor: FileDb, file_last_time: float = 0.0):
    """回收过期文件"""
    deleted = []
    for hashes in file_cursor.collect_expired_hashes(file_last_time):
        if sticker_cursor.query_hash_exist(hashes):
            continue
        file_cursor.delete_blob_relations(hashes)
        target_path = file_path(port_api, hashes)
        if os.path.isfile(target_path):
            try:
                os.remove(target_path)
            except OSError:
                continue
        deleted.append(hashes)
    return deleted

def force_delete_file(port_api : int, hashes : str, file_cursor : FileDb):
    file_cursor.force_delete_file(hashes)
    target_path = file_path(port_api, hashes)
    if os.path.isfile(target_path):
        os.remove(target_path)


# Experimental: streaming chunked upload for large files (resume + integrity check).
# Hardened vs the original #5 draft: track received chunks, incremental hash,
# per-file size ceiling, uid-scoped temps, concurrent-upload limit.
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

    chunk_dir = "res/{}/file/".format(port_api)

    if chunk_index == 0:
        if os.path.isdir(chunk_dir):
            for fname in os.listdir(chunk_dir):
                if fname.startswith(".tmp_{}_".format(uid)):
                    try:
                        fpath = os.path.join(chunk_dir, fname)
                        if time.time() - os.path.getmtime(fpath) > 3600:
                            os.remove(fpath)
                    except OSError:
                        pass
            existing_ids = set()
            for fname in os.listdir(chunk_dir):
                if fname.startswith(".tmp_{}_".format(uid)):
                    parts = fname.split("_")
                    if len(parts) >= 4:
                        existing_ids.add(parts[2])
            if len(existing_ids) >= 5:
                return {"success": False, "error": "Too many concurrent uploads"}

        file_id = sha256(str(time.time()) + str(uid) + file_name)
        prefix = ".tmp_{}_{}_".format(uid, file_id)
        if os.path.isdir(chunk_dir):
            for fname in os.listdir(chunk_dir):
                if fname.startswith(prefix):
                    try:
                        os.remove(os.path.join(chunk_dir, fname))
                    except OSError:
                        pass
        try:
            if not os.path.exists(chunk_dir):
                os.makedirs(chunk_dir)
        except Exception:
            return {"success": False, "error": "Directory creation failed"}
        total_path_tmp = os.path.join(chunk_dir, ".tmp_{}_{}_total".format(uid, file_id))
        try:
            with open(total_path_tmp, "w") as tf:
                tf.write(str(chunk_total))
        except Exception:
            return {"success": False, "error": "Failed to record chunk info"}
    else:
        if not file_id:
            return {"success": False, "error": "Missing file_id"}
        chunk0 = os.path.join(chunk_dir, ".tmp_{}_{}_0".format(uid, file_id))
        if not os.path.exists(chunk0):
            return {"success": False, "error": "Invalid file_id"}
        total_path_tmp = os.path.join(chunk_dir, ".tmp_{}_{}_total".format(uid, file_id))
        try:
            with open(total_path_tmp, "r") as tf:
                recorded_total = int(tf.read().strip())
        except Exception:
            return {"success": False, "error": "Failed to read chunk info"}
        if recorded_total != chunk_total:
            return {"success": False, "error": "chunk_total mismatch"}

    chunk_path = os.path.join(chunk_dir, ".tmp_{}_{}_{}".format(uid, file_id, chunk_index))
    try:
        with open(chunk_path, "wb") as f:
            f.write(decoded_chunk)
    except Exception:
        return {"success": False, "error": "Write failed"}

    if chunk_index == chunk_total - 1:
        combined = os.path.join(chunk_dir, ".tmp_{}_final_{}".format(file_id, random.getrandbits(64)))
        total_path_tmp = os.path.join(chunk_dir, ".tmp_{}_{}_total".format(uid, file_id))

        def cleanup_chunks():
            for i in range(chunk_total):
                try:
                    os.remove(os.path.join(chunk_dir, ".tmp_{}_{}_{}".format(uid, file_id, i)))
                except OSError:
                    pass
            try:
                os.remove(total_path_tmp)
            except OSError:
                pass

        try:
            for i in range(chunk_total):
                cp = os.path.join(chunk_dir, ".tmp_{}_{}_{}".format(uid, file_id, i))
                if not os.path.exists(cp):
                    return {"success": False, "error": "Missing chunk {}".format(i)}

            sha256_hash = hashlib.sha256()
            total_size = 0
            with open(combined, "wb") as out:
                for i in range(chunk_total):
                    cp = os.path.join(chunk_dir, ".tmp_{}_{}_{}".format(uid, file_id, i))
                    with open(cp, "rb") as f:
                        while True:
                            piece = f.read(1024 * 1024)
                            if not piece:
                                break
                            out.write(piece)
                            sha256_hash.update(piece)
                            total_size += len(piece)

            file_hash = sha256_hash.hexdigest()

            if expected_hash and file_hash != expected_hash:
                try:
                    os.remove(combined)
                except OSError:
                    pass
                cleanup_chunks()
                return {"success": False, "error": "Hash verification failed"}

            final_path = file_path(port_api, file_hash)
            try:
                with open(combined, "rb") as probe:
                    head = probe.read(4096)
                file_type = detect_file_type(head, file_name)
            except Exception:
                file_type = detect_file_type(b"", file_name)
            extension = os.path.splitext(file_name)[1].lower()

            with _upload_lock:
                wrote_blob = False
                try:
                    registered = False
                    if file_cursor is not None:
                        registered = file_cursor.file_exists(file_hash)
                    if not registered or not os.path.isfile(final_path):
                        os.rename(combined, final_path)
                        wrote_blob = True
                    else:
                        try:
                            os.remove(combined)
                        except OSError:
                            pass

                    if file_cursor is not None:
                        file_cursor.register_upload(
                            uid, file_hash, file_name, time.time(), total_size,
                            mime_type=file_type, extension=extension,
                        )
                except Exception:
                    if wrote_blob and os.path.isfile(final_path):
                        try:
                            still = file_cursor.file_exists(file_hash) if file_cursor else True
                        except Exception:
                            still = True
                        if not still:
                            try:
                                os.remove(final_path)
                            except OSError:
                                pass
                    elif os.path.exists(combined):
                        try:
                            os.remove(combined)
                        except OSError:
                            pass
                    cleanup_chunks()
                    return {"success": False, "error": "Finalization failed"}

            cleanup_chunks()
            return {"success": True, "file_hash": file_hash, "verified": expected_hash is not None}
        except Exception:
            if os.path.exists(combined):
                try:
                    os.remove(combined)
                except OSError:
                    pass
            cleanup_chunks()
            return {"success": False, "error": "Finalization failed"}

    return {"success": True, "file_id": file_id}

