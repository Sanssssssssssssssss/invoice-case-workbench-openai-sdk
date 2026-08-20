from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.state.case_store import CaseStore, utc_now


LONG_MESSAGE_THRESHOLD = 3000


class SessionVersionConflict(RuntimeError):
    pass


class SessionRepository:
    """Canonical conversation/session store.

    This repository intentionally stores conversation items outside case_state.
    CaseState remains the business truth; session items are only chat history and
    context-window material.
    """

    def __init__(self, store: CaseStore | None = None, db_path: Path | None = None) -> None:
        self.store = store or CaseStore()
        self.db_path = (db_path or self._default_db_path()).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def load_session(self, case_id: str) -> dict[str, Any]:
        case_id = self.store.validate_case_id(case_id)
        self.store.ensure_case_dirs(case_id)
        with self._connect() as conn:
            self._ensure_session(conn, case_id)
            return self._load_session_from_conn(conn, case_id)

    def append_user_turn(
        self,
        case_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        run_id: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        case_id = self.store.validate_case_id(case_id)
        self.store.ensure_case_dirs(case_id)
        text = str(message or "")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._ensure_session(conn, case_id)
            self._check_version(session, expected_version)
            turn_id = self._next_turn_id(conn, case_id)
            artifact_ref = ""
            inline = text
            if len(text) > LONG_MESSAGE_THRESHOLD:
                artifact_ref = self._save_turn_artifact(
                    case_id=case_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    artifact_type="user_message",
                    payload={"turn_id": turn_id, "message": text},
                )
                inline = ""
            summary = _message_summary(text, raw_is_artifact=bool(artifact_ref))
            conn.execute(
                """
                INSERT INTO session_items
                    (session_id, case_id, turn_id, run_id, role, content, content_ref,
                     content_summary, attachments_json, metadata_json, active, created_at)
                VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    self._session_id(case_id),
                    case_id,
                    turn_id,
                    str(run_id or ""),
                    inline,
                    artifact_ref,
                    summary,
                    json.dumps(attachments or [], ensure_ascii=False, default=str),
                    json.dumps({"user_message_artifact_ref": artifact_ref}, ensure_ascii=False),
                    utc_now(),
                ),
            )
            if artifact_ref:
                self._merge_artifact_refs(conn, case_id, [artifact_ref])
            self._bump_version(conn, case_id)
            conn.commit()
        return {
            "turn_id": turn_id,
            "user_message": inline,
            "user_message_ref": artifact_ref,
            "user_message_summary": summary,
            "attachments": attachments,
            "run_ids": [run_id],
            "assistant_reply": "",
            "assistant_summary": "",
            "created_at": utc_now(),
        }

    def append_assistant_turn(
        self,
        case_id: str,
        turn_id: str,
        reply: str,
        run_id: str = "",
        *,
        expected_version: int | None = None,
    ) -> None:
        case_id = self.store.validate_case_id(case_id)
        turn_id = str(turn_id or "").strip()
        if not turn_id:
            return
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._ensure_session(conn, case_id)
            self._check_version(session, expected_version)
            summary = _message_summary(reply)
            existing = conn.execute(
                """
                SELECT id FROM session_items
                WHERE case_id=? AND turn_id=? AND role='assistant' AND active=1
                ORDER BY id DESC LIMIT 1
                """,
                (case_id, turn_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE session_items
                    SET run_id=?, content=?, content_summary=?, metadata_json=?, created_at=?
                    WHERE id=?
                    """,
                    (
                        str(run_id or ""),
                        str(reply or "")[:6000],
                        summary,
                        json.dumps({"run_id": str(run_id or "")}, ensure_ascii=False),
                        utc_now(),
                        int(existing["id"]),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO session_items
                        (session_id, case_id, turn_id, run_id, role, content, content_ref,
                         content_summary, attachments_json, metadata_json, active, created_at)
                    VALUES (?, ?, ?, ?, 'assistant', ?, '', ?, '[]', ?, 1, ?)
                    """,
                    (
                        self._session_id(case_id),
                        case_id,
                        turn_id,
                        str(run_id or ""),
                        str(reply or "")[:6000],
                        summary,
                        json.dumps({"run_id": str(run_id or "")}, ensure_ascii=False),
                        utc_now(),
                    ),
                )
            self._bump_version(conn, case_id)
            conn.commit()

    def get_context_window(self, case_id: str, limit: int = 3) -> list[dict[str, Any]]:
        return self.list_recent_turns(case_id, n=limit)

    def list_recent_turns(self, case_id: str, n: int = 3) -> list[dict[str, Any]]:
        session = self.load_session(case_id)
        turns = list(session.get("turns") or [])[-n:]
        return [_compact_turn(item) for item in turns]

    def read_user_message(self, case_id: str, turn: dict[str, Any]) -> str:
        if turn.get("user_message_ref"):
            payload = self._read_artifact(case_id, str(turn["user_message_ref"]))
            return str(payload.get("message") or "")
        return str(turn.get("user_message") or "")

    def update_session_summary(self, case_id: str, summary_payload: dict[str, Any]) -> None:
        case_id = self.store.validate_case_id(case_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_session(conn, case_id)
            session_row = self._session_row(conn, case_id)
            history = _loads_list(session_row["compact_history_json"])
            history.append({"ts": utc_now(), "summary_payload": summary_payload})
            refs = _unique_strings(
                _loads_list(session_row["artifact_refs_json"]) + list(summary_payload.get("artifact_refs_to_keep") or [])
            )
            conn.execute(
                """
                UPDATE sessions
                SET session_summary=?, artifact_refs_json=?, compact_history_json=?,
                    version=version+1, updated_at=?
                WHERE case_id=?
                """,
                (
                    str(summary_payload.get("session_summary") or ""),
                    json.dumps(refs, ensure_ascii=False),
                    json.dumps(history[-20:], ensure_ascii=False, default=str),
                    utc_now(),
                    case_id,
                ),
            )
            conn.commit()

    def compact(self, case_id: str, summary_payload: dict[str, Any]) -> None:
        self.update_session_summary(case_id, summary_payload)

    def rollback_turn(self, case_id: str, turn_id: str = "latest") -> dict[str, Any]:
        case_id = self.store.validate_case_id(case_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_session(conn, case_id)
            target = str(turn_id or "latest")
            if target == "latest":
                row = conn.execute(
                    """
                    SELECT turn_id FROM session_items
                    WHERE case_id=? AND role='user' AND active=1
                    ORDER BY id DESC LIMIT 1
                    """,
                    (case_id,),
                ).fetchone()
                target = str(row["turn_id"] if row else "")
            if not target:
                conn.commit()
                return {"case_id": case_id, "turn_id": "", "removed": 0}
            cur = conn.execute(
                "UPDATE session_items SET active=0 WHERE case_id=? AND turn_id=? AND active=1",
                (case_id, target),
            )
            self._bump_version(conn, case_id)
            conn.commit()
        return {"case_id": case_id, "turn_id": target, "removed": int(cur.rowcount or 0)}

    def clear_session(self, case_id: str) -> None:
        case_id = self.store.validate_case_id(case_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_session(conn, case_id)
            conn.execute("DELETE FROM session_items WHERE case_id=?", (case_id,))
            conn.execute(
                """
                UPDATE sessions
                SET session_summary='', artifact_refs_json='[]', compact_history_json='[]',
                    version=version+1, updated_at=?
                WHERE case_id=?
                """,
                (utc_now(), case_id),
            )
            conn.commit()

    def get_conversation_items(self, case_id: str, limit: int = 20) -> list[dict[str, Any]]:
        case_id = self.store.validate_case_id(case_id)
        with self._connect() as conn:
            self._ensure_session(conn, case_id)
            rows = conn.execute(
                """
                SELECT role, content, content_ref, content_summary, attachments_json, metadata_json, created_at
                FROM session_items
                WHERE case_id=? AND active=1
                ORDER BY id DESC LIMIT ?
                """,
                (case_id, int(limit)),
            ).fetchall()
        records = []
        for row in reversed(rows):
            metadata = _loads_dict(row["metadata_json"])
            content = str(row["content"] or row["content_summary"] or "")
            records.append(
                {
                    "ts": row["created_at"],
                    "role": row["role"],
                    "content": content,
                    "attachments": self._safe_attachment_metadata(case_id, _loads_list(row["attachments_json"])),
                    "metadata": metadata,
                }
            )
        return records

    def _safe_attachment_metadata(self, case_id: str, attachments: list[Any]) -> list[dict[str, str]]:
        case_root = self.store.case_dir(case_id).resolve()
        records: list[dict[str, str]] = []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            name = Path(str(item.get("name") or "attachment")).name
            raw_path = str(item.get("relative_path") or item.get("path") or "").strip()
            relative_path = ""
            if raw_path:
                candidate = Path(raw_path)
                try:
                    relative_path = (
                        candidate.resolve().relative_to(case_root).as_posix()
                        if candidate.is_absolute()
                        else candidate.as_posix().lstrip("/")
                    )
                    self.store.resolve_case_path(case_id, relative_path)
                except (OSError, ValueError):
                    relative_path = ""
            records.append(
                {
                    "name": name,
                    "path": relative_path,
                    "content_type": str(item.get("content_type") or "application/octet-stream"),
                }
            )
        return records

    def _default_db_path(self) -> Path:
        settings = get_settings()
        if self.store.workspace_root == settings.workspace_root.resolve():
            return settings.session_db_path
        return self.store.workspace_root.parent / "sessions.sqlite"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL UNIQUE,
                    session_summary TEXT NOT NULL DEFAULT '',
                    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                    compact_history_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    content_ref TEXT NOT NULL DEFAULT '',
                    content_summary TEXT NOT NULL DEFAULT '',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_items_case_turn ON session_items(case_id, turn_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_items_case_active ON session_items(case_id, active, id)")

    def _ensure_session(self, conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        row = self._session_row(conn, case_id)
        if row:
            return row
        now = utc_now()
        conn.execute(
            """
            INSERT INTO sessions
                (session_id, case_id, session_summary, artifact_refs_json, compact_history_json, version, created_at, updated_at)
            VALUES (?, ?, '', '[]', '[]', 0, ?, ?)
            """,
            (self._session_id(case_id), case_id, now, now),
        )
        return self._session_row(conn, case_id)

    def _session_row(self, conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        return conn.execute("SELECT * FROM sessions WHERE case_id=?", (case_id,)).fetchone()

    def _load_session_from_conn(self, conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
        row = self._session_row(conn, case_id)
        items = conn.execute(
            """
            SELECT * FROM session_items
            WHERE case_id=? AND active=1
            ORDER BY id ASC
            """,
            (case_id,),
        ).fetchall()
        turns_by_id: dict[str, dict[str, Any]] = {}
        for item in items:
            turn_id = str(item["turn_id"] or "")
            if not turn_id:
                continue
            turn = turns_by_id.setdefault(
                turn_id,
                {
                    "turn_id": turn_id,
                    "user_message": "",
                    "user_message_ref": "",
                    "user_message_summary": "",
                    "attachments": [],
                    "run_ids": [],
                    "assistant_reply": "",
                    "assistant_summary": "",
                    "created_at": item["created_at"],
                },
            )
            run_id = str(item["run_id"] or "")
            if run_id and run_id not in turn["run_ids"]:
                turn["run_ids"].append(run_id)
            if item["role"] == "user":
                turn["user_message"] = str(item["content"] or "")
                turn["user_message_ref"] = str(item["content_ref"] or "")
                turn["user_message_summary"] = str(item["content_summary"] or "")
                turn["attachments"] = _loads_list(item["attachments_json"])
                turn["created_at"] = item["created_at"]
            elif item["role"] == "assistant":
                turn["assistant_reply"] = str(item["content"] or "")
                turn["assistant_summary"] = str(item["content_summary"] or "")
        turns = list(turns_by_id.values())
        return {
            "session_id": row["session_id"],
            "case_id": case_id,
            "turns": turns,
            "session_summary": str(row["session_summary"] or ""),
            "recent_turn_ids": [item["turn_id"] for item in turns[-3:]],
            "artifact_refs": _loads_list(row["artifact_refs_json"]),
            "compact_history": _loads_list(row["compact_history_json"]),
            "version": int(row["version"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _next_turn_id(self, conn: sqlite3.Connection, case_id: str) -> str:
        rows = conn.execute(
            "SELECT turn_id FROM session_items WHERE case_id=? AND role='user'",
            (case_id,),
        ).fetchall()
        highest = 0
        for row in rows:
            match = re.match(r"turn_(\d+)$", str(row["turn_id"] or ""))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"turn_{highest + 1:03d}"

    def _check_version(self, session: sqlite3.Row, expected_version: int | None) -> None:
        if expected_version is not None and int(session["version"] or 0) != int(expected_version):
            raise SessionVersionConflict(
                f"Session version conflict: expected {expected_version}, current {int(session['version'] or 0)}"
            )

    def _bump_version(self, conn: sqlite3.Connection, case_id: str) -> None:
        conn.execute("UPDATE sessions SET version=version+1, updated_at=? WHERE case_id=?", (utc_now(), case_id))

    def _merge_artifact_refs(self, conn: sqlite3.Connection, case_id: str, refs: list[str]) -> None:
        row = self._session_row(conn, case_id)
        merged = _unique_strings(_loads_list(row["artifact_refs_json"]) + refs)
        conn.execute(
            "UPDATE sessions SET artifact_refs_json=?, updated_at=? WHERE case_id=?",
            (json.dumps(merged, ensure_ascii=False), utc_now(), case_id),
        )

    def _session_id(self, case_id: str) -> str:
        return f"{self.store.validate_case_id(case_id)}:main"

    def _save_turn_artifact(
        self,
        *,
        case_id: str,
        run_id: str,
        turn_id: str,
        artifact_type: str,
        payload: dict[str, Any],
    ) -> str:
        root = self.store.resolve_case_path(case_id, f"traces/artifacts/{run_id}")
        root.mkdir(parents=True, exist_ok=True)
        index = len(list(root.glob("art_*.json"))) + 1
        filename = f"art_{index:03d}_{artifact_type}_{turn_id}.json"
        relative_path = f"traces/artifacts/{run_id}/{filename}"
        target = self.store.resolve_case_path(case_id, relative_path)
        target.write_text(
            json.dumps(
                {
                    "artifact_type": artifact_type,
                    "name": turn_id,
                    "run_id": run_id,
                    "case_id": case_id,
                    "created_at": utc_now(),
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return relative_path

    def _read_artifact(self, case_id: str, artifact_ref: str) -> dict[str, Any]:
        path = self.store.resolve_case_path(case_id, artifact_ref)
        data = json.loads(path.read_text(encoding="utf-8"))
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else {}


def _compact_turn(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": turn.get("turn_id", ""),
        "user_summary": turn.get("user_message_summary", ""),
        "assistant_summary": _planner_safe_summary(turn.get("assistant_summary", "")),
        "attachments": [
            {"name": item.get("name", ""), "path": item.get("path", "")}
            for item in turn.get("attachments", [])
            if isinstance(item, dict)
        ],
        "run_ids": list(turn.get("run_ids") or []),
    }


def _message_summary(text: Any, max_chars: int = 700, raw_is_artifact: bool = False) -> str:
    value = " ".join(str(text or "").split())
    if raw_is_artifact:
        return f"用户提交了一段超长文本材料，原文已保存为 artifact；Planner 不应把摘要当作证据。字符数约 {len(value)}。"
    return value[:max_chars] + ("..." if len(value) > max_chars else "")


def _planner_safe_summary(text: Any, max_chars: int = 220) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    for marker in (" | ", " --- ", " ## ", " **当前进度**", " **匹配审查**"):
        if marker in value:
            value = value.split(marker, 1)[0].strip()
            break
    return value[:max_chars] + ("..." if len(value) > max_chars else "")


def _loads_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _loads_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
