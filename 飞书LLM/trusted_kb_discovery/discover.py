from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from feishu_client.doc import doc_client
from feishu_client.search import search_client

TITLE_POSITIVE_SIGNALS = (
    "负责人",
    "归口",
    "对接人",
    "联系人",
    "支持",
    "答疑",
    "SOP",
    "流程",
    "指引",
    "指南",
    "FAQ",
    "制度",
    "规范",
    "手册",
    "服务台",
    "自助服务",
    "工单",
    "报修",
    "权限",
    "考勤",
    "报销",
    "差旅",
    "社保",
    "公积金",
    "入职",
    "离职",
)

TITLE_NEGATIVE_SIGNALS = (
    "周报",
    "月报",
    "日报",
    "纪要",
    "复盘",
    "分享",
    "培训",
    "会议记录",
    "外部",
    "草稿",
    "测试",
)

CONTENT_POSITIVE_SIGNALS = (
    "负责人",
    "归口",
    "对接人",
    "联系人",
    "提交工单",
    "服务台",
    "自助服务",
    "工作台",
    "申请入口",
    "审批入口",
    "流程如下",
    "操作步骤",
    "适用范围",
    "如有问题",
    "请联系",
)

DOC_TYPES_WITH_RAW = {"doc", "docx"}


@dataclass
class CandidateDoc:
    source_kind: str
    title: str
    docs_token: str = ""
    docs_type: str = ""
    url: str = ""
    owner_id: str = ""
    raw_content: str = ""
    raw_status: str = ""
    hit_count: int = 0
    categories: set[str] = field(default_factory=set)
    matched_queries: set[str] = field(default_factory=set)
    source_queries: list[dict[str, str]] = field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        token = (self.docs_token or "").strip()
        if token:
            return f"{self.docs_type}:{token}".lower()
        if self.url:
            return f"url:{self.url}".lower()
        return f"title:{self.title}".lower()


def _build_doc_url(item: dict[str, Any]) -> str:
    existing = str(item.get("url") or "").strip()
    if existing:
        return existing

    token = str(item.get("docs_token") or item.get("obj_token") or "").strip()
    if not token:
        return ""

    docs_type = str(item.get("docs_type") or item.get("obj_type") or "").strip().lower()
    base = (settings.feishu_web_base_url or "https://www.feishu.cn").rstrip("/")
    path_map = {
        "docx": "docx",
        "doc": "docs",
        "sheet": "sheets",
        "bitable": "base",
        "mindnote": "mindnotes",
        "slides": "slides",
        "wiki": "wiki",
    }
    return f"{base}/{path_map.get(docs_type, 'docx')}/{token}"


def _normalize_item(item: dict[str, Any], source_kind: str) -> CandidateDoc:
    docs_type = str(item.get("docs_type") or item.get("obj_type") or "").strip().lower()
    docs_token = str(item.get("docs_token") or item.get("obj_token") or "").strip()
    return CandidateDoc(
        source_kind=source_kind,
        title=str(item.get("title") or item.get("name") or "").strip() or "未命名文档",
        docs_token=docs_token,
        docs_type=docs_type,
        url=_build_doc_url(item),
        owner_id=str(item.get("owner_id") or "").strip(),
    )


def _score_doc(doc: CandidateDoc) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    category_count = len(doc.categories)
    if category_count:
        score += category_count * 3.5
        reasons.append(f"覆盖类别 {category_count}")

    if doc.hit_count:
        score += min(doc.hit_count, 8) * 1.5
        reasons.append(f"命中次数 {doc.hit_count}")

    title = doc.title
    title_positive = sum(1 for s in TITLE_POSITIVE_SIGNALS if s.lower() in title.lower())
    if title_positive:
        score += title_positive * 2.5
        reasons.append(f"标题强信号 {title_positive}")

    title_negative = sum(1 for s in TITLE_NEGATIVE_SIGNALS if s.lower() in title.lower())
    if title_negative:
        score -= title_negative * 3
        reasons.append(f"标题疑似噪声 -{title_negative}")

    raw = doc.raw_content or ""
    if raw:
        score += 2
        reasons.append("可读取正文")
        content_positive = sum(1 for s in CONTENT_POSITIVE_SIGNALS if s.lower() in raw.lower())
        if content_positive:
            score += min(content_positive, 6) * 1.2
            reasons.append(f"正文强信号 {content_positive}")
    else:
        if doc.raw_status == "no_permission":
            score -= 1
            reasons.append("无正文权限")
        elif doc.raw_status == "unavailable":
            score -= 0.5
            reasons.append("正文不可用")

    if doc.source_kind == "wiki":
        score += 1
        reasons.append("知识库来源")

    if len(title) <= 40:
        score += 0.5

    return round(score, 2), reasons


