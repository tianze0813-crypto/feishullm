from datetime import datetime
from typing import Any

from config import settings


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


def _escape_md_link_text(text: str) -> str:
    s = (text or "").strip()
    s = s.replace("\\", "\\\\")
    s = s.replace("[", "\\[").replace("]", "\\]")
    s = s.replace("(", "\\(").replace(")", "\\)")
    return s


def _escape_md_link_url(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return ""
    s = s.replace(" ", "%20")
    s = s.replace("(", "%28").replace(")", "%29")
    return s


def _build_doc_url(doc: dict[str, Any]) -> str:
    url = doc.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()

    token = (
        str(doc.get("docs_token") or doc.get("obj_token") or doc.get("token") or "").strip()
    )
    if not token:
        return ""

    docs_type = str(doc.get("docs_type") or "").strip().lower()
    base = str(getattr(settings, "feishu_web_base_url", "") or "").strip().rstrip("/")
    if not base:
        base = "https://www.feishu.cn"

    path_map = {
        "docx": "docx",
        "doc": "docs",
        "sheet": "sheets",
        "bitable": "base",
        "mindnote": "mindnotes",
        "slides": "slides",
        "wiki": "wiki",
    }
    path = path_map.get(docs_type) or path_map.get("docx")
    return f"{base}/{path}/{token}"


def _truncate(text: str, *, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def _header(title: str, *, template: str | None = None) -> dict[str, Any]:
    h: dict[str, Any] = {"title": {"tag": "plain_text", "content": title}}
    if isinstance(template, str) and template:
        h["template"] = template
    return h


def build_notice_card(title: str, content: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header(title),
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }


def build_thinking_card(question: str) -> dict[str, Any]:
    q = _truncate(question, max_chars=60)
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("飞书智能助手"),
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"正在为你查找…\n\n> {q}"},
            ]
        },
    }


def _format_meta(entities: dict[str, Any] | None) -> str:
    if not entities:
        return ""
    meta = entities.get("_meta")
    if not isinstance(meta, dict):
        return ""

    queries = meta.get("queries") or []
    hit_query = meta.get("hit_query") or ""
    hits = meta.get("hits") or {}
    visibility = meta.get("visibility") or {}
    clarify_needed = bool(meta.get("clarify_needed"))
    clarify_reason = str(meta.get("clarify_reason") or "").strip()
    inherited_entities = meta.get("inherited_entities") or []

    lines: list[str] = []
    if isinstance(hit_query, str) and hit_query:
        lines.append(f"`关键词：{hit_query}`")
    if isinstance(queries, list) and queries:
        shown = [str(q) for q in queries if q]
        if shown:
            lines.append(f"`候选：{' / '.join(shown[:3])}`")

    messages_hit = 0
    if isinstance(hits, dict) and hits:
        def _int(v: Any) -> int:
            try:
                return int(v)
            except Exception:
                return 0

        wiki = _int(hits.get("wiki"))
        docs = _int(hits.get("docs"))
        bitable = _int(hits.get("bitable"))
        messages_hit = _int(hits.get("messages"))
        contacts = _int(hits.get("contacts"))
        parts: list[str] = []
        if contacts:
            parts.append(f"通讯录 {contacts}")
        if wiki:
            parts.append(f"知识库 {wiki}")
        if docs:
            parts.append(f"文档 {docs}")
        if bitable:
            parts.append(f"多维表格 {bitable}")
        if messages_hit:
            parts.append(f"聊天记录 {messages_hit}")
        if parts:
            lines.append(f"`命中：{' / '.join(parts)}`")

    if messages_hit:
        search_scope = "群聊 + 私聊" if settings.include_p2p_message_search else "仅群聊"
        lines.append(f"`消息范围：{search_scope}`")

    if isinstance(visibility, dict) and visibility:
        def _int(v: Any) -> int:
            try:
                return int(v)
            except Exception:
                return 0

        no_perm = _int(visibility.get("docs_no_permission"))
        unavailable = _int(visibility.get("docs_unavailable"))
        if no_perm or unavailable:
            segs: list[str] = []
            if no_perm:
                segs.append(f"{no_perm} 篇文档无权限读取正文")
            if unavailable:
                segs.append(f"{unavailable} 篇文档正文不可用")
            lines.append(f"`正文可见性：{'；'.join(segs)}`")
    if clarify_needed and clarify_reason:
        lines.append(f"`需补充信息：{clarify_reason}`")
    if isinstance(inherited_entities, list) and inherited_entities:
        shown = [str(item).strip() for item in inherited_entities if str(item or "").strip()]
        if shown:
            lines.append(f"`最近上下文实体：{' / '.join(shown[:3])}`")

    return "\n".join(lines).strip()


