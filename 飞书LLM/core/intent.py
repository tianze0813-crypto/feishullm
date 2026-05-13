from __future__ import annotations

import json
from dataclasses import dataclass
import re

from utils.logger import get_logger

logger = get_logger()

VALID_INTENTS = {"find_person", "search_knowledge", "chitchat"}

# 飞书搜索是「多 token AND 匹配」，以下这些疑问词/虚词会稀释相关度，
# LLM JSON 解析失败降级时用这份清单做最小力度的规则去噪。
_FIND_PERSON_NOISE = (
    "找谁处理",
    "找谁对接",
    "该找谁",
    "找谁",
    "是谁",
    "谁负责",
    "谁对接",
    "归谁",
    "对接人",
    "对接",
    "处理一下",
    "处理",
    "负责人",
    "负责",
    "该",
)
_KNOWLEDGE_NOISE = (
    "是什么",
    "怎么办",
    "怎么做",
    "怎么走",
    "怎么",
    "咋办",
    "如何",
    "请问",
    "一下",
    "是啥",
    "是怎样",
    "是怎么",
)
_TAIL_PARTICLES = ("的", "吗", "呢", "啊", "呀", "哈", "哦", "嘛", "呗")

_FIND_PERSON_MARKERS = ("找谁", "谁负责", "谁对接", "归谁", "谁管", "对接人", "找 ", "归属")
_KNOWLEDGE_MARKERS = ("是什么", "怎么办", "怎么做", "怎么走", "如何", "流程", "规定", "政策", "SOP", "规范")

_JSON_DECODER = json.JSONDecoder()
_RE_MULTI_SPACE = re.compile(r"\s+")
_RE_CJK_START = re.compile(r"^\s*[\u4e00-\u9fff]")
_RE_CJK_ANY = re.compile(r"[\u4e00-\u9fff]")
_RE_ASCII_VERSION_SUFFIX = re.compile(r"(?i)([a-z][a-z_-]*)(\d+)(?=$|[^a-z0-9])")
# 不能用 \b：在 Python 的 Unicode 语义里，中文也属于“单词字符”，
# 所以“安装CVAT”这种中英直接粘连的场景里，\b 无法把 CVAT 识别出来。
_RE_ASCII_TERM = re.compile(r"(?i)(?<![a-z0-9._-])[a-z][a-z0-9._-]*(?![a-z0-9._-])")

_QUERY_NOISE = (
    "流程",
    "系统",
    "平台",
    "入口",
    "链接",
    "地址",
    "页面",
    "在哪",
    "在哪里",
    "怎么进",
    "怎么进入",
    "如何进入",
)

_ENTERPRISE_VARIANT_TO_CANONICAL: dict[str, str] = {
    "oa": "审批",
    "o a": "审批",
    "oa审批": "审批",
    "飞书审批": "审批",
    "审批流": "审批",
    "流程审批": "审批",
    "工单": "服务台",
    "it工单": "服务台",
    "服务台工单": "服务台",
    "it服务台": "服务台",
    "helpdesk": "服务台",
    "service desk": "服务台",
    "sso": "单点登录",
    "单点": "单点登录",
    "单点登陆": "单点登录",
    "mfa": "多因子认证",
    "2fa": "多因子认证",
    "双因子": "多因子认证",
    "vpn": "vpn",
    "内网": "vpn",
    "堡垒机": "堡垒机",
    "账号": "账户",
    "帐号": "账户",
    "账户权限": "权限",
    "权限申请": "权限",
    "权限开通": "权限",
    "权限审批": "权限",
    "开权限": "权限",
    "申请权限": "权限",
    "密码": "密码",
    "重置密码": "密码重置",
    "改密码": "密码修改",
    "找回密码": "密码重置",
    "入职": "入职",
    "入职办理": "入职",
    "入职流程": "入职",
    "转正": "转正",
    "离职": "离职",
    "离职办理": "离职",
    "离职流程": "离职",
    "请假": "请假",
    "休假": "请假",
    "年假": "请假",
    "病假": "请假",
    "加班": "加班",
    "考勤": "考勤",
    "打卡": "考勤",
    "补卡": "考勤补卡",
    "补打卡": "考勤补卡",
    "出差": "出差",
    "差旅": "出差",
    "差旅申请": "出差",
    "报销": "报销",
    "费用报销": "报销",
    "差旅报销": "报销",
    "发票": "发票",
    "开票": "发票",
    "合同": "合同",
    "用印": "用印",
    "盖章": "用印",
    "采购": "采购",
    "采购申请": "采购",
    "资产": "资产",
    "领用": "资产领用",
    "借用": "资产借用",
    "固定资产": "资产",
    "门禁": "门禁",
    "工卡": "门禁",
    "访客": "访客",
    "访客预约": "访客",
    "会议室": "会议室",
    "订会议室": "会议室预定",
    "会议室预订": "会议室预定",
    "预定会议室": "会议室预定",
    "邮箱": "邮箱",
    "企业邮箱": "邮箱",
    "邮件": "邮箱",
    "日历": "日历",
    "会议": "日历",
    "视频会议": "视频会议",
    "飞书会议": "视频会议",
    "网络": "网络",
    "wifi": "wifi",
    "wi-fi": "wifi",
    "无线网": "wifi",
    "打印": "打印",
    "打印机": "打印",
    "法务": "法务",
    "合规": "合规",
    "审计": "合规",
    "保密": "保密",
    "安全": "安全",
    "培训": "培训",
    "学习": "培训",
    "绩效": "绩效",
    "okr": "okr",
    "kpi": "kpi",
    "招聘": "招聘",
    "面试": "招聘",
}