async def _load_raw_content(open_id: str, docs: list[CandidateDoc], concurrency: int) -> None:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def worker(doc: CandidateDoc) -> None:
        if not doc.docs_token or doc.docs_type not in DOC_TYPES_WITH_RAW:
            return
        async with sem:
            try:
                content, status = await doc_client.safe_load_content(open_id, doc.docs_token)
            except PermissionError:
                doc.raw_status = "no_permission"
                return
            except Exception:
                doc.raw_status = "unavailable"
                return
            doc.raw_content = (content or "").strip()
            doc.raw_status = status or ""

    await asyncio.gather(*(worker(doc) for doc in docs))


async def _search_one_query(
    open_id: str,
    category: str,
    query: str,
    docs_page_size: int,
    wiki_page_size: int,
) -> list[CandidateDoc]:
    docs_task = search_client.search_docs(open_id, query, page_size=docs_page_size, docs_types=["doc", "docx"])
    wiki_task = search_client.search_wiki(open_id, query, page_size=wiki_page_size)
    docs_items, wiki_items = await asyncio.gather(docs_task, wiki_task)

    out: list[CandidateDoc] = []
    for item in docs_items:
        doc = _normalize_item(item, "docs")
        doc.hit_count = 1
        doc.categories.add(category)
        doc.matched_queries.add(query)
        doc.source_queries.append({"category": category, "query": query, "source": "docs"})
        out.append(doc)

    for item in wiki_items:
        doc = _normalize_item(item, "wiki")
        doc.hit_count = 1
        doc.categories.add(category)
        doc.matched_queries.add(query)
        doc.source_queries.append({"category": category, "query": query, "source": "wiki"})
        out.append(doc)

    return out


def _merge_candidates(items: list[CandidateDoc]) -> list[CandidateDoc]:
    merged: dict[str, CandidateDoc] = {}
    for item in items:
        key = item.dedupe_key
        if key not in merged:
            merged[key] = item
            continue
        base = merged[key]
        base.hit_count += item.hit_count
        base.categories.update(item.categories)
        base.matched_queries.update(item.matched_queries)
        base.source_queries.extend(item.source_queries)
        if not base.url and item.url:
            base.url = item.url
        if not base.owner_id and item.owner_id:
            base.owner_id = item.owner_id
        if base.source_kind != "wiki" and item.source_kind == "wiki":
            base.source_kind = "wiki"
    return list(merged.values())


def _to_jsonable(doc: CandidateDoc) -> dict[str, Any]:
    return {
        "title": doc.title,
        "source_kind": doc.source_kind,
        "docs_type": doc.docs_type,
        "docs_token": doc.docs_token,
        "url": doc.url,
        "owner_id": doc.owner_id,
        "hit_count": doc.hit_count,
        "categories": sorted(doc.categories),
        "matched_queries": sorted(doc.matched_queries),
        "score": doc.score,
        "reasons": doc.reasons,
        "raw_status": doc.raw_status,
        "raw_preview": (doc.raw_content[:500] + "…") if len(doc.raw_content) > 500 else doc.raw_content,
        "source_queries": doc.source_queries,
    }


def _write_outputs(out_dir: Path, docs: list[CandidateDoc], suffix: str = "") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"trusted_find_person_candidates{suffix}.json"
    csv_path = out_dir / f"trusted_find_person_candidates{suffix}.csv"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump([_to_jsonable(doc) for doc in docs], f, ensure_ascii=False, indent=2)

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "score",
                "title",
                "source_kind",
                "docs_type",
                "hit_count",
                "category_count",
                "categories",
                "matched_queries",
                "url",
                "raw_status",
                "reasons",
            ],
        )
        writer.writeheader()
        for doc in docs:
            writer.writerow(
                {
                    "score": doc.score,
                    "title": doc.title,
                    "source_kind": doc.source_kind,
                    "docs_type": doc.docs_type,
                    "hit_count": doc.hit_count,
                    "category_count": len(doc.categories),
                    "categories": " | ".join(sorted(doc.categories)),
                    "matched_queries": " | ".join(sorted(doc.matched_queries)),
                    "url": doc.url,
                    "raw_status": doc.raw_status,
                    "reasons": " | ".join(doc.reasons),
                }
            )

    return json_path, csv_path