def _should_keep_doc(doc: dict[str, Any], answer: str) -> bool:
    title = str(doc.get("title") or doc.get("name") or "").strip()
    if not title:
        return False
    return title in answer


def _should_keep_person(person: dict[str, Any], answer: str) -> bool:
    name = str(person.get("name") or "").strip()
    if name:
        return name in answer
    return False


def _should_keep_message(msg: dict[str, Any], answer: str) -> bool:
    text = str(msg.get("text") or "").strip()
    if not text:
        return False
    short = text[:20]
    if short and short in answer:
        return True
    # 对“安全问题找张三处理一下”这类句子，至少按名字命中也保留。
    for i in range(max(0, len(text) - 1)):
        name = text[i : i + 2]
        if len(name) == 2 and "\u4e00" <= name[0] <= "\u9fff" and "\u4e00" <= name[1] <= "\u9fff":
            if name in answer:
                return True
    return False


def _filter_entities_for_display(
    entities: dict[str, list[dict[str, Any]]] | None, answer: str
) -> dict[str, list[dict[str, Any]]] | None:
    if not entities:
        return entities
    filtered: dict[str, list[dict[str, Any]]] = {}
    meta = entities.get("_meta") if isinstance(entities.get("_meta"), dict) else None
    label = str((meta or {}).get("label") or "").strip()
    self_check = (meta or {}).get("self_check") if isinstance((meta or {}).get("self_check"), dict) else {}
    force_keep_docs = (
        label == "find_person"
        and (
            not _safe_bool(self_check.get("consistency_ok"), default=True)
            or _safe_bool(self_check.get("should_downgrade"))
            or _safe_bool(self_check.get("should_ask_more"))
            or bool(str(self_check.get("answer_hint") or "").strip())
        )
    )
    force_keep_people = label == "find_person"
    docs = [d for d in (entities.get("docs") or []) if isinstance(d, dict) and _should_keep_doc(d, answer)]
    if force_keep_docs and not docs:
        docs = [d for d in (entities.get("docs") or []) if isinstance(d, dict)][:3]
    people = [
        p for p in (entities.get("people") or []) if isinstance(p, dict) and _should_keep_person(p, answer)
    ]
    if force_keep_people and not people:
        people = [p for p in (entities.get("people") or []) if isinstance(p, dict)][:3]
    messages = [
        m
        for m in (entities.get("messages") or [])
        if isinstance(m, dict) and _should_keep_message(m, answer)
    ]
    evidence = [
        e
        for e in (entities.get("evidence") or [])
        if isinstance(e, dict) and str(e.get("text") or "").strip()
    ]
    if docs:
        filtered["docs"] = docs
    if people:
        filtered["people"] = people
    if messages:
        filtered["messages"] = messages
    if evidence:
        filtered["evidence"] = evidence[:2]
    if meta:
        filtered["_meta"] = meta
    return filtered


def _format_entities(entities: dict[str, list[dict[str, Any]]] | None) -> str:
    if not entities:
        return ""

    people = entities.get("people") or []
    docs = entities.get("docs") or []
    messages = entities.get("messages") or []

    lines: list[str] = []
    if docs:
        doc_lines: list[str] = []
        for doc in docs[:3]:
            title = str(doc.get("title") or doc.get("name") or "未命名文档").strip()
            url = _build_doc_url(doc)
            raw_err = doc.get("raw_content_error") or ""
            suffix = "（无权限读取正文）" if raw_err == "no_permission" else ""
            if isinstance(url, str) and url:
                safe_title = _escape_md_link_text(title)
                safe_url = _escape_md_link_url(url)
                doc_lines.append(f"- [{safe_title}]({safe_url}){suffix}")
            else:
                doc_lines.append(f"- {title}{suffix}")
        lines.append("**参考文档**\n" + "\n".join(doc_lines))

    if people:
        people_lines: list[str] = []
        for person in people[:3]:
            name = str(person.get("name") or "未知").strip()
            open_id = person.get("open_id")
            if isinstance(open_id, str) and open_id:
                people_lines.append(f"- <at id={open_id}></at>")
            else:
                people_lines.append(f"- {name}")
        lines.append("**相关人员**\n" + "\n".join(people_lines))

    if messages:
        msg_lines: list[str] = []
        for msg in messages[:3]:
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            if len(text) > 120:
                text = text[:119] + "…"
            sender_id = str(msg.get("sender_id") or "").strip()
            sender_type = str(msg.get("sender_type") or "").strip()
            if sender_id.startswith("ou_"):
                msg_lines.append(f"- <at id={sender_id}></at>：{text}")
            elif sender_id:
                msg_lines.append(f"- {sender_type}:{sender_id}：{text}")
            else:
                msg_lines.append(f"- {text}")
        if msg_lines:
            lines.append("**相关聊天**\n" + "\n".join(msg_lines))

    return "\n\n".join(lines).strip()


