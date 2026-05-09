import threading
import time
from typing import Any, Optional


class MemoryCache:
    """进程内 TTL 缓存。

    当前调用方包括 asyncio 事件循环线程（FastAPI/auth_client 拉 tenant_access_token）
    和 lark-oapi WS 回调线程（handle_message_event 在独立线程启动异步流水线），
    多线程写 dict 在 CPython 虽然有 GIL 兜底，但 set+pop 不是原子的，
    加一把 Lock 更保险，也为后面可能引入的 LRU 策略留空间。
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        expire_at = time.time() + ttl_seconds
        with self._lock:
            self._store[key] = (expire_at, value)

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            data = self._store.get(key)
            if not data:
                return None
            expire_at, value = data
            if expire_at < time.time():
                self._store.pop(key, None)
                return None
            return value

    def add_if_absent(self, key: str, value: Any, ttl_seconds: int = 300) -> bool:
        expire_at = time.time() + ttl_seconds
        with self._lock:
            data = self._store.get(key)
            if data:
                old_expire_at, _old_value = data
                if old_expire_at >= time.time():
                    return False
                self._store.pop(key, None)
            self._store[key] = (expire_at, value)
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


cache = MemoryCache()
