import asyncio
import json
import re
from typing import Any

from core.intent import IntentResult
from core.intent import _query_has_ascii_and_cjk
from core.intent import _query_is_ascii_only
from core.intent import build_query_candidates
from feishu_client.contact import contact_client
from feishu_client.doc import doc_client
from feishu_client.message import message_client
from feishu_client.search import search_client
from llm.client import LLMTimeoutError, llm_client
from utils.logger import get_logger
from utils.trusted_kb import trusted_kb_store

# 单篇文档正文拉取上限（条数）。再多会挤压 LLM context 也会拖慢并发。
_DOC_CONTENT_TOP_N = 3
# 单篇正文喂给 LLM 的字符上限——和 search_knowledge 保持一致。
_DOC_CONTENT_PREVIEW_CHARS = 600
# 文档正文拉取属于"主流程之外的补强"，超时不能拖垮整条流水线。
_DOC_CONTENT_FETCH_TIMEOUT = 4.0

logger = get_logger()

_NAME_STOPWORDS = {
    "安全问题",
    "办公室问题",
    "线上事故",
    "事故定级",
    "事故复盘",
    "线上事故定级",
    "线上事故复盘",
    "问题根因",
    "题根因",
    "根因",
    "复盘",
    "事故",
    "定级",
    "相关人员",
    "负责人",
    "对接人",
    "联系人",
    "管理员",
    "同学",
    "老师",
    "这里",
    "这个",
    "那个",
    "问题",
}
_NAME_PATTERNS = (
    re.compile(r"(?:找|联系|由|问)\s*([一-龥]{2,3})(?:处理|负责|对接|联系|即可|就行|就好)?"),
    re.compile(r"([一-龥]{2,3})\s*(?:负责|处理|对接|联系)"),
)
_BENEFIT_QUERY_MARKERS = (
    "餐补",
    "餐费",
    "餐卡",
    "加班餐",
    "报销",
    "费用报销",
    "发票",
    "差旅",
    "出差",
    "差旅费",
    "交通补贴",
    "通勤补贴",
    "打车",
    "网约车",
    "滴滴",
    "通讯补贴",
    "话费",
    "手机补贴",
    "住房补贴",
    "租房补贴",
    "福利",
    "补贴",
    "津贴",
    "补助",
    "社保",
    "公积金",
    "五险一金",
    "工资",
    "薪资",
    "发薪",
    "工资条",
    "个税",
    "税",
    "绩效",
    "奖金",
    "年终",
    "请假",
    "休假",
    "年假",
    "病假",
    "产假",
    "调休",
    "加班",
    "考勤",
    "打卡",
    "入职",
    "离职",
    "转正",
    "合同",
    "证明",
    "人事",
    "hr",
    "行政",
    "财务",
)
_BENEFIT_QUERY_BOOSTS = (
    "餐费",
    "餐补",
    "餐卡",
    "餐费问题",
    "餐卡问题",
    "餐费 餐卡",
    "餐费&餐卡问题",
    "加班餐费",
    "餐补发放",
    "餐补到账",
    "餐补未发放",
    "报销",
    "费用报销",
    "报销流程",
    "发票报销",
    "差旅",
    "差旅报销",
    "差旅政策",
    "出差报销",
    "交通补贴",
    "通勤补贴",
    "打车报销",
    "通讯补贴",
    "话费补贴",
    "社保",
    "公积金",
    "五险一金",
    "薪资",
    "工资",
    "发薪",
    "工资条",
    "个税",
    "请假",
    "年假",
    "考勤",
    "打卡",
    "入职",
    "离职",
    "转正",
    "人事",
    "行政",
    "财务",
    "福利",
)
_BENEFIT_POSITIVE_HINTS = (
    "餐费",
    "餐补",
    "餐卡",
    "加班餐",
    "福利",
    "补贴",
    "报销",
    "发票",
    "差旅",
    "出差",
    "交通",
    "通勤",
    "打车",
    "通讯",
    "社保",
    "公积金",
    "薪资",
    "工资",
    "个税",
    "请假",
    "年假",
    "考勤",
    "hr",
    "人事",
    "行政",
    "财务",
)
_BENEFIT_NEGATIVE_HINTS = (
    "教程",
    "研发",
    "开发",
    "限时免费",
    "免费版",
    "速通",
)
_BENEFIT_QUERY_PROFILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "meal",
        (
            "餐费",
            "餐补",
            "餐卡",
            "加班餐",
            "餐费问题",
            "餐卡问题",
            "餐费 餐卡",
            "餐费&餐卡问题",
            "餐补发放",
            "餐补到账",
            "餐补未发放",
        ),
    ),
    (
        "expense",
        (
            "报销",
            "费用报销",
            "报销流程",
            "发票报销",
            "差旅",
            "差旅报销",
            "差旅政策",
            "出差报销",
        ),
    ),
    (
        "allowance",
        (
            "交通补贴",
            "通勤补贴",
            "打车报销",
            "通讯补贴",
            "话费补贴",
            "住房补贴",
            "租房补贴",
        ),
    ),
    (
        "payroll",
        (
            "薪资",
            "工资",
            "发薪",
            "工资条",
            "个税",
            "奖金",
            "年终",
            "绩效",
        ),
    ),
    (
        "attendance",
        (
            "请假",
            "年假",
            "考勤",
            "打卡",
            "调休",
            "加班",
            "病假",
            "产假",
        ),
    ),
    (
        "hr",
        (
            "入职",
            "离职",
            "转正",
            "人事",
            "行政",
            "财务",
            "福利",
            "社保",
            "公积金",
            "五险一金",
        ),
    ),
)

_ISSUE_SEMANTIC_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "未到账",
        (
            "未到账",
            "没到账",
            "不到账",
            "未入账",
            "没入账",
            "未打款",
            "没打款",
        ),
    ),
    (
        "未发放",
        (
            "未发放",
            "没发放",
            "未发",
            "漏发",
            "少发",
            "未收到",
            "没收到",
        ),
    ),
    (
        "未生效",
        (
            "未生效",
            "没生效",
            "未开通",
            "没开通",
        ),
    ),
    (
        "异常",
        (
            "异常",
            "失败",
            "错误",
            "报错",
            "故障",
            "延迟",
        ),
    ),
)
_ISSUE_SEMANTIC_REPLACEMENTS: dict[str, tuple[str, ...]] = {
    "未到账": ("到账", "入账", "打款"),
    "未发放": ("发放", "发补贴", "发餐补"),
    "未生效": ("生效", "开通"),
}


