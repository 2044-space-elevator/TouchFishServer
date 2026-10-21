import re


_MENTION_RE = re.compile(r"(?<!\S)@([^\s@]+)")
_TRAILING_PUNCTUATION = ",.!?;:，。！？；："


def resolve_mentioned_uids(content, user_cursor, allowed_uids=None, exclude_uid=None):
    if not isinstance(content, str) or "@" not in content:
        return []
    names = {
        match.group(1).rstrip(_TRAILING_PUNCTUATION)
        for match in _MENTION_RE.finditer(content)
    }
    names.discard("")
    if not names:
        return []
    placeholders = ",".join("?" * len(names))
    rows = user_cursor.query(
        "SELECT uid, username FROM users WHERE stat != 'banned' AND username IN ({})".format(placeholders),
        tuple(names),
    )
    allowed = set(allowed_uids) if allowed_uids is not None else None
    return sorted({
        int(uid) for uid, _ in rows
        if uid != exclude_uid and (allowed is None or uid in allowed)
    })


def alert_from_preference(pref, uid, mentioned_uids) -> bool:
    """根据已批量取到的房间偏好判断是否需要提醒（缺 = 0 级）"""
    level = int((pref or {}).get("notify_level", 0))
    if level == 2:
        return False
    if level == 1:
        return uid in mentioned_uids
    return True


def should_alert(messages_cursor, uid, room_id, mentioned_uids):
    """按房间偏好判断是否需要提醒（单房询版"""
    return alert_from_preference(
        messages_cursor.get_room_preference(uid, room_id), uid, mentioned_uids
    )
