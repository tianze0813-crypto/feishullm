from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

from feishu_client.base import FeishuBaseClient
from config import settings
from utils.logger import get_logger

logger = get_logger()


class FeishuSearchClient:
    def __init__(self) -> None:
        self._base = FeishuBaseClient()

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1), reraise=True)
    async def search_docs(
        self,
        open_id: str,
        query: str,
        page_size: int = 5,
        owner_ids: list[str] | None = None,
        docs_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        # 飞书文档搜索：POST /open-apis/suite/docs-api/search/object
        # 返回 data.docs_entities[] {docs_token, docs_type, title, owner_id}
        # owner_ids 用于按文档创建者 open_id 过滤，例如找人命中后再查该人的个人文档。
        items = await self._search_objects(
            open_id=open_id,
            query=query,
            count=page_size,
            docs_types=docs_types,
            owner_ids=owner_ids,
        )
        logger.info(
            "search_docs hit={} query={!r} owner_ids={} docs_types={}",
            len(items),
            query,
            owner_ids,
            docs_types,
        )
        for idx, item in enumerate(items, start=1):
            title = str(item.get("title") or item.get("name") or "")
            if len(title) > 120:
                title = title[:120] + "…"
            logger.debug(
                "search_docs item#{} docs_type={} docs_token={} owner_id={} title={!r}",
                idx,
                item.get("docs_type"),
                item.get("docs_token"),
                item.get("owner_id"),
                title,
            )
        return items

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1), reraise=True)
    async def search_bitable(
        self, open_id: str, query: str, page_size: int = 5
    ) -> list[dict[str, Any]]:
        # 通过 docs_types 过滤得到多维表格命中，docs_token 即 bitable 的 app_token。
        items = await self._search_objects(
            open_id=open_id,
            query=query,
            count=page_size,
            docs_types=["bitable"],
            owner_ids=None,
        )
        result = [
            {
                "app_token": item.get("docs_token"),
                "docs_token": item.get("docs_token"),
                "docs_type": item.get("docs_type"),
                "title": item.get("title"),
                "owner_id": item.get("owner_id"),
            }
            for item in items
            if item.get("docs_token")
        ]
        logger.info("search_bitable hit={} query={!r}", len(result), query)
        return result

    async def _search_objects(
        self,
        *,
        open_id: str,
        query: str,
        count: int,
        docs_types: list[str] | None,
        owner_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "search_key": query,
            "count": max(1, min(count, 50)),
            "offset": 0,
        }
        if docs_types:
            body["docs_types"] = docs_types
        if owner_ids:
            # 去重后透传，避免空串污染过滤条件。
            cleaned = [uid for uid in dict.fromkeys(owner_ids) if uid]
            if cleaned:
                body["owner_ids"] = cleaned
        data = await self._base.request_as_user(
            open_id,
            "POST",
            "/open-apis/suite/docs-api/search/object",
            json_body=body,
        )
        return data.get("data", {}).get("docs_entities", []) or []

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1), reraise=True)
    async def search_messages(
        self,
        open_id: str,
        query: str,
        page_size: int = 5,
        group_only: bool = False,
    ) -> list[str]:
        # 消息搜索：POST /open-apis/search/v2/message
        # 返回 data.items[] 是 message_id 字符串数组；当结果被当前聊天框“淹没”时，
        # 需要继续翻页才能找到其它 chat 的命中，所以这里主动拉多页。
        # group_only=True 强制仅群聊（用于找人场景）；否则沿用全局开关。
        body: dict[str, Any] = {"query": query}
        effective_group_only = group_only or not settings.include_p2p_message_search
        if effective_group_only:
            body["chat_type"] = "group_chat"

        normalized: list[str] = []
        page_token = ""
        has_more = True
        page_limit = 3
        while has_more and len(normalized) < page_size and page_limit > 0:
            params = {"page_size": max(1, min(page_size, 100))}
            if page_token:
                params["page_token"] = page_token
            data = await self._base.request_as_user(
                open_id,
                "POST",
                "/open-apis/search/v2/message",
                params=params,
                json_body=body,
            )
            payload = data.get("data", {}) if isinstance(data, dict) else {}
            items = payload.get("items", []) or []
            for item in items:
                if isinstance(item, str) and item not in normalized:
                    normalized.append(item)
                    if len(normalized) >= page_size:
                        break
            has_more = bool(payload.get("has_more"))
            page_token = str(payload.get("page_token") or "")
            page_limit -= 1

        logger.info(
            "search_messages hit={} query={!r} group_only={}",
            len(normalized),
            query,
            effective_group_only,
        )
        return normalized

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1), reraise=True)
    async def search_wiki(
        self, open_id: str, query: str, page_size: int = 5
    ) -> list[dict[str, Any]]:
        # 知识库节点搜索：POST /open-apis/wiki/v2/nodes/search
        # page_size 在 query params；query 在 body。
        # 返回 data.items[] -> {node_id, obj_token, obj_type, title, url, space_id, icon}
        body: dict[str, Any] = {"query": query}
        data = await self._base.request_as_user(
            open_id,
            "POST",
            "/open-apis/wiki/v2/nodes/search",
            params={"page_size": max(1, min(page_size, 50))},
            json_body=body,
        )
        items = data.get("data", {}).get("items", []) or []
        result = [item for item in items if isinstance(item, dict)]
        logger.info("search_wiki hit={} query={!r}", len(result), query)
        return result


search_client = FeishuSearchClient()
