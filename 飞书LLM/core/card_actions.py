from __future__ import annotations

from config import settings
from typing import Any

from core.formatter import (
    build_notice_card,
    build_knowledge_chat_selector_card,
    build_oauth_card,
    build_result_card,
    build_topic_list_card,
)
from core.chitchat import run_chitchat
from core.intent import IntentResult, detect_intent
from core.find_person import run_find_person
from core.search_knowledge import run_search_knowledge
from feishu_client.auth import auth_client
from feishu_client.chat import chat_client
from feishu_client.doc import doc_client
from feishu_client.message import message_client
from feishu_client.wiki import wiki_client
from utils.cache import cache
from utils.conversation import conversation_store
from utils.feishu_oauth import build_authorize_url
from utils.logger import get_logger

logger = get_logger()

_DEFAULT_EXTERNAL_WIKI_NODE_TOKEN = "Fi8wwksSIiVheukqnLjclKe9nrG"
_DEFAULT_EXTERNAL_WIKI_LABEL = "默认 Wiki 知识库"


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default



async def dispatch_card_action(user_open_id: str, value: dict[str, Any] | None) -> str:
    if not user_open_id:
        return "缺少用户信息"
    payload = value or {}
    open_message_id = str(payload.get("_open_message_id") or "").strip()
    action = str(payload.get("action") or "")

    if action == "skip_oauth":
        await message_client.send_card(
            user_open_id,
            build_notice_card(
                "飞书智能助手 - 基础模式",
                "已进入基础模式：我可以提供基础说明与建议，但不会检索云文档/群消息/知识库。\n\n"
                "需要检索时，再点击授权按钮即可。",
            ),
        )
        return "基础模式"

    if action == "new_topic":
        sess = conversation_store.new_topic(user_open_id)
        logger.info("new topic created open_id={} session_id={}", user_open_id, sess.session_id)
        return "已开启新话题"

    if action == "list_topics":
        sessions = conversation_store.list_sessions(user_open_id, limit=10)
        await message_client.send_card(user_open_id, build_topic_list_card(sessions))
        return "已发送话题列表"

    if action == "switch_topic":
        session_id = str(payload.get("session_id") or "")
        sess = conversation_store.switch_topic(user_open_id, session_id)
        if not sess:
            logger.info("switch topic failed open_id={} session_id={}", user_open_id, session_id)
            return "话题不存在"
        topic = sess.topic or "未命名"
        logger.info("topic switched open_id={} session_id={} topic={}", user_open_id, sess.session_id, topic)
        return "已切换话题"

    if action == "triage_template":
        question = str(payload.get("question") or "").strip()
        content = (
            "请按这个模板回复（复制一行改内容即可）：\n\n"
            "系统/主题：____\n"
            "部门/业务线：____\n"
            "时间范围：____（可选）\n"
            "想要的结果：入口链接 / SOP流程 / 负责人 / 制度条款"
        )
        if question:
            content += f"\n\n你的原问题：`{question}`"
        await message_client.send_card(
            user_open_id,
            build_notice_card(
                "飞书智能助手 - 补充信息",
                content,
            ),
        )
        return "已发送模板"

    if action == "retry_with_query":
        label = str(payload.get("label") or "search_knowledge")
        question = str(payload.get("question") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
        if not question or not keyword:
            return "参数缺失"

        if not await auth_client.get_user_access_token(user_open_id):
            cache.set(f"pending:{user_open_id}", question, ttl_seconds=600)
            authorize_url = _build_authorize_url()
            await message_client.send_card(
                user_open_id,
                build_oauth_card(authorize_url, pending_question=question),
            )
            return "需要授权"

        conversation_store.set_topic(user_open_id, keyword)
        conversation_store.add_turn(user_open_id, "user", question, topic_hint=keyword)
        conversation_store.update_session_state(
            user_open_id,
            {
                "current_question": question,
                "current_intent": label if label in ("find_person", "search_knowledge") else "search_knowledge",
                "current_query": {
                    "keyword": keyword,
                    "keyword_fallback": "",
                    "person_hint": "",
                },
                "last_turn": {"role": "user", "content": question},
            },
        )
        intent = IntentResult(
            label=label if label in ("find_person", "search_knowledge") else "search_knowledge",
            keyword=keyword,
            keyword_fallback="",
            person_hint="",
            raw_question=question,
        )
        if intent.label == "find_person":
            answer, sources, entities = await run_find_person(user_open_id, intent)
        else:
            convo_ctx = conversation_store.get_context_text(user_open_id, max_turns=6)
            answer, sources, entities = await run_search_knowledge(
                user_open_id, intent, conversation_context=convo_ctx
            )
        conversation_store.add_turn(user_open_id, "assistant", str(answer))
        conversation_store.update_session_state(
            user_open_id,
            {
                "last_result": {
                    "intent": intent.label,
                    "question": question,
                    "search_key": intent.search_key,
                    "keyword_fallback": "",
                    "answer_summary": str(answer)[:280],
                    "sources": [str(s) for s in (sources or [])[:6]],
                    "conflict": "存在冲突" in str(answer or ""),
                },
                "last_turn": {"role": "assistant", "content": str(answer)[:240]},
            },
        )
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
        conversation_store.update_session_state(
            user_open_id,
            {
                "last_card_payload": {
                    "question": question,
                    "answer": str(answer)[:1200],
                    "sources": [str(s) for s in (sources or [])[:12]],
                    "entities": entities or {},
                    "evidence_expanded": False,
                }
            },
        )
        await message_client.send_card(user_open_id, card)
        return "已重试"

    if action == "toggle_evidence":
        expanded = _safe_bool(payload.get("expanded"))
        state = conversation_store.get_session_state(user_open_id)
        last_card = state.get("last_card_payload") if isinstance(state, dict) else None
        if not isinstance(last_card, dict):
            return "缺少上下文"
        question = str(last_card.get("question") or "").strip()
        answer = str(last_card.get("answer") or "").strip()
        sources = last_card.get("sources") if isinstance(last_card.get("sources"), list) else []
        entities = last_card.get("entities") if isinstance(last_card.get("entities"), dict) else {}
        sess = conversation_store.get_active_session(user_open_id)
        card = build_result_card(
            question=question,
            answer=answer,
            sources=[str(s) for s in (sources or [])],
            entities=entities,
            topic=sess.topic or None,
            session_id=sess.session_id,
            turns=sess.turns,
            evidence_expanded=expanded,
        )
        conversation_store.update_session_state(
            user_open_id,
            {"last_card_payload": {**last_card, "evidence_expanded": expanded}},
        )
        if open_message_id:
            await message_client.update_card(open_message_id, card)
            return "已更新卡片"
        await message_client.send_card(user_open_id, card)
        return "已发送更新卡片"

    if action == "attach_default_wiki":
        question = str(payload.get("question") or "").strip()
        try:
            attached = await _attach_default_external_wiki(user_open_id)
        except PermissionError:
            cache.set(f"pending:{user_open_id}", question or "接入默认Wiki", ttl_seconds=600)
            authorize_url = _build_authorize_url()
            await message_client.send_card(
                user_open_id,
                build_oauth_card(authorize_url, pending_question=question or "接入默认Wiki"),
            )
            return "需要授权"
        except Exception:
            logger.exception("attach default wiki failed open_id={}", user_open_id)
            await message_client.send_card(
                user_open_id,
                build_notice_card("飞书智能助手 - 接入失败", "默认 Wiki 接入失败，请稍后重试。"),
            )
            return "接入失败"
        if not attached:
            await message_client.send_card(
                user_open_id,
                build_notice_card("飞书智能助手 - 接入失败", "未能解析默认 Wiki，请检查 token 或访问权限。"),
            )
            return "接入失败"
        if not question:
            await message_client.send_card(
                user_open_id,
                build_notice_card(
                    "飞书智能助手 - 已接入外接知识",
                    f"已接入 `{attached.get('title') or _DEFAULT_EXTERNAL_WIKI_LABEL}`。\n\n你可以直接继续提问，后续知识问答会优先参考这份外接 Wiki。",
                ),
            )
            return "已接入"
        answer, sources, entities = await _rerun_search_question(user_open_id, question)
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
        conversation_store.update_session_state(
            user_open_id,
            {
                "last_card_payload": {
                    "question": question,
                    "answer": str(answer)[:1200],
                    "sources": [str(s) for s in (sources or [])[:12]],
                    "entities": entities or {},
                    "evidence_expanded": False,
                }
            },
        )
        if open_message_id:
            await message_client.update_card(open_message_id, card)
            return "已接入并重试"
        await message_client.send_card(user_open_id, card)
        return "已接入并重试"

    if action == "clear_external_knowledge":
        conversation_store.update_session_state(
            user_open_id,
            {"external_knowledge_docs": []},
        )
        await message_client.send_card(
            user_open_id,
            build_notice_card("飞书智能助手 - 已清除", "已清除当前会话的外接 Wiki/文档。"),
        )
        return "已清除"

    if action == "general_chat_fallback":
        question = str(payload.get("question") or "").strip()
        if not question:
            return "参数缺失"
        state = conversation_store.get_session_state(user_open_id)
        current_intent = str(state.get("current_intent") or "").strip()
        last_result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
        last_intent = str(last_result.get("intent") or "").strip()
        if current_intent == "find_person" or last_intent == "find_person":
            await message_client.send_card(
                user_open_id,
                build_notice_card(
                    "飞书智能助手 - 通识问答未启用",
                    "找人问题不触发通识问答。\n\n负责人、联系人和归属判断只依据内部资料与检索证据，请继续补充部门、项目或流程线索。",
                ),
            )
            return "找人问题不支持通识问答"
        convo_ctx = conversation_store.get_context_text(user_open_id, max_turns=6)
        answer, sources = await run_chitchat(
            user_open_id,
            question,
            conversation_context=convo_ctx,
            force_general=True,
        )
        conversation_store.add_turn(user_open_id, "assistant", str(answer))
        conversation_store.update_session_state(
            user_open_id,
            {
                "current_question": question,
                "current_intent": "chitchat",
                "general_chat_fallback_enabled": True,
                "last_result": {
                    "intent": "chitchat",
                    "question": question,
                    "search_key": "",
                    "keyword_fallback": "",
                    "answer_summary": str(answer)[:280],
                    "sources": [str(s) for s in (sources or [])[:6]],
                    "conflict": False,
                },
                "last_turn": {"role": "assistant", "content": str(answer)[:240]},
            },
        )
        sess = conversation_store.get_active_session(user_open_id)
        card = build_result_card(
            question=question,
            answer=answer,
            sources=sources,
            entities={"_meta": {"label": "chitchat", "exact_match": True}},
            topic=sess.topic or None,
            session_id=sess.session_id,
            turns=sess.turns,
        )
        await message_client.send_card(user_open_id, card)
        return "已切换为通识问答"

    if action == "open_knowledge_chat_selector":
        await _send_knowledge_chat_selector(user_open_id, offset=int(payload.get("offset") or 0))
        return "已打开"

    if action == "knowledge_chat_page":
        await _send_knowledge_chat_selector(user_open_id, offset=int(payload.get("offset") or 0))
        return "已翻页"

    if action == "knowledge_chat_set_all":
        conversation_store.update_session_state(
            user_open_id,
            {"knowledge_chat_ids": [], "knowledge_chat_names": {}},
        )
        await _send_knowledge_chat_selector(user_open_id, offset=int(payload.get("offset") or 0))
        return "已切换为全部群聊"

    if action == "knowledge_chat_toggle":
        chat_id = str(payload.get("chat_id") or "").strip()
        if not chat_id:
            return "参数缺失"
        state = conversation_store.get_session_state(user_open_id)
        current = state.get("knowledge_chat_ids") if isinstance(state, dict) else None
        selected: list[str] = []
        if isinstance(current, list):
            for item in current:
                cid = str(item or "").strip()
                if cid and cid not in selected:
                    selected.append(cid)
        if chat_id in selected:
            selected = [cid for cid in selected if cid != chat_id]
        else:
            selected.append(chat_id)
        names = state.get("knowledge_chat_names") if isinstance(state, dict) else None
        name_map: dict[str, str] = {}
        if isinstance(names, dict):
            for k, v in names.items():
                cid = str(k or "").strip()
                nm = str(v or "").strip()
                if cid and nm:
                    name_map[cid] = nm
        if chat_id not in name_map:
            name = await _get_chat_display_name(user_open_id, chat_id)
            if name:
                name_map[chat_id] = name
        conversation_store.update_session_state(
            user_open_id,
            {"knowledge_chat_ids": selected, "knowledge_chat_names": name_map},
        )
        await _send_knowledge_chat_selector(user_open_id, offset=int(payload.get("offset") or 0))
        return "已更新"

    logger.warning("unknown card action open_id={} payload={}", user_open_id, payload)
    return "未识别的操作"


def _build_authorize_url() -> str:
    return build_authorize_url()


async def _send_knowledge_chat_selector(user_open_id: str, *, offset: int = 0) -> None:
    state = conversation_store.get_session_state(user_open_id)
    selected_chat_ids_raw = state.get("knowledge_chat_ids") if isinstance(state, dict) else None
    selected_chat_ids: list[str] = []
    if isinstance(selected_chat_ids_raw, list):
        for item in selected_chat_ids_raw:
            cid = str(item or "").strip()
            if cid and cid not in selected_chat_ids:
                selected_chat_ids.append(cid)
    external_docs_raw = state.get("external_knowledge_docs") if isinstance(state, dict) else None
    external_sources: list[str] = []
    if isinstance(external_docs_raw, list):
        for item in external_docs_raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("source_label") or "").strip()
            if title and title not in external_sources:
                external_sources.append(title)
    chats = await _load_seen_chats(user_open_id)
    await message_client.send_card(
        user_open_id,
        build_knowledge_chat_selector_card(
            chats,
            selected_chat_ids,
            offset=int(offset),
            external_sources=external_sources,
        ),
    )