async def run_find_person(
    open_id: str,
    intent: IntentResult,
    conversation_context: str = "",
    *,
    current_chat_id: str | None = None,
) -> tuple[str, list[str], dict]:
    """找人/事物归属流水线（四步 + 短路+补充）：
    1. 通讯录匹配 -> 候选 open_id。优先用 intent.person_hint（问题里显式出现的人名）查，
       没人名时才退而求其次用 keyword（此时多半查不到，但会统一走 search_user 日志）。
    2. 文档搜索：主查 + 若有候选人则 owner_ids 过滤补查（并发）。
    3. 群聊消息搜索（group_only=True）。
    4. LLM 推理整合，产出"最可能负责人 + 依据"。
    所有搜索都用 intent.keyword（已去掉"找谁/该/处理"等噪声），避免长问句稀释飞书搜索相关度。
    """
    sources: list[str] = []

    search_key = intent.search_key
    question = intent.raw_question
    routing, routing_timed_out = await _route_find_person(
        question=question,
        search_key=search_key,
        keyword_fallback=intent.keyword_fallback,
        conversation_context=conversation_context,
    )
    clarify_needed, clarify_reason, clarify_options = _routing_clarify_hint(routing, search_key)
    issue_terms = _extract_issue_semantics(question, search_key, intent.keyword_fallback)
    routed_search_key = _preserve_issue_semantics(_pick_routed_search_key(search_key, routing), issue_terms)
    routed_fallback = _preserve_issue_semantics(
        _pick_routed_fallback(intent.keyword_fallback, routing),
        issue_terms,
    )
    routed_aliases = _inject_issue_semantic_queries(_extract_routing_aliases(routing), issue_terms)
    search_key = routed_search_key or _preserve_issue_semantics(search_key, issue_terms)
    contact_query = intent.person_hint.strip() or search_key
    llm_expansions, expand_timed_out = await _expand_queries_with_llm(
        label="find_person",
        question=question,
        search_key=search_key,
        fallback=routed_fallback,
        conversation_context=conversation_context,
    )
    llm_expansions = _inject_issue_semantic_queries(
        _merge_string_candidates(routed_aliases, llm_expansions),
        issue_terms,
    )

    logger.info(
        "find_person start open_id={} question={!r} search_key={!r} routed_fallback={!r} contact_query={!r} routing={} llm_expansions={}",
        open_id,
        question,
        search_key,
        routed_fallback,
        contact_query,
        routing,
        llm_expansions,
    )

    people = await _step_contact(open_id, contact_query, sources)
    owner_ids = [p.get("open_id") for p in people if isinstance(p.get("open_id"), str)]

    queries = build_query_candidates(
        "find_person",
        search_key,
        routed_fallback,
        extra_candidates=llm_expansions,
    )
    queries = _inject_issue_semantic_queries(queries, issue_terms)
    queries = _prioritize_find_person_queries(search_key, queries)
    trusted_queries = _build_trusted_doc_queries(
        question=question,
        search_key=search_key,
        keyword_fallback=routed_fallback,
        routing=routing,
        queries=queries,
    )
    trusted_queries = _inject_issue_semantic_queries(trusted_queries, issue_terms)
    queries = _boost_find_person_queries(
        question=question,
        search_key=search_key,
        keyword_fallback=routed_fallback,
        routing=routing,
        queries=queries,
    )
    queries = _inject_issue_semantic_queries(queries, issue_terms)
    logger.info("find_person query candidates={}", queries)
    validation_queries = queries[:2] if len(queries) > 2 else list(queries)
    trusted_task = asyncio.create_task(_step_trusted_docs_async(search_key, routed_fallback, trusted_queries))
    validation_task = asyncio.create_task(
        _run_query_route(
            open_id=open_id,
            queries=validation_queries,
            owner_ids=owner_ids,
            current_chat_id=current_chat_id,
            search_key=search_key,
        )
    )
    trusted_docs, validation_result = await asyncio.gather(trusted_task, validation_task)
    if trusted_docs:
        sources.append("可信知识库")

    trusted_short_circuit = _should_short_circuit_trusted_docs(
        trusted_docs,
        search_terms=[search_key, routed_fallback, *trusted_queries],
    )
    all_doc_main = list(validation_result["doc_main"])
    all_doc_owner = list(validation_result["doc_owner"])
    all_wiki_docs = list(validation_result["wiki_docs"])
    all_msg_records = list(validation_result["msg_records"])
    sources.extend(validation_result["sources"])

    hit_query = str(validation_result["hit_query"] or "")
    first_hit_query = str(validation_result["first_hit_query"] or "")
    validation_hit = bool(hit_query or first_hit_query)

    if trusted_short_circuit:
        sources.append("可信知识库优先")
        logger.info(
            "find_person trusted preferred search_key={!r} docs={} top_score={} validation_hit={}",
            search_key,
            len(trusted_docs),
            trusted_docs[0].get("_trusted_score") if trusted_docs else 0,
            validation_hit,
        )

    if not validation_hit and not trusted_short_circuit and len(validation_queries) < len(queries):
        remaining_result = await _run_query_route(
            open_id=open_id,
            queries=queries[len(validation_queries) :],
            owner_ids=owner_ids,
            current_chat_id=current_chat_id,
            search_key=search_key,
        )
        all_doc_main.extend(remaining_result["doc_main"])
        all_doc_owner.extend(remaining_result["doc_owner"])
        all_wiki_docs.extend(remaining_result["wiki_docs"])
        all_msg_records.extend(remaining_result["msg_records"])
        sources.extend(remaining_result["sources"])
        if not first_hit_query and remaining_result["first_hit_query"]:
            first_hit_query = str(remaining_result["first_hit_query"] or "")
        if remaining_result["hit_query"]:
            hit_query = str(remaining_result["hit_query"] or "")
        elif not hit_query and remaining_result["first_hit_query"]:
            hit_query = str(remaining_result["first_hit_query"] or "")

    if not hit_query and first_hit_query and first_hit_query != search_key:
        sources.append(f"使用扩展关键词 {first_hit_query!r} 命中")

    effective_query = hit_query or first_hit_query or search_key
    merged_docs = _merge_docs(all_doc_main, all_doc_owner, all_wiki_docs, trusted_docs, effective_query)
    msg_records = _merge_msg_records(all_msg_records)

    # 主 keyword 零命中时，启用 LLM 给出的 keyword_fallback 再搜一次。
    # 只在"文档 + 消息都空"时触发，避免对已有命中场景翻倍请求。
    doc_main = all_doc_main
    doc_owner = all_doc_owner
    wiki_docs = all_wiki_docs

    # 把命中文档的正文读出来喂给 LLM。没有这步，"XX 事务找张三"这类答案就永远
    # 推不出来——LLM 只能看到文档标题 + open_id，看不到正文里的"张三"。
    docs_with_text = 0
    if merged_docs:
        docs_with_text = await _enrich_docs_with_raw(
            open_id,
            merged_docs,
            top_n=6 if _is_benefit_query(effective_query) else _DOC_CONTENT_TOP_N,
        )
        _rank_docs_by_relevance(merged_docs, effective_query)
        merged_docs = _filter_docs_by_search_key(merged_docs, effective_query)

    msg_records = _filter_messages_by_search_key(msg_records, effective_query)

    logger.info(
        "find_person summary contacts={} docs_main={} docs_owner={} docs_wiki={} docs_trusted={} docs_with_text={} msg_records={} msg_with_text={} sources={}",
        len(people),
        len(doc_main),
        len(doc_owner),
        len(wiki_docs),
        len(trusted_docs),
        docs_with_text,
        len(msg_records),
        sum(1 for m in msg_records if m.get("text")),
        sources,
    )

    # 素材全空时直接返回确定话术：没有任何素材让 LLM 合成也只是编话术，
    # 还白费 1-2s + 有幻觉风险。给用户一个明确的"没找到"比模糊的"可能是"靠谱。
    if not people and not merged_docs and not msg_records:
        logger.info("find_person short-circuit: no material, skip llm synthesize")
        sources = ["未命中通讯录/文档/群聊消息"]
        entities = {
            "_meta": {
                "label": "find_person",
                "queries": queries,
                "hit_query": hit_query or search_key,
                "exact_match": (hit_query or search_key) == search_key,
                "keyword_fallback": routed_fallback or "",
                "routing": _routing_meta(routing),
                "clarify_needed": clarify_needed,
                "clarify_reason": clarify_reason,
                "clarify_options": clarify_options,
                "hits": {"docs": 0, "messages": 0, "contacts": 0},
                "visibility": {"docs_no_permission": 0, "docs_unavailable": 0},
            }
        }
        return (
            "没有在通讯录、云文档或群聊里找到相关负责人。建议补充部门、项目名或更具体的关键词后再试。",
            sources,
            entities,
        )

    # LLM 推理整合（第 4 步）：素材不空时才调用。
    llm_degraded = routing_timed_out or expand_timed_out
    self_check: dict[str, Any] = {}
    has_global_hits = bool(doc_main or doc_owner or wiki_docs or msg_records)
    if trusted_short_circuit and not people and merged_docs and not has_global_hits:
        answer = _fallback_summary(people, merged_docs, msg_records, effective_query)
        answer = _inject_conflict_notice(answer, merged_docs, msg_records, effective_query)
        llm_degraded = True
        logger.info("find_person skip llm by trusted preferred result search_key={!r}", effective_query)
    else:
        answer, synth_timed_out = await _step_llm_synthesize(
            question,
            people,
            merged_docs,
            msg_records,
            sources,
            effective_query,
            conversation_context,
        )
        llm_degraded = llm_degraded or synth_timed_out
        answer = _inject_conflict_notice(answer, merged_docs, msg_records, effective_query)
        if not llm_degraded:
            self_check = await _step_llm_self_check(
                question=question,
                routing=routing,
                queries=queries,
                people=people,
                docs=merged_docs,
                msg_records=msg_records,
                search_key=effective_query,
                draft_answer=answer,
            )
        else:
            logger.warning("find_person llm degraded, skip self_check search_key={!r}", effective_query)
    answer = _apply_self_check_result(
        answer,
        self_check,
        docs=merged_docs,
        people=people,
        search_key=effective_query,
    )

    entities = _build_entities(people, merged_docs, msg_records, search_key=effective_query)
    entities["_meta"] = {
        "label": "find_person",
        "queries": queries,
        "hit_query": hit_query or search_key,
        "exact_match": (hit_query or search_key) == search_key,
        "keyword_fallback": routed_fallback or "",
        "routing": _routing_meta(routing),
        "clarify_needed": clarify_needed,
        "clarify_reason": clarify_reason,
        "clarify_options": clarify_options,
        "self_check": _self_check_meta(self_check),
        "hits": {"docs": len(merged_docs), "messages": len(msg_records), "contacts": len(people)},
        "visibility": {
            "docs_no_permission": sum(
                1 for d in merged_docs if (d.get("raw_content_error") == "no_permission")
            ),
            "docs_unavailable": sum(
                1 for d in merged_docs if (d.get("raw_content_error") == "unavailable")
            ),
        },
    }
    return answer, sources, entities