def _load_query_map(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for category, queries in data.items():
        items = [str(q).strip() for q in (queries or []) if str(q).strip()]
        if items:
            out[str(category).strip()] = items
    return out


def _flatten_query_plan(query_map: dict[str, list[str]]) -> list[tuple[str, str]]:
    plan: list[tuple[str, str]] = []
    for category, queries in query_map.items():
        for query in queries:
            plan.append((category, query))
    return plan


def _slice_query_plan(
    plan: list[tuple[str, str]],
    batch_size: int,
    batch_index: int,
) -> tuple[list[tuple[str, str]], int]:
    if batch_size <= 0:
        return plan, 1
    batch_count = max(1, math.ceil(len(plan) / batch_size))
    safe_batch_index = min(max(1, batch_index), batch_count)
    start = (safe_batch_index - 1) * batch_size
    end = start + batch_size
    return plan[start:end], batch_count


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="批量发现可用于“找人/找入口”可信知识库的候选文档")
    parser.add_argument("--open-id", required=True, help="用于飞书 user token 检索的用户 open_id")
    parser.add_argument(
        "--queries-file",
        default=str(Path(__file__).resolve().parent / "queries_full.json"),
        help="事务关键词 JSON 文件",
    )
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "output"), help="输出目录")
    parser.add_argument("--docs-page-size", type=int, default=8, help="每个 query 拉取的文档数")
    parser.add_argument("--wiki-page-size", type=int, default=8, help="每个 query 拉取的 wiki 数")
    parser.add_argument("--raw-top-n", type=int, default=120, help="最多读取前 N 篇候选正文")
    parser.add_argument("--raw-concurrency", type=int, default=4, help="并发读取正文数")
    parser.add_argument("--top-n", type=int, default=100, help="最终保留前 N 篇高价值候选")
    parser.add_argument("--batch-size", type=int, default=0, help="每批处理多少个 query，0 表示全部")
    parser.add_argument("--batch-index", type=int, default=1, help="当前执行第几批，从 1 开始")
    parser.add_argument("--query-sleep-seconds", type=float, default=0.0, help="每个 query 之间等待秒数")
    args = parser.parse_args()

    query_map = _load_query_map(Path(args.queries_file))
    full_plan = _flatten_query_plan(query_map)
    selected_plan, batch_count = _slice_query_plan(full_plan, args.batch_size, args.batch_index)
    effective_batch_index = min(max(1, args.batch_index), batch_count)
    batch_suffix = (
        ""
        if args.batch_size <= 0
        else f".batch-{effective_batch_index:02d}-of-{batch_count:02d}"
    )

    print(
        f"[批次] index={effective_batch_index}/{batch_count} "
        f"query_count={len(selected_plan)}/{len(full_plan)} "
        f"sleep={args.query_sleep_seconds}s"
    )

    all_hits: list[CandidateDoc] = []
    for idx, (category, query) in enumerate(selected_plan, start=1):
        print(f"[查询] {idx}/{len(selected_plan)} category={category} query={query}")
        try:
            hits = await _search_one_query(
                open_id=args.open_id,
                category=category,
                query=query,
                docs_page_size=args.docs_page_size,
                wiki_page_size=args.wiki_page_size,
            )
        except Exception as exc:
            print(f"  [失败] query={query} error={exc}")
            continue
        print(f"  [命中] query={query} count={len(hits)}")
        all_hits.extend(hits)
        if args.query_sleep_seconds > 0 and idx < len(selected_plan):
            await asyncio.sleep(args.query_sleep_seconds)

    merged = _merge_candidates(all_hits)
    merged.sort(key=lambda x: (len(x.categories), x.hit_count), reverse=True)

    raw_targets = merged[: max(0, args.raw_top_n)]
    await _load_raw_content(args.open_id, raw_targets, concurrency=args.raw_concurrency)

    for doc in merged:
        doc.score, doc.reasons = _score_doc(doc)

    ranked = sorted(
        merged,
        key=lambda x: (
            x.score,
            len(x.categories),
            x.hit_count,
            1 if x.raw_content else 0,
            1 if x.source_kind == "wiki" else 0,
        ),
        reverse=True,
    )[: max(1, args.top_n)]

    json_path, csv_path = _write_outputs(Path(args.out_dir), ranked, suffix=batch_suffix)

    print("\n=== 完成 ===")
    print(f"候选文档数: {len(merged)}")
    print(f"批次后缀: {batch_suffix or '(full)'}")
    print(f"输出 JSON: {json_path}")
    print(f"输出 CSV : {csv_path}")
    print("\nTop 20 候选：")
    for idx, doc in enumerate(ranked[:20], start=1):
        print(
            f"{idx:02d}. score={doc.score:<5} "
            f"cats={len(doc.categories)} hits={doc.hit_count} "
            f"title={doc.title}"
        )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
