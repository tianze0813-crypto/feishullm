import asyncio
import os
import re
from typing import Any

from config import settings
from core.chitchat import run_chitchat
from core.formatter import build_notice_card, build_oauth_card, build_result_card, build_thinking_card
from core.find_person import run_find_person
from core.image_analysis import analyze_message_image
from core.intent import IntentResult, detect_intent
from core.search_knowledge import run_search_knowledge
from feishu_client.auth import auth_client
from feishu_client.message import message_client
from llm.client import llm_client
from utils.cache import cache
from utils.conversation import conversation_store
from utils.feishu_oauth import build_authorize_url
from utils.logger import get_logger

logger = get_logger()

_PENDING_QUESTION_TTL = 600
_RECENT_MESSAGE_TTL = 600
_INFLIGHT_TEXT_TTL = max(30, settings.processing_timeout_seconds + 30)
_DUPLICATE_NOTICE_TTL = 15


def _build_authorize_url() -> str:
    return build_authorize_url()


def _pending_question_key(open_id: str) -> str:
    return f"pending:{open_id}"


def _recent_message_key(message_id: str) -> str:
    return f"recent_message:{message_id}"


def _normalize_message_text(text: str) -> str:
    compact = re.sub(r"\s+", "", (text or "").strip())
    return compact[:120]


def _inflight_text_key(open_id: str, message_text: str) -> str:
    return f"inflight_text:{open_id}:{_normalize_message_text(message_text)}"


def _duplicate_notice_key(open_id: str, message_text: str) -> str:
    return f"duplicate_notice:{open_id}:{_normalize_message_text(message_text)}"


