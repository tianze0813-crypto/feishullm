import asyncio
import json
import re

from core.intent import IntentResult
from core.intent import _query_has_ascii_and_cjk
from core.intent import _query_is_ascii_only
from core.intent import build_query_candidates
from core.text_summary import summarize_context_sections
from utils.conversation import conversation_store
from utils.logger import get_logger
from feishu_client.bitable import bitable_client
from feishu_client.doc import doc_client
from feishu_client.message import message_client
from feishu_client.search import search_client
from llm.client import llm_client

logger = get_logger()

_MAX_SUMMARY_DOC_PULLS_PER_RUN = 10
_MAX_SUMMARY_DOC_ENTITIES_PER_RUN = 10

_RE_DOCX_TOKEN = re.compile(r"/docx/([a-zA-Z0-9]+)")
_RE_DOC_TOKEN = re.compile(r"/docs/([a-zA-Z0-9]+)")
_RE_WIKI_TOKEN = re.compile(r"/wiki/([a-zA-Z0-9]+)")
_RE_BITABLE_TOKEN = re.compile(r"/base/([a-zA-Z0-9]+)")
_RE_TOKEN_PARAM = re.compile(
    r"(?i)(?:docs_token|doc_token|wiki_token|node_token|app_token|base_token)=([a-z0-9]+)"
)

_TOPIC_SUMMARY_MARKERS = (
    "总结",
    "归纳",
    "梳理",
    "盘点",
    "汇总",
    "主要有什么",
    "有哪些",
    "整体情况",
    "工作流程",
    "业务范围",
    "工作业务",
    "主要任务",
    "工作内容",
    "做什么",
    "主要做什么",
    "主要做哪些事",
    "做啥",
    "干什么",
    "干什么的",
    "干嘛",
    "负责什么",
    "主要负责什么",
    "职责",
    "常用工具",
    "常用软件",
    "常用系统",
    "常用平台",
    "工具清单",
    "工具列表",
    "使用什么工具",
    "需要使用什么工具",
)

_MAX_EXTERNAL_DOC_CONTEXTS_PER_RUN = 5
_MAX_EXTERNAL_QUERY_DOCS = 12


