"""用户 OAuth token 本地持久化存储。

设计目标：
- 避免服务重启丢失用户授权：access_token/refresh_token/open_id 等序列化到本地文件。
- 单文件 JSON 结构：`{open_id: record}`，写时走"临时文件 + os.replace"原子替换，避免写一半崩溃文件损坏。
- 线程安全：对读/写加 threading.Lock；FastAPI + WS 事件线程 + 异步任务都可能并发访问。
- 不使用 MemoryCache 的 TTL：access_token 过期后只 evict 访问令牌，refresh_token 仍然保留供续命。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from utils.logger import get_logger

logger = get_logger()

_DEFAULT_STORE_DIR = Path(__file__).resolve().parent.parent / ".tokens"
_DEFAULT_STORE_FILE = _DEFAULT_STORE_DIR / "user_tokens.json"


class UserTokenStore:
    def __init__(self, store_path: Path | None = None) -> None:
        self._path = store_path or _DEFAULT_STORE_FILE
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> int:
        """从文件加载到内存。返回加载到的用户数量。"""
        with self._lock:
            self._data = self._read_file_locked()
            self._loaded = True
            count = len(self._data)
        logger.info("user token store loaded {} entries from {}", count, self._path)
        return count

    def get(self, open_id: str) -> dict[str, Any] | None:
        with self._lock:
            if not self._loaded:
                self._data = self._read_file_locked()
                self._loaded = True
            record = self._data.get(open_id)
            if not record:
                # 在 dev reload / 多进程场景下，授权回调可能由其他进程刚写入文件。
                # miss 时回盘重读一次，避免当前进程长期拿着旧内存态误判“需要重新授权”。
                latest = self._read_file_locked()
                if latest != self._data:
                    self._data = latest
                    record = self._data.get(open_id)
            return dict(record) if record else None

    def set(self, open_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            if not self._loaded:
                self._data = self._read_file_locked()
                self._loaded = True
            self._data[open_id] = dict(record)
            self._write_file_locked()

    def delete(self, open_id: str) -> None:
        with self._lock:
            if not self._loaded:
                self._data = self._read_file_locked()
                self._loaded = True
            if open_id in self._data:
                self._data.pop(open_id, None)
                self._write_file_locked()

    def all_open_ids(self) -> list[str]:
        with self._lock:
            if not self._loaded:
                self._data = self._read_file_locked()
                self._loaded = True
            return list(self._data.keys())

    def _read_file_locked(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            data = json.loads(raw)
        except Exception:
            logger.exception("failed to read token store {}, starting empty", self._path)
            return {}
        if not isinstance(data, dict):
            logger.warning("token store file is not a dict, ignoring: {}", self._path)
            return {}
        # 防御性过滤：只保留 value 是 dict 且包含 access_token 的条目。
        return {
            k: v
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, dict) and v.get("access_token")
        }

    def _write_file_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + os.replace 保证原子性，避免写一半崩溃导致文件损坏。
        fd, tmp_path = tempfile.mkstemp(
            prefix=".user_tokens.",
            suffix=".json.tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except Exception:
            # 失败要清掉临时文件，别留残骸。
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


user_token_store = UserTokenStore()