async def handle_message_event(
    user_open_id: str,
    message_text: str,
    chat_id: str | None = None,
    message_id: str | None = None,
    msg_type: str | None = None,
) -> None:
    logger.info(
        "incoming message pid={} open_id={} chat_id={} message_id={} msg_type={} text={!r}",
        os.getpid(),
        user_open_id,
        str(chat_id or ""),
        str(message_id or ""),
        str(msg_type or ""),
        message_text,
    )
    if message_id and not cache.add_if_absent(
        _recent_message_key(message_id),
        True,
        ttl_seconds=_RECENT_MESSAGE_TTL,
    ):
        logger.info(
            "skip duplicated incoming event open_id={} message_id={} text={!r}",
            user_open_id,
            message_id,
            message_text,
        )
        return

    inflight_key = ""
    if (msg_type or "") != "image" and message_text.strip():
        inflight_key = _inflight_text_key(user_open_id, message_text)
        if not cache.add_if_absent(
            inflight_key,
            {"message_id": str(message_id or ""), "text": message_text},
            ttl_seconds=_INFLIGHT_TEXT_TTL,
        ):
            logger.info(
                "skip duplicated in-flight question open_id={} message_id={} text={!r}",
                user_open_id,
                str(message_id or ""),
                message_text,
            )
            notice_key = _duplicate_notice_key(user_open_id, message_text)
            if cache.add_if_absent(notice_key, True, ttl_seconds=_DUPLICATE_NOTICE_TTL):
                try:
                    await message_client.send_card(
                        user_open_id,
                        build_notice_card("飞书智能助手", "上一条相同问题仍在处理中，请稍等。"),
                    )
                except Exception:
                    logger.exception("failed to send duplicate in-flight notice")
            return
    try:
        if (msg_type or "") == "image" and message_id:
            await _handle_image_message(
                user_open_id,
                message_text,
                chat_id=chat_id,
                message_id=message_id,
            )
            return
        session = conversation_store.get_active_session(user_open_id)
        model_context = _build_model_context(user_open_id)
        resolved_question = await _maybe_rewrite_question(user_open_id, message_text, session)
        if resolved_question != message_text:
            logger.info("question rewritten: {!r} -> {!r}", message_text, resolved_question)
        intent = await detect_intent(resolved_question, conversation_context=model_context)
        if intent.label != "chitchat" and not intent.keyword and session.topic:
            intent = IntentResult(
                label=intent.label,
                keyword=session.topic,
                keyword_fallback=intent.keyword_fallback,
                person_hint=intent.person_hint,
                raw_question=resolved_question,
            )
        elif intent.raw_question != resolved_question:
            intent = IntentResult(
                label=intent.label,
                keyword=intent.keyword,
                keyword_fallback=intent.keyword_fallback,
                person_hint=intent.person_hint,
                raw_question=resolved_question,
            )
        logger.info(
            "intent detected: label={} keyword={!r} person_hint={!r}",
            intent.label,
            intent.keyword,
            intent.person_hint,
        )
        _update_session_state_before(
            user_open_id,
            message_text,
            intent,
            chat_id=chat_id,
            rewritten_question=resolved_question if resolved_question != message_text else "",
        )

        if intent.label != "chitchat":
            if not await auth_client.get_user_access_token(user_open_id):
                cache.set(
                    _pending_question_key(user_open_id),
                    {"text": message_text, "chat_id": str(chat_id or "")},
                    ttl_seconds=_PENDING_QUESTION_TTL,
                )
                authorize_url = _build_authorize_url()
                await message_client.send_card(
                    user_open_id,
                    build_oauth_card(authorize_url, pending_question=message_text),
                )
                return
            if intent.keyword:
                conversation_store.set_topic(user_open_id, intent.keyword)

        thinking_msg_id = await message_client.send_card(
            user_open_id,
            build_thinking_card(message_text),
        )

        # 用独立 task + shield 包裹主流水线，保证 asyncio.wait_for 超时时
        # 只会中断"等它完成"的这一步，不会 cancel 底层 task。这样：
        # - 超时 ≤ N 秒：先给用户发一条"查询比较复杂"的等待提示
        # - 然后继续 await 同一个 task 到完成，不会重跑搜索和 LLM
        pipeline_task = asyncio.create_task(_execute_with_intent(user_open_id, message_text, intent, chat_id))
        try:
            conversation_store.add_turn(
                user_open_id,
                "user",
                message_text,
                topic_hint=intent.keyword or session.topic,
            )
            answer, sources, entities = await asyncio.wait_for(
                asyncio.shield(pipeline_task),
                timeout=settings.processing_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("processing still running at {}s, sending interim reply",
                           settings.processing_timeout_seconds)
            try:
                await message_client.update_card(
                    thinking_msg_id,
                    build_notice_card("飞书智能助手", "查询比较复杂，请稍等…"),
                )
            except Exception:
                logger.exception("failed to send interim card")
            answer, sources, entities = await pipeline_task
        conversation_store.add_turn(user_open_id, "assistant", str(answer))
        _update_session_state_after(user_open_id, intent, answer, sources, entities, chat_id=chat_id)
        await _send_final_card(user_open_id, message_text, answer, sources, entities)
    except PermissionError:
        logger.warning("oauth token missing or expired for user {}", user_open_id)
        cache.set(
            _pending_question_key(user_open_id),
            {"text": message_text, "chat_id": str(chat_id or "")},
            ttl_seconds=_PENDING_QUESTION_TTL,
        )
        authorize_url = _build_authorize_url()
        await message_client.send_card(
            user_open_id,
            build_oauth_card(authorize_url, pending_question=message_text),
        )
    except Exception:
        logger.exception("message pipeline failed for user {}", user_open_id)
        if 'thinking_msg_id' in locals() and thinking_msg_id:
            try:
                await message_client.update_card(
                    thinking_msg_id,
                    build_notice_card("飞书智能助手 - 查询失败", "查询过程中出现异常，请稍后重试。"),
                )
            except Exception:
                pass
        else:
            await message_client.send_card(
                user_open_id,
                build_notice_card("飞书智能助手 - 查询失败", "查询过程中出现异常，请稍后重试。"),
            )
    finally:
        if inflight_key:
            cache.delete(inflight_key)


async def _send_final_card(
    user_open_id: str,
    question: str,
    answer: str,
    sources: list[str],
    entities: dict | None,
) -> None:
    sess = conversation_store.get_active_session(user_open_id)
    card = build_result_card(
        question=question,
        answer=answer,
        sources=sources,
        entities=entities,
        topic=sess.topic or None,
        session_id=sess.session_id,
        turns=sess.turns,
    )
    await message_client.send_card(user_open_id, card)


async def _handle_image_message(
    user_open_id: str,
    question: str,
    *,
    chat_id: str | None,
    message_id: str,
) -> None:
    conversation_store.add_turn(user_open_id, "user", question, topic_hint="图片分析")
    conversation_store.update_session_state(
        user_open_id,
        {
            "topic": "图片分析",
            "current_question": question,
            "current_chat_id": str(chat_id or ""),
            "current_intent": "image_analysis",
            "current_query": {"keyword": "图片分析", "keyword_fallback": "", "person_hint": ""},
            "current_image_message_id": message_id,
            "last_turn": {"role": "user", "content": question},
        },
    )
    thinking_msg_id = await message_client.send_card(
        user_open_id,
        build_thinking_card(question),
    )
    answer = await analyze_message_image(user_open_id, message_id, question)
    conversation_store.add_turn(user_open_id, "assistant", answer)
    conversation_store.update_session_state(
        user_open_id,
        {
            "last_result": {
                "intent": "image_analysis",
                "question": question,
                "search_key": "图片分析",
                "keyword_fallback": "",
                "answer_summary": _short_text(answer, 280),
                "sources": ["图片分析"],
                "conflict": False,
                "entities": {"people": [], "docs": [], "messages": []},
            },
            "last_turn": {"role": "assistant", "content": _short_text(answer, 240)},
        },
    )
    if thinking_msg_id:
        try:
            await message_client.update_card(
                thinking_msg_id,
                build_result_card(
                    question=question,
                    answer=answer,
                    sources=["图片分析"],
                    entities={"_meta": {"label": "search_knowledge", "hit_query": "图片分析", "exact_match": True}},
                    topic="图片分析",
                    session_id=conversation_store.active_session_id(user_open_id),
                    turns=conversation_store.get_active_session(user_open_id).turns,
                ),
            )
            return
        except Exception:
            logger.exception("failed to update image analysis card")
    await _send_final_card(user_open_id, question, answer, ["图片分析"], {"_meta": {"label": "search_knowledge", "hit_query": "图片分析", "exact_match": True}})


async def _execute_with_intent(
    open_id: str, question: str, intent, chat_id: str | None
) -> tuple[str, list[str], dict]:
    convo_ctx = _build_model_context(open_id, max_turns=6)
    if intent.label == "chitchat":
        answer, sources = await run_chitchat(open_id, question, conversation_context=convo_ctx)
        return answer, sources, {"_meta": {"label": "chitchat", "exact_match": True}}

    if intent.label == "find_person":
        return await run_find_person(
            open_id, intent, conversation_context=convo_ctx, current_chat_id=chat_id
        )
    # 默认走 search_knowledge（意图解析失败时降级已经归到 chitchat，
    # 走到这里只剩 search_knowledge 一个分支）。
    return await run_search_knowledge(
        open_id, intent, conversation_context=convo_ctx, current_chat_id=chat_id
    )


_RE_PERSON_NAME = re.compile(
    r"(?:找|联系|是|由|请找|@)\s*([一-鿿]{2,3})"
    r"|([一-鿿]{2,3})\s*(?:负责|处理|对接|管理|是)"
)


def _extract_last_person_name(conversation_text: str) -> str:
    """从对话历史中提取助手最近提到的人名，用于追问指代消解。
    从最近的助手回复往前翻，取第一个找到的——避免被"暂无明确候选"覆盖。"""
    if not conversation_text:
        return ""
    # 收集所有助手回复，从后往前扫描
    assistant_lines = [l for l in conversation_text.split("\n") if l.startswith("助手：")]
    for line in reversed(assistant_lines):
        matches = _RE_PERSON_NAME.findall(line)
        for match in reversed(matches):
            name = match[0] or match[1]
            if name and name not in ("什么", "怎么", "哪里", "为什么", "怎么办", "是一个"):
                return name
    return ""


def _looks_like_followup(text: str) -> bool:
    q = (text or "").strip()
    if not q:
        return False
    # 指代词 + 短句：用户很可能在指代上一条的上下文
    ref_markers = (
        "这", "那", "它", "他", "她", "这个", "那个",
        "上面", "前面", "刚才", "继续", "然后", "再", "还", "也",
    )
    if any(m in q for m in ref_markers):
        return len(q) <= 20
    # 无法独立成句的追问词（不是独立话题，必须依赖上文）
    followup_starts = (
        "怎么", "为什么", "在哪", "是什么", "怎么办",
        "然后呢", "还有呢", "具体", "详细", "比如", "举例",
    )
    if any(q.startswith(p) for p in followup_starts):
        return True
    return False


async def _maybe_rewrite_question(
    user_open_id: str,
    question: str,
    session,
) -> str:
    q = (question or "").strip()
    if not q:
        return question
    if not session.topic or not _looks_like_followup(q):
        return question
    convo_ctx = conversation_store.get_context_text(user_open_id, max_turns=12)
    session_state = conversation_store.get_session_state_text(user_open_id)
    try:
        rewritten = await llm_client.resolve_pronouns(
            q,
            conversation_context=convo_ctx,
            session_state=session_state,
        )
    except Exception:
        logger.exception("resolve_pronouns failed, fallback to heuristic")
        rewritten = ""
    rewritten = (rewritten or "").strip()
    if rewritten and rewritten != q:
        return rewritten

    last_name = _extract_last_person_from_state(user_open_id) or (_extract_last_person_name(convo_ctx) if convo_ctx else "")
    contact_markers = ("联系方式", "怎么联系", "如何联系", "电话", "手机号", "邮箱", "邮件", "微信")
    if last_name and any(m in q for m in contact_markers):
        return f"{last_name} 联系方式"
    if any(m in q for m in ("这个", "那个", "它", "这件事", "这条", "上面那个", "前面那个")):
        topic = session.topic or _extract_topic_from_state(user_open_id)
        if topic:
            return q.replace("上面那个", topic).replace("前面那个", topic).replace("这件事", topic).replace("这条", topic).replace("这个", topic).replace("那个", topic).replace("它", topic)
    if _looks_like_followup(q):
        topic = session.topic or _extract_topic_from_state(user_open_id)
        if topic and q.startswith(("怎么", "为什么", "在哪", "是什么", "怎么办", "如何")):
            return f"{topic}{q}"
    return question


def _build_model_context(user_open_id: str, max_turns: int = 8) -> str:
    state_text = conversation_store.get_session_state_text(user_open_id)
    convo_text = conversation_store.get_context_text(user_open_id, max_turns=max_turns)
    parts = []
    if state_text:
        parts.append("[会话状态摘要]\n" + state_text)
    if convo_text:
        parts.append("[最近对话]\n" + convo_text)
    return "\n\n".join(parts).strip()


def _extract_last_person_from_state(user_open_id: str) -> str:
    state = conversation_store.get_session_state(user_open_id)
    if not isinstance(state, dict):
        return ""
    last_result = state.get("last_result")
    if not isinstance(last_result, dict):
        return ""
    entities = last_result.get("entities")
    if isinstance(entities, dict):
        people = entities.get("people") or []
        for item in people:
            name = str(item or "").strip()
            if name:
                return name
    return ""


def _extract_topic_from_state(user_open_id: str) -> str:
    state = conversation_store.get_session_state(user_open_id)
    if not isinstance(state, dict):
        return ""
    return str(state.get("topic") or "").strip()


def _update_session_state_before(
    user_open_id: str,
    message_text: str,
    intent: IntentResult,
    *,
    chat_id: str | None,
    rewritten_question: str = "",
) -> None:
    session = conversation_store.get_active_session(user_open_id)
    conversation_store.update_session_state(
        user_open_id,
        {
            "session_id": session.session_id,
            "topic": session.topic or intent.keyword or "",
            "current_question": _short_text(message_text, 200),
            "rewritten_question": _short_text(rewritten_question, 200) if rewritten_question else "",
            "current_chat_id": str(chat_id or ""),
            "current_intent": intent.label,
            "current_query": {
                "keyword": intent.keyword,
                "keyword_fallback": intent.keyword_fallback,
                "person_hint": intent.person_hint,
            },
            "last_turn": {
                "role": "user",
                "content": _short_text(message_text, 200),
            },
        },
    )


def _update_session_state_after(
    user_open_id: str,
    intent: IntentResult,
    answer: str,
    sources: list[str],
    entities: dict[str, Any] | None,
    *,
    chat_id: str | None,
) -> None:
    session = conversation_store.get_active_session(user_open_id)
    state_patch = {
        "session_id": session.session_id,
        "topic": session.topic or intent.keyword or "",
        "current_chat_id": str(chat_id or ""),
        "general_chat_fallback_enabled": bool(
            conversation_store.get_session_state(user_open_id).get("general_chat_fallback_enabled")
        )
        if intent.label == "search_knowledge"
        else False,
        "last_result": {
            "intent": intent.label,
            "question": intent.raw_question,
            "search_key": intent.search_key,
            "keyword_fallback": intent.keyword_fallback,
            "answer_summary": _short_text(answer, 280),
            "sources": [str(s) for s in (sources or [])[:6]],
            "conflict": "存在冲突" in str(answer or ""),
            "entities": _summarize_entities(entities),
        },
        "last_card_payload": {
            "question": intent.raw_question,
            "answer": _short_text(answer, 1200),
            "sources": [str(s) for s in (sources or [])[:12]],
            "entities": entities or {},
            "evidence_expanded": False,
        },
        "last_turn": {
            "role": "assistant",
            "content": _short_text(answer, 240),
        },
    }
    conversation_store.update_session_state(user_open_id, state_patch)


def _summarize_entities(entities: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entities, dict):
        return {}
    people = []
    for item in entities.get("people") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name and name not in people:
                people.append(name)
    docs = []
    for item in entities.get("docs") or []:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or "").strip()
            if title and title not in docs:
                docs.append(title)
    messages = []
    for item in entities.get("messages") or []:
        if isinstance(item, dict):
            text = _short_text(str(item.get("text") or "").strip(), 80)
            if text:
                messages.append(text)
    meta = entities.get("_meta") if isinstance(entities.get("_meta"), dict) else {}
    return {
        "people": people[:5],
        "docs": docs[:5],
        "messages": messages[:3],
        "meta": {
            "label": str(meta.get("label") or ""),
            "hit_query": str(meta.get("hit_query") or ""),
            "exact_match": bool(meta.get("exact_match")) if meta else False,
        },
    }


def _short_text(text: str, max_len: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max(0, int(max_len) - 1)] + "…"