_ENTERPRISE_CANONICAL_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "审批": ("审批", "审批流程", "飞书审批", "OA审批"),
    "服务台": ("服务台", "IT服务台", "IT工单", "工单"),
    "权限": ("权限", "权限申请", "权限开通", "账号权限"),
    "密码重置": ("密码重置", "重置密码", "找回密码"),
    "密码修改": ("密码修改", "改密码", "修改密码"),
    "单点登录": ("单点登录", "SSO", "单点"),
    "多因子认证": ("多因子认证", "MFA", "2FA"),
    "vpn": ("VPN", "内网", "远程办公"),
    "考勤": ("考勤", "打卡", "签到"),
    "考勤补卡": ("补卡", "补打卡", "考勤补卡"),
    "出差": ("出差", "差旅", "差旅申请"),
    "报销": ("报销", "费用报销", "差旅报销"),
    "发票": ("发票", "开票", "发票申请"),
    "合同": ("合同", "合同审批", "合同归档"),
    "用印": ("用印", "盖章", "印章"),
    "采购": ("采购", "采购申请", "采购审批"),
    "资产": ("资产", "固定资产", "资产盘点"),
    "资产领用": ("资产领用", "领用", "领用申请"),
    "资产借用": ("资产借用", "借用", "借用申请"),
    "门禁": ("门禁", "工卡", "门禁卡"),
    "访客": ("访客", "访客预约", "访客申请"),
    "会议室": ("会议室", "会议室预定", "会议室预订"),
    "日历": ("日历", "会议安排", "会议通知"),
    "视频会议": ("视频会议", "飞书会议", "会议"),
    "邮箱": ("邮箱", "企业邮箱", "邮件"),
    "wifi": ("WiFi", "无线网", "网络"),
    "合规": ("合规", "审计", "风控"),
    "保密": ("保密", "信息安全"),
    "绩效": ("绩效", "OKR", "KPI"),
    "招聘": ("招聘", "面试", "入职"),
}


@dataclass
class IntentResult:
    """一次意图识别的完整产出。

    Attributes:
        label: find_person / search_knowledge / chitchat
        keyword: 剔除疑问词/虚词后的核心名词短语，作为飞书搜索 API 的 search_key。
        keyword_fallback: 更短/更宽的备用关键词；主 keyword 搜到零命中时用它重试一次。
        person_hint: 问题里显式出现的人名（2~4 字中文），仅 find_person 场景常用。
        raw_question: 原始问题，兜底或日志使用。
    """

    label: str
    keyword: str
    person_hint: str
    raw_question: str
    keyword_fallback: str = ""

    @property
    def search_key(self) -> str:
        return self.keyword or self.raw_question