async def _load_seen_chats(user_open_id: str) -> list[dict[str, Any]]:
    state = conversation_store.get_session_state(user_open_id)
    name_map = state.get("knowledge_chat_names") if isinstance(state, dict) else None
    cached_names: dict[str, str] = {}
    if isinstance(name_map, dict):
        for k, v in name_map.items():
            cid = str(k or "").strip()
            nm = str(v or "").strip()
            if cid and nm:
                cached_names[cid] = nm
    updated_names: dict[str, str] = dict(cached_names)
    chats = await _list_group_chats_for_user(user_open_id, cached_names, updated_names)
    if not chats:
        chats = await _load_seen_chats_fallback(user_open_id, cached_names, updated_names)
    if updated_names != cached_names:
        conversation_store.update_session_state(
            user_open_id,
            {"knowledge_chat_names": updated_names},
        )
    return chats


async def _list_group_chats_for_user(
    user_open_id: str,
    cached_names: dict[str, str],
    updated_names: dict[str, str],
) -> list[dict[str, Any]]:
    chats: list[dict[str, Any]] = []
    page_token = ""
    page_limit = 3
    while page_limit > 0:
        try:
            items, next_token, has_more = await chat_client.list_chats(
                user_open_id,
                page_size=100,
                page_token=page_token,
            )
        except PermissionError:
            logger.warning("list chats permission denied open_id={}", user_open_id)
            return []
        except Exception:
            logger.exception("list chats failed open_id={}", user_open_id)
            return []
        for item in items:
            chat_id = str(item.get("chat_id") or item.get("id") or "").strip()
            if not chat_id:
                continue
            chat_mode = str(item.get("chat_mode") or "").strip().lower()
            if chat_mode == "p2p":
                continue
            name = str(item.get("name") or item.get("chat_name") or item.get("title") or "").strip()
            if not name:
                i18n_names = item.get("i18n_names") if isinstance(item.get("i18n_names"), dict) else {}
                if isinstance(i18n_names, dict):
                    name = str(
                        i18n_names.get("zh_cn")
                        or i18n_names.get("en_us")
                        or i18n_names.get("ja_jp")
                        or ""
                    ).strip()
            if not name:
                name = cached_names.get(chat_id, "")
            if name:
                updated_names[chat_id] = name
            chats.append({"chat_id": chat_id, "name": name})
        if not has_more or not next_token:
            break
        page_token = next_token
        page_limit -= 1
    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for chat in chats:
        cid = str(chat.get("chat_id") or "").strip()
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        deduped.append(chat)
    return deduped[:100]


