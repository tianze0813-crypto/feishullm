from __future__ import annotations

from urllib.parse import urlencode

from config import settings

FEISHU_OAUTH_SCOPES: tuple[str, ...] = (
    "base:app:read",
    "base:record:retrieve",
    "base:table:read",
    "bitable:app:readonly",
    "calendar:calendar:read",
    "contact:contact.base:readonly",
    "contact:user.basic_profile:readonly",
    "contact:user.employee_id:readonly",
    "contact:user:search",
    "docs:document.content:read",
    "docx:document:readonly",
    "drive:drive.search:readonly",
    "im:chat:readonly",
    "im:message.group_msg:get_as_user",
    "im:message.p2p_msg:get_as_user",
    "im:message:readonly",
    "offline_access",
    "search:department:read",
    "search:docs:read",
    "search:message",
    "wiki:wiki:readonly",
)


def build_authorize_url() -> str:
    query = urlencode(
        {
            "app_id": settings.app_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "scope": " ".join(FEISHU_OAUTH_SCOPES),
        }
    )
    return f"https://open.feishu.cn/open-apis/authen/v1/authorize?{query}"
