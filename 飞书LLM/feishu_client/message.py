import asyncio
import base64
import json
import re
from typing import Any

import httpx

from feishu_client.base import FeishuBaseClient
from feishu_client.base import FeishuApiError
from utils.logger import get_logger

logger = get_logger()

_RE_URL = re.compile(r"https?://[^\s<>\]\)\"']+")


class FeishuMessageClient:
    def __init__(self) -> None:
        self._base = FeishuBaseClient()

    async def send_text(
        self, receive_id: str, text: str, receive_id_type: str = "open_id"
    ) -> str | None:
        return await self._send_message(
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            msg_type="text",
            content={"text": text},
        )

    async def send_card(
        self, receive_id: str, card: dict[str, Any], receive_id_type: str = "open_id"
    ) -> str | None:
        return await self._send_message(
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            msg_type="interactive",
            content=card,
        )

    async def update_card(
        self, message_id: str, card: dict[str, Any]
    ) -> None:
        await self._base.request_as_tenant(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            json_body={"content": json.dumps(card, ensure_ascii=False)},
        )

    async def _send_message(
        self,
        *,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: dict[str, Any],
    ) -> str | None:
        path = "/open-apis/im/v1/messages"
        params = {"receive_id_type": receive_id_type}
        body = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        try:
            data = await self._base.request_as_tenant("POST", path, params=params, json_body=body)
        except httpx.HTTPStatusError as err:
            body_text = ""
            try:
                body_text = err.response.text or ""
            except Exception:
                body_text = ""
            if body_text and len(body_text) > 2000:
                body_text = body_text[:2000] + "...(truncated)"
            logger.exception(
                "send message http failed | status={} body={} receive_id_type={} receive_id={} msg_type={}",
                err.response.status_code if err.response else -1,
                body_text,
                receive_id_type,
                receive_id,
                msg_type,
            )
            raise
        except FeishuApiError as err:
            logger.exception(
                "send message failed | code={} msg={} receive_id_type={} receive_id={} msg_type={}",
                err.code,
                err.msg,
                receive_id_type,
                receive_id,
                msg_type,
            )
            raise
        except Exception:
            logger.exception(
                "send message failed | receive_id_type={} receive_id={} msg_type={}",
                receive_id_type,
                receive_id,
                msg_type,
            )
            raise
        logger.info(
            "message sent | receive_id_type={} receive_id={} msg_type={}",
            receive_id_type,
            receive_id,
            msg_type,
        )
        message_id = (data.get("data") or {}).get("message_id") if isinstance(data, dict) else None
        return str(message_id) if message_id else None

    async def get_message(self, open_id: str, message_id: str) -> dict[str, Any] | None:
        """拉单条消息详情。以用户身份调用，走用户可见范围。

        响应结构：data.items[0] = {message_id, msg_type, sender, chat_id, body:{content}}
        content 本身是 JSON 字符串，text 消息里是 {"text": "..."}。
        """
        try:
            data = await self._base.request_as_user(
                open_id,
                "GET",
                f"/open-apis/im/v1/messages/{message_id}",
            )
        except PermissionError:
            raise
        except Exception:
            logger.exception("get_message failed message_id={}", message_id)
            return None
        items = (data.get("data") or {}).get("items") or []
        return items[0] if items else None

    async def fetch_messages_text(
        self, open_id: str, message_ids: list[str], limit: int = 5
    ) -> list[dict[str, Any]]:
        """批量拉消息正文。单条失败不影响整体；最多 limit 条避免拖慢流水线。

        返回每条：{message_id, msg_type, chat_id, sender, text}
        text 为空字符串时表示没能成功解析正文。
        """
        ids = [m for m in message_ids if isinstance(m, str)][:limit]
        if not ids:
            return []

        results = await asyncio.gather(
            *[self.get_message(open_id, mid) for mid in ids],
            return_exceptions=True,
        )

        enriched: list[dict[str, Any]] = []
        text_ok = 0
        for mid, raw in zip(ids, results):
            if isinstance(raw, PermissionError):
                raise raw
            if isinstance(raw, Exception) or not isinstance(raw, dict):
                enriched.append(
                    {"message_id": mid, "msg_type": "", "sender": {}, "chat_id": "", "text": ""}
                )
                continue
            text = _extract_message_text(raw)
            links = _extract_message_links(raw)
            if text:
                text_ok += 1
            enriched.append(
                {
                    "message_id": mid,
                    "msg_type": raw.get("msg_type", ""),
                    "chat_id": raw.get("chat_id", ""),
                    "sender": raw.get("sender") or {},
                    "text": text,
                    "links": links,
                }
            )
        logger.info(
            "fetch_messages_text requested={} parsed={} with_text={}",
            len(ids),
            len([r for r in enriched if r.get("msg_type")]),
            text_ok,
        )
        return enriched

    async def get_message_asset(self, open_id: str, message_id: str) -> dict[str, Any] | None:
        raw = await self.get_message(open_id, message_id)
        if not isinstance(raw, dict):
            return None
        msg_type = str(raw.get("msg_type") or "")
        body = raw.get("body") or {}
        content_str = body.get("content") or ""
        if not isinstance(content_str, str) or not content_str.strip():
            return None
        try:
            content = json.loads(content_str)
        except Exception:
            return None
        if not isinstance(content, dict):
            return None

        file_key = ""
        mime_type = "application/octet-stream"
        if msg_type == "image":
            file_key = str(content.get("image_key") or "")
            mime_type = "image/png"
        elif msg_type == "file":
            file_key = str(content.get("file_key") or "")
        if not file_key:
            return None

        try:
            response = await self._base.request_raw_as_user(
                open_id,
                "GET",
                f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                extra_headers={"Accept": "application/octet-stream"},
            )
        except Exception:
            logger.exception("download message asset failed message_id={} file_key={}", message_id, file_key)
            return None

        content_type = response.headers.get("content-type", mime_type)
        data = response.content or b""
        if not data:
            return None
        return {
            "message_id": message_id,
            "msg_type": msg_type,
            "file_key": file_key,
            "content_type": content_type,
            "bytes": data,
            "base64": base64.b64encode(data).decode("ascii"),
        }


