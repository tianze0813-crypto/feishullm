from feishu_client.base import FeishuApiError, FeishuBaseClient, FeishuPermissionDeniedError


class FeishuDocClient:
    def __init__(self) -> None:
        self._base = FeishuBaseClient()

    async def get_docx_raw_content(self, open_id: str, document_id: str) -> str:
        data = await self._base.request_as_user(
            open_id,
            "GET",
            f"/open-apis/docx/v1/documents/{document_id}/raw_content",
        )
        return data.get("data", {}).get("content", "")

    async def get_doc_raw_content(self, open_id: str, document_id: str) -> str:
        data = await self._base.request_as_user(
            open_id,
            "GET",
            f"/open-apis/doc/v2/{document_id}/raw_content",
        )
        return data.get("data", {}).get("content", "")

    async def safe_load_content(self, open_id: str, doc_token: str) -> tuple[str, str]:
        for loader in (self.get_docx_raw_content, self.get_doc_raw_content):
            try:
                content = await loader(open_id, doc_token)
                if content:
                    return content, ""
            except PermissionError:
                raise
            except FeishuApiError as err:
                if err.code == 1770002:
                    continue
                if err.code == 99991400:
                    return "", "rate_limited"
                continue
            except FeishuPermissionDeniedError:
                return "", "no_permission"
            except Exception:
                continue
        return "", "unavailable"


doc_client = FeishuDocClient()
