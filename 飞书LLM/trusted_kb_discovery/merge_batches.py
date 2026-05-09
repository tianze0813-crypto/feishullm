from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TITLE_POSITIVE_SIGNALS = (
    "负责人",
    "归口",
    "对接人",
    "联系人",
    "支持",
    "答疑",
    "申请",
    "流程",
    "指引",
    "指南",
    "FAQ",
    "常见问题",
    "制度",
    "规定",
    "手册",
    "自助服务",
    "报修",
    "权限",
    "考勤",
    "报销",
    "差旅",
    "薪酬",
    "社保",
    "公积金",
    "入职",
    "离职",
)

CONTENT_POSITIVE_SIGNALS = (
    "负责人",
    "归口",
    "对接人",
    "联系人",
    "提交工单",
    "自助报修",
    "IT自助报修",
    "服务台",
    "自助服务",
    "工作台",
    "申请入口",
    "审批入口",
    "操作步骤",
    "常见问题",
    "如有问题",
    "请联系",
)

TITLE_HARD_NEGATIVE_SIGNALS = (
    "个人说明书",
    "业务熟悉记录",
    "校招话术",
    "日报",
    "周报",
    "月报",
    "纪要",
    "晨会",
    "项目计划",
    "工作计划",
    "营销日历",
    "入门介绍",
)

TITLE_SOFT_NEGATIVE_SIGNALS = (
    "教程",
    "材料",
    "分享",
    "说明书",
    "架构",
    "接口",
    "需求文档",
    "导入",
    "统计",
)

NOISE_WHITELIST_EXCEPTIONS = (
    "常见问题解决手册",
    "信息化指引手册",
    "行政后勤服务手册",
    "操作手册",
)


@dataclass
class MergedDoc:
    title: str
    source_kinds: set[str] = field(default_factory=set)
    docs_type: str = ""
    docs_token: str = ""
    url: str = ""
    owner_id: str = ""
    batch_files: set[str] = field(default_factory=set)
    total_hit_count: int = 0
    categories: set[str] = field(default_factory=set)
    matched_queries: set[str] = field(default_factory=set)
    source_queries: list[dict[str, str]] = field(default_factory=list)
    raw_statuses: set[str] = field(default_factory=set)
    raw_preview: str = ""
    original_scores: list[float] = field(default_factory=list)
    merged_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    downgrade_flags: list[str] = field(default_factory=list)

    @property
    def batch_count(self) -> int:
        return len(self.batch_files)

    @property
    def category_count(self) -> int:
        return len(self.categories)

    @property
    def query_count(self) -> int:
        return len(self.matched_queries)

    @property
    def has_raw(self) -> bool:
        return bool(self.raw_preview.strip())

    @property
    def avg_original_score(self) -> float:
        if not self.original_scores:
            return 0.0
        return round(sum(self.original_scores) / len(self.original_scores), 2)

    @property
    def max_original_score(self) -> float:
        if not self.original_scores:
            return 0.0
        return round(max(self.original_scores), 2)


def _dedupe_key(item: dict[str, Any]) -> str:
    token = str(item.get("docs_token") or "").strip()
    if token:
        return f"{str(item.get('docs_type') or '').lower()}:{token}".lower()
    url = str(item.get("url") or "").strip()
    if url:
        return f"url:{url}".lower()
    return f"title:{str(item.get('title') or '').strip()}".lower()