def build_result_card(
    question: str,
    answer: str,
    sources: list[str],
    entities: dict[str, list[dict[str, Any]]] | None = None,
    *,
    topic: str | None = None,
    session_id: str | None = None,
    turns: int | None = None,
    evidence_expanded: bool = False,
) -> dict[str, Any]:
    display_entities = _filter_entities_for_display(entities, answer)
    meta_md = _format_meta(display_entities)
    meta_elements = [{"tag": "markdown", "content": meta_md}] if meta_md else []
    entities_md = _format_entities(display_entities)
    entities_elements = [{"tag": "markdown", "content": entities_md}] if entities_md else []
    evidence_elements = _build_evidence_elements(display_entities, expanded=evidence_expanded)
    meta = (display_entities or {}).get("_meta") if isinstance(display_entities, dict) else None
    label = (meta or {}).get("label") if isinstance(meta, dict) else ""
    hit_query = (meta or {}).get("hit_query") if isinstance(meta, dict) else ""
    queries = (meta or {}).get("queries") if isinstance(meta, dict) else []
    keyword_fallback = (meta or {}).get("keyword_fallback") if isinstance(meta, dict) else ""
    exact_match = bool((meta or {}).get("exact_match")) if isinstance(meta, dict) else False
    clarify_needed = bool((meta or {}).get("clarify_needed")) if isinstance(meta, dict) else False
    clarify_reason = str((meta or {}).get("clarify_reason") or "").strip() if isinstance(meta, dict) else ""
    clarify_options = (meta or {}).get("clarify_options") if isinstance(meta, dict) else []

    is_no_hit = False
    if sources:
        is_no_hit = any("未命中" in str(s) for s in sources)
    if _answer_suggests_general_chat(answer):
        is_no_hit = True
    if clarify_needed:
        is_no_hit = True

    retry_candidates: list[str] = []
    if isinstance(queries, list):
        for q in queries:
            if not q:
                continue
            qs = str(q).strip()
            if not qs:
                continue
            if isinstance(hit_query, str) and hit_query and qs == hit_query:
                continue
            if qs in retry_candidates:
                continue
            retry_candidates.append(qs)
    if isinstance(keyword_fallback, str) and keyword_fallback.strip():
        fb = keyword_fallback.strip()
        if fb not in retry_candidates and fb != (hit_query or ""):
            retry_candidates.insert(0, fb)
    retry_candidates = retry_candidates[:2]
    if clarify_needed:
        retry_candidates = []
    topic_line = ""
    if isinstance(topic, str) and topic.strip():
        t = topic.strip()
        sid = (session_id or "").strip()
        sid_part = f" / {sid[:8]}" if sid else ""
        turns_part = f" / {int(turns)}轮" if isinstance(turns, int) and turns >= 0 else ""
        topic_line = f"**当前话题**：{t}{sid_part}{turns_part}"
    team_summary_question = _is_team_summary_question(question, meta if isinstance(meta, dict) else None)

    next_actions: list[dict[str, Any]] = []
    if retry_candidates:
        next_actions.append({"tag": "hr"})
        next_actions.append({"tag": "markdown", "content": "**下一步**：换个关键词快速重试"})
        for cand in retry_candidates:
            next_actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"用「{cand}」重试"},
                    "type": "primary",
                    "value": {
                        "action": "retry_with_query",
                        "label": str(label or "search_knowledge"),
                        "question": question,
                        "keyword": cand,
                    },
                }
            )
    if clarify_needed:
        next_actions.append({"tag": "hr"})
        next_actions.append({"tag": "markdown", "content": "可在下方选择群组，或接入默认 Wiki 后重试"})
        next_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "选择群组"},
                "type": "primary",
                "value": {
                    "action": "open_knowledge_chat_selector",
                },
            }
        )
        next_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "接入默认Wiki并重试"},
                "type": "default",
                "value": {
                    "action": "attach_default_wiki",
                    "question": question,
                },
            }
        )
    elif is_no_hit:
        if label == "search_knowledge":
            next_actions.append({"tag": "hr"})
            next_actions.append(
                {
                    "tag": "markdown",
                    "content": "可在下方选择群组，或接入默认 Wiki 后重试",
                }
            )
            next_actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "选择群组"},
                    "type": "primary",
                    "value": {
                        "action": "open_knowledge_chat_selector",
                    },
                }
            )
            next_actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "接入默认Wiki并重试"},
                    "type": "default",
                    "value": {
                        "action": "attach_default_wiki",
                        "question": question,
                    },
                }
            )
        elif label == "find_person":
            next_actions.append({"tag": "hr"})
            next_actions.append(
                {
                    "tag": "markdown",
                    "content": (
                        "**下一步**\n"
                        "- 建议补充系统/流程名、部门/业务线或更具体的职责词\n"
                        "- 我会按补充信息重新检索并给出负责人候选与依据"
                    ),
                }
            )
            next_actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "按模板补充信息"},
                    "type": "primary",
                    "value": {
                        "action": "triage_template",
                        "label": str(label or "find_person"),
                        "question": question,
                    },
                }
            )
            next_actions.append(
                {
                    "tag": "markdown",
                    "content": (
                        "**通识问答说明**\n"
                        "- 找人问题不触发通识问答\n"
                        "- 负责人、联系人和归属判断只依据内部资料与检索证据"
                    ),
                }
            )

    suffix = "答复"
    template = "blue"
    if label == "find_person":
        suffix = "负责人"
        template = "green"
    elif label == "search_knowledge":
        suffix = "知识"
        template = "blue"
    elif label == "chitchat":
        suffix = "对话"
        template = "turquoise"

    answer_md = _truncate(answer, max_chars=1200)
    qa_md = f"**结论**\n{answer_md}\n\n> **问题**：{_truncate(question, max_chars=200)}"
    if label == "chitchat":
        qa_md = (
            "> **说明**：以下为通用知识回答，未基于内部知识库；涉及负责人、内部流程或组内规则时，请以内部资料为准。\n\n"
            + qa_md
        )
    if isinstance(hit_query, str) and hit_query and not exact_match:
        qa_md += f"\n> **命中方式**：按近似关键词 `{hit_query}` 命中"

    show_group_selector_in_footer = label == "search_knowledge" and not (clarify_needed or is_no_hit)

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header(f"飞书智能助手 · {suffix}", template=template),
        "body": {
            "elements": [
                {"tag": "markdown", "content": qa_md},
                *evidence_elements,
                *(
                    [{"tag": "hr"}, *meta_elements]
                    if meta_elements
                    else []
                ),
                *(
                    [{"tag": "hr"}, *entities_elements]
                    if entities_elements
                    else []
                ),
                *next_actions,
                {"tag": "hr"},
                *(
                    [
                        {"tag": "markdown", "content": topic_line},
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看历史会话"},
                            "type": "default",
                            "value": {"action": "list_topics"},
                        },
                        *(
                            [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "选择群组"},
                                    "type": "default",
                                    "value": {"action": "open_knowledge_chat_selector"},
                                }
                            ]
                            if show_group_selector_in_footer
                            else []
                        ),
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "开启新话题"},
                            "type": "default",
                            "value": {"action": "new_topic"},
                        },
                    ]
                    if topic_line
                    else [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看历史会话"},
                            "type": "default",
                            "value": {"action": "list_topics"},
                        },
                        *(
                            [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "选择群组"},
                                    "type": "default",
                                    "value": {"action": "open_knowledge_chat_selector"},
                                }
                            ]
                            if show_group_selector_in_footer
                            else []
                        ),
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "开启新话题"},
                            "type": "default",
                            "value": {"action": "new_topic"},
                        }
                    ]
                ),
            ]
        },
    }


