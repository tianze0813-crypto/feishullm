from feishu_client.base import FeishuBaseClient


class FeishuBitableClient:
    def __init__(self) -> None:
        self._base = FeishuBaseClient()

    async def list_records(
        self,
        open_id: str,
        app_token: str,
        table_id: str,
        page_size: int = 20,
    ) -> list[dict]:
        data = await self._base.request_as_user(
            open_id,
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params={"page_size": page_size},
        )
        return data.get("data", {}).get("items", [])

    async def search_records(
        self,
        open_id: str,
        app_token: str,
        table_id: str,
        page_size: int = 50,
    ) -> list[dict]:
        """调 bitable records/search 拉回一页记录，上层做本地关键字过滤。

        为什么不用 filter：飞书 filter 要求指定 field_name 和 operator，需要先
        知道表的字段 schema；而我们只有用户的关键字，不知道这张表里是"对接人"
        还是"owner"。折衷做法是 page_size 拉大（50），本地做 substring 匹配。
        """
        page_size = max(1, min(page_size, 500))
        data = await self._base.request_as_user(
            open_id,
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            params={"page_size": page_size},
            json_body={},
        )
        return data.get("data", {}).get("items", []) or []

    async def list_tables(self, open_id: str, app_token: str, page_size: int = 20) -> list[dict]:
        data = await self._base.request_as_user(
            open_id,
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables",
            params={"page_size": page_size},
        )
        return data.get("data", {}).get("items", [])


bitable_client = FeishuBitableClient()