async def _load_seen_chats_fallback(
    user_open_id: str,
    cached_names: dict[str, str],
    updated_names: dict[str, str],
) -> list[dict[str, Any]]:
    state = conversation_store.get_session_state(user_open_id)
    chat_ids: list[str] = []
    current_chat_id = str((state.get("current_chat_id") if isinstance(state, dict) else "") or "").strip()
    if current_chat_id:
        chat_ids.append(current_chat_id)
    seen = state.get("seen_chat_ids") if isinstance(state, dict) else None
    if isinstance(seen, list):
        for item in seen:
            cid = str(item or "").strip()
            if cid and cid not in chat_ids:
                chat_ids.append(cid)
    chats: list[dict[str, Any]] = []
    for cid in chat_ids[:50]:
        detail = await _get_chat_detail(user_open_id, cid)
        if not detail:
            continue
        chat_mode = str(detail.get("chat_mode") or "").strip().lower()
        if chat_mode == "p2p":
            continue
        name = _extract_chat_name(detail) or cached_names.get(cid, "")
        if name:
            updated_names[cid] = name
        chats.append({"chat_id": cid, "name": name})
    return chats


async def _get_chat_detail(user_open_id: str, chat_id: str) -> dict[str, Any]:
    uid = str(user_open_id or "").strip()
    cid = str(chat_id or "").strip()
    if not cid or not uid:
        return {}
    try:
        payload = await chat_client.get_chat(uid, cid)
    except PermissionError:
        logger.warning("get chat detail permission denied open_id={} chat_id={}", uid, cid)
        return {}
    except Exception:
        logger.exception("get chat detail failed open_id={} chat_id={}", uid, cid)
        return {}
    if not isinstance(payload, dict):
        logger.warning("get chat detail empty payload open_id={} chat_id={}", uid, cid)
        return {}
    chat_obj = payload.get("chat") if isinstance(payload.get("chat"), dict) else payload
    return chat_obj if isinstance(chat_obj, dict) else {}


