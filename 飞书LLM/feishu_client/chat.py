from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

from feishu_client.base import FeishuBaseClient


class FeishuChatClient:
    def __init__(self) -> None:
        self._base = FeishuBaseClient()

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1), reraise=True)
    async def list_chats(
        self,
        open_id: str,
        *,
        page_size: int = 50,
        page_token: str = "",
    ) -> tuple[list[dict[str, Any]], str, bool]:
        uid = str(open_id or "").strip()
        if not uid:
            return [], "", False
        params: dict[str, Any] = {"page_size": max(1, min(int(page_size), 100))}
        if page_token:
            params["page_token"] = page_token
        data = await self._base.request_as_user(uid, "GET", "/open-apis/im/v1/chats", params=params)
        payload = data.get("data") if isinstance(data, dict) else {}
        if not isinstance(payload, dict):
            return [], "", False
        items = payload.get("items") or []
        chats = [item for item in items if isinstance(item, dict)]
        next_token = str(payload.get("page_token") or "")
        has_more = bool(payload.get("has_more"))
        return chats, next_token, has_more

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1), reraise=True)
    async def get_chat(self, open_id: str, chat_id: str) -> dict[str, Any]:
        cid = str(chat_id or "").strip()
        uid = str(open_id or "").strip()
        if not cid or not uid:
            return {}
        data = await self._base.request_as_user(uid, "GET", f"/open-apis/im/v1/chats/{cid}")
        payload = data.get("data") if isinstance(data, dict) else {}
        if isinstance(payload, dict):
            return payload
        return {}


chat_client = FeishuChatClient()
