from __future__ import annotations
"""
速率限制器

配置 res/{port_api}/config.json ，示例如下：
{
    "rate_limits": {
        "default":           {"requests": 60,  "range": 60},
        "/auth/register":    {"requests": 5,   "range": 300},
        "/file/upload_file": {"requests": 20,  "range": 60}
    }
}

- requests : 在 range 秒内允许的最大请求数
- range    : 时间窗口，单位为秒
- default  : 对未单独配置的 API 生效；若不设置则不限速

"""

import json
import time
import threading
from collections import defaultdict


class RateLimiter:
    def __init__(self, port_api: int):
        self._lock = threading.Lock()
        self._requests: dict = defaultdict(list)
        try:
            with open("res/{}/config.json".format(port_api), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self._limits: dict = cfg.get("rate_limits", {})
        except Exception:
            self._limits = {}
        self._start_cleanup()

    def _start_cleanup(self):
        def _loop():
            while True:
                time.sleep(300)
                self._cleanup()
        t = threading.Thread(target=_loop, daemon=True, name="tfv5-ratelimit-gc")
        t.start()

    def _cleanup(self):
        with self._lock:
            max_range = max(
                (v.get("range", 60) for v in self._limits.values()), default=60
            )
            cutoff = time.time() - max_range * 2
            expired = [k for k, v in self._requests.items() if not v or v[-1] < cutoff]
            for k in expired:
                del self._requests[k]

    def _get_limit_for(self, endpoint: str) -> dict | None:
        """返回适用的限制规则，优先端点规则，其次 default，无则 None。"""
        return self._limits.get(endpoint) or self._limits.get("default")

    def reload(self, port_api: int) -> None:
        """从配置文件加载 ratelimit"""
        try:
            with open("res/{}/config.json".format(port_api), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            new_limits = cfg.get("rate_limits", {})
        except Exception:
            return
        with self._lock:
            self._limits = new_limits

    def is_allowed(self, ip: str, endpoint: str) -> bool:
        """
        判断来自 ip 的对 endpoint 的本次请求是否在速率限制内。
        """
        limit = self._get_limit_for(endpoint)
        if limit is None:
            return True
        max_requests: int = limit["requests"]
        range_: float = float(limit["range"])
        now = time.time()
        key = (ip, endpoint)
        with self._lock:
            cutoff = now - range_
            self._requests[key] = [t for t in self._requests[key] if t > cutoff]
            if len(self._requests[key]) >= max_requests:
                return False
            self._requests[key].append(now)
            return True