async def detect_intent(question: str, conversation_context: str = "") -> IntentResult:
    """调用 LLM 做意图分类 + 关键词抽取。
    若 LLM 返回的 JSON 解析失败或字段非法，降级到规则抽取（保证流水线不崩）。
    """
    try:
        from llm.client import llm_client

        raw = await llm_client.classify_intent(question, conversation_context=conversation_context)
    except Exception:
        logger.exception("classify_intent llm call failed, fallback to rule-based")
        return _rule_based_intent(question)

    parsed = _parse_llm_intent(raw, question)
    if parsed is None:
        logger.warning("classify_intent llm output invalid, fallback to rule-based: {!r}", raw)
        return _rule_based_intent(question)

    logger.info(
        "intent parsed label={} keyword={!r} keyword_fallback={!r} person_hint={!r}",
        parsed.label,
        parsed.keyword,
        parsed.keyword_fallback,
        parsed.person_hint,
    )
    return parsed


def build_query_candidates(
    label: str,
    keyword: str,
    keyword_fallback: str,
    extra_candidates: list[str] | None = None,
) -> list[str]:
    candidates: list[str] = []
    if keyword:
        candidates.append(keyword)
    if keyword_fallback and _is_safe_keyword_fallback(keyword, keyword_fallback) and keyword_fallback not in candidates:
        candidates.append(keyword_fallback)
    for item in extra_candidates or []:
        text = str(item or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    expanded: list[str] = []
    for q in list(candidates):
        expanded.extend(_expand_enterprise_queries(q))
    for q in expanded:
        if q and q not in candidates:
            candidates.append(q)

    if label == "find_person":
        for q in _expand_suffix_queries(candidates, _FIND_PERSON_SUFFIXES):
            if q and q not in candidates:
                candidates.append(q)
    elif label == "search_knowledge":
        for q in _expand_suffix_queries(candidates, _KNOWLEDGE_SUFFIXES):
            if q and q not in candidates:
                candidates.append(q)

    english_first: list[str] = []
    for q in list(candidates):
        if not _query_has_ascii_and_cjk(q):
            continue
        for term in _extract_ascii_terms(q):
            if term and term not in english_first:
                english_first.append(term)
            relaxed = _relax_versioned_ascii_terms(term)
            if relaxed and relaxed not in english_first:
                english_first.append(relaxed)
    ordered = english_first + candidates

    ranked = sorted(
        ordered,
        key=lambda s: (
            0 if (_query_is_ascii_only(s) and _query_has_ascii_and_cjk(keyword)) else 1,
            0 if s == keyword else 1,
            0 if (_query_is_ascii_only(s) and len(s) <= 10) else 1,
            0 if len(s) <= 6 else 1,
            len(s),
        ),
    )
    out: list[str] = []
    max_out = 1 if label == "chitchat" else (8 if label in {"find_person", "search_knowledge"} else 6)
    for q in ranked:
        qn = _normalize_query(q)
        if not qn:
            continue
        if qn in (_normalize_query(x) for x in out):
            continue
        out.append(q.strip())
        if len(out) >= max_out:
            break
    return out


_FIND_PERSON_SUFFIXES = ("负责人", "归口", "对接人", "接口人", "联系人", "支持", "答疑", "咨询")
_KNOWLEDGE_SUFFIXES = ("SOP", "指引", "规范", "制度", "手册", "FAQ", "流程", "操作指南", "使用说明", "报销", "政策")


def _expand_suffix_queries(terms: list[str], suffixes: tuple[str, ...]) -> list[str]:
    if not terms or not suffixes:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        base = str(term or "").strip()
        if not base:
            continue
        base_norm = _normalize_query(base)
        if any(_normalize_query(suf) in base_norm for suf in suffixes):
            continue
        for suf in suffixes:
            suf_text = str(suf or "").strip()
            if not suf_text:
                continue
            q = f"{base} {suf_text}".strip()
            qn = _normalize_query(q)
            if not qn or qn in seen:
                continue
            seen.add(qn)
            out.append(q)
            if len(out) >= 6:
                return out
    return out


def _normalize_query(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.replace("（", "(").replace("）", ")")
    s = _RE_MULTI_SPACE.sub(" ", s)
    return s.strip()


def _strip_query_noise(text: str) -> str:
    out = text
    for token in _QUERY_NOISE:
        out = out.replace(token, "")
    return out.strip()


def _expand_enterprise_queries(query: str) -> list[str]:
    if not query:
        return []
    q0 = _normalize_query(query)
    q0 = _strip_query_noise(q0)
    variants: list[str] = [query.strip()]
    if q0 and q0 != _normalize_query(query):
        variants.append(q0)

    relaxed = _relax_versioned_ascii_terms(query)
    if relaxed and relaxed not in variants:
        variants.append(relaxed)
    if q0:
        relaxed_normalized = _relax_versioned_ascii_terms(q0)
        if relaxed_normalized and relaxed_normalized not in variants:
            variants.append(relaxed_normalized)

    hits: list[tuple[str, str]] = []
    for variant, canonical in _ENTERPRISE_VARIANT_TO_CANONICAL.items():
        if _variant_matches_query(q0, variant):
            hits.append((variant, canonical))
    for variant, canonical in hits[:3]:
        replaced = q0.replace(variant, canonical)
        if replaced and replaced not in variants:
            variants.append(replaced)
        variants.append(canonical)

    canonicals = []
    for s in list(variants):
        sn = _normalize_query(s)
        if sn in _ENTERPRISE_VARIANT_TO_CANONICAL:
            canonicals.append(_ENTERPRISE_VARIANT_TO_CANONICAL[sn])
    for c in canonicals[:3]:
        expansions = _ENTERPRISE_CANONICAL_EXPANSIONS.get(c) or ()
        for e in expansions:
            en = str(e).strip()
            if en and en not in variants:
                variants.append(en)

    short = _strip_query_noise(q0)
    if short and short not in variants:
        variants.append(short)

    deduped: list[str] = []
    seen: set[str] = set()
    for v in variants:
        vn = _normalize_query(v)
        if not vn or vn in seen:
            continue
        seen.add(vn)
        deduped.append(v.strip())
    return deduped[:8]


def _variant_matches_query(query_normalized: str, variant: str) -> bool:
    q = (query_normalized or "").strip()
    v = _normalize_query(variant)
    if not q or not v:
        return False
    if q == v:
        return True
    # 中文业务词扩展要保守，避免“信息安全问题 -> 安全”这种串域。
    # 这类别名主要依赖清洗后的“精确短语”命中，不再对纯中文做 substring 扩展。
    if _contains_cjk(q) and _contains_cjk(v):
        return False
    return v in q


def _parse_llm_intent(raw: str, question: str) -> IntentResult | None:
    if not raw:
        return None
    text = raw.strip()
    # 用 raw_decode 而不是贪婪 regex：遇到 LLM 在 JSON 前后加了解释文字、
    # 或返回了多个 JSON 片段时，只取第一个合法 JSON 对象、忽略尾部杂项。
    data = _extract_first_json_object(text)
    if not isinstance(data, dict):
        return None

    label = str(data.get("intent") or "").strip().lower()
    if label not in VALID_INTENTS:
        return None
    keyword = str(data.get("keyword") or "").strip()
    keyword_fallback = str(data.get("keyword_fallback") or "").strip()
    person_hint = str(data.get("person_hint") or "").strip()

    # 额外保护：LLM 偶尔会把噪声带回来，再做一次极轻量的去噪。
    keyword = _strip_noise(keyword, label)
    keyword_fallback = _strip_noise(keyword_fallback, label)
    # fallback 等于 keyword 或非闲聊场景下是 keyword 超集的话，没价值，清掉。
    if keyword_fallback and keyword and (keyword_fallback == keyword or keyword in keyword_fallback):
        keyword_fallback = ""
    if label != "chitchat" and not keyword_fallback and keyword and _RE_CJK_START.match(keyword):
        keyword_fallback = _derive_rule_fallback(keyword)

    if label == "chitchat":
        # 闲聊不做搜索，关键词一律清空，避免下游误用。
        keyword = ""
        keyword_fallback = ""
        person_hint = ""
    elif not keyword and not person_hint:
        # 非闲聊意图却没有可用搜索词也没有人名——等于没法执行任何检索，
        # 硬走 find_person/search_knowledge 会拿 raw_question 回到"长问句稀释"状态，
        # 降级为 chitchat 给用户一个明确的"无法理解"回复更诚实。
        logger.info(
            "llm intent {} with empty keyword/person_hint, degrade to chitchat", label
        )
        label = "chitchat"
        keyword_fallback = ""

    return IntentResult(
        label=label,
        keyword=keyword,
        person_hint=person_hint,
        raw_question=question,
        keyword_fallback=keyword_fallback,
    )


def _extract_first_json_object(text: str) -> object | None:
    """从任意文本里提取第一个合法 JSON 对象；失败返回 None。

    相比 `re.search(r"\\{.*\\}")` 的贪婪匹配，这里能正确处理：
    - JSON 前后带解释文字：「好的，这是结果：{...}。」
    - 多段 JSON 拼接：「{a:1} 见上 {b:2}」只取第一个
    - 嵌套 JSON：decoder 内部本来就有括号栈匹配
    """
    start = 0
    while True:
        idx = text.find("{", start)
        if idx == -1:
            return None
        try:
            obj, _ = _JSON_DECODER.raw_decode(text[idx:])
            return obj
        except json.JSONDecodeError:
            # 这个 { 后面不是合法 JSON，往后继续找下一个候选位置。
            start = idx + 1


def _rule_based_intent(question: str) -> IntentResult:
    """LLM 不可用时的保守降级：
    - 用关键字粗判意图
    - 用 noise 清单削掉疑问词，剩下的作为 keyword
    - fallback 用截断到 2 个中文字符的版本（粗暴但比没有好）
    """
    q = question.strip()
    if not q:
        return IntentResult(label="chitchat", keyword="", person_hint="", raw_question=question)

    if any(mk in q for mk in _FIND_PERSON_MARKERS):
        label = "find_person"
    elif any(mk in q for mk in _KNOWLEDGE_MARKERS):
        label = "search_knowledge"
    else:
        label = "chitchat"

    keyword = "" if label == "chitchat" else _strip_noise(q, label)
    fallback = _derive_rule_fallback(keyword) if label != "chitchat" else ""
    return IntentResult(
        label=label,
        keyword=keyword,
        person_hint="",
        raw_question=question,
        keyword_fallback=fallback,
    )


def _derive_rule_fallback(keyword: str) -> str:
    """只做安全的兜底：
    - 英文技术词版本号回退，如 ros2 -> ros
    - 不再做中文关键词截短，避免“信息安全 -> 信息/安全”这类串域
    """
    return _relax_versioned_ascii_terms(keyword)


def _strip_noise(text: str, label: str) -> str:
    if not text:
        return ""
    out = text
    if label == "find_person":
        for token in _FIND_PERSON_NOISE:
            out = out.replace(token, "")
    elif label == "search_knowledge":
        for token in _KNOWLEDGE_NOISE:
            out = out.replace(token, "")
    for tail in _TAIL_PARTICLES:
        if out.endswith(tail):
            out = out[: -len(tail)]
    out = out.strip(" ，。,.?!？！")
    return out


def _contains_cjk(text: str) -> bool:
    return bool(_RE_CJK_ANY.search(text or ""))


def _query_has_ascii_and_cjk(text: str) -> bool:
    s = text or ""
    return _contains_cjk(s) and bool(_RE_ASCII_TERM.search(s))


def _query_is_ascii_only(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if _contains_cjk(s):
        return False
    return bool(_RE_ASCII_TERM.search(s))


def _extract_ascii_terms(text: str) -> list[str]:
    out: list[str] = []
    for term in _RE_ASCII_TERM.findall(text or ""):
        token = str(term).strip()
        if len(token) < 2:
            continue
        lowered = token.lower()
        if lowered not in [x.lower() for x in out]:
            out.append(token)
    return out


def _relax_versioned_ascii_terms(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    relaxed = _RE_ASCII_VERSION_SUFFIX.sub(r"\1", s)
    relaxed = _RE_MULTI_SPACE.sub(" ", relaxed).strip()
    if not relaxed or relaxed == s:
        return ""
    return relaxed


def _is_safe_keyword_fallback(keyword: str, fallback: str) -> bool:
    key = (keyword or "").strip()
    fb = (fallback or "").strip()
    if not fb:
        return False
    if not key:
        return True
    if fb == key or key in fb:
        return False
    if _contains_cjk(key) and _contains_cjk(fb):
        # 中文业务词默认保守，不做“信息安全 -> 安全”这类降级。
        return False
    relaxed = _relax_versioned_ascii_terms(key)
    if relaxed and _normalize_query(relaxed) == _normalize_query(fb):
        return True
    if not _contains_cjk(key) and not _contains_cjk(fb):
        return True
    return False
