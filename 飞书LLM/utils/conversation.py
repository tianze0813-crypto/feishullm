from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import BASE_DIR
from utils.logger import get_logger

logger = get_logger()


@dataclass
class ConversationTurn:
    role: str
    content: str
    ts: float


@dataclass
class ConversationSession:
    session_id: str
    user_open_id: str
    topic: str = ""
    last_user_text: str = ""
    last_user_ts: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    history: list[ConversationTurn] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def turns(self) -> int:
        return len(self.history)


class ConversationStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (BASE_DIR / ".memory" / "conversations.db")
        self._conn: sqlite3.Connection | None = None
        self._db_initialized = False
        self._lock = threading.Lock()
        self._user_loaded: set[str] = set()
        self._sessions: dict[str, dict[str, ConversationSession]] = {}
        self._active: dict[str, str] = {}

    def _ensure_db(self) -> None:
        if self._db_initialized and self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              user_open_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              topic TEXT NOT NULL DEFAULT '',
              last_user_text TEXT NOT NULL DEFAULT '',
              last_user_ts REAL NOT NULL DEFAULT 0,
              session_state TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (user_open_id, session_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS turns (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_open_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              ts REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_turns_user_session_ts
            ON turns(user_open_id, session_id, ts DESC)
            """
        )
        self._conn.commit()
        self._ensure_session_columns()
        self._db_initialized = True
        logger.info("conversation store db ready at {}", self._db_path)

    def _ensure_session_columns(self) -> None:
        assert self._conn is not None
        try:
            cur = self._conn.execute("PRAGMA table_info(sessions)")
            cols = {str(row[1]) for row in cur.fetchall() if row and len(row) >= 2}
        except Exception:
            cols = set()
        if "last_user_text" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN last_user_text TEXT NOT NULL DEFAULT ''")
        if "last_user_ts" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN last_user_ts REAL NOT NULL DEFAULT 0")
        if "session_state" not in cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN session_state TEXT NOT NULL DEFAULT '{}'")
        self._conn.commit()

    def ensure_user_loaded(self, user_open_id: str) -> None:
        if not user_open_id:
            return
        with self._lock:
            if user_open_id in self._user_loaded:
                return
            self._ensure_db()
            assert self._conn is not None
            self._sessions.setdefault(user_open_id, {})
            self._load_sessions_locked(user_open_id)
            self._user_loaded.add(user_open_id)
            if user_open_id not in self._active:
                self._active[user_open_id] = self._pick_default_active_locked(user_open_id)

    def get_active_session(self, user_open_id: str) -> ConversationSession:
        self.ensure_user_loaded(user_open_id)
        with self._lock:
            active_id = self._active.get(user_open_id)
            if active_id:
                sess = self._sessions.get(user_open_id, {}).get(active_id)
                if sess:
                    return sess
            sess = self._create_session_locked(user_open_id)
            self._active[user_open_id] = sess.session_id
            return sess

    def new_topic(self, user_open_id: str) -> ConversationSession:
        self.ensure_user_loaded(user_open_id)
        with self._lock:
            sess = self._create_session_locked(user_open_id)
            self._active[user_open_id] = sess.session_id
            return sess

    def list_sessions(self, user_open_id: str, limit: int = 10) -> list[ConversationSession]:
        self.ensure_user_loaded(user_open_id)
        with self._lock:
            items = list(self._sessions.get(user_open_id, {}).values())
            items.sort(key=lambda s: s.updated_at or s.created_at, reverse=True)
            return items[: max(1, int(limit))]

    def switch_topic(self, user_open_id: str, session_id: str) -> ConversationSession | None:
        self.ensure_user_loaded(user_open_id)
        with self._lock:
            sess = self._sessions.get(user_open_id, {}).get(session_id)
            if not sess:
                return None
            self._active[user_open_id] = session_id
            self._load_turns_locked(user_open_id, session_id, limit=20)
            return sess

    def add_turn(
        self,
        user_open_id: str,
        role: str,
        content: str,
        *,
        topic_hint: str | None = None,
    ) -> ConversationSession:
        sess = self.get_active_session(user_open_id)
        now = time.time()
        turn = ConversationTurn(role=role, content=content, ts=now)
        with self._lock:
            sess.history.append(turn)
            if len(sess.history) > 20:
                sess.history = sess.history[-20:]
            if role == "user":
                if topic_hint and not sess.topic:
                    sess.topic = topic_hint
                if topic_hint and topic_hint != sess.topic:
                    sess.topic = topic_hint
                if not sess.topic:
                    sess.topic = _derive_topic_from_text(content)
                sess.last_user_text = _short_text(content, 80)
                sess.last_user_ts = now
            if sess.created_at <= 0:
                sess.created_at = now
            sess.updated_at = now
            self._persist_turn_locked(user_open_id, sess, turn)
            self._persist_session_locked(user_open_id, sess)
            return sess

    def set_topic(self, user_open_id: str, topic: str) -> None:
        if not topic:
            return
        sess = self.get_active_session(user_open_id)
        with self._lock:
            sess.topic = topic
            sess.updated_at = time.time()
            self._persist_session_locked(user_open_id, sess)

    def get_context_text(self, user_open_id: str, max_turns: int = 6) -> str:
        sess = self.get_active_session(user_open_id)
        turns = sess.history[-max(0, int(max_turns)) :]
        lines: list[str] = []
        for t in turns:
            role = "用户" if t.role == "user" else "助手"
            content = (t.content or "").strip()
            if not content:
                continue
            if len(content) > 200:
                content = content[:200] + "…"
            lines.append(f"{role}：{content}")
        return "\n".join(lines).strip()

    def get_session_state(self, user_open_id: str) -> dict[str, Any]:
        sess = self.get_active_session(user_open_id)
        with self._lock:
            return _clone_state(sess.state)

    def get_session_state_text(self, user_open_id: str) -> str:
        state = self.get_session_state(user_open_id)
        if not state:
            return ""
        lines: list[str] = []
        topic = str(state.get("topic") or "").strip()
        if topic:
            lines.append(f"当前话题：{topic}")
        current_intent = str(state.get("current_intent") or "").strip()
        if current_intent:
            lines.append(f"当前意图：{current_intent}")
        if bool(state.get("general_chat_fallback_enabled")):
            lines.append("当前模式：知识问答可自动切换通识问答")
        current_query = state.get("current_query") if isinstance(state.get("current_query"), dict) else {}
        keyword = str(current_query.get("keyword") or "").strip()
        if keyword:
            lines.append(f"当前检索词：{keyword}")
        last_result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
        answer_summary = str(last_result.get("answer_summary") or "").strip()
        if answer_summary:
            lines.append(f"上轮结论：{_short_text(answer_summary, 160)}")
        entities = last_result.get("entities") if isinstance(last_result.get("entities"), dict) else {}
        people = [str(x).strip() for x in (entities.get("people") or []) if str(x).strip()]
        if people:
            lines.append(f"上轮人物：{' / '.join(people[:5])}")
        docs = [str(x).strip() for x in (entities.get("docs") or []) if str(x).strip()]
        if docs:
            lines.append(f"上轮文档：{' / '.join(docs[:3])}")
        messages = [str(x).strip() for x in (entities.get("messages") or []) if str(x).strip()]
        if messages:
            lines.append(f"上轮聊天：{' / '.join(_short_text(x, 40) for x in messages[:2])}")
        if bool(last_result.get("conflict")):
            lines.append("上轮状态：存在冲突")
        return "\n".join(lines).strip()

    def update_session_state(
        self,
        user_open_id: str,
        patch: dict[str, Any] | None,
        *,
        replace: bool = False,
    ) -> ConversationSession:
        sess = self.get_active_session(user_open_id)
        with self._lock:
            base = {} if replace else _clone_state(sess.state)
            next_state = _deep_merge_state(base, patch or {})
            sess.state = next_state
            sess.updated_at = time.time()
            self._persist_session_locked(user_open_id, sess)
            return sess

    def clear_session_state(self, user_open_id: str) -> ConversationSession:
        return self.update_session_state(user_open_id, {}, replace=True)

    def active_session_id(self, user_open_id: str) -> str:
        sess = self.get_active_session(user_open_id)
        return sess.session_id

    def _pick_default_active_locked(self, user_open_id: str) -> str:
        items = list(self._sessions.get(user_open_id, {}).values())
        if not items:
            sess = self._create_session_locked(user_open_id)
            return sess.session_id
        items.sort(key=lambda s: s.updated_at or s.created_at, reverse=True)
        return items[0].session_id

    def _create_session_locked(self, user_open_id: str) -> ConversationSession:
        now = time.time()
        sid = uuid.uuid4().hex
        sess = ConversationSession(
            session_id=sid,
            user_open_id=user_open_id,
            topic="",
            last_user_text="",
            last_user_ts=0.0,
            created_at=now,
            updated_at=now,
            history=[],
            state={},
        )
        self._sessions.setdefault(user_open_id, {})[sid] = sess
        self._persist_session_locked(user_open_id, sess)
        return sess

    def _load_sessions_locked(self, user_open_id: str) -> None:
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT session_id, topic, last_user_text, last_user_ts, session_state, created_at, updated_at FROM sessions WHERE user_open_id = ? ORDER BY updated_at DESC",
            (user_open_id,),
        )
        rows = cur.fetchall()
        for session_id, topic, last_user_text, last_user_ts, session_state, created_at, updated_at in rows:
            sid = str(session_id)
            sess = ConversationSession(
                session_id=sid,
                user_open_id=user_open_id,
                topic=str(topic or ""),
                last_user_text=str(last_user_text or ""),
                last_user_ts=float(last_user_ts or 0.0),
                created_at=float(created_at or 0.0),
                updated_at=float(updated_at or 0.0),
                history=[],
                state=_decode_state(session_state),
            )
            self._sessions[user_open_id][sid] = sess
        active_id = self._pick_default_active_locked(user_open_id)
        self._active[user_open_id] = active_id
        self._load_turns_locked(user_open_id, active_id, limit=20)

    def _load_turns_locked(self, user_open_id: str, session_id: str, *, limit: int) -> None:
        sess = self._sessions.get(user_open_id, {}).get(session_id)
        if not sess or limit <= 0:
            return
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT role, content, ts FROM turns WHERE user_open_id = ? AND session_id = ? ORDER BY ts DESC LIMIT ?",
            (user_open_id, session_id, int(limit)),
        )
        rows = cur.fetchall()
        turns = [ConversationTurn(role=str(r), content=str(c), ts=float(ts)) for r, c, ts in rows]
        turns.reverse()
        sess.history = turns

    def _persist_session_locked(self, user_open_id: str, sess: ConversationSession) -> None:
        self._ensure_db()
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT INTO sessions(user_open_id, session_id, topic, last_user_text, last_user_ts, session_state, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_open_id, session_id) DO UPDATE SET
              topic=excluded.topic,
              last_user_text=excluded.last_user_text,
              last_user_ts=excluded.last_user_ts,
              session_state=excluded.session_state,
              updated_at=excluded.updated_at
            """,
            (
                user_open_id,
                sess.session_id,
                sess.topic or "",
                sess.last_user_text or "",
                float(sess.last_user_ts or 0.0),
                _encode_state(sess.state),
                float(sess.created_at or time.time()),
                float(sess.updated_at or time.time()),
            ),
        )
        self._conn.commit()

    def _persist_turn_locked(self, user_open_id: str, sess: ConversationSession, turn: ConversationTurn) -> None:
        self._ensure_db()
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO turns(user_open_id, session_id, role, content, ts) VALUES(?, ?, ?, ?, ?)",
            (user_open_id, sess.session_id, turn.role, turn.content, float(turn.ts)),
        )
        self._conn.commit()


conversation_store = ConversationStore()


def _short_text(text: str, max_len: int) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    return s[: max(0, int(max_len) - 1)] + "…"


def _derive_topic_from_text(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return "未命名话题"
    for ch in ("\n", "\r", "\t"):
        s = s.replace(ch, " ")
    s = " ".join(x for x in s.split(" ") if x)
    s = s.strip(" ,.!?，。！？：:;；")
    return _short_text(s, 12) or "未命名话题"


def _decode_state(raw: object) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            logger.warning("failed to decode session_state, fallback to empty")
    return {}


def _encode_state(state: dict[str, Any] | None) -> str:
    try:
        return json.dumps(state or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        logger.warning("failed to encode session_state, fallback to empty")
        return "{}"


def _clone_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {}
    try:
        return json.loads(json.dumps(state, ensure_ascii=False))
    except Exception:
        return dict(state)


def _deep_merge_state(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge_state(dict(base[key]), value)
        else:
            base[key] = value
    return base