def _load_batch_items(output_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(output_dir.glob("trusted_find_person_candidates.batch-*-of-*.json")):
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                out.append((path.name, item))
    return out


def _normalize(items: list[tuple[str, dict[str, Any]]]) -> list[MergedDoc]:
    merged: dict[str, MergedDoc] = {}
    for batch_name, item in items:
        key = _dedupe_key(item)
        if key not in merged:
            merged[key] = MergedDoc(
                title=str(item.get("title") or "").strip() or "未命名文档",
                docs_type=str(item.get("docs_type") or "").strip(),
                docs_token=str(item.get("docs_token") or "").strip(),
                url=str(item.get("url") or "").strip(),
                owner_id=str(item.get("owner_id") or "").strip(),
            )
        doc = merged[key]
        doc.source_kinds.add(str(item.get("source_kind") or "").strip())
        doc.batch_files.add(batch_name)
        doc.total_hit_count += int(item.get("hit_count") or 0)
        doc.categories.update(str(x).strip() for x in (item.get("categories") or []) if str(x).strip())
        doc.matched_queries.update(str(x).strip() for x in (item.get("matched_queries") or []) if str(x).strip())
        doc.source_queries.extend(item.get("source_queries") or [])
        raw_status = str(item.get("raw_status") or "").strip()
        if raw_status:
            doc.raw_statuses.add(raw_status)
        preview = str(item.get("raw_preview") or "").strip()
        if len(preview) > len(doc.raw_preview):
            doc.raw_preview = preview
        try:
            doc.original_scores.append(float(item.get("score") or 0))
        except Exception:
            pass
    return list(merged.values())


def _count_matches(text: str, signals: tuple[str, ...]) -> int:
    lower_text = text.lower()
    return sum(1 for signal in signals if signal.lower() in lower_text)


def _should_exempt_soft_noise(title: str) -> bool:
    lower_title = title.lower()
    return any(item.lower() in lower_title for item in NOISE_WHITELIST_EXCEPTIONS)


def _rescore(doc: MergedDoc) -> None:
    score = 0.0
    reasons: list[str] = []
    downgrade_flags: list[str] = []

    score += min(doc.batch_count, 8) * 2.5
    reasons.append(f"跨批出现 {doc.batch_count}")

    score += doc.category_count * 3.5
    reasons.append(f"覆盖类别 {doc.category_count}")

    score += min(doc.total_hit_count, 20) * 1.2
    reasons.append(f"累计命中 {doc.total_hit_count}")

    title_positive = _count_matches(doc.title, TITLE_POSITIVE_SIGNALS)
    if title_positive:
        score += title_positive * 2.2
        reasons.append(f"标题强信号 {title_positive}")

    if doc.has_raw:
        score += 2.5
        reasons.append("可读取正文")
        content_positive = _count_matches(doc.raw_preview, CONTENT_POSITIVE_SIGNALS)
        if content_positive:
            score += min(content_positive, 6) * 1.1
            reasons.append(f"正文强信号 {content_positive}")
    elif "rate_limited" in doc.raw_statuses:
        score -= 0.5
        downgrade_flags.append("正文限流")
    elif "unavailable" in doc.raw_statuses:
        score -= 1.0
        downgrade_flags.append("正文不可用")

    if "wiki" in doc.source_kinds:
        score += 1.0
        reasons.append("包含知识库来源")

    if "docs" in doc.source_kinds:
        score += 0.5

    hard_negative = _count_matches(doc.title, TITLE_HARD_NEGATIVE_SIGNALS)
    if hard_negative:
        penalty = hard_negative * 15.0
        score -= penalty
        downgrade_flags.append(f"强噪声标题 -{penalty:g}")

    soft_negative = 0 if _should_exempt_soft_noise(doc.title) else _count_matches(doc.title, TITLE_SOFT_NEGATIVE_SIGNALS)
    if soft_negative:
        penalty = soft_negative * 6.0
        score -= penalty
        downgrade_flags.append(f"弱噪声标题 -{penalty:g}")

    if "FAQ" in doc.title or "常见问题" in doc.title:
        score += 1.5

    if hard_negative and doc.batch_count >= 3:
        score -= 8.0
        downgrade_flags.append("高重复强噪声")

    if soft_negative and doc.batch_count >= 4 and doc.category_count >= 3:
        score -= 4.0
        downgrade_flags.append("高重复弱噪声")

    if doc.query_count >= 5:
        score += 1.0
        reasons.append(f"查询覆盖 {doc.query_count}")

    doc.merged_score = round(score, 2)
    doc.reasons = reasons
    doc.downgrade_flags = downgrade_flags


def _to_jsonable(doc: MergedDoc) -> dict[str, Any]:
    return {
        "title": doc.title,
        "source_kinds": sorted(doc.source_kinds),
        "docs_type": doc.docs_type,
        "docs_token": doc.docs_token,
        "url": doc.url,
        "owner_id": doc.owner_id,
        "batch_count": doc.batch_count,
        "total_hit_count": doc.total_hit_count,
        "categories": sorted(doc.categories),
        "matched_queries": sorted(doc.matched_queries),
        "avg_original_score": doc.avg_original_score,
        "max_original_score": doc.max_original_score,
        "merged_score": doc.merged_score,
        "reasons": doc.reasons,
        "downgrade_flags": doc.downgrade_flags,
        "raw_statuses": sorted(doc.raw_statuses),
        "raw_preview": doc.raw_preview,
        "source_queries": doc.source_queries,
    }


def _write_csv(path: Path, docs: list[MergedDoc]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "merged_score",
                "title",
                "batch_count",
                "total_hit_count",
                "category_count",
                "query_count",
                "avg_original_score",
                "max_original_score",
                "source_kinds",
                "categories",
                "matched_queries",
                "url",
                "downgrade_flags",
                "reasons",
            ],
        )
        writer.writeheader()
        for doc in docs:
            writer.writerow(
                {
                    "merged_score": doc.merged_score,
                    "title": doc.title,
                    "batch_count": doc.batch_count,
                    "total_hit_count": doc.total_hit_count,
                    "category_count": doc.category_count,
                    "query_count": doc.query_count,
                    "avg_original_score": doc.avg_original_score,
                    "max_original_score": doc.max_original_score,
                    "source_kinds": " | ".join(sorted(doc.source_kinds)),
                    "categories": " | ".join(sorted(doc.categories)),
                    "matched_queries": " | ".join(sorted(doc.matched_queries)),
                    "url": doc.url,
                    "downgrade_flags": " | ".join(doc.downgrade_flags),
                    "reasons": " | ".join(doc.reasons),
                }
            )