def _build_evidence_elements(
    entities: dict[str, list[dict[str, Any]]] | None, *, expanded: bool
) -> list[dict[str, Any]]:
    if not entities:
        return []
    meta = entities.get("_meta") if isinstance(entities.get("_meta"), dict) else {}
    label = str((meta or {}).get("label") or "").strip()
    if label != "find_person":
        return []
    evidence = entities.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        return []
    top = evidence[0] if isinstance(evidence[0], dict) else {}
    text = str(top.get("text") or "").strip()
    if not text:
        return []
    source_type = str(top.get("source_type") or "").strip() or "证据"
    title = str(top.get("title") or "").strip()
    url = str(top.get("url") or "").strip()

    short = text if len(text) <= 70 else text[:69].rstrip() + "…"
    shown = text if expanded else short

    source_line = f"- 来源：{source_type}"
    if title:
        if url:
            safe_title = _escape_md_link_text(title)
            safe_url = _escape_md_link_url(url)
            source_line += f"《[{safe_title}]({safe_url})》"
        else:
            source_line += f"《{title}》"

    state_note = "（已展开）" if expanded else "（已收起，约200字上下文）"
    md = "**依据**\n" + source_line + f"{state_note}\n- 正文片段：\n> {shown}"

    button_text = "收起依据" if expanded else "展开依据"
    button_value = {"action": "toggle_evidence", "expanded": "false" if expanded else "true"}
    return [
        {"tag": "hr"},
        {"tag": "markdown", "content": md},
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": button_text},
            "type": "default",
            "value": button_value,
        },
    ]


