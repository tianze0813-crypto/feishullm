"""共享的 httpx.AsyncClient 封装。

每次新开 `async with httpx.AsyncClient(...)` 都要做一次 TCP + TLS 握手，
对 find_person / search_knowledge 这种一问多 API 的场景延迟开销很大，
尤其 `asyncio.gather` 批量调用时更明显。

这里暴露两个全局 client：
- `get_feishu_client()`：给 feishu_client/*.py 用，默认 timeout 15s
- `get_llm_client()`：给 llm/client.py 用，默认 timeout 30s（LLM 响应慢）
两者均 lazy 初始化且线程安全（asyncio 单 loop 内协程切换不需加锁，
但初始化一次性动作可能在竞态场景被重入，用 asyncio.Lock 保护）。
"""

from __future__ import annotations

import asyncio

import httpx

_feishu_client: httpx.AsyncClient | None = None
_llm_client: httpx.AsyncClient | None = None
_feishu_lock = asyncio.Lock()
_llm_lock = asyncio.Lock()


async def get_feishu_client() -> httpx.AsyncClient:
    global _feishu_client
    if _feishu_client is None:
        async with _feishu_lock:
            if _feishu_client is None:
                _feishu_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(15.0, connect=5.0),
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                )
    return _feishu_client


async def get_llm_client() -> httpx.AsyncClient:
    global _llm_client
    if _llm_client is None:
        async with _llm_lock:
            if _llm_client is None:
                _llm_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=5.0),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
    return _llm_client


async def aclose_all() -> None:
    """FastAPI 关闭时调用，清理连接池。生产环境非必需——进程退出 OS 会回收。"""
    global _feishu_client, _llm_client
    if _feishu_client is not None:
        await _feishu_client.aclose()
        _feishu_client = None
    if _llm_client is not None:
        await _llm_client.aclose()
        _llm_client = None