def _routing_clarify_hint(routing: dict[str, Any], search_key: str) -> tuple[bool, str, list[str]]:
    if not isinstance(routing, dict):
        return False, "", []
    ambiguous = _safe_bool(routing.get("is_ambiguous"))
    confidence = _safe_float(routing.get("confidence"))
    options = [
        str(item).strip()
        for item in (routing.get("clarify_options") or [])
        if str(item or "").strip()
    ][:4]
    if ambiguous or (confidence > 0 and confidence < 0.45):
        key = str(search_key or "").strip()
        reason = "问题过泛，建议补充更具体的系统/流程/部门信息后再判断。"
        if key:
            reason = f"“{key}”较泛或存在歧义，建议补充更具体的系统/流程/部门信息。"
        return True, reason, options
    return False, "", options


async def _route_find_person(
    *,
    question: str,
    search_key: str,
    keyword_fallback: str,
    conversation_context: str,
) -> tuple[dict[str, Any], bool]:
    try:
        routed = await llm_client.route_find_person(
            question=question,
            search_key=search_key,
            keyword_fallback=keyword_fallback,
            conversation_context=conversation_context,
        )
    except LLMTimeoutError:
        logger.warning("route_find_person timeout search_key={!r}", search_key)
        return {}, True
    except Exception:
        logger.exception("route_find_person failed search_key={!r}", search_key)
        return {}, False
    if not isinstance(routed, dict):
        return {}, False
    return routed, False


def _pick_routed_search_key(search_key: str, routing: dict[str, Any]) -> str:
    for key in ("keyword", "canonical_domain"):
        text = str(routing.get(key) or "").strip()
        if text:
            return text
    return search_key


def _extract_issue_semantics(*texts: str) -> list[str]:
    haystack = "\n".join(str(text or "").strip().lower() for text in texts if str(text or "").strip())
    if not haystack:
        return []
    out: list[str] = []
    for canonical, markers in _ISSUE_SEMANTIC_GROUPS:
        if any(marker.lower() in haystack for marker in markers):
            out.append(canonical)
    return out


def _preserve_issue_semantics(text: str, issue_terms: list[str]) -> str:
    value = str(text or "").strip()
    if not value or not issue_terms:
        return value
    merged = value
    for term in issue_terms:
        merged = _merge_issue_term(merged, term)
    return merged.strip()


def _merge_issue_term(text: str, issue_term: str) -> str:
    value = str(text or "").strip()
    term = str(issue_term or "").strip()
    if not value or not term or term in value:
        return value

    replacements = _ISSUE_SEMANTIC_REPLACEMENTS.get(term, ())
    for positive in replacements:
        if positive in value:
            return value.replace(positive, term)

    compact_value = _compact_query(value)
    compact_term = _compact_query(term)
    if compact_term and compact_term in compact_value:
        return value

    joiner = "" if len(value) <= 8 and " " not in value else " "
    return f"{value}{joiner}{term}".strip()


def _inject_issue_semantic_queries(queries: list[str], issue_terms: list[str]) -> list[str]:
    if not issue_terms:
        return _merge_string_candidates(queries)
    expanded: list[str] = []
    for query in queries or []:
        text = str(query or "").strip()
        if not text:
            continue
        expanded.append(text)
        merged = _preserve_issue_semantics(text, issue_terms)
        if merged and merged != text:
            expanded.append(merged)
    return _merge_string_candidates(expanded)


def _pick_routed_fallback(keyword_fallback: str, routing: dict[str, Any]) -> str:
    for key in ("keyword_fallback", "fallback_domain"):
        text = str(routing.get(key) or "").strip()
        if text:
            return text
    return keyword_fallback or ""