def _answer_suggests_general_chat(answer: str) -> bool:
    text = str(answer or "").strip()
    if not text:
        return True
    if text.startswith("暂时没有在") or text.startswith("没有在通讯录"):
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


def _is_team_summary_question(question: str, meta: dict[str, Any] | None) -> bool:
    q = str(question or "").strip()
    if not q:
        return False
    if not bool((meta or {}).get("summary_mode")):
        return False
    team_markers = ("我们组", "本组", "我们团队", "本团队", "我们部门", "本部门")
    return any(marker in q for marker in team_markers)


def build_oauth_card(authorize_url: str, pending_question: str | None = None) -> dict[str, Any]:
    question_part = ""
    if isinstance(pending_question, str) and pending_question.strip():
        q = _truncate(pending_question, max_chars=80)
        question_part = f"\n\n授权后将自动继续处理：`{q}`"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("飞书智能助手 · 需要授权", template="red"),
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "为了按你的实际权限检索云文档、群消息和知识库内容，需要先完成一次飞书授权。\n\n"
                        "**授权后可用：**\n"
                        "- 查知识库/云文档/多维表格\n"
                        "- 搜群聊消息（可选包含私聊）\n\n"
                        "**不授权也能用：**\n"
                        "- 基础说明与简单问答（不检索）"
                        f"{question_part}"
                    ),
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "立即授权"},
                    "type": "primary",
                    "url": authorize_url,
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "暂不授权（基础模式）"},
                    "type": "default",
                    "value": {"action": "skip_oauth"},
                },
            ]
        },
    }


def build_topic_list_card(sessions: list[Any]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if not sessions:
        elements.append({"tag": "markdown", "content": "暂无历史话题。"})
    else:
        for idx, sess in enumerate(sessions[:10], 1):
            session_id = str(getattr(sess, "session_id", "") or "")
            topic = str(getattr(sess, "topic", "") or "") or "未命名话题"
            turns = getattr(sess, "turns", None)
            turns_part = f"（{int(turns)}轮）" if isinstance(turns, int) and turns >= 0 else ""
            last_user_text = str(getattr(sess, "last_user_text", "") or "").strip()
            ts_value = getattr(sess, "last_user_ts", None)
            if not isinstance(ts_value, (int, float)) or ts_value <= 0:
                ts_value = getattr(sess, "updated_at", 0.0)
            time_part = ""
            if isinstance(ts_value, (int, float)) and ts_value > 0:
                try:
                    time_part = datetime.fromtimestamp(float(ts_value)).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    time_part = ""
            header = f"**{idx}. {topic}** {turns_part}"
            if session_id:
                header += f"  `{session_id[:8]}`"
            if time_part:
                header += f"\n更新时间：{time_part}"
            if last_user_text:
                if len(last_user_text) > 60:
                    last_user_text = last_user_text[:59] + "…"
                header += f"\n最近提问：{last_user_text}"
            elements.append({"tag": "markdown", "content": header})
            if session_id:
                elements.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "切换到该话题"},
                        "type": "primary",
                        "value": {"action": "switch_topic", "session_id": session_id},
                    }
                )
            if idx != min(len(sessions), 10):
                elements.append({"tag": "hr"})
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "开启新话题"},
            "type": "default",
            "value": {"action": "new_topic"},
        }
    )
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("飞书智能助手 · 历史会话", template="purple"),
        "body": {"elements": elements},
    }