def _extract_message_text(msg: dict[str, Any]) -> str:
    """从飞书单条消息对象里抽可读文本。只保证 text / post 两种最常见形态。

    其它类型（interactive/image/file/sticker 等）返回"[<msg_type> 消息]"占位，
    便于 LLM 上下文里仍然知道命中了某种非文本消息。
    """
    body = msg.get("body") or {}
    content_str = body.get("content") or ""
    if not content_str:
        return ""
    try:
        content = json.loads(content_str)
    except Exception:
        # content 不是合法 JSON 的极少数兜底情况。
        return str(content_str)[:500]

    msg_type = msg.get("msg_type", "")
    if msg_type == "text" and isinstance(content, dict):
        return str(content.get("text") or "").strip()
    if msg_type == "post" and isinstance(content, dict):
        return _flatten_post_content(content)
    # 其它类型兜底：如果 content 是 dict 且含 text 字段，就用它；否则给类型占位。
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"].strip()
    return f"[{msg_type or 'unknown'} 消息]"

def _extract_message_links(msg: dict[str, Any]) -> list[str]:
    body = msg.get("body") or {}
    content_str = body.get("content") or ""
    if not content_str:
        return []
    try:
        content = json.loads(content_str)
    except Exception:
        content = {}
    msg_type = str(msg.get("msg_type") or "")
    links: list[str] = []
    if msg_type == "post" and isinstance(content, dict):
        links.extend(_collect_post_links(content))
    text = ""
    if msg_type == "text" and isinstance(content, dict):
        text = str(content.get("text") or "")
    elif isinstance(content, dict) and isinstance(content.get("text"), str):
        text = content.get("text") or ""
    if text:
        for m in _RE_URL.findall(text):
            if m and m not in links:
                links.append(m)
    return links


def _flatten_post_content(post_content: dict[str, Any]) -> str:
    """飞书 post 消息：{ zh_cn: {title, content: [[{tag, text, ...}, ...], ...]} }，
    把所有 tag=text/at 节点的文本拼起来，换行分隔段落。
    """
    segments: list[str] = []
    for lang_block in post_content.values():
        if not isinstance(lang_block, dict):
            continue
        title = lang_block.get("title") or ""
        if title:
            segments.append(str(title))
        paragraphs = lang_block.get("content") or []
        if not isinstance(paragraphs, list):
            continue
        for para in paragraphs:
            if not isinstance(para, list):
                continue
            line_parts: list[str] = []
            for node in para:
                if not isinstance(node, dict):
                    continue
                tag = node.get("tag")
                if tag in ("text", "md", "code_inline"):
                    line_parts.append(str(node.get("text") or ""))
                elif tag == "at":
                    line_parts.append(f"@{node.get('user_name') or node.get('user_id') or ''}")
                elif tag == "a":
                    line_parts.append(str(node.get("text") or node.get("href") or ""))
            if line_parts:
                segments.append("".join(line_parts))
        break
    return "\n".join(s for s in segments if s).strip()

def _collect_post_links(post_content: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for lang_block in post_content.values():
        if not isinstance(lang_block, dict):
            continue
        paragraphs = lang_block.get("content") or []
        if not isinstance(paragraphs, list):
            continue
        for para in paragraphs:
            if not isinstance(para, list):
                continue
            for node in para:
                if not isinstance(node, dict):
                    continue
                if node.get("tag") != "a":
                    continue
                href = node.get("href")
                if isinstance(href, str):
                    u = href.strip()
                    if u and u not in links:
                        links.append(u)
        break
    return links


message_client = FeishuMessageClient()