async def run_search_knowledge(
    open_id: str,
    intent: IntentResult,
    conversation_context: str = "",
    *,
    current_chat_id: str | None = None,
) -> tuple[str, list[str], dict]:
    """知识问答流水线（五步 + 短路+补充）：
    1. 知识库 search_wiki
    2. 云文档 search_docs + 拉正文
    3. 多维表格 search_bitable + list_records
    4. 聊天记录 search_messages
    5. LLM 生成 RAG 回答（context 按四源顺序拼接并标注来源）

    四路搜索统一使用 intent.keyword（意图解析阶段已除去疑问词/虚词），避免
    飞书多 token AND 匹配被长问句稀释。LLM 回答用原始 question 保留自然语气。
    """
    sources: list[str] = []

    search_key = intent.search_key
    question = intent.raw_question
    fallback = intent.keyword_fallback.strip() if intent.keyword_fallback else ""
    summary_mode = _is_topic_summary_question(question)
    auto_general_fallback = _should_auto_general_fallback(open_id, intent)
    routing = await _route_search_knowledge(
        question=question,
        search_key=search_key,
        keyword_fallback=fallback,
        conversation_context=conversation_context,
        summary_mode=summary_mode,
    )
    summary_mode = summary_mode or _routing_prefers_summary(routing)
    search_key = _pick_routed_keyword(search_key, routing)
    fallback = _pick_routed_fallback(fallback, routing)
    preserve_entities = _extract_preserve_entities(routing)
    inherited_entities: list[str] = []
    clarify_needed = False
    clarify_reason = ""
    clarify_options = _extract_clarify_options(routing)
    has_team_reference = _has_team_reference(question)
    selected_group_ids = _get_selected_knowledge_chat_ids(open_id) if has_team_reference else []
    selected_group_names = _get_selected_knowledge_chat_names(open_id) if has_team_reference else []
    has_single_selected_group = len(selected_group_ids) == 1 and len(selected_group_names) == 1
    if selected_group_names:
        inherited_entities = _merge_string_candidates(inherited_entities, selected_group_names)
        sources.append(f"已选择群组：{' / '.join(selected_group_names[:3])}")
    if has_team_reference and not has_single_selected_group:
        clarify_needed = True
        if len(selected_group_ids) > 1:
            clarify_reason = "当前选中了多个群组，无法判断“我们组”具体指哪一个组，请先收敛到单一群组或直接说组名。"
        else:
            clarify_reason = "当前问题里的“我们组”指代不够具体，且未选中单一群组，无法确定要检索的组别。"
        if not clarify_options:
            clarify_options = [
                "先选择 1 个目标群组",
                "直接补充具体组名",
                "补充项目名或系统名",
            ]
    elif has_single_selected_group:
        preserve_entities = _merge_string_candidates(selected_group_names, preserve_entities)
    if summary_mode and has_team_reference and not preserve_entities and not clarify_needed:
        if has_single_selected_group:
            inherited_entities = _merge_string_candidates(inherited_entities, selected_group_names)
            preserve_entities = _merge_string_candidates(selected_group_names, preserve_entities)
        else:
            inherited_entities = _inherit_summary_entities(
                open_id,
                question=question,
                conversation_context=conversation_context,
                current_search_key=search_key,
            )
            if inherited_entities:
                preserve_entities = _merge_string_candidates(preserve_entities, inherited_entities)
                sources.append(f"继承最近话题实体：{' / '.join(inherited_entities[:3])}")
            else:
                clarify_needed = True
                clarify_reason = "当前问题只有“我们组”这类泛指，缺少具体组名、项目名或系统名。"
    routed_aliases = _extract_routing_aliases(routing)
    llm_expansions = await _expand_queries_with_llm(
        label="search_knowledge",
        question=question,
        search_key=search_key,
        fallback=fallback,
        conversation_context=conversation_context,
    )
    llm_expansions = _merge_string_candidates(routed_aliases, llm_expansions)
    external_query_terms = _extract_external_query_terms(
        open_id,
        search_key=search_key,
        fallback=fallback,
        preserve_entities=preserve_entities,
    )
    llm_expansions = _merge_string_candidates(external_query_terms, llm_expansions)
    if has_team_reference and preserve_entities:
        llm_expansions = _merge_string_candidates(
            _build_team_reference_queries(
                search_key=search_key,
                fallback=fallback,
                preserve_entities=preserve_entities,
                aliases=llm_expansions,
            ),
            llm_expansions,
        )

    logger.info(
        "search_knowledge start open_id={} question={!r} search_key={!r} fallback={!r} summary_mode={} auto_general_fallback={} routing={} llm_expansions={}",
        open_id,
        question,
        search_key,
        fallback,
        summary_mode,
        auto_general_fallback,
        routing,
        llm_expansions,
    )
    if clarify_needed:
        answer = _build_team_summary_clarify_answer(
            question=question,
            inherited_entities=inherited_entities,
            clarify_reason=clarify_reason,
        )
        return answer, ["需要先补充更具体的组别线索"], {
            "_meta": {
                "label": "search_knowledge",
                "queries": [],
                "hit_query": "",
                "exact_match": False,
                "keyword_fallback": fallback,
                "routing": _routing_meta(routing),
                "summary_applied": False,
                "summary_mode": True,
                "clarify_needed": True,
                "clarify_reason": clarify_reason,
                "clarify_options": clarify_options,
                "inherited_entities": inherited_entities[:5],
                "hits": {"wiki": 0, "docs": 0, "bitable": 0, "messages": 0},
                "visibility": {"docs_no_permission": 0, "docs_unavailable": 0},
            }
        }

    external_query_titles = _rank_external_query_titles(
        _extract_external_query_titles(open_id),
        question=question,
        search_key=search_key,
        fallback=fallback,
    )
    queries = build_query_candidates(
        "search_knowledge",
        search_key,
        fallback,
        extra_candidates=_merge_string_candidates(external_query_titles, llm_expansions),
    )
    if summary_mode:
        queries = _extend_topic_summary_queries(
            question,
            queries,
            preserve_entities=preserve_entities,
        )
        queries = _merge_string_candidates(external_query_titles, queries)
    logger.info(
        "external query titles open_id={} titles={}",
        open_id,
        external_query_titles[:8],
    )
    context_sections: list[str] = []
    entities: dict = {}
    hit_query = ""
    first_hit_query = ""
    used_queries: list[str] = []
    max_summary_hits = _summary_hit_limit(
        question=question,
        search_key=search_key,
        fallback=fallback,
        has_external_titles=bool(external_query_titles),
    )
    logger.info("search_knowledge summary hit limit={}", max_summary_hits)
    logger.info("search_knowledge query candidates={}", queries)
    max_summary_sections = 10
    max_normal_hits = 2
    max_normal_sections = 12
    remaining_doc_budget = _MAX_SUMMARY_DOC_PULLS_PER_RUN if summary_mode else None
    accepted_hits = 0
    for q in queries:
        doc_budget: int | None = None
        wiki_budget: int | None = None
        docs_budget: int | None = None
        if summary_mode and isinstance(remaining_doc_budget, int):
            remaining_query_slots = 1
            remaining_query_slots = max(1, max_summary_hits - len(used_queries))
            doc_budget = max(
                0,
                min(
                    remaining_doc_budget,
                    (remaining_doc_budget + remaining_query_slots - 1) // remaining_query_slots,
                ),
            )
            wiki_budget, docs_budget = _split_doc_pull_budget(doc_budget)
        ctx, ent, consumed_doc_pulls = await _collect_contexts(
            open_id,
            q,
            sources,
            current_chat_id,
            summary_mode=summary_mode,
            doc_pull_budget=doc_budget,
            wiki_doc_pull_budget=wiki_budget,
            docs_doc_pull_budget=docs_budget,
        )
        if summary_mode and isinstance(remaining_doc_budget, int):
            remaining_doc_budget = max(0, remaining_doc_budget - consumed_doc_pulls)
        if not ctx:
            continue
        if not first_hit_query:
            first_hit_query = q
        if summary_mode:
            used_queries.append(q)
            context_sections = _merge_context_sections(context_sections, ctx, max_sections=max_summary_sections)
            entities = _merge_search_entities(
                entities,
                ent,
                max_docs=_MAX_SUMMARY_DOC_ENTITIES_PER_RUN,
            )
            if q != search_key:
                sources.append(f"专题扩展关键词 {q!r} 命中")
            if len(used_queries) >= max_summary_hits or len(context_sections) >= max_summary_sections:
                break
            continue

        used_queries.append(q)
        context_sections = _merge_context_sections(context_sections, ctx, max_sections=max_normal_sections)
        entities = _merge_search_entities(entities, ent)

        if q != search_key:
            sources.append(f"使用扩展关键词 {q!r} 命中")

        if _query_has_ascii_and_cjk(search_key) and _query_is_ascii_only(q):
            continue

        accepted_hits += 1
        if not hit_query:
            hit_query = q
        if accepted_hits >= max_normal_hits or len(context_sections) >= max_normal_sections:
            break
    if not hit_query and first_hit_query:
        hit_query = first_hit_query
        if first_hit_query != search_key:
            sources.append(f"使用扩展关键词 {first_hit_query!r} 命中")
    if summary_mode and used_queries:
        hit_query = hit_query or used_queries[0]
    sources = _merge_string_candidates(sources)

    if summary_mode and isinstance(remaining_doc_budget, int):
        logger.info(
            "search_knowledge summary sources={} context_sections={} used_queries={} doc_pulls_used={} doc_pulls_budget={}",
            sources,
            len(context_sections),
            used_queries,
            _MAX_SUMMARY_DOC_PULLS_PER_RUN - remaining_doc_budget,
            _MAX_SUMMARY_DOC_PULLS_PER_RUN,
        )
    else:
        logger.info(
            "search_knowledge summary sources={} context_sections={} used_queries={}",
            sources,
            len(context_sections),
            used_queries,
        )

    # 四源全零命中时：不再喂空 context 给 LLM 求兜底话术（结果往往是编造/模糊的），
    # 直接给用户一个明确的"没找到"回复，省 1-2s 并避免幻觉。
    if not context_sections:
        logger.info("search_knowledge short-circuit: no context, skip llm answer")
        meta = {
            "_meta": {
                "label": "search_knowledge",
                "queries": queries,
                "hit_query": hit_query or search_key,
                "exact_match": (hit_query or search_key) == search_key,
                "keyword_fallback": fallback,
                "routing": _routing_meta(routing),
                "summary_applied": False,
                "summary_mode": summary_mode,
                "clarify_needed": False,
                "clarify_reason": "",
                "clarify_options": clarify_options,
                "inherited_entities": inherited_entities[:5],
                "hits": {"wiki": 0, "docs": 0, "bitable": 0, "messages": 0},
                "visibility": {"docs_no_permission": 0, "docs_unavailable": 0},
            }
        }
        if auto_general_fallback:
            answer = await llm_client.general_chat(question, conversation_context=conversation_context)
            meta["_meta"]["auto_general_fallback"] = True
            return (
                answer,
                ["内部知识未命中，已自动切换通识问答", "通识问答：DeepSeek 自身知识"],
                meta,
            )
        if summary_mode and isinstance(remaining_doc_budget, int):
            meta["_meta"]["doc_pulls_used"] = _MAX_SUMMARY_DOC_PULLS_PER_RUN - remaining_doc_budget
            meta["_meta"]["doc_pulls_budget"] = _MAX_SUMMARY_DOC_PULLS_PER_RUN
        entities = meta
        return (
            "暂时没有在知识库、云文档、多维表格或聊天记录里找到相关内容。可换个关键词或补充更多上下文再试。",
            ["未命中任何检索源"],
            entities,
        )

    summarized_sections, summary_applied = await summarize_context_sections(question, context_sections)
    context = "\n\n".join(summarized_sections or context_sections)
    if summary_applied:
        sources.append("证据摘要")
    if summary_mode:
        answer = await llm_client.topic_summary(
            question,
            context,
            conversation_context=conversation_context,
            preserve_entities=preserve_entities,
        )
    else:
        answer = await llm_client.answer(
            question,
            context,
            conversation_context=conversation_context,
        )
    # 暂时停用最后一层 LLM 自测收紧，先观察原始 RAG 回答效果。
    # self_check = await _step_search_self_check(
    #     question=question,
    #     routing=routing,
    #     summary_mode=summary_mode,
    #     queries=queries,
    #     entities=entities,
    #     context_sections=summarized_sections or context_sections,
    #     draft_answer=answer,
    # )
    # answer = _apply_search_self_check_result(answer, self_check, summary_mode=summary_mode)
    self_check: dict = {}
    auto_general_fallback_used = False
    if auto_general_fallback and (
        _answer_needs_general_fallback(answer) or _self_check_requests_general_fallback(self_check)
    ):
        answer = await llm_client.general_chat(question, conversation_context=conversation_context)
        sources = [str(s) for s in sources if "通识问答" not in str(s)]
        sources.append("内部资料不足，已自动切换通识问答")
        sources.append("通识问答：DeepSeek 自身知识")
        auto_general_fallback_used = True
    if isinstance(entities, dict):
        docs = entities.get("docs") or []
        entities["_meta"] = {
            "label": "search_knowledge",
            "queries": queries,
            "hit_query": hit_query or search_key,
            "exact_match": (hit_query or search_key) == search_key,
            "keyword_fallback": fallback,
            "routing": _routing_meta(routing),
            "self_check": _self_check_meta(self_check),
            "summary_applied": summary_applied,
            "summary_mode": summary_mode,
            "clarify_needed": _should_clarify_after_self_check(question, summary_mode, self_check),
            "clarify_reason": _clarify_reason_from_self_check(question, summary_mode, self_check),
            "clarify_options": clarify_options,
            "inherited_entities": inherited_entities[:5],
            "used_queries": used_queries[:3],
            "auto_general_fallback": auto_general_fallback_used,
            "hits": entities.get("_hits") or {},
            "visibility": {
                "docs_no_permission": sum(
                    1 for d in docs if isinstance(d, dict) and (d.get("raw_content_error") == "no_permission")
                ),
                "docs_unavailable": sum(
                    1 for d in docs if isinstance(d, dict) and (d.get("raw_content_error") == "unavailable")
                ),
            },
        }
        if summary_mode and isinstance(remaining_doc_budget, int):
            entities["_meta"]["doc_pulls_used"] = _MAX_SUMMARY_DOC_PULLS_PER_RUN - remaining_doc_budget
            entities["_meta"]["doc_pulls_budget"] = _MAX_SUMMARY_DOC_PULLS_PER_RUN
    return answer, sources, entities


async def _route_search_knowledge(
    *,
    question: str,
    search_key: str,
    keyword_fallback: str,
    conversation_context: str,
    summary_mode: bool,
) -> dict:
    if not search_key:
        return {}
    try:
        routed = await llm_client.route_search_knowledge(
            question=question,
            search_key=search_key,
            keyword_fallback=keyword_fallback,
            conversation_context=conversation_context,
            summary_mode=summary_mode,
        )
    except Exception:
        logger.exception("route_search_knowledge failed search_key={!r}", search_key)
        return {}
    return routed if isinstance(routed, dict) else {}


def _pick_routed_keyword(search_key: str, routing: dict) -> str:
    text = str((routing or {}).get("keyword") or "").strip()
    return text or search_key


def _pick_routed_fallback(fallback: str, routing: dict) -> str:
    text = str((routing or {}).get("keyword_fallback") or "").strip()
    return text or fallback


