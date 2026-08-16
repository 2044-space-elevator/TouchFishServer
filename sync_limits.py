SYNC_MAX_LIMIT = 200
SYNC_MAX_MISSING_ITEMS = 200
SYNC_MAX_MISSING_RANGES = 50
SYNC_MAX_RANGE_SPAN = 200


def parse_sync_missing_sequences(req: dict):
    """严格解析同步缺口，返回去重后的序号，非法请求返回 None。"""
    raw_missing = req.get("missing_sequences")
    raw_ranges = req.get("missing_sequence_ranges")
    if raw_missing is None:
        raw_missing = []
    if raw_ranges is None:
        raw_ranges = []
    if not isinstance(raw_missing, list) or not isinstance(raw_ranges, list):
        return None
    if len(raw_missing) > SYNC_MAX_MISSING_ITEMS:
        return None
    if len(raw_ranges) > SYNC_MAX_MISSING_RANGES:
        return None

    sequences = set()
    planned_count = 0
    for value in raw_missing:
        if isinstance(value, (bool, float)):
            return None
        try:
            sequence = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if sequence <= 0:
            return None
        if str(value).strip() != str(sequence):
            return None
        sequences.add(sequence)
        planned_count += 1
        if planned_count > SYNC_MAX_MISSING_ITEMS:
            return None

    for item in raw_ranges:
        if not isinstance(item, dict):
            return None
        try:
            start_value = item["start_seq"]
            end_value = item["end_seq"]
            if isinstance(start_value, (bool, float)) or isinstance(end_value, (bool, float)):
                return None
            start = int(start_value)
            end = int(end_value)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if start <= 0 or end < start:
            return None
        if str(start_value).strip() != str(start) or str(end_value).strip() != str(end):
            return None
        span = end - start + 1
        if span > SYNC_MAX_RANGE_SPAN:
            return None
        planned_count += span
        if planned_count > SYNC_MAX_MISSING_ITEMS:
            return None
        sequences.update(range(start, end + 1))

    if len(sequences) > SYNC_MAX_MISSING_ITEMS:
        return None
    return sorted(sequences)
