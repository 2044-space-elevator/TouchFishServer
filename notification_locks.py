"""
处罚不可删除通知（在生效期内）
"""

import time

PUNISHMENT_NOTICE_EVENT = "punishment.created"


def _is_active(row, now: float) -> bool:
    revoked_at = row[7]
    expires_at = row[6]
    if revoked_at is not None:
        return False
    return expires_at is None or expires_at > now


def apply_punishment_locks(serialized, punishments_cursor, now=None):
    if punishments_cursor is None:
        return serialized
    moment = time.time() if now is None else now
    cache = {}
    for item in serialized:
        info = item.get("info") or {}
        if info.get("event") != PUNISHMENT_NOTICE_EVENT:
            continue
        punishment_id = (info.get("meta") or {}).get("punishment_id")
        if not isinstance(punishment_id, int):
            # 老格式通知没有 punishment_id：无法判定就按可删处理，
            # 不能因为字段缺失把用户的通知永久锁死。
            continue
        if punishment_id not in cache:
            row = punishments_cursor.get(punishment_id)
            cache[punishment_id] = bool(row) and _is_active(row, moment)
        item["locked"] = cache[punishment_id]
    return serialized


def locked_ids(serialized):
    return {item["id"] for item in serialized if item.get("locked")}