def build_knowledge_chat_selector_card(
    chats: list[dict[str, Any]],
    selected_chat_ids: list[str],
    *,
    offset: int = 0,
    page_size: int = 8,
    external_sources: list[str] | None = None,
) -> dict[str, Any]:
    selected = [str(x or "").strip() for x in (selected_chat_ids or []) if str(x or "").strip()]
    selected_set = set(selected)
    elements: list[dict[str, Any]] = []
    normalized_chats = [c for c in (chats or []) if isinstance(c, dict)]
    name_lookup: dict[str, str] = {}
    for chat in normalized_chats:
        chat_id = str(chat.get("chat_id") or "").strip()
        if not chat_id:
            continue
        name = str(chat.get("name") or chat.get("title") or "").strip() or "未命名群聊"
        name_lookup[chat_id] = name

    if selected:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**当前范围**：仅已选群聊（{len(selected)} 个）",
            }
        )
    else:
        elements.append({"tag": "markdown", "content": "**当前范围**：全部群聊"})

    elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "使用全部群聊"}, "type": "default", "value": {"action": "knowledge_chat_set_all", "offset": int(offset)}})
    normalized_external = [str(item or "").strip() for item in (external_sources or []) if str(item or "").strip()]
    if normalized_external:
        elements.append(
            {
                "tag": "markdown",
                "content": "**已接入外接知识**：\n" + "\n".join(f"- {item}" for item in normalized_external[:3]),
            }
        )
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "清除外接知识"},
                "type": "default",
                "value": {"action": "clear_external_knowledge"},
            }
        )
    else:
        elements.append({"tag": "markdown", "content": "**已接入外接知识**：未接入"})
    elements.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "接入默认Wiki"},
            "type": "default",
            "value": {"action": "attach_default_wiki"},
        }
    )
    if selected:
        elements.append(
            {
                "tag": "markdown",
                "content": "已选（最多展示 6 个）："
                + "\n"
                + "\n".join(f"- {name_lookup.get(cid, '未命名群聊')}" for cid in selected[:6]),
            }
        )
    elements.append({"tag": "hr"})

    start = max(0, int(offset))
    size = max(1, min(int(page_size), 10))
    page = normalized_chats[start : start + size]
    if not page:
        elements.append(
            {
                "tag": "markdown",
                "content": "暂无可选群聊。\n\n请先在目标群里 @我 发一句话，再回来设置。",
            }
        )
    else:
        for idx, chat in enumerate(page, start=start + 1):
            chat_id = str(chat.get("chat_id") or "").strip()
            if not chat_id:
                continue
            name = name_lookup.get(chat_id, "未命名群聊")
            is_selected = chat_id in selected_set
            elements.append(
                {
                    "tag": "markdown",
                    "content": f"**{idx}. {name}**",
                }
            )
            elements.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消选择" if is_selected else "选择"},
                    "type": "default" if is_selected else "primary",
                    "value": {
                        "action": "knowledge_chat_toggle",
                        "chat_id": chat_id,
                        "offset": int(offset),
                    },
                }
            )

    has_prev = start > 0
    has_next = start + size < len(normalized_chats)
    if has_prev or has_next:
        elements.append({"tag": "hr"})
    if has_prev:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "上一页"},
                "type": "default",
                "value": {"action": "knowledge_chat_page", "offset": max(0, start - size)},
            }
        )
    if has_next:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "下一页"},
                "type": "default",
                "value": {"action": "knowledge_chat_page", "offset": start + size},
            }
        )

    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "markdown",
            "content": "提示：这里默认展示你最近和机器人互动过的群聊。",
        }
    )
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("飞书智能助手 · 群聊范围", template="purple"),
        "body": {"elements": elements},
    }
