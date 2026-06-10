from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.state.case_store import CaseStore, utc_now


class MemoryService:
    """Lightweight retrieval memory.

    Memory hints are advisory only. They must never update CaseState or satisfy
    requirements without source evidence.
    """

    def __init__(self, store: CaseStore | None = None, db_path: Path | None = None) -> None:
        self.store = store or CaseStore()
        self.db_path = (db_path or self._default_db_path()).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def add_memory(
        self,
        *,
        case_id: str = "",
        memory_type: str,
        text: str,
        source_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_ref = str(source_ref or "").strip()
        if not source_ref:
            raise ValueError("memory source_ref is required")
        case_id = self.store.validate_case_id(case_id) if case_id else ""
        text = str(text or "").strip()
        if not text:
            raise ValueError("memory text is required")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories
                    (case_id, memory_type, text, source_ref, metadata_json, active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    case_id,
                    str(memory_type or "note"),
                    text,
                    source_ref,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    utc_now(),
                ),
            )
        return {
            "id": int(cur.lastrowid),
            "case_id": case_id,
            "memory_type": str(memory_type or "note"),
            "text": text,
            "source_ref": source_ref,
        }

    def search(
        self,
        *,
        case_id: str = "",
        query: str = "",
        limit: int = 5,
        memory_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        case_id = self.store.validate_case_id(case_id) if case_id else ""
        tokens = _tokens(query)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, case_id, memory_type, text, source_ref, metadata_json, created_at
                FROM memories
                WHERE active=1 AND (case_id='' OR case_id=?)
                ORDER BY id DESC LIMIT 200
                """,
                (case_id,),
            ).fetchall()
        allowed = set(memory_types or [])
        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            if allowed and str(row["memory_type"]) not in allowed:
                continue
            haystack = f"{row['memory_type']} {row['text']} {row['source_ref']} {row['metadata_json']}".lower()
            score = sum(1 for token in tokens if token in haystack) if tokens else 1
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda item: (item[0], int(item[1]["id"])), reverse=True)
        result = []
        token_count = max(1, len(tokens))
        for score, row in scored[: max(0, int(limit))]:
            relevance = min(1.0, float(score) / token_count)
            result.append(
                {
                    "id": int(row["id"]),
                    "case_id": str(row["case_id"] or ""),
                    "memory_type": str(row["memory_type"] or ""),
                    "text": str(row["text"] or "")[:700],
                    "source_ref": str(row["source_ref"] or ""),
                    "metadata": _loads_dict(row["metadata_json"]),
                    "created_at": str(row["created_at"] or ""),
                    "score": score,
                    "relevance_score": round(relevance, 4),
                    "confidence": round(relevance, 4),
                    "boundary": "memory_hint_only_not_case_truth",
                }
            )
        return result

    def clear_case(self, case_id: str) -> None:
        case_id = self.store.validate_case_id(case_id)
        with self._connect() as conn:
            conn.execute("UPDATE memories SET active=0 WHERE case_id=?", (case_id,))

    def _default_db_path(self) -> Path:
        settings = get_settings()
        if self.store.workspace_root == settings.workspace_root.resolve():
            return settings.memory_db_path
        return self.store.workspace_root.parent / "memory.sqlite"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL DEFAULT '',
                    memory_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_case_active ON memories(case_id, active, id)")


def _tokens(value: str) -> list[str]:
    text = str(value or "").lower()
    words = re.findall(r"[\w\u4e00-\u9fff]{2,}", text)
    seen: set[str] = set()
    result: list[str] = []
    for word in words:
        for token in _memory_query_tokens(word):
            if token not in seen:
                seen.add(token)
                result.append(token)
    return result[:20]


def _memory_query_tokens(word: str) -> list[str]:
    if not re.search(r"[\u4e00-\u9fff]", word):
        return [word]
    tokens = [word]
    chinese_spans = re.findall(r"[\u4e00-\u9fff]{2,}", word)
    for span in chinese_spans:
        for size in (2, 3):
            if len(span) <= size:
                continue
            tokens.extend(span[idx : idx + size] for idx in range(0, len(span) - size + 1))
    return tokens


def _loads_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
