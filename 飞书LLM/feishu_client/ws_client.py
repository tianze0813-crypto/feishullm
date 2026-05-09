import asyncio
import json
from asyncio import AbstractEventLoop
from typing import Any, Callable

from core.card_actions import dispatch_card_action
from core.event_handler import handle_message_event
from config import settings
from utils.logger import get_logger

logger = get_logger()


class FeishuWsClient:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._started = False
        self._loop: AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run_long_connection())

    async def _run_long_connection(self) -> None:
        def _handle_event(data: object) -> None:
            try:
                event = _read_path(data, "event")
                sender_open_id = _read_path(event, "sender", "sender_id", "open_id")
                chat_id = _read_path(event, "message", "chat_id")
                message_id = _read_path(event, "message", "message_id")
                msg_type = _read_path(event, "message", "message_type")
                content_raw = _read_path(event, "message", "content") or "{}"
                content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
                text = (content or {}).get("text", "").strip()
                if sender_open_id and (text or msg_type == "image"):
                    if self._loop is None:
                        raise RuntimeError("main event loop not initialized")
                    asyncio.run_coroutine_threadsafe(
                        handle_message_event(
                            user_open_id=sender_open_id,
                            message_text=text or "请分析这张图片",
                            chat_id=str(chat_id or "") if chat_id else None,
                            message_id=str(message_id or "") if message_id else None,
                            msg_type=str(msg_type or "") if msg_type else None,
                        ),
                        self._loop,
                    )
            except Exception:
                logger.exception("failed to handle websocket event")

        logger.info("starting feishu ws long connection")
        await asyncio.to_thread(self._run_ws_blocking, _handle_event)

    def _run_ws_blocking(self, event_fn: Callable[[Any], None]) -> None:
        try:
            # IMPORTANT: import inside worker thread so lark_oapi.ws.client
            # initializes its module-level loop in this thread.
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
        except ImportError:
            logger.warning("lark_oapi not available, long connection client not started")
            return

        # lark-oapi ws client internally drives its own 独立 asyncio loop，
        # 这里预先为本工作线程建一个 fresh loop，避免和 uvicorn 主 loop 冲突。
        # 用 try/finally 兜 close() —— 否则进程 shutdown / ws_client.start() 抛异常时
        # loop 不会被回收，selector fd / 内部 task 会残留。
        worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(worker_loop)
        try:
            def _handle_card_action(data: object) -> object:
                try:
                    event = _read_path(data, "event")
                    operator_open_id = _read_path(event, "operator", "open_id")
                    open_message_id = _read_path(event, "context", "open_message_id")
                    value = _read_path(event, "action", "value")
                    value_dict: dict[str, Any] | None = None
                    if isinstance(value, dict):
                        value_dict = value
                    elif isinstance(value, str) and value.strip():
                        try:
                            parsed = json.loads(value)
                            if isinstance(parsed, dict):
                                value_dict = parsed
                        except Exception:
                            value_dict = None
                    logger.info(
                        "incoming card action open_id={} action={}",
                        operator_open_id or "",
                        (value_dict or {}).get("action", ""),
                    )
                    action = str((value_dict or {}).get("action") or "")
                    if isinstance(value_dict, dict) and open_message_id:
                        value_dict["_open_message_id"] = str(open_message_id or "")
                    toast_content = {
                        "new_topic": "已开启新话题",
                        "switch_topic": "已切换会话",
                        "list_topics": "正在打开历史会话",
                        "skip_oauth": "已进入基础模式",
                        "triage_template": "已发送补充模板",
                        "retry_with_query": "正在重新检索",
                        "general_chat_fallback": "正在切换为通识问答",
                        "toggle_evidence": "正在更新卡片",
                    }.get(action, "已收到")
                    if operator_open_id and self._loop is not None:
                        asyncio.run_coroutine_threadsafe(
                            dispatch_card_action(str(operator_open_id), value_dict),
                            self._loop,
                        )
                    return P2CardActionTriggerResponse({"toast": {"type": "success", "content": toast_content}})
                except Exception:
                    logger.exception("failed to handle card action trigger")
                    return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "处理失败"}})

            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(event_fn)
                .register_p2_card_action_trigger(_handle_card_action)
                .build()
            )
            ws_client = lark.ws.Client(
                settings.app_id,
                settings.app_secret,
                event_handler=event_handler,
                log_level=getattr(lark.LogLevel, settings.ws_log_level, lark.LogLevel.INFO),
            )
            ws_client.start()
        finally:
            try:
                worker_loop.close()
            except Exception:
                logger.exception("failed to close ws worker event loop")

    async def handle_incoming_text(self, user_open_id: str, text: str) -> None:
        """
        Temporary entry for local tests before real event subscription is connected.
        """
        await handle_message_event(user_open_id=user_open_id, message_text=text)


def _read_path(data: object, *keys: str) -> object | None:
    current: object | None = data
    for key in keys:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


ws_client = FeishuWsClient()
