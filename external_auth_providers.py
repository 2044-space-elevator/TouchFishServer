from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

CACHE_TTL = 60.0
FAIL_TTL = 30.0
TIMEOUT = 2.0
MAX_BYTES = 64 * 1024

_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}
_inflight: set[str] = set()


def _normalize(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("id")
        name = item.get("name")
        if not isinstance(pid, str) or not pid or len(pid) > 32:
            continue
        if not isinstance(name, str) or not name.strip():
            name = pid
        result.append({"id": pid, "name": name.strip()[:48]})
    return result


def _fetch(base_url: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/site"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "touchfish-server"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_BYTES)
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        return []
    return _normalize(data.get("oauth_providers"))


def providers(base_url: str) -> list[dict]:
    if not isinstance(base_url, str):
        return []
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return []

    now = time.time()
    with _lock:
        cached = _cache.get(base_url)
        if cached is not None and cached[0] > now:
            return list(cached[1])
        if base_url in _inflight:
            return list(cached[1]) if cached is not None else []
        _inflight.add(base_url)

    try:
        value = _fetch(base_url)
        ok = True
    except (urllib.error.URLError, OSError, ValueError):
        value, ok = [], False
    finally:
        with _lock:
            _inflight.discard(base_url)

    with _lock:
        _cache[base_url] = (time.time() + (CACHE_TTL if ok else FAIL_TTL), value)
    return list(value)