def _extract_chat_name(chat_obj: dict[str, Any]) -> str:
    if not isinstance(chat_obj, dict):
        return ""
    name = str(chat_obj.get("name") or chat_obj.get("chat_name") or chat_obj.get("title") or "").strip()
    if name:
        return name
    i18n_names = chat_obj.get("i18n_names") if isinstance(chat_obj.get("i18n_names"), dict) else {}
    if isinstance(i18n_names, dict):
        return str(
            i18n_names.get("zh_cn")
            or i18n_names.get("en_us")
            or i18n_names.get("ja_jp")
            or ""
        ).strip()
    return ""


async def _get_chat_display_name(user_open_id: str, chat_id: str) -> str:
    chat_obj = await _get_chat_detail(user_open_id, chat_id)
    cid = str(chat_id or "").strip()
    uid = str(user_open_id or "").strip()
    if not chat_obj:
        return ""
    name = _extract_chat_name(chat_obj)
    if not name:
        logger.warning("chat payload without name open_id={} chat_id={} payload={}", uid, cid, chat_obj)
    return name


async def _rerun_search_question(
    user_open_id: str,
    question: str,
) -> tuple[str, list[str], dict]:
    sess = conversation_store.get_active_session(user_open_id)
    conversation_context = conversation_store.get_context_text(user_open_id, max_turns=6)
    intent = await detect_intent(question, conversation_context=conversation_context)
    if intent.label != "search_knowledge":
        intent = IntentResult(
            label="search_knowledge",
            keyword=intent.keyword or sess.topic or question,
            keyword_fallback=intent.keyword_fallback,
            person_hint="",
            raw_question=question,
        )
    elif not intent.keyword and sess.topic:
        intent = IntentResult(
            label="search_knowledge",
            keyword=sess.topic,
            keyword_fallback=intent.keyword_fallback,
            person_hint=intent.person_hint,
            raw_question=question,
        )
    elif intent.raw_question != question:
        intent = IntentResult(
            label=intent.label,
            keyword=intent.keyword,
            keyword_fallback=intent.keyword_fallback,
            person_hint=intent.person_hint,
            raw_question=question,
        )
    if intent.keyword:
        conversation_store.set_topic(user_open_id, intent.keyword)
    state = conversation_store.get_session_state(user_open_id)
    current_chat_id = str((state.get("current_chat_id") if isinstance(state, dict) else "") or "").strip()
    answer, sources, entities = await run_search_knowledge(
        user_open_id,
        intent,
        conversation_context=conversation_context,
        current_chat_id=current_chat_id or None,
    )
    conversation_store.update_session_state(
        user_open_id,
        {
            "current_question": question,
            "current_intent": "search_knowledge",
            "current_query": {
                "keyword": intent.search_key,
                "keyword_fallback": intent.keyword_fallback or "",
                "person_hint": "",
            },
            "last_result": {
                "intent": "search_knowledge",
                "question": question,
                "search_key": intent.search_key,
                "keyword_fallback": intent.keyword_fallback or "",
                "answer_summary": str(answer)[:280],
                "sources": [str(s) for s in (sources or [])[:6]],
                "conflict": "存在冲突" in str(answer or ""),
            },
            "last_turn": {"role": "assistant", "content": str(answer)[:240]},
        },
    )
    conversation_store.add_turn(user_open_id, "assistant", str(answer))
    return answer, sources, entities


