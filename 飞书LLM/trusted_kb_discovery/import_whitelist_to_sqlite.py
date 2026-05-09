from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.trusted_kb import TrustedDocRecord, trusted_kb_store


def _load_whitelist_keys(csv_path: Path) -> set[str]:
    keys: set[str] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip()
            if title:
                keys.add(f"title:{title}".lower())
            if url:
                keys.add(f"url:{url}".lower())
    return keys


def _safe_list(data: object) -> list[str]:
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _record_key(item: dict) -> set[str]:
    keys: set[str] = set()
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    token = str(item.get("docs_token") or "").strip()
    if title:
        keys.add(f"title:{title}".lower())
    if url:
        keys.add(f"url:{url}".lower())
    if token:
        keys.add(f"token:{token}".lower())
    return keys


def _infer_usage_modes(item: dict) -> list[str]:
    title = str(item.get("title") or "").strip().lower()
    categories = [str(x).strip().lower() for x in (item.get("categories") or []) if str(x).strip()]
    queries = [str(x).strip().lower() for x in (item.get("matched_queries") or []) if str(x).strip()]
    text = "\n".join([title, " ".join(categories), " ".join(queries)])

    find_person_markers = (
        "负责人",
        "归口",
        "对接人",
        "联系人",
        "支持",
        "答疑",
        "服务台",
        "自助服务",
        "报修",
        "申请",
        "权限",
        "考勤",
        "请假",
        "报销",
        "差旅",
        "薪资",
        "社保",
        "公积金",
        "入职",
        "离职",
        "行政",
        "财务",
        "人事",
    )
    knowledge_markers = (
        "faq",
        "常见问题",
        "指引",
        "指南",
        "流程",
        "制度",
        "规范",
        "手册",
        "操作",
        "说明",
        "总览",
        "管理",
        "操作方法",
    )

    modes: list[str] = []
    if any(marker in text for marker in find_person_markers):
        modes.append("find_person")
    if any(marker in text for marker in knowledge_markers):
        modes.append("search_knowledge")
    if not modes:
        modes = ["search_knowledge"]
    return modes


def main() -> None:
    parser = argparse.ArgumentParser(description="将可信白名单种子导入 SQLite")
    parser.add_argument(
        "--merged-json",
        default=str(Path(__file__).resolve().parent / "output" / "merged_candidates.json"),
        help="merge_batches.py 生成的 merged_candidates.json",
    )
    parser.add_argument(
        "--whitelist-csv",
        default=str(Path(__file__).resolve().parent / "output" / "trusted_whitelist_seed.csv"),
        help="merge_batches.py 生成的 trusted_whitelist_seed.csv",
    )
    args = parser.parse_args()

    merged_json = Path(args.merged_json)
    whitelist_csv = Path(args.whitelist_csv)

    whitelist_keys = _load_whitelist_keys(whitelist_csv)
    items = json.loads(merged_json.read_text(encoding="utf-8"))

    records: list[TrustedDocRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not (_record_key(item) & whitelist_keys):
            continue
        records.append(
            TrustedDocRecord(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                docs_token=str(item.get("docs_token") or "").strip(),
                docs_type=str(item.get("docs_type") or "").strip(),
                usage_modes=_infer_usage_modes(item),
                source_kinds=_safe_list(item.get("source_kinds")),
                categories=_safe_list(item.get("categories")),
                matched_queries=_safe_list(item.get("matched_queries")),
                raw_preview=str(item.get("raw_preview") or "").strip(),
                owner_id=str(item.get("owner_id") or "").strip(),
                merged_score=float(item.get("merged_score") or 0),
                batch_count=int(item.get("batch_count") or 0),
                total_hit_count=int(item.get("total_hit_count") or 0),
            )
        )

    imported = trusted_kb_store.replace_all(records)
    print("=== trusted kb import done ===")
    print(f"imported_docs: {imported}")
    print(f"db_path: {trusted_kb_store.db_path}")


if __name__ == "__main__":
    main()