def _extract_routing_aliases(routing: dict) -> list[str]:
    items = (routing or {}).get("aliases")
    if not isinstance(items, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        lowered = text.lower()
        if not text or lowered in seen:
            continue
        seen.add(lowered)
        result.append(text)
        if len(result) >= 5:
            break
    return result


def _extract_preserve_entities(routing: dict) -> list[str]:
    items = (routing or {}).get("preserve_entities")
    if not isinstance(items, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        lowered = text.lower()
        if not text or lowered in seen:
            continue
        seen.add(lowered)
        result.append(text)
        if len(result) >= 5:
            break
    return result


def _extract_clarify_options(routing: dict) -> list[str]:
    items = (routing or {}).get("clarify_options")
    if not isinstance(items, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= 4:
            break
    return result


def _merge_string_candidates(*groups: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            text = str(item or "").strip()
            lowered = text.lower()
            if not text or lowered in seen:
                continue
            seen.add(lowered)
            result.append(text)
    return result


def _build_team_reference_queries(
    *,
    search_key: str,
    fallback: str,
    preserve_entities: list[str],
    aliases: list[str] | None = None,
) -> list[str]:
    entities = [str(item or "").strip() for item in (preserve_entities or []) if str(item or "").strip()]
    base_terms = [search_key, fallback] + [str(item or "").strip() for item in (aliases or [])]
    queries: list[str] = []
    for entity in entities[:3]:
        queries.append(entity)
        for term in base_terms[:6]:
            text = str(term or "").strip()
            if not text or text == entity:
                continue
            queries.append(f"{entity} {text}".strip())
    return _merge_string_candidates(queries)


def _split_doc_pull_budget(total_budget: int | None) -> tuple[int | None, int | None]:
    if total_budget is None:
        return None, None
    total = max(0, int(total_budget))
    if total <= 0:
        return 0, 0
    docs_budget = min(total, max(1, (total * 3 + 4) // 5))
    wiki_budget = max(0, total - docs_budget)
    return wiki_budget, docs_budget


def _get_attached_external_docs(open_id: str) -> list[dict[str, str]]:
    state = conversation_store.get_session_state(open_id)
    raw = state.get("external_knowledge_docs") if isinstance(state, dict) else None
    if not isinstance(raw, list):
        return []
    docs: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        docs.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "docs_token": str(item.get("docs_token") or "").strip(),
                "docs_type": str(item.get("docs_type") or "").strip(),
                "node_token": str(item.get("node_token") or "").strip(),
                "raw_content": str(item.get("raw_content") or "").strip(),
                "raw_content_error": str(item.get("raw_content_error") or "").strip(),
                "source_label": str(item.get("source_label") or "").strip(),
            }
        )
    return docs


def _step_attached_external_docs(
    open_id: str,
    search_key: str,
    sources: list[str],
    *,
    summary_mode: bool = False,
) -> tuple[str, list[dict]]:
    attached = _get_attached_external_docs(open_id)
    if not attached:
        return "", []
    ranked: list[tuple[int, dict[str, str]]] = []
    for item in attached:
        title = str(item.get("title") or "").strip()
        raw_content = str(item.get("raw_content") or "").strip()
        score = max(
            _message_scope_score(raw_content, search_key),
            _message_scope_score(title, search_key),
        )
        if score <= 0 and len(attached) == 1:
            score = 1
        if score > 0:
            ranked.append((score, item))
    if not ranked:
        return "", []
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [item for _, item in ranked[:_MAX_EXTERNAL_DOC_CONTEXTS_PER_RUN]]
    lines: list[str] = []
    entity_docs: list[dict] = []
    for item in chosen:
        title = str(item.get("title") or "未命名外接知识").strip()
        url = str(item.get("url") or "").strip()
        raw_content = str(item.get("raw_content") or "").strip()
        status = str(item.get("raw_content_error") or "").strip()
        label = str(item.get("source_label") or "外接知识").strip()
        url_part = f"（{url}）" if url else ""
        lines.append(f"- 标题：{title}（{label}）{url_part}")
        if raw_content:
            preview = _build_query_centered_preview(raw_content, search_key, 600 if summary_mode else 800)
            lines.append(f"  正文片段：{preview}")
        elif status:
            lines.append(f"  正文片段：({status})")
        entity_docs.append(
            {
                "title": title,
                "url": url,
                "docs_token": str(item.get("docs_token") or item.get("node_token") or "").strip(),
                "docs_type": str(item.get("docs_type") or "wiki").strip(),
                "raw_content_error": status,
                "_external_attached": True,
            }
        )
    if entity_docs:
        sources.append("外接知识库")
    return "\n".join(lines), entity_docs


_EXTERNAL_QUERY_GENERIC_TERMS = {
    "知识库",
    "文档",
    "wiki",
    "默认",
    "飞书",
    "流程",
    "业务",
    "说明",
    "规范",
    "工作",
    "团队",
    "小组",
    "小分队",
    "数据",
}


def _extract_external_query_terms(
    open_id: str,
    *,
    search_key: str,
    fallback: str,
    preserve_entities: list[str],
) -> list[str]:
    attached = _get_attached_external_docs(open_id)
    if not attached:
        return []
    seed_terms = _merge_string_candidates([search_key, fallback], preserve_entities)
    query_is_process = any(marker in (search_key + " " + fallback) for marker in ("流程", "业务", "SOP", "规范"))
    scored: dict[str, int] = {}

    def _push(term: str, score: int) -> None:
        text = str(term or "").strip()
        if not text:
            return
        lowered = text.lower()
        compact = re.sub(r"\s+", "", lowered)
        if len(text) < 2:
            return
        if lowered in {str(x).strip().lower() for x in seed_terms if str(x).strip()}:
            return
        if compact in _EXTERNAL_QUERY_GENERIC_TERMS:
            return
        if text in scored:
            scored[text] = max(scored[text], score)
        else:
            scored[text] = score

    for item in attached[:_MAX_EXTERNAL_QUERY_DOCS]:
        title = str(item.get("title") or "").strip()
        raw = str(item.get("raw_content") or "").strip()
        docs_type = str(item.get("docs_type") or "").strip().lower()
        if title:
            _push(title, 24 if docs_type in {"doc", "docx"} else 18)
            for seg in re.split(r"[|/、，,：:（）()\-\s]+", title):
                _push(seg, 14)
        for match in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,20}|[\u4e00-\u9fff]{2,8}", raw[:1600]):
            bonus = 0
            if query_is_process and any(marker in match for marker in ("流程", "步骤", "标注", "采集", "训练", "评估", "运控", "感知", "回灌")):
                bonus = 8
            _push(match, 6 + bonus)
    ranked = sorted(scored.items(), key=lambda item: (-item[1], len(item[0])))
    terms = [text for text, _ in ranked[:6]]
    if query_is_process:
        expanded: list[str] = []
        for term in terms[:4]:
            expanded.append(term)
            expanded.append(f"{term} 业务流程")
            expanded.append(f"{term} 工作流程")
        return _merge_string_candidates(expanded)
    return _merge_string_candidates(terms)


def _extract_external_query_titles(open_id: str) -> list[str]:
    attached = _get_attached_external_docs(open_id)
    if not attached:
        return []
    titles: list[str] = []
    for item in attached[:_MAX_EXTERNAL_QUERY_DOCS]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        titles.append(title)
        compact = re.sub(r"\s+", " ", title).strip()
        if compact != title:
            titles.append(compact)
    return _merge_string_candidates(titles)


def _rank_external_query_titles(
    titles: list[str],
    *,
    question: str,
    search_key: str,
    fallback: str,
) -> list[str]:
    merged_text = " ".join(
        str(item or "").strip() for item in (question, search_key, fallback) if str(item or "").strip()
    )
    is_tool_query = any(marker in merged_text for marker in ("工具", "软件", "系统", "平台", "应用", "设备"))
    is_process_query = any(marker in merged_text for marker in ("流程", "工作流", "步骤", "规范", "SOP", "手册", "怎么做"))

    def _score(title: str) -> tuple[int, int]:
        text = str(title or "").strip()
        lowered = text.lower()
        score = 0
        if is_tool_query:
            if any(marker in text for marker in ("工具", "平台", "系统", "应用", "软件")):
                score += 30
            if any(marker in text for marker in ("操作手册", "规范")):
                score += 10
        if is_process_query:
            if any(marker in text for marker in ("操作手册", "流程", "规范", "SOP", "步骤")):
                score += 30
            if any(marker in text for marker in ("工具", "平台")):
                score += 8
        if any(marker in text for marker in ("采集", "标注", "采标", "感知", "运控", "闭环")):
            score += 12
        if "知识库" in text:
            score -= 6
        if re.match(r"^\d{2}\-", text):
            score += 4
        return score, -len(lowered)

    ranked = sorted(
        _merge_string_candidates(titles),
        key=lambda item: _score(item),
        reverse=True,
    )
    return ranked


def _summary_hit_limit(
    *,
    question: str,
    search_key: str,
    fallback: str,
    has_external_titles: bool,
) -> int:
    merged_text = " ".join(
        str(item or "").strip() for item in (question, search_key, fallback) if str(item or "").strip()
    )
    if has_external_titles and any(marker in merged_text for marker in ("工具", "软件", "系统", "平台", "应用", "设备")):
        return 5
    if has_external_titles and any(marker in merged_text for marker in ("流程", "工作流", "步骤", "规范", "SOP", "手册", "怎么做")):
        return 5
    return 3


def _routing_meta(routing: dict) -> dict:
    return {
        "keyword": str((routing or {}).get("keyword") or "").strip(),
        "keyword_fallback": str((routing or {}).get("keyword_fallback") or "").strip(),
        "intent_focus": str((routing or {}).get("intent_focus") or "").strip(),
        "preserve_entities": [
            str(item).strip()
            for item in ((routing or {}).get("preserve_entities") or [])
            if str(item or "").strip()
        ][:5],
        "is_ambiguous": _safe_bool((routing or {}).get("is_ambiguous")),
        "clarify_options": [
            str(item).strip()
            for item in ((routing or {}).get("clarify_options") or [])
            if str(item or "").strip()
        ][:4],
        "confidence": _safe_float((routing or {}).get("confidence")),
    }


def _routing_prefers_summary(routing: dict) -> bool:
    return str((routing or {}).get("intent_focus") or "").strip().lower() == "summary"


async def _step_search_self_check(
    *,
    question: str,
    routing: dict,
    summary_mode: bool,
    queries: list[str],
    entities: dict,
    context_sections: list[str],
    draft_answer: str,
) -> dict:
    try:
        result = await llm_client.self_check_search_answer(
            question=question,
            routing_context=json.dumps(_routing_meta(routing), ensure_ascii=False),
            summary_mode=summary_mode,
            queries=json.dumps(queries[:8], ensure_ascii=False),
            hit_summary=json.dumps((entities or {}).get("_hits") or {}, ensure_ascii=False),
            context_excerpt=_build_context_excerpt(context_sections),
            draft_answer=draft_answer,
        )
    except Exception:
        logger.exception("self_check_search_answer failed question={!r}", question)
        return {}
    logger.info("search_knowledge self_check={}", result)
    return result if isinstance(result, dict) else {}


def _build_context_excerpt(context_sections: list[str]) -> str:
    pieces: list[str] = []
    total = 0
    for item in context_sections or []:
        text = str(item or "").strip()
        if not text:
            continue
        clipped = text[:700]
        pieces.append(clipped)
        total += len(clipped)
        if len(pieces) >= 4 or total >= 1800:
            break
    return "\n\n".join(pieces)


def _apply_search_self_check_result(answer: str, self_check: dict, *, summary_mode: bool) -> str:
    if not isinstance(self_check, dict) or not answer:
        return answer
    should_downgrade = _safe_bool(self_check.get("should_downgrade"))
    should_ask_more = _safe_bool(self_check.get("should_ask_more"))
    consistency_ok = _safe_bool(self_check.get("consistency_ok"), default=True)
    if not should_downgrade and not should_ask_more and consistency_ok:
        return answer
    hint = str(self_check.get("answer_hint") or "").strip()
    if summary_mode and _self_check_allows_summary_inference(self_check):
        prefix = "以下归纳基于现有文档判断/推测，供参考："
        if answer.startswith(prefix) or answer.startswith("根据现有文档判断") or answer.startswith("根据现有文档推测"):
            return answer
        return f"{prefix}\n{answer}".strip()
    if _should_clarify_from_self_check(self_check):
        clarification = _build_clarify_guidance_from_self_check(self_check)
        if clarification:
            return clarification
    if summary_mode:
        prefix = "根据现有资料暂无法完整归纳。"
        default_hint = "建议补充更明确的业务文档、职责说明或流程材料后再试。"
    else:
        prefix = "根据现有检索结果，暂无法确定该问题的准确答案。"
        default_hint = "建议换个关键词、补充更多上下文，或切换到通识问答。"
    if answer.startswith(prefix):
        return answer
    guidance = hint or default_hint
    return f"{prefix}\n\n基于现有材料，仅可提供有限说明：\n{answer}\n\n建议：{guidance}".strip()


def _self_check_requests_general_fallback(self_check: dict) -> bool:
    if not isinstance(self_check, dict):
        return False
    return _safe_bool(self_check.get("should_general_fallback"))


def _self_check_allows_summary_inference(self_check: dict) -> bool:
    if not isinstance(self_check, dict):
        return False
    hint = str(self_check.get("answer_hint") or "").strip()
    risk_note = str(self_check.get("risk_note") or "").strip()
    text = f"{hint}\n{risk_note}"
    return "推测" in text or "判断" in text


def _should_clarify_from_self_check(self_check: dict) -> bool:
    if not isinstance(self_check, dict):
        return False
    if not _safe_bool(self_check.get("should_ask_more")):
        return False
    text = f"{str(self_check.get('risk_note') or '').strip()}\n{str(self_check.get('answer_hint') or '').strip()}"
    markers = ("具体组别", "组名", "项目名", "业务领域", "系统名")
    return any(marker in text for marker in markers)


def _build_clarify_guidance_from_self_check(self_check: dict) -> str:
    if not _should_clarify_from_self_check(self_check):
        return ""
    risk_note = str(self_check.get("risk_note") or "").strip()
    hint = str(self_check.get("answer_hint") or "").strip()
    details = hint or risk_note or "当前问题缺少明确的组别线索。"
    return (
        "要更准确地归纳这个团队，需要你先补充更具体的线索。\n\n"
        "建议至少补充其中一项：\n"
        "- 具体组名或部门名\n"
        "- 项目名或业务方向\n"
        "- 你们组常用的系统名，如 数据标注平台、采集平台、内部工具平台\n\n"
        f"原因：{details}"
    ).strip()


def _self_check_meta(self_check: dict) -> dict:
    if not isinstance(self_check, dict):
        return {}
    return {
        "consistency_ok": _safe_bool(self_check.get("consistency_ok"), default=True),
        "evidence_strength": str(self_check.get("evidence_strength") or "").strip(),
        "should_downgrade": _safe_bool(self_check.get("should_downgrade")),
        "should_general_fallback": _safe_bool(self_check.get("should_general_fallback")),
        "should_ask_more": _safe_bool(self_check.get("should_ask_more")),
        "risk_note": str(self_check.get("risk_note") or "").strip(),
    }


def _safe_bool(value: object, default: bool = False) -> bool:
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


def _safe_float(value: object) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def _should_auto_general_fallback(open_id: str, intent: IntentResult) -> bool:
    if intent.label != "search_knowledge":
        return False
    state = conversation_store.get_session_state(open_id)
    if isinstance(state, dict) and bool(state.get("general_chat_fallback_enabled")):
        return True
    merged_text = " ".join(
        str(item or "").strip()
        for item in (
            intent.raw_question,
            intent.search_key,
            intent.keyword_fallback,
        )
        if str(item or "").strip()
    ).lower()
    howto_markers = (
        "怎么安装",
        "如何安装",
        "安装",
        "部署",
        "配置",
        "教程",
        "指南",
        "命令",
        "下载",
        "pip",
        "conda",
        "github",
        "git ",
    )
    if any(marker in merged_text for marker in howto_markers):
        return True
    search_key = str(intent.search_key or "").strip()
    raw_question = str(intent.raw_question or "").strip()
    if (_query_is_ascii_only(search_key) or _query_has_ascii_and_cjk(raw_question)) and any(
        marker in merged_text for marker in ("怎么", "如何", "安装", "部署", "配置", "教程")
    ):
        return True
    return False


def _answer_needs_general_fallback(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return True
    broad_markers = (
        "根据现有检索结果，暂无法确定",
        "根据现有资料，暂无法确定",
        "根据现有资料暂无法确定",
        "根据现有检索结果，无法确定",
        "根据现有资料，无法确定",
        "根据现有资料无法确定",
    )
    if any(marker in text for marker in broad_markers):
        return True
    markers = (
        "根据现有检索结果，暂无法确定该问题的准确答案",
        "根据现有资料暂无法完整归纳",
        "暂时没有在知识库、云文档、多维表格或聊天记录里找到相关内容",
        "没有直接证据",
        "缺少直接证据",
        "暂无直接证据",
        "暂无法确认",
        "无法确认",
        "未明确提及",
        "没有明确提及",
        "资料未说明",
        "文档未说明",
        "文档里没有写明",
        "现有资料不足以",
        "现有检索结果不足以",
    )
    return any(marker in text for marker in markers)


def _build_query_centered_preview(text: str, query: str, max_chars: int) -> str:
    raw = str(text or "").strip()
    if not raw or max_chars <= 0 or len(raw) <= max_chars:
        return raw
    query_text = str(query or "").strip()
    if not query_text:
        return raw[:max_chars] + "…"

    lower_raw = raw.lower()
    lower_query = query_text.lower()
    pos = lower_raw.find(lower_query)
    if pos < 0:
        compact_raw_chars: list[str] = []
        compact_to_raw: list[int] = []
        for idx, ch in enumerate(raw):
            if ch.isspace():
                continue
            compact_raw_chars.append(ch.lower())
            compact_to_raw.append(idx)
        compact_query = "".join(ch.lower() for ch in query_text if not ch.isspace())
        compact_raw = "".join(compact_raw_chars)
        compact_pos = compact_raw.find(compact_query) if compact_query else -1
        if compact_pos >= 0 and compact_pos < len(compact_to_raw):
            pos = compact_to_raw[compact_pos]

    if pos < 0:
        return raw[:max_chars] + "…"

    half = max_chars // 2
    start = max(0, pos - half)
    end = min(len(raw), start + max_chars)
    start = max(0, end - max_chars)
    snippet = raw[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(raw):
        snippet = snippet + "…"
    return snippet


async def _collect_contexts(
    open_id: str,
    search_key: str,
    sources: list[str],
    current_chat_id: str | None,
    *,
    summary_mode: bool = False,
    doc_pull_budget: int | None = None,
    wiki_doc_pull_budget: int | None = None,
    docs_doc_pull_budget: int | None = None,
) -> tuple[list[str], dict, int]:
    """四源并发检索，拼出 context_sections 列表。"""
    selected_chat_ids = _get_selected_knowledge_chat_ids(open_id)
    strict_scope = bool(selected_chat_ids)
    external_ctx, external_docs = _step_attached_external_docs(
        open_id,
        search_key,
        sources,
        summary_mode=summary_mode,
    )

    msg_ctx = ""
    scope: dict[str, set[str]] = {"docs": set(), "wiki": set(), "bitable": set()}
    if strict_scope:
        msg_ctx, scope = await _step_messages_with_scope(
            open_id,
            search_key,
            sources,
            current_chat_id,
            selected_chat_ids=selected_chat_ids,
            summary_mode=summary_mode,
        )
        if any(scope.get(k) for k in ("docs", "wiki", "bitable")):
            sources.append("已按所选群聊限制：知识库/云文档/表格仅引用群聊内出现过的链接")

        bitable_task = asyncio.create_task(
            _step_bitable(
                open_id,
                search_key,
                sources,
                summary_mode=summary_mode,
                allowed_bitable_app_tokens=scope.get("bitable"),
            ),
        )
        wiki_ctx, wiki_docs, wiki_doc_pulls = await _step_wiki(
            open_id,
            search_key,
            sources,
            summary_mode=summary_mode,
            doc_pull_budget=wiki_doc_pull_budget if wiki_doc_pull_budget is not None else doc_pull_budget,
            allowed_wiki_node_ids=scope.get("wiki"),
            allowed_doc_tokens=scope.get("docs"),
        )
        doc_ctx, docs, doc_doc_pulls = await _step_docs(
            open_id,
            search_key,
            sources,
            summary_mode=summary_mode,
            doc_pull_budget=docs_doc_pull_budget if docs_doc_pull_budget is not None else (
                None if doc_pull_budget is None else max(0, doc_pull_budget - wiki_doc_pulls)
            ),
            allowed_doc_tokens=scope.get("docs"),
        )
        bitable_ctx = await bitable_task
    else:
        bitable_task = asyncio.create_task(
            _step_bitable(open_id, search_key, sources, summary_mode=summary_mode),
        )
        msg_task = asyncio.create_task(
            _step_messages(open_id, search_key, sources, current_chat_id, summary_mode=summary_mode),
        )
        wiki_ctx, wiki_docs, wiki_doc_pulls = await _step_wiki(
            open_id,
            search_key,
            sources,
            summary_mode=summary_mode,
            doc_pull_budget=wiki_doc_pull_budget if wiki_doc_pull_budget is not None else doc_pull_budget,
        )
        doc_ctx, docs, doc_doc_pulls = await _step_docs(
            open_id,
            search_key,
            sources,
            summary_mode=summary_mode,
            doc_pull_budget=docs_doc_pull_budget if docs_doc_pull_budget is not None else (
                None if doc_pull_budget is None else max(0, doc_pull_budget - wiki_doc_pulls)
            ),
        )
        bitable_ctx, msg_ctx = await asyncio.gather(bitable_task, msg_task)

    context_sections: list[str] = []
    entities: dict[str, list[dict]] = {"docs": [], "people": []}
    hits: dict[str, int] = {"wiki": 0, "docs": 0, "bitable": 0, "messages": 0}

    if external_ctx:
        context_sections.append("[来源：外接知识]\n" + external_ctx)
    if external_docs:
        hits["docs"] += len(external_docs)
        entities["docs"].extend(external_docs)

    if wiki_ctx:
        context_sections.append("[来源：知识库]\n" + wiki_ctx)
    if wiki_docs:
        hits["wiki"] = len(wiki_docs)
        entities["docs"].extend(wiki_docs)

    if doc_ctx:
        context_sections.append("[来源：云文档]\n" + doc_ctx)
    if docs:
        hits["docs"] = len(docs)
        entities["docs"].extend(docs)

    if bitable_ctx:
        hits["bitable"] = 1
        context_sections.append("[来源：多维表格]\n" + bitable_ctx)

    if msg_ctx:
        hits["messages"] = 1
        context_sections.append("[来源：聊天记录]\n" + msg_ctx)

    docs_list = entities.get("docs") or []
    if isinstance(docs_list, list) and docs_list:
        def _score(d: dict) -> int:
            raw_err = d.get("raw_content_error") or ""
            raw_score = 10 if not raw_err else 0
            url_score = 2 if (d.get("url") or "") else 0
            docs_type_value = d.get("docs_type")
            docs_type = docs_type_value.lower() if isinstance(docs_type_value, str) else ""
            type_score = 3 if docs_type == "docx" else 2 if docs_type == "doc" else 0
            external_score = 12 if d.get("_external_attached") else 0
            return raw_score + url_score + type_score + external_score

        sortable = [d for d in docs_list if isinstance(d, dict)]
        sortable.sort(key=_score, reverse=True)
        if summary_mode:
            entities["docs"] = sortable[:_MAX_SUMMARY_DOC_ENTITIES_PER_RUN]
        else:
            entities["docs"] = sortable

    entities["_hits"] = hits
    return context_sections, entities, wiki_doc_pulls + doc_doc_pulls


async def _step_wiki(
    open_id: str,
    search_key: str,
    sources: list[str],
    *,
    summary_mode: bool = False,
    doc_pull_budget: int | None = None,
    allowed_wiki_node_ids: set[str] | None = None,
    allowed_doc_tokens: set[str] | None = None,
) -> tuple[str, list[dict], int]:
    if not search_key:
        return "", [], 0
    if doc_pull_budget is not None and doc_pull_budget <= 0:
        logger.info("skip wiki/doc pull for query={!r}: doc budget exhausted", search_key)
        return "", [], 0
    if allowed_wiki_node_ids is not None and not allowed_wiki_node_ids:
        return "", [], 0
    if allowed_doc_tokens is not None and not allowed_doc_tokens and allowed_wiki_node_ids is None:
        return "", [], 0
    try:
        nodes = await search_client.search_wiki(open_id, search_key, page_size=8 if summary_mode else 5)
    except PermissionError:
        raise
    except Exception:
        logger.exception("search wiki failed")
        return "", [], 0
    if not nodes:
        return "", [], 0
    if allowed_wiki_node_ids is not None or allowed_doc_tokens is not None:
        filtered: list[dict] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("node_id") or "").strip()
            obj_token = str(node.get("obj_token") or "").strip()
            ok = False
            if allowed_wiki_node_ids is not None and node_id and node_id in allowed_wiki_node_ids:
                ok = True
            if allowed_doc_tokens is not None and obj_token and obj_token in allowed_doc_tokens:
                ok = True
            if ok:
                filtered.append(node)
        nodes = filtered
        if not nodes:
            return "", [], 0
    sources.append("知识库检索")

    # wiki 节点里 obj_type in {docx, doc} 的是实际文档，可以按 obj_token 拉正文；
    # 其他类型（sheet/bitable/mindnote 等）没有 raw_content API，保留 title+url。
    docx_targets: list[tuple[int, str]] = []  # (节点索引, obj_token)
    limit = 8 if summary_mode else 5
    if doc_pull_budget is not None:
        limit = min(limit, max(0, int(doc_pull_budget)))
    for idx, node in enumerate(nodes[:limit]):
        obj_type_value = node.get("obj_type")
        obj_type = obj_type_value.lower() if isinstance(obj_type_value, str) else ""
        obj_token = node.get("obj_token")
        if (not obj_type or obj_type in ("docx", "doc")) and isinstance(obj_token, str) and obj_token:
            docx_targets.append((idx, obj_token))

    raw_map: dict[int, str] = {}
    raw_status: dict[int, str] = {}
    doc_pull_count = len(docx_targets)
    if docx_targets:
        try:
            sem = asyncio.Semaphore(2)

            async def _fetch(tok: str) -> tuple[str, str]:
                async with sem:
                    return await doc_client.safe_load_content(open_id, tok)

            raws = await asyncio.wait_for(
                asyncio.gather(
                    *[_fetch(tok) for _, tok in docx_targets],
                    return_exceptions=True,
                ),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning("wiki enrich docx raw_content timeout, skip")
            raws = []
        except PermissionError:
            raise
        for (idx, _), result in zip(docx_targets, raws):
            if isinstance(result, PermissionError):
                raise result
            if isinstance(result, Exception):
                logger.warning("wiki docx raw_content failed: {}", result)
                continue
            if isinstance(result, tuple) and len(result) == 2:
                content, status = result
                if status:
                    raw_status[idx] = status
                if isinstance(content, str) and content:
                    raw_map[idx] = content
                    raw_status[idx] = ""
                continue
            if isinstance(result, str) and result:
                raw_map[idx] = result

    lines: list[str] = []
    entity_docs: list[dict] = []
    for idx, node in enumerate(nodes[:limit]):
        title = node.get("title") or "未命名节点"
        url = node.get("url") or ""
        extra = f"（{url}）" if url else ""
        lines.append(f"- {title}{extra}")
        raw = raw_map.get(idx, "")
        if raw:
            preview = _build_query_centered_preview(raw, search_key, 600)
            lines.append(f"  正文片段：{preview}")
        obj_token = node.get("obj_token")
        if isinstance(obj_token, str) and obj_token:
            entity_docs.append(
                {
                    "title": title,
                    "url": url or "",
                    "docs_token": obj_token,
                    "docs_type": node.get("obj_type") or "",
                    "raw_content_error": raw_status.get(idx, ""),
                }
            )
    return "\n".join(lines), entity_docs, doc_pull_count


async def _step_docs(
    open_id: str,
    search_key: str,
    sources: list[str],
    *,
    summary_mode: bool = False,
    doc_pull_budget: int | None = None,
    allowed_doc_tokens: set[str] | None = None,
) -> tuple[str, list[dict], int]:
    if not search_key:
        return "", [], 0
    if doc_pull_budget is not None and doc_pull_budget <= 0:
        logger.info("skip docs pull for query={!r}: doc budget exhausted", search_key)
        return "", [], 0
    if allowed_doc_tokens is not None and not allowed_doc_tokens:
        return "", [], 0
    try:
        docs = await search_client.search_docs(
            open_id,
            search_key,
            page_size=6 if summary_mode else 3,
            docs_types=["doc", "docx"],
        )
    except PermissionError:
        raise
    except Exception:
        logger.exception("search docs failed")
        return "", [], 0
    if not docs:
        return "", [], 0
    if allowed_doc_tokens is None:
        sources.append("文档搜索")
    else:
        sources.append("文档搜索（按群聊范围）")
    doc_items = [d for d in docs if isinstance(d, dict)]
    if allowed_doc_tokens is not None:
        doc_items = [
            d
            for d in doc_items
            if str(_extract_doc_token(d) or "").strip() in allowed_doc_tokens
        ]
        if not doc_items:
            return "", [], 0
    targets: list[tuple[int, str]] = []
    limit = 6 if summary_mode else 5
    if doc_pull_budget is not None:
        limit = min(limit, max(0, int(doc_pull_budget)))
    for idx, item in enumerate(doc_items[:limit]):
        docs_type = (item.get("docs_type") or "").lower()
        token = _extract_doc_token(item) or ""
        if token and docs_type in ("", "doc", "docx"):
            targets.append((idx, token))

    raw_map: dict[int, str] = {}
    raw_status: dict[int, str] = {}
    doc_pull_count = len(targets)
    if targets:
        try:
            sem = asyncio.Semaphore(2)

            async def _fetch(tok: str) -> tuple[str, str]:
                async with sem:
                    return await doc_client.safe_load_content(open_id, tok)

            raws = await asyncio.wait_for(
                asyncio.gather(
                    *[_fetch(tok) for _, tok in targets],
                    return_exceptions=True,
                ),
                timeout=4.0,
            )
        except asyncio.TimeoutError:
            logger.warning("docs enrich raw_content timeout, skip")
            raws = []
        except PermissionError:
            raise
        for (idx, _), result in zip(targets, raws):
            if isinstance(result, PermissionError):
                raise result
            if isinstance(result, Exception):
                logger.warning("docs raw_content failed: {}", result)
                raw_status[idx] = "unavailable"
                continue
            if isinstance(result, tuple) and len(result) == 2:
                content, status = result
                if status:
                    raw_status[idx] = status
                if isinstance(content, str) and content:
                    raw_map[idx] = content
                    raw_status[idx] = ""
                continue
            if isinstance(result, str) and result:
                raw_map[idx] = result

    lines: list[str] = []
    for idx, item in enumerate(doc_items[:limit]):
        title = item.get("title") or item.get("name") or "未命名文档"
        docs_type = (item.get("docs_type") or "").lower()
        url = item.get("url") or ""
        url_part = f"（{url}）" if url else ""
        lines.append(f"- 标题：{title}（类型：{docs_type or 'doc'}）{url_part}")
        raw = raw_map.get(idx, "")
        if raw:
            preview = _build_query_centered_preview(raw, search_key, 600)
            lines.append(f"  正文片段：{preview}")
        status = raw_status.get(idx, "")
        if status == "no_permission":
            lines.append("  正文片段：(无权限读取正文)")
    entity_docs: list[dict] = []
    for idx, item in enumerate(doc_items[:limit]):
        entity_docs.append(
            {
                "title": item.get("title") or item.get("name") or "",
                "url": item.get("url") or "",
                "docs_token": _extract_doc_token(item) or "",
                "docs_type": item.get("docs_type") or "",
                "raw_content_error": raw_status.get(idx, ""),
            }
        )
    return "\n".join(lines), entity_docs, doc_pull_count



async def _step_bitable(
    open_id: str,
    search_key: str,
    sources: list[str],
    *,
    summary_mode: bool = False,
    allowed_bitable_app_tokens: set[str] | None = None,
) -> str:
    if not search_key:
        return ""
    if allowed_bitable_app_tokens is not None and not allowed_bitable_app_tokens:
        return ""
    try:
        bitable_items = await search_client.search_bitable(open_id, search_key, page_size=5 if summary_mode else 3)
        if allowed_bitable_app_tokens is not None:
            bitable_items = [
                b
                for b in (bitable_items or [])
                if isinstance(b, dict)
                and str(b.get("app_token") or b.get("docs_token") or "").strip()
                in allowed_bitable_app_tokens
            ]
        bitable_context = await _load_bitable_context(
            open_id, bitable_items, search_key, summary_mode=summary_mode
        )
    except PermissionError:
        raise
    except Exception:
        logger.exception("search bitable records failed")
        return ""
    if not bitable_context:
        return ""
    sources.append("多维表格检索")
    return "\n".join(bitable_context)


async def _step_messages(
    open_id: str,
    search_key: str,
    sources: list[str],
    current_chat_id: str | None,
    *,
    summary_mode: bool = False,
) -> str:
    if not search_key:
        return ""
    state = conversation_store.get_session_state(open_id)
    selected_chat_ids_raw = state.get("knowledge_chat_ids") if isinstance(state, dict) else None
    selected_chat_ids: list[str] = []
    if isinstance(selected_chat_ids_raw, list):
        for item in selected_chat_ids_raw:
            cid = str(item or "").strip()
            if cid and cid not in selected_chat_ids:
                selected_chat_ids.append(cid)
    try:
        # 沿用全局 include_p2p_message_search 开关，不强制 group_only。
        msg_hits = await search_client.search_messages(open_id, search_key, page_size=30 if summary_mode else 20)
    except PermissionError:
        raise
    except Exception:
        logger.exception("search messages failed")
        return ""
    if not msg_hits:
        return ""

    # 对比 find_person：知识问答同样需要"真正的文本内容"才能让 LLM 引用。
    # 只拿 message_id 列表塞给 LLM 它只能说"我看到有 5 条但不知道内容"，毫无价值。
    try:
        enriched = await message_client.fetch_messages_text(open_id, msg_hits, limit=20 if summary_mode else 15)
    except PermissionError:
        raise
    except Exception:
        logger.exception("fetch knowledge message content failed")
        enriched = []

    lines: list[str] = []
    if selected_chat_ids:
        filtered = [m for m in (enriched or []) if str(m.get("chat_id") or "") in selected_chat_ids]
    else:
        filtered = [
            m
            for m in (enriched or [])
            if not current_chat_id or str(m.get("chat_id") or "") != str(current_chat_id)
        ]
    filtered = _rank_message_records(filtered, search_key)
    max_lines = 8 if summary_mode else 5
    for idx, msg in enumerate(filtered[:max_lines], start=1):
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        # 单条限长避免挤占 LLM context；知识问答一般不关心 sender，不展示。
        preview = text if len(text) <= 300 else text[:300] + "…"
        lines.append(f"{idx}. {preview}")
    if not lines:
        return ""
    sources.append("聊天记录命中（已选群聊）" if selected_chat_ids else "聊天记录命中")
    return "\n".join(lines)


def _get_selected_knowledge_chat_ids(open_id: str) -> list[str]:
    state = conversation_store.get_session_state(open_id)
    raw = state.get("knowledge_chat_ids") if isinstance(state, dict) else None
    selected: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            cid = str(item or "").strip()
            if cid and cid not in selected:
                selected.append(cid)
    return selected


def _get_selected_knowledge_chat_names(open_id: str) -> list[str]:
    state = conversation_store.get_session_state(open_id)
    selected_ids = _get_selected_knowledge_chat_ids(open_id)
    name_map = state.get("knowledge_chat_names") if isinstance(state, dict) else None
    cached_names: dict[str, str] = {}
    if isinstance(name_map, dict):
        for k, v in name_map.items():
            cid = str(k or "").strip()
            nm = str(v or "").strip()
            if cid and nm:
                cached_names[cid] = nm
    names: list[str] = []
    for cid in selected_ids:
        nm = str(cached_names.get(cid, "") or "").strip()
        if nm and nm not in names:
            names.append(nm)
    return names


_SCOPE_GENERIC_QUERY_TERMS = {
    "sop",
    "faq",
    "工作",
    "操作",
    "业务",
    "流程",
    "规范",
    "说明",
    "工具",
    "系统",
    "平台",
    "应用",
    "常用",
}


def _scope_query_terms(search_key: str) -> list[str]:
    text = str(search_key or "").strip()
    if not text:
        return []
    parts: list[str] = []
    if text:
        parts.append(text)
    for segment in re.split(r"[\s/,_\-|]+", text):
        seg = str(segment or "").strip()
        if not seg:
            continue
        parts.append(seg)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in parts:
        normalized = str(item or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(str(item).strip())
    return deduped


def _is_scope_seed_query(search_key: str) -> bool:
    terms = _scope_query_terms(search_key)
    if not terms:
        return False
    significant = [
        term
        for term in terms
        if len(term.strip()) >= 3 and term.strip().lower() not in _SCOPE_GENERIC_QUERY_TERMS
    ]
    return bool(significant or len(str(search_key or "").strip()) >= 6)


_WEAK_WELCOME_MARKERS = (
    "欢迎",
    "加入",
    "新同学",
    "入群",
    "加入群聊",
)


def _is_weak_welcome_message(message_text: str) -> bool:
    text = str(message_text or "").strip()
    if not text:
        return False
    if "欢迎" in text and any(marker in text for marker in ("加入", "入群", "新同学")):
        return True
    lowered = text.lower()
    return "welcome" in lowered and "join" in lowered


def _message_scope_score(message_text: str, search_key: str) -> int:
    text = str(message_text or "").strip()
    if not text:
        return 0
    lowered = text.lower()
    search_text = str(search_key or "").strip().lower()
    if search_text and search_text in lowered:
        return 3
    matched_terms = 0
    for term in _scope_query_terms(search_key):
        normalized = term.strip().lower()
        if len(normalized) < 2 or normalized in _SCOPE_GENERIC_QUERY_TERMS:
            continue
        if normalized in lowered:
            matched_terms += 1
    score = 0
    if matched_terms >= 2:
        score = 2
    elif matched_terms == 1 and any(marker in lowered for marker in ("文档", "wiki", "sop", "规范", "流程", "系统", "平台", "工具")):
        score = 2
    else:
        score = matched_terms
    if _is_weak_welcome_message(text):
        score = max(0, score - 2)
    return score


def _rank_message_records(messages: list[dict], search_key: str) -> list[dict]:
    scored: list[tuple[int, int, dict]] = []
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        score = _message_scope_score(text, search_key)
        if search_key and str(search_key).strip().lower() in text.lower():
            score += 2
        if any(marker in text for marker in ("流程", "步骤", "规范", "SOP", "工具", "系统", "平台", "操作")):
            score += 2
        if msg.get("links"):
            score += 1
        if _is_weak_welcome_message(text):
            score -= 3
        scored.append((score, idx, msg))
    if not scored:
        return []
    has_strong = any(score >= 2 and not _is_weak_welcome_message(str(msg.get("text") or "")) for score, _, msg in scored)
    if has_strong:
        scored = [
            (score, idx, msg)
            for score, idx, msg in scored
            if not _is_weak_welcome_message(str(msg.get("text") or "")) or score >= 2
        ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [msg for _, _, msg in scored]


def _extract_scope_from_links(links: list[str]) -> dict[str, set[str]]:
    docs: set[str] = set()
    wiki: set[str] = set()
    bitable: set[str] = set()
    for link in links or []:
        u = str(link or "").strip()
        if not u:
            continue
        for token in _RE_DOCX_TOKEN.findall(u):
            if token:
                docs.add(token)
        for token in _RE_DOC_TOKEN.findall(u):
            if token:
                docs.add(token)
        for token in _RE_WIKI_TOKEN.findall(u):
            if token:
                wiki.add(token)
        for token in _RE_BITABLE_TOKEN.findall(u):
            if token:
                bitable.add(token)
        for token in _RE_TOKEN_PARAM.findall(u):
            t = str(token or "").strip()
            if not t:
                continue
            if t.startswith("bas"):
                bitable.add(t)
            elif t.startswith("wik"):
                wiki.add(t)
            else:
                docs.add(t)
    return {"docs": docs, "wiki": wiki, "bitable": bitable}


async def _step_messages_with_scope(
    open_id: str,
    search_key: str,
    sources: list[str],
    current_chat_id: str | None,
    *,
    selected_chat_ids: list[str],
    summary_mode: bool = False,
) -> tuple[str, dict[str, set[str]]]:
    if not search_key:
        return "", {"docs": set(), "wiki": set(), "bitable": set()}
    selected_set = {str(x or "").strip() for x in (selected_chat_ids or []) if str(x or "").strip()}
    if not selected_set:
        return "", {"docs": set(), "wiki": set(), "bitable": set()}
    try:
        # 飞书 search/v2/message 的 page_size 过大时会直接返回 invalid param，
        # 这里控制在稳定可用的小页大小，后续仍由 search_messages 内部翻页聚合。
        msg_hits = await search_client.search_messages(open_id, search_key, page_size=30 if summary_mode else 20)
    except PermissionError:
        raise
    except Exception:
        logger.exception("search messages failed")
        return "", {"docs": set(), "wiki": set(), "bitable": set()}
    if not msg_hits:
        return "", {"docs": set(), "wiki": set(), "bitable": set()}
    try:
        enriched = await message_client.fetch_messages_text(open_id, msg_hits, limit=40 if summary_mode else 30)
    except PermissionError:
        raise
    except Exception:
        logger.exception("fetch knowledge message content failed")
        enriched = []
    filtered = [m for m in (enriched or []) if str(m.get("chat_id") or "").strip() in selected_set]
    filtered = _rank_message_records(filtered, search_key)
    scope_links: list[str] = []
    if _is_scope_seed_query(search_key):
        scope_messages = [
            m
            for m in filtered[:20]
            if _message_scope_score(str(m.get("text") or ""), search_key) >= 2
        ]
        for m in scope_messages:
            raw_links = m.get("links")
            if isinstance(raw_links, list):
                for u in raw_links:
                    s = str(u or "").strip()
                    if s and s not in scope_links:
                        scope_links.append(s)
            text = str(m.get("text") or "")
            for token in _RE_DOCX_TOKEN.findall(text):
                scope_links.append(f"/docx/{token}")
            for token in _RE_DOC_TOKEN.findall(text):
                scope_links.append(f"/docs/{token}")
            for token in _RE_WIKI_TOKEN.findall(text):
                scope_links.append(f"/wiki/{token}")
            for token in _RE_BITABLE_TOKEN.findall(text):
                scope_links.append(f"/base/{token}")
            for token in _RE_TOKEN_PARAM.findall(text):
                scope_links.append(f"docs_token={token}")
    scope = _extract_scope_from_links(scope_links)
    lines: list[str] = []
    if selected_set:
        filtered_for_view = filtered
    else:
        filtered_for_view = [
            m
            for m in (enriched or [])
            if not current_chat_id or str(m.get("chat_id") or "") != str(current_chat_id)
        ]
    max_lines = 8 if summary_mode else 5
    for idx, msg in enumerate(filtered_for_view[:max_lines], start=1):
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        preview = text if len(text) <= 300 else text[:300] + "…"
        lines.append(f"{idx}. {preview}")
    if not lines:
        return "", scope
    sources.append("聊天记录命中（已选群聊）")
    return "\n".join(lines), scope


def _extract_doc_token(item: dict) -> str | None:
    # 新版文档搜索返回 docs_token；兼容老格式 obj_token/document_id/doc_token/token。
    for key in ("docs_token", "obj_token", "document_id", "doc_token", "token"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("doc", "document", "item"):
        nested = item.get(key)
        if isinstance(nested, dict):
            token = _extract_doc_token(nested)
            if token:
                return token
    return None
def _flatten_field_value(value: object) -> str:
    """把多维表格里任意字段值拍成可搜索的纯字符串。

    字段类型繁多：string / number / 对象（含 text / name / value）/ 数组。
    不需要精确还原，只要能做 substring 匹配即可。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(_flatten_field_value(v) for v in value)
    if isinstance(value, dict):
        # 常见富文本/链接单元格：{"text": "...", "link": "..."} 或 {"name": "xxx"}
        preferred_keys = ("text", "name", "value", "en_name")
        for k in preferred_keys:
            if k in value:
                sub = _flatten_field_value(value[k])
                if sub:
                    return sub
        return " ".join(_flatten_field_value(v) for v in value.values())
    return str(value)


def _record_matches_keyword(record: dict, keyword_lower: str) -> bool:
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        return False
    for v in fields.values():
        if keyword_lower in _flatten_field_value(v).lower():
            return True
    return False


def _record_to_line(record: dict, table_name: str) -> str:
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        return ""
    kv_parts: list[str] = []
    for k, v in list(fields.items())[:8]:
        flat = _flatten_field_value(v)
        if flat:
            # 避免单字段过长把 LLM context 撑爆。
            if len(flat) > 120:
                flat = flat[:120] + "…"
            kv_parts.append(f"{k}:{flat}")
    if not kv_parts:
        return ""
    prefix = f"[{table_name}] " if table_name else ""
    return prefix + " | ".join(kv_parts)


async def _load_bitable_context(
    open_id: str, bitable_items: list[dict], search_key: str, *, summary_mode: bool = False
) -> list[str]:
    """对每个命中的多维表格：遍历所有表 -> search_records -> 本地 substring 过滤。

    不再像旧版那样"只看第一张表的前 5 条记录"，避免目标行在表后部或不在
    第一张表时完全漏掉；同时把无关表的记录也当命中塞给 LLM 的"噪声命中"问题解决。
    """
    if not search_key:
        return []
    keyword_lower = search_key.lower()
    contexts: list[str] = []
    visited: set[tuple[str, str]] = set()
    hits_total = 0
    # 控制单轮总开销：多个 bitable * 多张表 * 每张表 search 一次，上限 ~5 张表够用。
    max_tables_to_scan = 8 if summary_mode else 5
    max_contexts = 8 if summary_mode else 5

    for item in bitable_items:
        app_token, hinted_table_id = _extract_bitable_tokens(item)
        if not app_token:
            continue

        # 先把这个 bitable 的所有表拿出来——之前只拿第 1 张导致其他表被漏。
        try:
            tables = await bitable_client.list_tables(open_id, app_token, page_size=20)
        except PermissionError:
            raise
        except Exception:
            logger.warning("bitable list_tables failed app_token={}", app_token)
            continue

        # 如果搜索命中里已经指定了 table_id，优先扫它；剩下的作为补充。
        table_dicts = [t for t in tables if isinstance(t, dict)]
        if hinted_table_id:
            table_dicts.sort(
                key=lambda t: 0 if t.get("table_id") == hinted_table_id else 1
            )

        for table in table_dicts:
            if max_tables_to_scan <= 0:
                break
            table_id = table.get("table_id")
            if not isinstance(table_id, str) or not table_id:
                continue
            key = (app_token, table_id)
            if key in visited:
                continue
            visited.add(key)
            max_tables_to_scan -= 1

            table_name = str(table.get("name") or table_id)
            try:
                records = await bitable_client.search_records(
                    open_id=open_id,
                    app_token=app_token,
                    table_id=table_id,
                    page_size=80 if summary_mode else 50,
                )
            except PermissionError:
                raise
            except Exception:
                logger.warning(
                    "bitable search_records failed app_token={} table={}",
                    app_token,
                    table_id,
                )
                continue

            table_hits = 0
            for record in records:
                if not isinstance(record, dict):
                    continue
                if not _record_matches_keyword(record, keyword_lower):
                    continue
                line = _record_to_line(record, table_name)
                if line:
                    contexts.append(line)
                    table_hits += 1
                    if len(contexts) >= max_contexts:
                        break
            hits_total += table_hits
            if len(contexts) >= max_contexts:
                break
        if len(contexts) >= max_contexts:
            break

    logger.info(
        "bitable enrich keyword={!r} hits={} contexts={}",
        search_key,
        hits_total,
        len(contexts),
    )
    return contexts


def _is_topic_summary_question(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return False
    return any(marker in q for marker in _TOPIC_SUMMARY_MARKERS)


def _has_team_reference(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return False
    return any(marker in q for marker in ("我们组", "本组", "我们团队", "本团队", "我们部门", "本部门"))


def _inherit_summary_entities(
    open_id: str,
    *,
    question: str,
    conversation_context: str,
    current_search_key: str,
) -> list[str]:
    state = conversation_store.get_session_state(open_id)
    candidates: list[str] = []
    if isinstance(state, dict):
        topic = str(state.get("topic") or "").strip()
        if topic:
            candidates.append(topic)
        current_query = state.get("current_query") if isinstance(state.get("current_query"), dict) else {}
        current_keyword = str(current_query.get("keyword") or "").strip()
        if current_keyword:
            candidates.append(current_keyword)
        last_result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
        last_search_key = str(last_result.get("search_key") or "").strip()
        if last_search_key:
            candidates.append(last_search_key)
    candidates.extend(_extract_recent_entities_from_text(conversation_context))
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item or "").strip()
        lowered = text.lower()
        if (
            not text
            or lowered in seen
            or _is_generic_summary_term(text)
            or text == str(current_search_key or "").strip()
            or text == str(question or "").strip()
        ):
            continue
        seen.add(lowered)
        result.append(text)
        if len(result) >= 5:
            break
    return result


def _extract_recent_entities_from_text(text: str) -> list[str]:
    import re

    if not text:
        return []
    candidates: list[str] = []
    for match in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{1,15}\b", text):
        token = str(match or "").strip()
        if token and any(ch.isupper() for ch in token):
            candidates.append(token)
    for phrase in ("数据标注", "标注平台", "机器人", "感知", "训练集", "SOP"):
        if phrase in text:
            candidates.append(phrase)
    return candidates


def _is_generic_summary_term(text: str) -> bool:
    normalized = str(text or "").strip().replace(" ", "")
    if not normalized:
        return True
    generic_terms = {
        "主要任务",
        "职责分工",
        "工作内容",
        "业务范围",
        "核心业务",
        "工作流程",
        "流程规范",
        "核心任务",
        "职能",
        "做什么",
        "做啥",
        "干什么",
        "干什么的",
        "负责什么",
        "职责",
        "常用工具",
        "常用软件",
        "常用系统",
        "常用平台",
        "工具清单",
        "工具列表",
        "我们组",
        "本组",
        "我们团队",
        "本团队",
        "我们部门",
        "本部门",
    }
    return normalized in generic_terms


def _build_team_summary_clarify_answer(
    *,
    question: str,
    inherited_entities: list[str],
    clarify_reason: str,
) -> str:
    hint = ""
    if inherited_entities:
        hint = f"（我从最近上下文里识别到：{' / '.join(inherited_entities[:3])}）"
    reason = str(clarify_reason or "").strip()
    reason_part = f"\n\n原因：{reason}" if reason else ""
    return (
        "当前问题里的“我们组”指代不够具体，无法确定要检索的组别范围。"
        f"{hint}\n\n"
        "可在下方选择群组尝试。"
        f"{reason_part}"
    ).strip()


def _should_clarify_after_self_check(question: str, summary_mode: bool, self_check: dict) -> bool:
    if not summary_mode or not _has_team_reference(question):
        return False
    return _should_clarify_from_self_check(self_check)


def _clarify_reason_from_self_check(question: str, summary_mode: bool, self_check: dict) -> str:
    if not _should_clarify_after_self_check(question, summary_mode, self_check):
        return ""
    return str(self_check.get("risk_note") or self_check.get("answer_hint") or "").strip()


def _extend_topic_summary_queries(
    question: str,
    queries: list[str],
    *,
    preserve_entities: list[str] | None = None,
) -> list[str]:
    q = (question or "").strip()
    if not q:
        return queries

    expanded: list[str] = []
    has_team_ref = any(marker in q for marker in ("我们组", "本组", "我们团队", "本团队", "我们部门", "本部门"))
    if has_team_ref:
        if any(marker in q for marker in ("任务", "做什么", "做啥", "干嘛", "干什么", "干什么的", "职责", "负责什么", "工作内容")):
            expanded.extend(["主要任务", "职责分工", "工作内容", "核心业务", "业务方向", "业务流程", "工作流程"])
        if any(marker in q for marker in ("流程", "怎么做", "怎么走", "工作流", "SOP")):
            expanded.extend(["工作流程", "流程规范", "SOP"])
        if any(marker in q for marker in ("业务", "业务范围", "业务线")):
            expanded.extend(["业务范围", "核心业务", "业务方向", "工作内容"])
        if any(marker in q for marker in ("工具", "软件", "系统", "平台", "应用", "使用什么", "需要使用什么")):
            expanded.extend(
                [
                    "常用工具",
                    "常用软件",
                    "常用系统",
                    "常用平台",
                    "常用应用",
                    "工具清单",
                    "工具列表",
                    "使用平台",
                    "操作平台",
                    "系统入口",
                ]
            )
        if not expanded:
            expanded.extend(["主要任务", "职责分工", "工作流程", "业务范围", "常用工具", "常用系统"])

    ordered = list(preserve_entities or []) + list(queries[:2]) + expanded + list(queries[2:])
    deduped: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        text = str(item or "").strip()
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        deduped.append(text)
        if len(deduped) >= 10:
            break
    return deduped


async def _expand_queries_with_llm(
    *,
    label: str,
    question: str,
    search_key: str,
    fallback: str,
    conversation_context: str,
) -> list[str]:
    if not search_key:
        return []
    try:
        return await llm_client.expand_queries(
            intent=label,
            question=question,
            keyword=search_key,
            keyword_fallback=fallback,
            conversation_context=conversation_context,
        )
    except Exception:
        logger.exception("expand_queries failed label={} search_key={!r}", label, search_key)
        return []


def _merge_context_sections(existing: list[str], incoming: list[str], *, max_sections: int) -> list[str]:
    merged = list(existing)
    seen = {str(item).strip() for item in merged if str(item).strip()}
    for item in incoming:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
        if len(merged) >= max_sections:
            break
    return merged


def _merge_search_entities(base: dict, incoming: dict, *, max_docs: int | None = None) -> dict:
    merged: dict = {}
    docs: list[dict] = []
    people: list[dict] = []
    hits = {"wiki": 0, "docs": 0, "bitable": 0, "messages": 0}

    for source in (base or {}, incoming or {}):
        if not isinstance(source, dict):
            continue
        for item in source.get("docs") or []:
            if isinstance(item, dict):
                docs.append(item)
        for item in source.get("people") or []:
            if isinstance(item, dict):
                people.append(item)
        source_hits = source.get("_hits") or {}
        if isinstance(source_hits, dict):
            for key in hits:
                hits[key] += int(source_hits.get(key) or 0)

    merged_docs = _dedupe_entity_dicts(docs, ("docs_token", "title", "url"))
    merged["docs"] = merged_docs[:max_docs] if isinstance(max_docs, int) and max_docs > 0 else merged_docs
    merged["people"] = _dedupe_entity_dicts(people, ("open_id", "name"))
    merged["_hits"] = hits
    return merged


def _dedupe_entity_dicts(items: list[dict], keys: tuple[str, ...]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = ""
        for key in keys:
            value = str(item.get(key) or "").strip()
            if value:
                identity = value
                break
        if not identity:
            identity = str(item).strip()
        if not identity or identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


def _extract_bitable_tokens(item: dict) -> tuple[str, str]:
    app_token = ""
    table_id = ""

    for key in ("app_token", "bitable_app_token"):
        value = item.get(key)
        if isinstance(value, str) and value:
            app_token = value
            break
    for key in ("table_id", "bitable_table_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            table_id = value
            break

    for nested_key in ("bitable", "node", "item", "resource"):
        nested = item.get(nested_key)
        if isinstance(nested, dict):
            sub_app, sub_table = _extract_bitable_tokens(nested)
            if not app_token and sub_app:
                app_token = sub_app
            if not table_id and sub_table:
                table_id = sub_table
    return app_token, table_id
