from typing import Any

from feishu_client.base import FeishuBaseClient


class FeishuWikiClient:
    def __init__(self) -> None:
        self._base = FeishuBaseClient()

    async def get_node(
        self,
        open_id: str,
        node_token: str,
        *,
        obj_type: str = "wiki",
    ) -> dict[str, Any]:
        data = await self._base.request_as_user(
            open_id,
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={
                "token": str(node_token or "").strip(),
                "obj_type": str(obj_type or "wiki").strip(),
            },
        )
        node = data.get("data", {}).get("node", {}) if isinstance(data, dict) else {}
        return node if isinstance(node, dict) else {}

    async def list_space_nodes(
        self,
        open_id: str,
        space_id: str,
        *,
        parent_node_token: str = "",
        page_size: int = 50,
        page_token: str = "",
    ) -> tuple[list[dict[str, Any]], str, bool]:
        path = f"/open-apis/wiki/v2/spaces/{str(space_id or '').strip()}/nodes"
        params: dict[str, Any] = {
            "page_size": max(1, min(int(page_size), 200)),
        }
        if parent_node_token:
            params["parent_node_token"] = str(parent_node_token).strip()
        if page_token:
            params["page_token"] = str(page_token).strip()
        data = await self._base.request_as_user(
            open_id,
            "GET",
            path,
            params=params,
        )
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        items = payload.get("items", []) if isinstance(payload, dict) else []
        nodes = [item for item in items if isinstance(item, dict)]
        return nodes, str(payload.get("page_token") or ""), bool(payload.get("has_more"))


wiki_client = FeishuWikiClient()
