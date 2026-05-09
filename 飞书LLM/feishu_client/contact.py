from feishu_client.base import FeishuBaseClient
from utils.logger import get_logger

logger = get_logger()


class FeishuContactClient:
    def __init__(self) -> None:
        self._base = FeishuBaseClient()

    async def search_user(
        self, open_id: str, keyword: str, page_size: int = 20
    ) -> list[dict]:
        """通讯录用户搜索。

        走正规端点 GET /open-apis/search/v1/user（scope: contact:user:search），
        由飞书后端按 name 做相关度匹配，支持中英文和拼音。
        响应字段：name / open_id / user_id / department_ids / avatar（不含 email）。
        """
        data = await self._base.request_as_user(
            open_id,
            "GET",
            "/open-apis/search/v1/user",
            params={
                "query": keyword,
                "page_size": max(1, min(page_size, 200)),
            },
        )
        users = data.get("data", {}).get("users", []) or []
        logger.info(
            "contact search_user hit={} query={!r} endpoint=/open-apis/search/v1/user",
            len(users),
            keyword,
        )
        return [u for u in users if isinstance(u, dict)]


contact_client = FeishuContactClient()
