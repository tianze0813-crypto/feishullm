from typing import Any

import httpx

from config import settings
from feishu_client.auth import auth_client
from utils.http import get_feishu_client
from utils.logger import get_logger
from utils.token_store import user_token_store

logger = get_logger()

# 飞书公开文档里这几个 code 都对应"用户 access_token 失效/被撤销/权限不足"，
# 出现时应该清掉本地 token 记录、回到 OAuth 授权流程，而不是抛通用 RuntimeError
# 让上层 except Exception 兜成"查询过程中出现异常"。
# 参考 https://open.feishu.cn/document/server-docs/docs/faq
_USER_TOKEN_INVALID_CODES = {
    99991661,  # access_token 无效
    99991662,  # access_token 过期
    99991663,  # tenant_access_token 过期（在用户态 API 下通常也代表需要重拉）
    99991664,  # access_token 被撤销
    99991668,  # app_access_token 参数缺失
    99991672,  # 用户未授权
}

_RESOURCE_PERMISSION_CODES = {
    20006,  # 无权限访问资源（如命中文档但无读取权限）
}


class FeishuApiError(RuntimeError):
    """飞书业务层错误（HTTP 200 但 code != 0），保留原始 code 和 data。"""

    def __init__(self, code: int, msg: str, data: dict[str, Any]) -> None:
        super().__init__(f"feishu api error code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.data = data


class FeishuPermissionDeniedError(RuntimeError):
    def __init__(self, code: int, msg: str, data: dict[str, Any]) -> None:
        super().__init__(f"feishu permission denied code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.data = data


class FeishuBaseClient:
    async def request_as_tenant(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # App-level capabilities (such as bot message sending) should use tenant token.
        token = await auth_client.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        return await self._request(method, path, headers=headers, params=params, json_body=json_body)

    async def request_as_user(
        self,
        open_id: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # User-scoped data access must use user token to preserve permission boundary.
        access_token = await auth_client.get_user_access_token(open_id)
        if not access_token:
            raise PermissionError(f"missing user oauth token for open_id={open_id}")
        headers = {"Authorization": f"Bearer {access_token}"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            return await self._request(method, path, headers=headers, params=params, json_body=json_body)
        except FeishuApiError as err:
            if err.code in _RESOURCE_PERMISSION_CODES:
                raise FeishuPermissionDeniedError(err.code, err.msg, err.data) from err
            # 运行时用户 token 失效/被撤销：清掉本地记录，让上层走授权卡片分支重新走 OAuth。
            if err.code in _USER_TOKEN_INVALID_CODES:
                logger.warning(
                    "user token rejected by feishu code={} msg={}, drop local record for open_id={}",
                    err.code,
                    err.msg,
                    open_id,
                )
                user_token_store.delete(open_id)
                raise PermissionError(
                    f"user token invalid for open_id={open_id}, code={err.code}"
                ) from err
            raise

    async def request_raw_as_user(
        self,
        open_id: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        access_token = await auth_client.get_user_access_token(open_id)
        if not access_token:
            raise PermissionError(f"missing user oauth token for open_id={open_id}")
        headers = {"Authorization": f"Bearer {access_token}"}
        if extra_headers:
            headers.update(extra_headers)
        url = f"{settings.feishu_base_url}{path}"
        client = await get_feishu_client()
        response = await client.request(
            method=method,
            url=url,
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        url = f"{settings.feishu_base_url}{path}"
        client = await get_feishu_client()
        response = await client.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            headers=headers,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = ""
            parsed: dict[str, Any] | None = None
            try:
                body = response.text or ""
            except Exception:
                body = ""
            if body and len(body) > 2000:
                body = body[:2000] + "...(truncated)"
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    parsed = payload
            except Exception:
                parsed = None
            if parsed and parsed.get("code", 0) != 0:
                code = int(parsed.get("code", -1))
                msg = str(parsed.get("msg", ""))
                logger.warning(
                    "feishu api error status={} url={} code={} msg={} body={}",
                    response.status_code,
                    str(response.url),
                    code,
                    msg,
                    body,
                )
                raise FeishuApiError(code=code, msg=msg, data=parsed) from exc
            logger.exception(
                "feishu http error status={} url={} body={}",
                response.status_code,
                str(response.url),
                body,
            )
            raise
        data = response.json()

        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise FeishuApiError(
                code=int(data.get("code", -1)),
                msg=str(data.get("msg", "")),
                data=data,
            )
        return data