def _build_external_doc_url(node_token: str, docs_token: str, docs_type: str) -> str:
    base = str(getattr(settings, "feishu_web_base_url", "") or "").strip().rstrip("/") or "https://www.feishu.cn"
    if node_token:
        return f"{base}/wiki/{node_token}"
    path_map = {"docx": "docx", "doc": "docs", "wiki": "wiki"}
    path = path_map.get(str(docs_type or "").strip().lower(), "docx")
    return f"{base}/{path}/{docs_token}" if docs_token else ""


async def _build_external_doc_record(
    user_open_id: str,
    node: dict[str, Any],
    *,
    source_label: str,
) -> dict[str, Any]:
    node_token = str(node.get("node_token") or "").strip()
    docs_token = str(node.get("obj_token") or node_token).strip()
    docs_type = str(node.get("obj_type") or "wiki").strip().lower() or "wiki"
    title = str(node.get("title") or source_label or "未命名Wiki").strip() or "未命名Wiki"
    url = _build_external_doc_url(node_token, docs_token, docs_type)
    raw_content = ""
    raw_status = ""
    if docs_token and docs_type in {"doc", "docx"}:
        raw_content, raw_status = await doc_client.safe_load_content(user_open_id, docs_token)
    return {
        "title": title,
        "url": url,
        "docs_token": docs_token,
        "docs_type": docs_type,
        "node_token": node_token,
        "space_id": str(node.get("space_id") or "").strip(),
        "raw_content": raw_content[:12000],
        "raw_content_error": raw_status,
        "source_label": source_label,
        "has_child": bool(node.get("has_child")),
    }