def _extract_routing_aliases(routing: dict[str, Any]) -> list[str]:
    items = routing.get("aliases")
    if not isinstance(items, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        compact = _compact_query(text)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        out.append(text)
        if len(out) >= 4:
            break
    return out


def _merge_string_candidates(*groups: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            text = str(item or "").strip()
            compact = _compact_query(text)
            if not compact or compact in seen:
                continue
            seen.add(compact)
            out.append(text)
    return out


def _routing_meta(routing: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_domain": str(routing.get("canonical_domain") or "").strip(),
        "keyword": str(routing.get("keyword") or "").strip(),
        "keyword_fallback": str(routing.get("keyword_fallback") or "").strip(),
        "is_ambiguous": _safe_bool(routing.get("is_ambiguous")),
        "clarify_options": [
            str(item).strip()
            for item in (routing.get("clarify_options") or [])
            if str(item or "").strip()
        ][:4],
        "confidence": _safe_float(routing.get("confidence")),
    }


def _safe_float(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


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


async def _step_llm_self_check(
    *,
    question: str,
    routing: dict[str, Any],
    queries: list[str],
    people: list[dict],
    docs: list[dict],
    msg_records: list[dict],
    search_key: str,
    draft_answer: str,
) -> dict[str, Any]:
    id_to_name = _build_open_id_name_map(people)
    try:
        result = await llm_client.self_check_person_answer(
            question=question,
            routing_context=json.dumps(_routing_meta(routing), ensure_ascii=False),
            queries=json.dumps(queries[:6], ensure_ascii=False),
            contacts=_format_contacts(people),
            docs=_format_docs(docs, id_to_name, search_key),
            messages=_format_messages(msg_records, id_to_name),
            draft_answer=draft_answer,
        )
    except Exception:
        logger.exception("llm self_check_person_answer failed")
        return {}
    if not isinstance(result, dict):
        return {}
    logger.info("find_person self_check={}", result)
    return result


def _apply_self_check_result(
    answer: str,
    self_check: dict[str, Any],
    *,
    docs: list[dict] | None = None,
    people: list[dict] | None = None,
    search_key: str = "",
) -> str:
    if not isinstance(self_check, dict) or not answer:
        return answer
    should_downgrade = _safe_bool(self_check.get("should_downgrade"))
    should_ask_more = _safe_bool(self_check.get("should_ask_more"))
    consistency_ok = _safe_bool(self_check.get("consistency_ok"), default=True)
    if not should_downgrade and not should_ask_more and consistency_ok:
        return answer

    answer_hint = str(self_check.get("answer_hint") or "").strip()
    risk_note = str(self_check.get("risk_note") or "").strip()
    suggestion = answer_hint or risk_note or "建议补充部门、项目名或更具体的职责词后再试。"

    # 若 self-check 已经识别出标准口径是“走自助服务/提交工单”，
    # 直接输出标准入口，避免保留前面不稳定的负责人候选误导用户。
    if _is_service_desk_suggestion(suggestion):
        text = suggestion
        if "未找到明确负责人" not in text and "如需人工协助" not in text:
            text = f"{text}，如需人工协助请补充具体问题或部门信息"
        return text.strip("。 ") + "。"

    evidence_answer = _build_soft_downgrade_answer(
        docs=docs or [],
        people=people or [],
        search_key=search_key,
        suggestion=suggestion,
        consistency_ok=consistency_ok,
        should_ask_more=should_ask_more,
    )
    if evidence_answer:
        return evidence_answer

    if "暂无明确候选" in answer:
        if suggestion and suggestion not in answer:
            return f"{answer}\n建议：{suggestion}".strip()
        return answer

    if not consistency_ok:
        prefix = "当前结论与问题职责域可能存在偏差，以下结果仅供参考。"
    elif should_ask_more:
        prefix = "当前证据仍不足以唯一确定负责人，以下为较可能候选供参考。"
    else:
        prefix = "当前证据偏弱，以下结果建议进一步确认。"

    if suggestion:
        return f"{prefix}\n\n{answer}\n\n建议：{suggestion}".strip()
    return f"{prefix}\n\n{answer}".strip()


def _build_soft_downgrade_answer(
    *,
    docs: list[dict],
    people: list[dict],
    search_key: str,
    suggestion: str,
    consistency_ok: bool,
    should_ask_more: bool,
) -> str:
    if people or not docs:
        return ""

    normalized_key = str(search_key or "").strip() or "该问题"
    if not consistency_ok:
        opener = f"暂未从现有证据中直接定位到“{normalized_key}”的明确负责人，但已命中相关文档线索。"
    elif should_ask_more:
        opener = f"当前还不能唯一确定“{normalized_key}”该找谁，但已命中以下相关文档线索。"
    else:
        opener = f"当前负责人证据偏弱，但已命中与“{normalized_key}”相关的文档线索。"

    doc_lines: list[str] = []
    for doc in docs[:2]:
        if not isinstance(doc, dict):
            continue
        title = str(doc.get("title") or doc.get("name") or "").strip()
        raw = str(doc.get("raw_content") or "").strip()
        snippet = _build_evidence_snippet(raw, search_key)
        if title and snippet:
            doc_lines.append(f"《{title}》提到：{snippet}")
        elif title:
            doc_lines.append(f"可先参考文档《{title}》")
        if len(doc_lines) >= 2:
            break

    if not doc_lines:
        return ""

    next_step = suggestion or _build_followup_hint(search_key)
    return f"{opener}\n- " + "\n- ".join(doc_lines) + f"\n建议：{next_step}"


def _build_evidence_snippet(raw: str, search_key: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    snippet = _build_query_centered_preview(text, search_key, 90)
    snippet = snippet.replace("\n", " ").strip()
    if len(snippet) > 90:
        snippet = snippet[:89].rstrip() + "…"
    return snippet


def _build_followup_hint(search_key: str) -> str:
    key = str(search_key or "").strip()
    benefit_terms = _benefit_terms_for_text(key)
    if benefit_terms:
        if any(term in key for term in ("餐费", "餐补", "餐卡", "加班餐")):
            return "建议补充餐费类型、所属部门或具体场景（如餐补发放/餐卡/加班餐）后再判断。"
        if any(term in key for term in ("报销", "差旅", "发票")):
            return "建议补充报销类型、所属部门或具体系统后再进一步判断。"
    return "建议补充部门、项目名或更具体的职责词后再试。"


def _self_check_meta(self_check: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(self_check, dict):
        return {}
    return {
        "consistency_ok": _safe_bool(self_check.get("consistency_ok"), default=True),
        "evidence_strength": str(self_check.get("evidence_strength") or "").strip(),
        "should_downgrade": _safe_bool(self_check.get("should_downgrade")),
        "should_ask_more": _safe_bool(self_check.get("should_ask_more")),
        "risk_note": str(self_check.get("risk_note") or "").strip(),
        "answer_hint": str(self_check.get("answer_hint") or "").strip(),
    }


def _is_service_desk_suggestion(text: str) -> bool:
    value = str(text or "").strip().lower()
    if not value:
        return False
    markers = (
        "自助服务",
        "提交工单",
        "工单",
        "服务台",
        "工作台",
        "itsm",
        "报修入口",
    )
    return any(marker in value for marker in markers)


def _build_entities(
    people: list[dict],
    docs: list[dict],
    msg_records: list[dict],
    *,
    search_key: str = "",
) -> dict:
    result: dict[str, list[dict]] = {"people": [], "docs": [], "messages": [], "evidence": []}
    seen_people: set[str] = set()
    for person in people[:5]:
        if not isinstance(person, dict):
            continue
        open_id = person.get("open_id") or ""
        if isinstance(open_id, str) and open_id:
            if open_id in seen_people:
                continue
            seen_people.add(open_id)
        result["people"].append({"name": person.get("name") or "", "open_id": open_id})

    result["evidence"] = _build_evidence_items(docs, msg_records, search_key)

    for doc in docs[:5]:
        if not isinstance(doc, dict):
            continue
        result["docs"].append(
            {
                "title": doc.get("title") or doc.get("name") or "",
                "url": doc.get("url") or "",
                "docs_token": doc.get("docs_token") or doc.get("obj_token") or "",
                "docs_type": doc.get("docs_type") or "",
                "raw_content_error": doc.get("raw_content_error") or "",
            }
        )

    for msg in (msg_records or [])[:5]:
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("text") or "").strip()
        sender = msg.get("sender") or {}
        sender_id = sender.get("id") or ""
        sender_type = sender.get("sender_type") or sender.get("id_type") or ""
        result["messages"].append(
            {
                "text": text,
                "sender_id": str(sender_id or ""),
                "sender_type": str(sender_type or ""),
                "message_id": str(msg.get("message_id") or ""),
            }
        )
    return result


def _build_evidence_items(
    docs: list[dict],
    msg_records: list[dict],
    search_key: str,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    key = str(search_key or "").strip()

    for doc in docs[:5]:
        if not isinstance(doc, dict):
            continue
        raw = str(doc.get("raw_content") or "").strip()
        if not raw:
            continue
        title = str(doc.get("title") or doc.get("name") or "").strip()
        url = str(doc.get("url") or "").strip()
        snippet = _build_query_centered_preview(raw, key, 220)
        snippet = snippet.replace("\n", " ").strip()
        if len(snippet) > 220:
            snippet = snippet[:219].rstrip() + "…"
        if snippet:
            items.append(
                {
                    "source_type": "文档",
                    "title": title or "未命名文档",
                    "url": url,
                    "text": snippet,
                }
            )
            break

    return items


async def _expand_queries_with_llm(
    *,
    label: str,
    question: str,
    search_key: str,
    fallback: str,
    conversation_context: str,
) -> tuple[list[str], bool]:
    if not search_key:
        return [], False
    try:
        return (
            await llm_client.expand_queries(
                intent=label,
                question=question,
                keyword=search_key,
                keyword_fallback=fallback,
                conversation_context=conversation_context,
            ),
            False,
        )
    except LLMTimeoutError:
        logger.warning("expand_queries timeout label={} search_key={!r}", label, search_key)
        return [], True
    except Exception:
        logger.exception("expand_queries failed label={} search_key={!r}", label, search_key)
        return [], False


def _looks_like_service_desk_doc(doc: dict[str, Any]) -> bool:
    text = "\n".join(
        [
            str(doc.get("title") or doc.get("name") or "").strip(),
            str(doc.get("raw_content") or "").strip()[:800],
        ]
    ).lower()
    if not text:
        return False
    markers = (
        "自助服务",
        "it自助",
        "服务台",
        "提交工单",
        "工单",
        "itsm",
        "报修入口",
    )
    return any(marker in text for marker in markers)


def _build_service_desk_answer(docs: list[dict], search_key: str) -> str:
    for doc in docs[:5]:
        if _looks_like_service_desk_doc(doc):
            title = str(doc.get("title") or doc.get("name") or "").strip()
            if "it" in str(search_key or "").lower() or "报修" in str(search_key or ""):
                answer = "IT报修可通过飞书工作台-IT自助服务提交工单"
            else:
                answer = "该问题建议优先通过飞书工作台中的自助服务或服务台入口提交工单"
            if title:
                answer += f"。可参考文档《{title}》"
            answer += "。如需人工协助请补充具体问题或部门信息。"
            return answer
    return ""


def _fallback_summary(
    people: list[dict], docs: list[dict], msg_records: list[dict], search_key: str = ""
) -> str:
    service_desk_answer = _build_service_desk_answer(docs, search_key)
    if service_desk_answer:
        return service_desk_answer
    parts: list[str] = []
    if people:
        top = people[0]
        name = top.get("name", "未知")
        pid = top.get("open_id", "")
        parts.append(f"最可能负责人候选：{name}（open_id：{pid}）。")
    else:
        parts.append("未检索到明确候选人，建议补充部门或项目名。")
    if docs:
        titles = [d.get("title") or d.get("name") for d in docs if d.get("title") or d.get("name")]
        if titles:
            parts.append(f"相关文档：{'、'.join(titles[:3])}。")
    if msg_records:
        with_text = sum(1 for m in msg_records if m.get("text"))
        parts.append(
            f"另外命中 {len(msg_records)} 条群聊消息（已读取正文 {with_text} 条）可作辅助线索。"
        )
    return " ".join(parts)


def _prioritize_find_person_queries(search_key: str, queries: list[str]) -> list[str]:
    if not queries:
        return []
    base_key = _compact_query(search_key)

    def _score(query: str) -> tuple[int, int, int, int]:
        q = (query or "").strip()
        compact = _compact_query(q)
        exact = 0 if compact and compact == base_key else 1
        # 找人场景里，"IT" 这类裸英文词通常过泛，放到更具体词之后再尝试。
        generic_ascii = 1 if _query_is_ascii_only(q) else 0
        semantic = 1
        if compact and base_key and (compact in base_key or base_key in compact):
            semantic = 0
        specificity = -len(compact)
        return (exact, generic_ascii, semantic, specificity)

    ranked = sorted(
        [str(q or "").strip() for q in queries if str(q or "").strip()],
        key=_score,
    )
    deduped: list[str] = []
    seen: set[str] = set()
    for q in ranked:
        compact = _compact_query(q)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        deduped.append(q)
    return deduped


def _is_benefit_query(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return any(marker in value for marker in _BENEFIT_QUERY_MARKERS)


def _boost_find_person_queries(
    *,
    question: str,
    search_key: str,
    keyword_fallback: str,
    routing: dict[str, Any],
    queries: list[str],
) -> list[str]:
    seed_terms = _merge_string_candidates(
        [question, search_key, keyword_fallback],
        [str(routing.get("canonical_domain") or "").strip()],
        [
            str(item).strip()
            for item in (routing.get("aliases") or [])
            if str(item or "").strip()
        ],
        queries,
    )
    haystack = " ".join(seed_terms)
    if not any(marker in haystack for marker in _BENEFIT_QUERY_MARKERS):
        return queries

    profile_boosts = _select_benefit_profile_boosts(haystack)
    boosted = _merge_string_candidates(
        [search_key, keyword_fallback],
        profile_boosts or list(_BENEFIT_QUERY_BOOSTS[:10]),
        queries,
    )
    boosted = boosted[:16]
    logger.info("benefit query boost applied: {}", boosted)
    return boosted


def _select_benefit_profile_boosts(text: str) -> list[str]:
    return _benefit_terms_for_text(text)


def _benefit_terms_for_text(text: str) -> list[str]:
    lower_text = str(text or "").strip().lower()
    if not lower_text:
        return []
    out: list[str] = []
    matched_profile = False
    for _, profile_terms in _BENEFIT_QUERY_PROFILES:
        if any(term.lower() in lower_text for term in profile_terms):
            matched_profile = True
            out.extend(profile_terms)
    if matched_profile:
        return _merge_string_candidates(out)
    return []


def _build_trusted_doc_queries(
    *,
    question: str,
    search_key: str,
    keyword_fallback: str,
    routing: dict[str, Any],
    queries: list[str],
) -> list[str]:
    benefit_terms = _benefit_terms_for_text(" ".join([question, search_key, keyword_fallback]))
    routed_aliases = [
        str(item).strip()
        for item in (routing.get("aliases") or [])
        if str(item or "").strip()
    ]
    candidates = _merge_string_candidates(
        [search_key, keyword_fallback],
        [str(routing.get("canonical_domain") or "").strip(), str(routing.get("keyword") or "").strip()],
        routed_aliases[:4],
        list(queries[:6]),
        [question] if len(str(question or "").strip()) <= 12 else [],
    )
    narrowed: list[str] = []
    base = _compact_query(search_key or keyword_fallback)
    for item in candidates:
        compact = _compact_query(item)
        if not compact:
            continue
        if base and (compact in base or base in compact):
            narrowed.append(item)
            continue
        if benefit_terms and any(marker in item for marker in benefit_terms):
            narrowed.append(item)
    return _merge_string_candidates(narrowed)[:8]


def _compact_query(text: str) -> str:
    return "".join(str(text or "").split()).lower()


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


def _extract_names_from_text(text: str) -> list[str]:
    out: list[str] = []
    s = (text or "").strip()
    if not s:
        return out
    for pattern in _NAME_PATTERNS:
        for match in pattern.findall(s):
            name = str(match).strip()
            if len(name) < 2 or len(name) > 4:
                continue
            if name in _NAME_STOPWORDS:
                continue
            if name.endswith(("问题", "根因", "流程", "复盘", "定级", "事故")):
                continue
            if name not in out:
                out.append(name)
    return out


def _query_is_strong_match(text: str, search_key: str) -> bool:
    t = (text or "").strip()
    key = (search_key or "").strip()
    if not t or not key:
        return False
    if t == key:
        return True
    escaped = re.escape(key)
    pattern = re.compile(
        rf"(^|[\s:：,，。；;、/\\\-\(\)（）\[\]【】]){escaped}($|[\s:：,，。；;、/\\\-\(\)（）\[\]【】]|找|由|归|问|负责|处理|对接|联系)"
    )
    return bool(pattern.search(t))


def _filter_docs_by_search_key(docs: list[dict], search_key: str) -> list[dict]:
    if not docs or not search_key:
        return docs
    matched = [
        d
        for d in docs
        if _query_is_strong_match(str(d.get("raw_content") or ""), search_key)
        or _query_is_strong_match(str(d.get("title") or d.get("name") or ""), search_key)
    ]
    benefit_terms = _benefit_terms_for_text(search_key)
    if not matched and benefit_terms:
        matched = [
            d
            for d in docs
            if any(
                marker in f"{str(d.get('title') or d.get('name') or '')}\n{str(d.get('raw_content') or '')}"
                for marker in benefit_terms
            )
        ]
    if matched:
        logger.info(
            "docs filtered by search_key={!r}: total={} kept={}",
            search_key,
            len(docs),
            len(matched),
        )
        return matched
    return docs


def _filter_messages_by_search_key(msg_records: list[dict], search_key: str) -> list[dict]:
    if not msg_records or not search_key:
        return msg_records
    matched = [m for m in msg_records if _query_is_strong_match(str(m.get("text") or ""), search_key)]
    if matched:
        logger.info(
            "messages filtered by search_key={!r}: total={} kept={}",
            search_key,
            len(msg_records),
            len(matched),
        )
        return matched
    return msg_records


def _collect_claims(docs: list[dict], msg_records: list[dict], search_key: str) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for doc in docs[:5]:
        if not isinstance(doc, dict):
            continue
        raw = str(doc.get("raw_content") or "").strip()
        if not raw:
            continue
        title = str(doc.get("title") or doc.get("name") or "未命名文档").strip()
        if not (_query_is_strong_match(raw, search_key) or _query_is_strong_match(title, search_key)):
            continue
        for name in _extract_names_from_text(raw):
            claims.append(
                {
                    "name": name,
                    "source_type": "文档",
                    "source_name": title,
                    "snippet": raw[:80].replace("\n", " "),
                }
            )

    for msg in msg_records[:5]:
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        if not _query_is_strong_match(text, search_key):
            continue
        for name in _extract_names_from_text(text):
            claims.append(
                {
                    "name": name,
                    "source_type": "群聊消息",
                    "source_name": "群聊消息",
                    "snippet": text[:80].replace("\n", " "),
                }
            )
    return claims


def _inject_conflict_notice(answer: str, docs: list[dict], msg_records: list[dict], search_key: str) -> str:
    claims = _collect_claims(docs, msg_records, search_key)
    if not claims:
        return answer

    unique_names: list[str] = []
    for c in claims:
        name = c.get("name") or ""
        if name and name not in unique_names:
            unique_names.append(name)
    if len(unique_names) < 2:
        return answer

    # 优先找“文档 vs 群聊消息”的冲突；找不到再取前两个不同姓名来源。
    doc_claim = next((c for c in claims if c.get("source_type") == "文档"), None)
    msg_claim = next(
        (
            c
            for c in claims
            if c.get("source_type") == "群聊消息"
            and c.get("name")
            and c.get("name") != (doc_claim or {}).get("name")
        ),
        None,
    )
    if not doc_claim or not msg_claim:
        first = claims[0]
        second = next((c for c in claims[1:] if c.get("name") != first.get("name")), None)
        if not second:
            return answer
        doc_claim, msg_claim = first, second

    conflict = (
        f"存在冲突：{doc_claim['source_type']}《{doc_claim['source_name']}》指向{doc_claim['name']}，"
        f"{msg_claim['source_type']}指向{msg_claim['name']}；请结合最新群内分工自行判断。"
    )
    if "存在冲突" in answer:
        return answer
    return f"{conflict}\n\n{answer}".strip()


async def _step_contact(
    open_id: str, query: str, sources: list[str]
) -> list[dict]:
    if not query:
        return []
    try:
        people = await contact_client.search_user(open_id, query)
    except PermissionError:
        raise
    except Exception:
        logger.exception("contact search failed")
        return []
    if people:
        sources.append("通讯录匹配")
    return people


async def _step_docs(
    open_id: str,
    search_key: str,
    owner_ids: list[str],
    sources: list[str],
) -> tuple[list[dict], list[dict]]:
    if not search_key:
        return [], []
    # 主查：按清洗后的 keyword 搜全量文档。
    # 补查：若已有候选人，用 owner_ids 再查一次，拿候选人的个人文档（更精准）。
    tasks = [
        search_client.search_docs(open_id, search_key, page_size=5, docs_types=["doc", "docx"])
    ]
    if owner_ids:
        tasks.append(
            search_client.search_docs(
                open_id, search_key, page_size=5, owner_ids=owner_ids[:10], docs_types=["doc", "docx"]
            )
        )
    else:
        tasks.append(_noop_docs())

    try:
        main_result, owner_result = await asyncio.gather(*tasks, return_exceptions=True)
    except PermissionError:
        raise

    main_docs = _unwrap_doc_result(main_result, "doc main search failed")
    owner_docs = _unwrap_doc_result(owner_result, "doc owner_ids search failed")

    if main_docs or owner_docs:
        sources.append("文档搜索")
    return main_docs, owner_docs


async def _step_wiki_docs(open_id: str, search_key: str, sources: list[str]) -> list[dict]:
    if not search_key:
        return []
    try:
        items = await search_client.search_wiki(open_id, search_key, page_size=5)
    except PermissionError:
        raise
    except Exception:
        logger.exception("search wiki failed")
        return []

    docs: list[dict] = []
    for item in items:
        obj_type_value = item.get("obj_type")
        obj_type = obj_type_value.lower() if isinstance(obj_type_value, str) else ""
        if obj_type and obj_type not in ("doc", "docx"):
            continue
        token = item.get("obj_token")
        if not isinstance(token, str) or not token:
            continue
        docs.append(
            {
                "docs_type": obj_type,
                "obj_token": token,
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "owner_id": "",
            }
        )
    if docs:
        sources.append("知识库")
    return docs


def _step_trusted_docs(search_key: str, keyword_fallback: str, queries: list[str]) -> list[dict]:
    candidates: list[str] = []
    for item in [search_key, keyword_fallback, *(queries or [])]:
        text = str(item or "").strip()
        if text and text not in candidates:
            candidates.append(text)
    if not candidates:
        return []
    try:
        docs = trusted_kb_store.search(candidates, limit=6, mode="find_person")
    except Exception:
        logger.exception("trusted kb search failed search_key={!r}", search_key)
        return []
    if docs:
        logger.info(
            "trusted kb hit search_key={!r} docs={} queries={}",
            search_key,
            len(docs),
            candidates[:8],
        )
    return docs


async def _step_trusted_docs_async(
    search_key: str,
    keyword_fallback: str,
    queries: list[str],
) -> list[dict]:
    return await asyncio.to_thread(_step_trusted_docs, search_key, keyword_fallback, queries)


async def _run_query_route(
    *,
    open_id: str,
    queries: list[str],
    owner_ids: list[str],
    current_chat_id: str | None,
    search_key: str,
) -> dict[str, Any]:
    all_doc_main: list[dict] = []
    all_doc_owner: list[dict] = []
    all_wiki_docs: list[dict] = []
    all_msg_records: list[dict] = []
    route_sources: list[str] = []
    hit_query = ""
    first_hit_query = ""

    for q in queries:
        docs_task = _step_docs(open_id, q, owner_ids, route_sources)
        wiki_task = _step_wiki_docs(open_id, q, route_sources)
        msgs_task = _step_messages(open_id, q, route_sources, current_chat_id)
        (doc_main, doc_owner), wiki_docs, msg_records = await asyncio.gather(
            docs_task, wiki_task, msgs_task
        )
        all_doc_main.extend(doc_main)
        all_doc_owner.extend(doc_owner)
        all_wiki_docs.extend(wiki_docs)
        all_msg_records.extend(msg_records)
        if doc_main or doc_owner or wiki_docs or msg_records:
            if not first_hit_query:
                first_hit_query = q
            if _query_has_ascii_and_cjk(search_key) and _query_is_ascii_only(q):
                continue
            hit_query = q
            if q != search_key:
                route_sources.append(f"使用扩展关键词 {q!r} 命中")
            break

    return {
        "doc_main": all_doc_main,
        "doc_owner": all_doc_owner,
        "wiki_docs": all_wiki_docs,
        "msg_records": all_msg_records,
        "sources": route_sources,
        "hit_query": hit_query,
        "first_hit_query": first_hit_query,
    }


def _should_short_circuit_trusted_docs(docs: list[dict], search_terms: list[str]) -> bool:
    if not docs:
        return False
    top = docs[0] if isinstance(docs[0], dict) else {}
    score = float(top.get("_trusted_score") or 0)
    if score < 55:
        return False

    clean_terms = [str(term).strip().lower() for term in search_terms if str(term).strip()]
    if not clean_terms:
        return False

    title = str(top.get("title") or top.get("name") or "").strip().lower()
    raw = str(top.get("raw_content") or "").strip().lower()
    meta = top.get("_trusted_meta") if isinstance(top.get("_trusted_meta"), dict) else {}
    matched_queries = [str(item).strip().lower() for item in (meta.get("matched_queries") or []) if str(item).strip()]
    categories = [str(item).strip().lower() for item in (meta.get("categories") or []) if str(item).strip()]

    direct_hits = 0
    for term in clean_terms[:6]:
        compact = _compact_query(term)
        if not compact:
            continue
        if compact in _compact_query(title):
            direct_hits += 2
            continue
        if any(compact in _compact_query(item) for item in matched_queries):
            direct_hits += 2
            continue
        if any(compact in _compact_query(item) for item in categories):
            direct_hits += 1
            continue
        if compact in _compact_query(raw[:300]):
            direct_hits += 1

    if _looks_like_service_desk_doc(top) and direct_hits >= 1:
        return True
    return direct_hits >= 2


async def _noop_docs() -> list[dict]:
    return []


def _unwrap_doc_result(result: Any, failure_msg: str) -> list[dict]:
    if isinstance(result, PermissionError):
        raise result
    if isinstance(result, Exception):
        logger.warning("{}: {}", failure_msg, result)
        return []
    if isinstance(result, list):
        return result
    return []


def _merge_docs(
    main_docs: list[dict],
    owner_docs: list[dict],
    wiki_docs: list[dict],
    trusted_docs: list[dict],
    search_key: str,
) -> list[dict]:
    # 可信知识库优先，再拼候选人个人文档、主查结果与 wiki；按 docs_token 去重。
    seen: set[str] = set()
    merged: list[dict] = []
    for doc in list(trusted_docs):
        if isinstance(doc, dict) and "_source" not in doc:
            doc["_source"] = "trusted"
        token = doc.get("docs_token") or doc.get("obj_token") or doc.get("title") or ""
        if token in seen:
            continue
        seen.add(token)
        merged.append(doc)
    for doc in list(owner_docs):
        if isinstance(doc, dict) and "_source" not in doc:
            doc["_source"] = "owner"
        token = doc.get("docs_token") or doc.get("obj_token") or doc.get("title") or ""
        if token in seen:
            continue
        seen.add(token)
        merged.append(doc)
    for doc in list(main_docs):
        if isinstance(doc, dict) and "_source" not in doc:
            doc["_source"] = "main"
        token = doc.get("docs_token") or doc.get("obj_token") or doc.get("title") or ""
        if token in seen:
            continue
        seen.add(token)
        merged.append(doc)
    for doc in list(wiki_docs):
        if isinstance(doc, dict) and "_source" not in doc:
            doc["_source"] = "wiki"
        token = doc.get("docs_token") or doc.get("obj_token") or doc.get("title") or ""
        if token in seen:
            continue
        seen.add(token)
        merged.append(doc)
    _rank_docs_by_relevance(merged, search_key)
    return merged


def _rank_docs_by_relevance(docs: list[dict], search_key: str) -> None:
    key = (search_key or "").strip()
    benefit_query = _is_benefit_query(key)

    def _score(d: dict) -> int:
        src = d.get("_source")
        src_score = (
            40
            if src == "trusted"
            else 30
            if src == "owner"
            else 20
            if src == "main"
            else 10
            if src == "wiki"
            else 0
        )
        docs_type_value = d.get("docs_type")
        docs_type = docs_type_value.lower() if isinstance(docs_type_value, str) else ""
        type_score = 5 if docs_type == "docx" else 4 if docs_type == "doc" else 0
        url_score = 1 if (d.get("url") or "") else 0

        title = str(d.get("title") or d.get("name") or "").strip()
        raw = str(d.get("raw_content") or "").strip()
        has_raw = 8 if raw else 0

        rel = 0
        if key:
            if key in title:
                rel += 8
            if raw and key in raw:
                rel += 15
            markers = ("找", "负责", "对接", "处理", "联系")
            if raw and any(m in raw for m in markers):
                rel += 5
        if benefit_query:
            doc_text = f"{title}\n{raw}".lower()
            rel += sum(6 for token in _BENEFIT_POSITIVE_HINTS if token in doc_text)
            rel -= sum(8 for token in _BENEFIT_NEGATIVE_HINTS if token in doc_text)
        return src_score + type_score + has_raw + url_score + rel

    docs.sort(key=_score, reverse=True)


def _merge_msg_records(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        mid = r.get("message_id") or ""
        if isinstance(mid, str) and mid:
            if mid in seen:
                continue
            seen.add(mid)
        merged.append(r)
    return merged


async def _enrich_docs_with_raw(open_id: str, docs: list[dict], *, top_n: int = _DOC_CONTENT_TOP_N) -> int:
    """对命中文档的前 N 篇并发拉原始正文，写入 doc['raw_content']。
    返回成功读到非空正文的数量。其他类型（sheet/bitable/slides）跳过。
    """
    targets: list[tuple[int, dict]] = []
    for idx, doc in enumerate(docs):
        if len(targets) >= max(1, top_n):
            break
        docs_type_value = doc.get("docs_type")
        docs_type = docs_type_value.lower() if isinstance(docs_type_value, str) else ""
        token = doc.get("docs_token") or doc.get("obj_token")
        if not isinstance(token, str) or not token:
            continue
        if str(doc.get("raw_content") or "").strip():
            continue
        if docs_type and docs_type not in ("doc", "docx"):
            continue
        targets.append((idx, doc))

    logger.debug(
        "enrich docs raw_content targets={} total_docs={}",
        len(targets),
        len(docs),
    )

    if not targets:
        return 0

    async def _fetch(doc: dict) -> tuple[str, str]:
        token = doc.get("docs_token") or doc.get("obj_token") or ""
        return await doc_client.safe_load_content(open_id, token)

    try:
        sem = asyncio.Semaphore(2)

        async def _guarded_fetch(doc: dict) -> tuple[str, str]:
            async with sem:
                return await _fetch(doc)

        # 给整批一个保险超时：单篇慢是常事，整批也不该超过几秒。
        results = await asyncio.wait_for(
            asyncio.gather(*[_guarded_fetch(doc) for _, doc in targets], return_exceptions=True),
            timeout=_DOC_CONTENT_FETCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "enrich docs raw_content timeout after {}s, skip", _DOC_CONTENT_FETCH_TIMEOUT
        )
        return 0
    except PermissionError:
        raise

    ok = 0
    for (_, doc), result in zip(targets, results):
        if isinstance(result, PermissionError):
            raise result
        if isinstance(result, Exception):
            logger.warning(
                "enrich docs raw_content failed for token={!r}: {}",
                doc.get("docs_token") or doc.get("obj_token"),
                result,
            )
            continue
        if isinstance(result, tuple) and len(result) == 2:
            content, status = result
            if status:
                doc["raw_content_error"] = status
            if isinstance(content, str) and content:
                doc["raw_content"] = content
                doc["raw_content_error"] = ""
                ok += 1
            continue
        if isinstance(result, str) and result:
            doc["raw_content"] = result
            doc["raw_content_error"] = ""
            ok += 1
    return ok


async def _step_messages(
    open_id: str, search_key: str, sources: list[str], current_chat_id: str | None
) -> list[dict]:
    # 1) 搜到 message_id 列表；2) 再逐条拉正文，让 LLM 能真的读到"安全问题找张三"这种原文。
    if not search_key:
        return []
    try:
        msg_ids = await search_client.search_messages(
            open_id, search_key, page_size=50, group_only=True
        )
    except PermissionError:
        raise
    except Exception:
        logger.exception("message search failed")
        return []
    if not msg_ids:
        return []

    try:
        enriched = await message_client.fetch_messages_text(open_id, msg_ids, limit=30)
    except PermissionError:
        raise
    except Exception:
        logger.exception("fetch message content failed")
        enriched = [{"message_id": mid, "text": "", "sender": {}, "msg_type": ""} for mid in msg_ids]

    def _looks_relevant(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if search_key and search_key not in t:
            compact_key = "".join(ch for ch in search_key if not ch.isspace())
            if compact_key and not all(ch in t for ch in compact_key):
                return False
        markers = ("找", "负责", "对接", "处理", "联系")
        return any(m in t for m in markers)

    kept = [
        m
        for m in enriched
        if isinstance(m, dict)
        and (not current_chat_id or str(m.get("chat_id") or "") != str(current_chat_id))
    ]
    if current_chat_id:
        logger.info(
            "message hits filtered by chat_id: current_chat_id={} total={} kept={}",
            current_chat_id,
            len([m for m in enriched if isinstance(m, dict)]),
            len(kept),
        )

    picked = [m for m in kept if _looks_relevant(str(m.get("text") or ""))]
    # 如果关键词太短导致“找人句式”过滤过严，则退回前几条，保证不丢命中线索。
    if not picked:
        if _is_benefit_query(search_key):
            picked = []
        else:
            picked = list(kept)
    picked = picked[:5]

    if picked:
        sources.append("聊天记录")
    return picked


async def _step_llm_synthesize(
    question: str,
    people: list[dict],
    docs: list[dict],
    msg_records: list[dict],
    sources: list[str],
    search_key: str,
    conversation_context: str = "",
) -> tuple[str, bool]:
    # 构建 open_id -> 可读姓名映射：LLM 看到文档创建者是"张三"比看到
    # "ou_xxx..."更能做跨素材关联（"这篇文档的创建者就是通讯录命中的那个人"）。
    # 数据只能来自本次通讯录命中，查不到的 owner_id 原样保留。
    id_to_name = _build_open_id_name_map(people)
    contacts_ctx = _format_contacts(people)
    docs_ctx = _format_docs(docs, id_to_name, search_key)
    messages_ctx = _format_messages(msg_records, id_to_name)

    try:
        answer = await llm_client.synthesize_person(
            question=question,
            contacts=contacts_ctx,
            docs=docs_ctx,
            messages=messages_ctx,
            conversation_context=conversation_context,
        )
        sources.append("LLM 推理")
        return answer, False
    except LLMTimeoutError:
        logger.warning("llm synthesize_person timeout, fallback to rule-based summary")
        return _fallback_summary(people, docs, msg_records, search_key), True
    except Exception:
        logger.exception("llm synthesize_person failed, fallback to rule-based summary")
        return _fallback_summary(people, docs, msg_records, search_key), False


def _format_contacts(people: list[dict]) -> str:
    if not people:
        return ""
    lines: list[str] = []
    # /open-apis/search/v1/user 不返回 email，只能用 name / open_id / department_ids。
    for person in people[:5]:
        name = person.get("name") or "未知"
        pid = person.get("open_id") or ""
        dept = ""
        dept_list = person.get("department_ids") or []
        if isinstance(dept_list, list) and dept_list:
            dept = f"，部门ID：{','.join(str(d) for d in dept_list[:3])}"
        lines.append(f"- {name}（open_id：{pid}{dept}）")
    return "\n".join(lines)


def _build_open_id_name_map(people: list[dict]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for person in people or []:
        pid = person.get("open_id")
        name = person.get("name")
        if isinstance(pid, str) and isinstance(name, str) and pid and name:
            mapping[pid] = name
    return mapping


def _describe_owner(owner_id: str, id_to_name: dict[str, str]) -> str:
    if not owner_id:
        return "未知"
    name = id_to_name.get(owner_id)
    if name:
        return f"{name}（open_id：{owner_id}）"
    return f"open_id：{owner_id}"


def _format_docs(docs: list[dict], id_to_name: dict[str, str], search_key: str) -> str:
    if not docs:
        return ""
    lines: list[str] = []
    for doc in docs[:5]:
        title = doc.get("title") or doc.get("name") or "未命名文档"
        owner = doc.get("owner_id") or ""
        docs_type = doc.get("docs_type") or "doc"
        lines.append(f"- [{docs_type}] {title}（创建者：{_describe_owner(owner, id_to_name)}）")
        raw = (doc.get("raw_content") or "").strip()
        if raw:
            # 围绕命中点取窗口，避免只把正文开头喂给 LLM。
            preview = _build_query_centered_preview(raw, search_key, _DOC_CONTENT_PREVIEW_CHARS)
            lines.append(f"  正文片段：{preview}")
    return "\n".join(lines)


def _format_messages(msg_records: list[dict], id_to_name: dict[str, str]) -> str:
    if not msg_records:
        return ""
    lines: list[str] = []
    for idx, msg in enumerate(msg_records[:5], start=1):
        text = (msg.get("text") or "").strip()
        sender = msg.get("sender") or {}
        sender_id = sender.get("id") or ""
        sender_type = sender.get("sender_type") or sender.get("id_type") or ""
        # sender.id 通常是 open_id（当 id_type=open_id 时），命中通讯录名单时给 LLM 展示姓名。
        sender_name = id_to_name.get(sender_id, "")
        sender_label = f"{sender_name}({sender_type}:{sender_id})" if sender_name else f"{sender_type}:{sender_id}"
        if text:
            # 限长，避免单条过长挤占 LLM context。
            preview = text if len(text) <= 400 else text[:400] + "…"
            lines.append(f"{idx}. [{sender_label}] {preview}")
        else:
            # 内容拉不回来或非文本类型，仍给 LLM 一个 fallback 占位。
            lines.append(
                f"{idx}. [{sender_label}] (未能读取正文，msg_type={msg.get('msg_type', '')})"
            )
    return "\n".join(lines)


