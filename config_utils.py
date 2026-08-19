import re


def normalize_default_join_targets(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(',', ' ').split()
    if not isinstance(raw, list):
        raise ValueError("default_join_targets must be a list")
    targets = []
    for value in raw:
        target = str(value).strip().upper()
        if not re.fullmatch(r"[UG][1-9][0-9]*", target):
            raise ValueError("invalid default join target")
        if target not in targets:
            targets.append(target)
    return targets
