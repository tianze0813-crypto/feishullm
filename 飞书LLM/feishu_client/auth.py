import time
from typing import Any

from config import settings
from utils.cache import cache
from utils.http import get_feishu_client
from utils.logger import get_logger
from utils.token_store import user_token_store

logger = get_logger()

TENANT_TOKEN_CACHE_KEY = "feishu:tenant_access_token"


class FeishuAuthClient:
    async def get_tenant_access_token(self) -> str:
        cached = cache.get(TENANT_TOKEN_CACHE_KEY)
        if cached:
            logger.debug("tenant access token cache hit")
            return cached

        payload = {"app_id": settings.app_id, "app_secret": settings.app_secret}
        url = f"{settings.feishu_base_url}/open-apis/auth/v3/tenant_access_token/internal"

        client = await get_feishu_client()
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            raise RuntimeError(f"get_tenant_access_token failed: {data}")

        token = data.get("tenant_access_token", "")
        expire = int(data.get("expire", 3600))
        if not token:
            raise RuntimeError("tenant_access_token missing")

        cache.set(TENANT_TOKEN_CACHE_KEY, token, ttl_seconds=max(60, expire - 120))
        logger.info("tenant access token refreshed, ttl={}s", max(60, expire - 120))
        return token

    async def exchange_oauth_code(self, code: str) -> dict[str, Any]:
        # 飞书 OAuth v2 endpoint：顶层返回 access_token 等字段，但不含 open_id/union_id，
        # 需要再用 access_token 调 user_info 获取用户身份后合并缓存。
        url = f"{settings.feishu_base_url}/open-apis/authen/v2/oauth/token"
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.app_id,
            "client_secret": settings.app_secret,
            "redirect_uri": settings.oauth_redirect_uri,
        }
        client = await get_feishu_client()
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

        if data.get("code", 0) != 0 or not data.get("access_token"):
            raise RuntimeError(f"exchange_oauth_code failed: {data}")

        access_token = data["access_token"]
        user_info = await self._fetch_user_info(access_token)
        merged = {**data, **user_info}

        self._save_user_token(merged)
        logger.info(
            "oauth token exchanged and stored for open_id={}", merged.get("open_id", "")
        )
        return merged

    async def _fetch_user_info(self, access_token: str) -> dict[str, Any]:
        url = f"{settings.feishu_base_url}/open-apis/authen/v1/user_info"
        headers = {"Authorization": f"Bearer {access_token}"}
        client = await get_feishu_client()
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if data.get("code", 0) != 0:
            raise RuntimeError(f"fetch user_info failed: {data}")
        info = data.get("data") or {}
        if not info.get("open_id"):
            raise RuntimeError(f"user_info missing open_id: {data}")
        return info

    def _save_user_token(self, oauth_data: dict[str, Any]) -> None:
        open_id = oauth_data.get("open_id")
        if not open_id:
            logger.warning("oauth token without open_id, skip persist")
            return

        now = int(time.time())
        access_ttl = int(oauth_data.get("expires_in", 7200))
        # 飞书 v2 刷新 token 默认 30 天，若响应里带了 refresh_token_expires_in 就用它。
        refresh_ttl = int(oauth_data.get("refresh_token_expires_in", 30 * 24 * 3600))
        record = {
            **oauth_data,
            "expire_at": now + access_ttl,
            "refresh_expire_at": now + refresh_ttl,
        }
        user_token_store.set(open_id, record)
        logger.info(
            "user token persisted open_id={} access_ttl={}s refresh_ttl={}s",
            open_id,
            access_ttl,
            refresh_ttl,
        )

    def get_user_token_record(self, open_id: str) -> dict[str, Any] | None:
        return user_token_store.get(open_id)

    async def get_user_access_token(self, open_id: str) -> str | None:
        record = self.get_user_token_record(open_id)
        if not record:
            logger.warning("missing oauth token for open_id={}", open_id)
            return None
        access_token = record.get("access_token")
        expire_at = int(record.get("expire_at", 0))
        # 预留 60s 缓冲，避免拿到即将过期的 token 被 API 拒绝。
        if access_token and expire_at > int(time.time()) + 60:
            return access_token

        refresh_token = record.get("refresh_token")
        refresh_expire_at = int(record.get("refresh_expire_at", 0))
        if not refresh_token:
            logger.warning(
                "refresh token missing for open_id={}, drop record to trigger re-auth",
                open_id,
            )
            user_token_store.delete(open_id)
            return None
        if refresh_expire_at and refresh_expire_at <= int(time.time()):
            # refresh_token 本身也过期了，只能重新走 OAuth。清掉旧记录触发授权卡片分支。
            logger.warning(
                "refresh token expired for open_id={}, drop record to trigger re-auth",
                open_id,
            )
            user_token_store.delete(open_id)
            return None

        logger.info("user access token expired, refreshing open_id={}", open_id)
        try:
            refreshed = await self.refresh_user_access_token(refresh_token)
        except Exception:
            logger.exception("refresh access token failed, drop record for open_id={}", open_id)
            user_token_store.delete(open_id)
            return None
        # v2 刷新返回不含 open_id，把原记录的 open_id/union_id 带过来再存。
        merged = {**record, **refreshed, "open_id": open_id}
        self._save_user_token(merged)
        return merged.get("access_token")

    async def refresh_user_access_token(self, refresh_token: str) -> dict[str, Any]:
        # 飞书 OAuth v2 刷新 endpoint：通过 client_id/client_secret 认证，不走 tenant token。
        url = f"{settings.feishu_base_url}/open-apis/authen/v2/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.app_id,
            "client_secret": settings.app_secret,
        }
        client = await get_feishu_client()
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get("code", 0) != 0 or not data.get("access_token"):
            raise RuntimeError(f"refresh access token failed: {data}")
        logger.info("user access token refreshed")
        return data


auth_client = FeishuAuthClient()