async def _list_child_wiki_nodes(
    user_open_id: str,
    *,
    space_id: str,
    parent_node_token: str,
    page_limit: int = 2,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token = ""
    remaining = max(1, int(page_limit))
    while remaining > 0:
        nodes, next_token, has_more = await wiki_client.list_space_nodes(
            user_open_id,
            space_id,
            parent_node_token=parent_node_token,
            page_size=50,
            page_token=page_token,
        )
        items.extend(node for node in nodes if isinstance(node, dict))
        if not has_more or not next_token:
            break
        page_token = next_token
        remaining -= 1
    return items


_EXTERNAL_WIKI_MAX_DISCOVERED_NODES = 80
_EXTERNAL_WIKI_MAX_ATTACHED_DOCS = 12


async def _walk_external_wiki_descendants(
    user_open_id: str,
    *,
    space_id: str,
    root_node_token: str,
) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    queue: list[str] = [root_node_token]
    visited: set[str] = {root_node_token}
    while queue and len(discovered) < _EXTERNAL_WIKI_MAX_DISCOVERED_NODES:
        parent_node_token = queue.pop(0)
        nodes = await _list_child_wiki_nodes(
            user_open_id,
            space_id=space_id,
            parent_node_token=parent_node_token,
            page_limit=3,
        )
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_token = str(node.get("node_token") or "").strip()
            if not node_token or node_token in visited:
                continue
            visited.add(node_token)
            discovered.append(node)
            if bool(node.get("has_child")):
                queue.append(node_token)
            if len(discovered) >= _EXTERNAL_WIKI_MAX_DISCOVERED_NODES:
                break
    return discovered


def _score_external_node(node: dict[str, Any]) -> int:
    title = str(node.get("title") or "").strip()
    docs_type = str(node.get("obj_type") or "").strip().lower()
    lowered = title.lower()
    score = 0
    if docs_type in {"doc", "docx"}:
        score += 20
    if any(marker in lowered for marker in ("流程", "规范", "sop", "操作", "步骤", "说明", "工具", "系统", "平台")):
        score += 12
    if any(marker in lowered for marker in ("数据标注", "数据采集", "感知", "运控", "闭环")):
        score += 10
    if bool(node.get("has_child")):
        score += 3
    if title:
        score += min(len(title), 10)
    return score


async def _collect_external_wiki_records(
    user_open_id: str,
    root_node: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    root_space_id = str(root_node.get("space_id") or "").strip()
    root_node_token = str(root_node.get("node_token") or "").strip()
    if not root_space_id or not root_node_token:
        return [], 0
    descendants = await _walk_external_wiki_descendants(
        user_open_id,
        space_id=root_space_id,
        root_node_token=root_node_token,
    )
    deduped_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in sorted(descendants, key=_score_external_node, reverse=True):
        node_token = str(node.get("node_token") or "").strip()
        if not node_token or node_token in seen:
            continue
        seen.add(node_token)
        deduped_nodes.append(node)
        if len(deduped_nodes) >= _EXTERNAL_WIKI_MAX_ATTACHED_DOCS:
            break
    records: list[dict[str, Any]] = []
    for node in deduped_nodes:
        try:
            record = await _build_external_doc_record(
                user_open_id,
                node,
                source_label=f"{_DEFAULT_EXTERNAL_WIKI_LABEL} / 子文档",
            )
        except Exception:
            logger.exception("build external child record failed node={}", node)
            continue
        records.append(record)
    return records, len(descendants)


async def _attach_default_external_wiki(user_open_id: str) -> dict[str, Any]:
    node = await wiki_client.get_node(
        user_open_id,
        _DEFAULT_EXTERNAL_WIKI_NODE_TOKEN,
        obj_type="wiki",
    )
    if not node:
        return {}
    record = await _build_external_doc_record(
        user_open_id,
        node,
        source_label=_DEFAULT_EXTERNAL_WIKI_LABEL,
    )
    child_records, discovered_total = await _collect_external_wiki_records(user_open_id, node)
    state = conversation_store.get_session_state(user_open_id)
    existing_raw = state.get("external_knowledge_docs") if isinstance(state, dict) else None
    merged: list[dict[str, Any]] = [record, *child_records]
    if isinstance(existing_raw, list):
        for item in existing_raw:
            if not isinstance(item, dict):
                continue
            same_node = any(
                str(item.get("node_token") or "").strip() == str(doc.get("node_token") or "").strip()
                for doc in merged
            )
            same_doc = any(
                str(item.get("docs_token") or "").strip() == str(doc.get("docs_token") or "").strip()
                for doc in merged
            )
            if same_node or same_doc:
                continue
            merged.append(item)
    conversation_store.update_session_state(
        user_open_id,
        {"external_knowledge_docs": merged[:_EXTERNAL_WIKI_MAX_ATTACHED_DOCS + 1]},
    )
    logger.info(
        "external wiki attached open_id={} root_title={!r} root_type={} discovered_nodes={} child_docs={} raw_ready={} titles={}",
        user_open_id,
        str(record.get("title") or ""),
        str(record.get("docs_type") or ""),
        discovered_total,
        len(child_records),
        sum(1 for item in merged[:_EXTERNAL_WIKI_MAX_ATTACHED_DOCS + 1] if str(item.get("raw_content") or "").strip()),
        [str(item.get("title") or "").strip() for item in merged[:_EXTERNAL_WIKI_MAX_ATTACHED_DOCS + 1]],
    )
    return record
