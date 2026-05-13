from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import BASE_DIR

logger = logging.getLogger(__name__)


@dataclass
class TrustedDocRecord:
    title: str
    url: str
    docs_token: str
    docs_type: str
    usage_modes: list[str]
    source_kinds: list[str]
    categories: list[str]
    matched_queries: list[str]
    raw_preview: str
    owner_id: str
    merged_score: float
    batch_count: int
    total_hit_count: int


class TrustedKnowledgeStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (BASE_DIR / ".memory" / "trusted_kb.db")
        self._conn: sqlite3.Connection | None = None
        self._db_initialized = False
        self._lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure_db(self) -> None:
        if self._db_initialized and self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trusted_docs (
              doc_key TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              url TEXT NOT NULL DEFAULT '',
              docs_token TEXT NOT NULL DEFAULT '',
              docs_type TEXT NOT NULL DEFAULT '',
              owner_id TEXT NOT NULL DEFAULT '',
              usage_modes TEXT NOT NULL DEFAULT '[]',
              source_kinds TEXT NOT NULL DEFAULT '[]',
              categories TEXT NOT NULL DEFAULT '[]',
              matched_queries TEXT NOT NULL DEFAULT '[]',
              raw_preview TEXT NOT NULL DEFAULT '',
              merged_score REAL NOT NULL DEFAULT 0,
              batch_count INTEGER NOT NULL DEFAULT 0,
              total_hit_count INTEGER NOT NULL DEFAULT 0,
              updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trusted_docs_score
            ON trusted_docs(merged_score DESC, batch_count DESC, total_hit_count DESC)
            """
        )
        self._conn.commit()
        self._ensure_columns()
        self._db_initialized = True
        logger.info("trusted kb db ready at %s", self._db_path)

    def _ensure_columns(self) -> None:
        assert self._conn is not None
        try:
            cur = self._conn.execute("PRAGMA table_info(trusted_docs)")
            cols = {str(row[1]) for row in cur.fetchall() if row and len(row) >= 2}
        except Exception:
            cols = set()
        if "usage_modes" not in cols:
            self._conn.execute("ALTER TABLE trusted_docs ADD COLUMN usage_modes TEXT NOT NULL DEFAULT '[]'")
        self._conn.commit()

    def replace_all(self, records: list[TrustedDocRecord]) -> int:
        with self._lock:
            self._ensure_db()
            assert self._conn is not None
            now = time.time()
            self._conn.execute("DELETE FROM trusted_docs")
            payload = [
                (
                    _build_doc_key(record.docs_token, record.url, record.title),
                    record.title,
                    record.url,
                    record.docs_token,
                    record.docs_type,
                    record.owner_id,
                    json.dumps(record.usage_modes, ensure_ascii=False),
                    json.dumps(record.source_kinds, ensure_ascii=False),
                    json.dumps(record.categories, ensure_ascii=False),
                    json.dumps(record.matched_queries, ensure_ascii=False),
                    record.raw_preview,
                    float(record.merged_score),
                    int(record.batch_count),
                    int(record.total_hit_count),
                    now,
                )
                for record in records
            ]
            self._conn.executemany(
                """
                INSERT INTO trusted_docs (
                  doc_key, title, url, docs_token, docs_type, owner_id,
                  usage_modes, source_kinds, categories, matched_queries, raw_preview,
                  merged_score, batch_count, total_hit_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            self._conn.commit()
            logger.info("trusted kb imported %s docs into %s", len(records), self._db_path)
            return len(records)

    def count(self) -> int:
        with self._lock:
            self._ensure_db()
            assert self._conn is not None
            row = self._conn.execute("SELECT COUNT(1) AS c FROM trusted_docs").fetchone()
            return int(row["c"] if row else 0)

    def search(self, queries: list[str], limit: int = 5, mode: str = "") -> list[dict[str, Any]]:
        clean_queries = [str(q).strip() for q in queries if str(q).strip()]
        if not clean_queries:
            return []
        wanted_mode = str(mode or "").strip().lower()
        with self._lock:
            self._ensure_db()
            assert self._conn is not None
            rows = self._conn.execute(
                """
                SELECT
                  title, url, docs_token, docs_type, owner_id,
                  usage_modes, source_kinds, categories, matched_queries, raw_preview,
                  merged_score, batch_count, total_hit_count
                FROM trusted_docs
                ORDER BY merged_score DESC, batch_count DESC, total_hit_count DESC
                LIMIT 500
                """
            ).fetchall()

        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            usage_modes = _safe_json_list(row["usage_modes"])
            if wanted_mode and wanted_mode not in [item.lower() for item in usage_modes]:
                continue
            categories = _safe_json_list(row["categories"])
            matched_queries = _safe_json_list(row["matched_queries"])
            source_kinds = _safe_json_list(row["source_kinds"])
            raw_preview = str(row["raw_preview"] or "").strip()
            title = str(row["title"] or "").strip()
            lower_title = title.lower()
            lower_categories = [item.lower() for item in categories]
            lower_matched_queries = [item.lower() for item in matched_queries]
            lower_raw = raw_preview.lower()
            compact_title = "".join(lower_title.split())
            compact_categories = ["".join(item.split()) for item in lower_categories]
            compact_matched_queries = ["".join(item.split()) for item in lower_matched_queries]
            compact_raw = "".join(lower_raw.split())
            score = min(float(row["merged_score"] or 0), 20.0)
            matched_terms = 0
            strict_hits = 0
            for query in clean_queries:
                q = query.lower()
                compact_q = "".join(q.split())
                if q == lower_title or (compact_q and compact_q == compact_title):
                    score += 36
                    matched_terms += 1
                    strict_hits += 1
                    continue
                if q in lower_title or (compact_q and compact_q in compact_title):
                    score += 24
                    matched_terms += 1
                    strict_hits += 1
                    continue
                if q in lower_matched_queries or (compact_q and compact_q in compact_matched_queries):
                    score += 28
                    matched_terms += 1
                    strict_hits += 1
                    continue
                if any(q in item for item in lower_matched_queries) or any(
                    compact_q and compact_q in item for item in compact_matched_queries
                ):
                    score += 18
                    matched_terms += 1
                    strict_hits += 1
                    continue
                if q in lower_categories or (compact_q and compact_q in compact_categories):
                    score += 16
                    matched_terms += 1
                    strict_hits += 1
                    continue
                if any(q in item for item in lower_categories) or any(
                    compact_q and compact_q in item for item in compact_categories
                ):
                    score += 10
                    matched_terms += 1
                    continue
                if q in lower_raw or (compact_q and compact_q in compact_raw):
                    score += 6
                    matched_terms += 1
            if matched_terms <= 0:
                continue
            if strict_hits <= 0 and matched_terms < 2:
                continue
            score += min(matched_terms, 5) * 2
            ranked.append(
                (
                    score,
                    {
                        "title": title,
                        "name": title,
                        "url": str(row["url"] or "").strip(),
                        "docs_token": str(row["docs_token"] or "").strip(),
                        "docs_type": str(row["docs_type"] or "").strip(),
                        "owner_id": str(row["owner_id"] or "").strip(),
                        "raw_content": raw_preview,
                        "raw_content_error": "",
                        "_source": "trusted",
                        "_trusted_kb": True,
                        "_trusted_score": round(score, 2),
                        "_trusted_meta": {
                            "merged_score": float(row["merged_score"] or 0),
                            "batch_count": int(row["batch_count"] or 0),
                            "total_hit_count": int(row["total_hit_count"] or 0),
                            "usage_modes": usage_modes,
                            "source_kinds": source_kinds,
                            "categories": categories,
                            "matched_queries": matched_queries,
                        },
                    },
                )
            )
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in ranked[: max(1, int(limit))]]


def _build_doc_key(docs_token: str, url: str, title: str) -> str:
    token = str(docs_token or "").strip()
    if token:
        return f"token:{token}".lower()
    normalized_url = str(url or "").strip()
    if normalized_url:
        return f"url:{normalized_url}".lower()
    return f"title:{str(title or '').strip()}".lower()


def _safe_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


trusted_kb_store = TrustedKnowledgeStore()