def _write_json(path: Path, docs: list[MergedDoc]) -> None:
    path.write_text(json.dumps([_to_jsonable(doc) for doc in docs], ensure_ascii=False, indent=2), encoding="utf-8")


def _build_whitelist_seed(docs: list[MergedDoc]) -> list[MergedDoc]:
    out: list[MergedDoc] = []
    for doc in docs:
        if doc.merged_score < 18:
            continue
        if doc.batch_count < 2 and doc.category_count < 2:
            continue
        if doc.downgrade_flags:
            continue
        out.append(doc)
    return out


def _build_noise_candidates(docs: list[MergedDoc]) -> list[MergedDoc]:
    out: list[MergedDoc] = []
    for doc in docs:
        if any("强噪声标题" in flag for flag in doc.downgrade_flags):
            out.append(doc)
            continue
        if doc.merged_score <= 8 and doc.batch_count >= 2:
            out.append(doc)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="合并 trusted_kb_discovery 的分批结果并重新打分")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent / "output"),
        help="discover.py/discover.ps1 的输出目录",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=200,
        help="总表最多保留多少条结果",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    raw_items = _load_batch_items(output_dir)
    merged = _normalize(raw_items)
    for doc in merged:
        _rescore(doc)

    ranked = sorted(
        merged,
        key=lambda doc: (
            doc.merged_score,
            doc.batch_count,
            doc.category_count,
            doc.total_hit_count,
            1 if doc.has_raw else 0,
        ),
        reverse=True,
    )

    merged_top = ranked[: max(1, args.top_n)]
    whitelist_seed = _build_whitelist_seed(merged_top)
    noise_candidates = _build_noise_candidates(ranked)

    merged_json = output_dir / "merged_candidates.json"
    merged_csv = output_dir / "merged_candidates.csv"
    whitelist_csv = output_dir / "trusted_whitelist_seed.csv"
    noise_csv = output_dir / "noise_candidates.csv"

    _write_json(merged_json, merged_top)
    _write_csv(merged_csv, merged_top)
    _write_csv(whitelist_csv, whitelist_seed)
    _write_csv(noise_csv, noise_candidates)

    print("=== merge done ===")
    print(f"batch_items: {len(raw_items)}")
    print(f"merged_docs: {len(merged)}")
    print(f"merged_csv: {merged_csv}")
    print(f"whitelist_seed_csv: {whitelist_csv}")
    print(f"noise_candidates_csv: {noise_csv}")
    print("Top 20 merged candidates:")
    for idx, doc in enumerate(merged_top[:20], start=1):
        flags = f" flags={','.join(doc.downgrade_flags)}" if doc.downgrade_flags else ""
        print(
            f"{idx:02d}. score={doc.merged_score:<5} "
            f"batches={doc.batch_count} cats={doc.category_count} hits={doc.total_hit_count} "
            f"title={doc.title}{flags}"
        )


if __name__ == "__main__":
    main()
