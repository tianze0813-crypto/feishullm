from __future__ import annotations

from feishu_client.message import message_client
from llm.client import llm_client
from utils.logger import get_logger

logger = get_logger()


async def analyze_message_image(open_id: str, message_id: str, question: str) -> str:
    asset = await message_client.get_message_asset(open_id, message_id)
    if not asset:
        return "已收到图片，但暂时无法读取图片内容。"
    try:
        return await llm_client.analyze_image(
            question=question,
            image_base64=str(asset.get("base64") or ""),
            mime_type=str(asset.get("content_type") or "image/png"),
        )
    except Exception:
        logger.exception("image analysis failed message_id={}", message_id)
        return "已收到图片，但当前模型暂时无法完成图片分析。"
