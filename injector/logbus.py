# -*- coding: utf-8 -*-
"""线程安全的日志总线，UI 通过 /api/state 拉取。"""

import sys
import time
import threading
from collections import deque


class LogBus:
    def __init__(self, maxlen: int = 400):
        self._items = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def log(self, msg: str, level: str = "info"):
        with self._lock:
            self._seq += 1
            self._items.append({
                "id": self._seq,
                "t": time.strftime("%H:%M:%S"),
                "level": level,
                "msg": str(msg),
            })

    def since(self, last_id: int):
        with self._lock:
            return [i for i in self._items if i["id"] > last_id]

    def tail(self, n: int = 60):
        with self._lock:
            return list(self._items)[-n:]


LOGS = LogBus()


def _ensure_utf8_stdout():
    """某些环境（CI / 英文系统）stdout 是 cp1252，中文 print 会抛 UnicodeEncodeError。"""
    if sys.stdout is None:
        return
    try:
        if (sys.stdout.encoding or "").lower() not in ("utf-8", "utf8"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_ensure_utf8_stdout()


def log(msg: str, level: str = "info"):
    LOGS.log(msg, level)
    # 打包成无控制台 exe 后 sys.stdout 为 None，直接 print 会抛异常
    if sys.stdout is not None:
        try:
            print(f"[{level}] {msg}", flush=True)
        except (UnicodeEncodeError, OSError, ValueError):
            pass
