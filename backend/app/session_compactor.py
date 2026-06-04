from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm import LlmClient
from app.prompt_loader import load_system_prompt
from app.state.case_store import CaseStore
from app.state.session_repository import SessionRepository


CONTEXT_CHAR_LIMIT = 200000
RECENT_TURNS_TO_KEEP = 3
SUMMARY_MAX_CHARS = 1800


COMPACTOR_PROMPT = load_system_prompt("agents/session_compactor/prompt.md")


class SessionCompactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_summary: str = ""
    stable_case_facts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    recent_decisions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    artifact_refs_to_keep: list[str] = Field(default_factory=list)
    dropped_or_compacted_turns: list[str] = Field(default_factory=list)
    summary_limitations: list[str] = Field(default_factory=list)
    candidate_case_notes: list[str] = Field(default_factory=list)

    @field_validator(
        "stable_case_facts",
        "open_questions",
        "recent_decisions",
        "risks",
        "artifact_refs_to_keep",
        "dropped_or_compacted_turns",
        "summary_limitations",
        "candidate_case_notes",
        mode="before",
    )
    @classmethod
    def _coerce_string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        result: list[str] = []
        for item in items:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict) and item.get("turn_id"):
                text = str(item.get("turn_id") or "").strip()
            else:
                text = json.dumps(item, ensure_ascii=False, default=str).strip()
            if text:
                result.append(text)
        return result


class SessionCompactor:
    def __init__(self, repository: SessionRepository, case_store: CaseStore, llm: LlmClient | None = None) -> None:
        self.sessions = repository
        self.cases = case_store
        self.llm = llm or LlmClient()

    def compact_before_run(
        self,
        case_id: str,
        *,
        force: bool = False,
        reason: str = "",
        estimated_context_chars: int = 0,
        context_char_limit: int | None = None,
    ) -> dict[str, Any] | None:
        limit = context_char_limit if context_char_limit is not None else int(
            getattr(self.llm.settings, "context_char_limit", CONTEXT_CHAR_LIMIT)
        )
        if not force and estimated_context_chars <= limit:
            return None

        session = self.sessions.load_session(case_id)
        turns = list(session.get("turns") or [])
        case_state = self.cases.load(case_id)
        recent = turns[-RECENT_TURNS_TO_KEEP:]
        older = turns[:-RECENT_TURNS_TO_KEEP]
        turns_to_compact = older if older else turns
        payload = {
            "reason": reason,
            "estimated_context_chars": estimated_context_chars,
            "context_char_limit": limit,
            "previous_session_summary": str(session.get("session_summary") or ""),
            "turns_to_compact": [_turn_for_compaction(item) for item in turns_to_compact],
            "recent_turns_kept": [_turn_for_compaction(item) for item in recent],
            "case_snapshot": _case_snapshot(case_state),
            "compact_rules": {
                "recent_turns_kept_count": RECENT_TURNS_TO_KEEP,
                "do_not_modify_case_state": True,
                "do_not_emit_next_action_hint": True,
                "summary_max_chars": SUMMARY_MAX_CHARS,
            },
        }
        compacted_model = self.llm.complete_structured(
            role="session_compactor",
            system_prompt=COMPACTOR_PROMPT,
            payload=payload,
            model_type=SessionCompactPayload,
            prompt_version="session_compactor_v4.2+global_policy_v1.0",
            model=self.llm.settings.session_compact_model,
        )
        compacted = compacted_model.model_dump()
        if not compacted.get("session_summary"):
            raise ValueError("session_compactor returned empty session_summary")
        compacted["session_summary"] = str(compacted["session_summary"])[:SUMMARY_MAX_CHARS]
        compacted["artifact_refs_to_keep"] = _unique_strings(
            list(compacted.get("artifact_refs_to_keep") or []) + _artifact_refs(session, turns_to_compact)
        )[:30]
        compacted["dropped_or_compacted_turns"] = _unique_strings(
            list(compacted.get("dropped_or_compacted_turns") or [])
            + [str(turn.get("turn_id") or "") for turn in turns_to_compact]
        )
        compacted["reason"] = reason
        compacted["model"] = self.llm.settings.session_compact_model
        compacted["strategy"] = "pre_run_llm"
        compacted["estimated_context_chars"] = estimated_context_chars
        compacted["context_char_limit"] = limit
        compacted["summary_limitations"] = _unique_strings(
            list(compacted.get("summary_limitations") or [])
            + [
                "该摘要只压缩 session transcript，不替代 case_state、artifact 原文或 role 判断。",
                "压缩不修改 case_state。",
            ]
        )
        self.sessions.update_session_summary(case_id, compacted)
        return compacted


def _turn_for_compaction(turn: dict[str, Any]) -> dict[str, Any]:
    user_ref = str(turn.get("user_message_ref") or "")
    user_message = str(turn.get("user_message") or "")
    return {
        "turn_id": turn.get("turn_id", ""),
        "user_message": "[raw user message stored as artifact]" if user_ref else user_message[:1800],
        "user_message_ref": user_ref,
        "user_message_summary": turn.get("user_message_summary", ""),
        "attachments": [
            {"name": item.get("name", ""), "path": item.get("path", ""), "content_type": item.get("content_type", "")}
            for item in turn.get("attachments", [])
            if isinstance(item, dict)
        ],
        "run_ids": list(turn.get("run_ids") or []),
        "assistant_reply": "",
        "assistant_summary": _summary_for_compactor(turn.get("assistant_summary", "")),
        "created_at": turn.get("created_at", ""),
    }


def _case_snapshot(case_state: Any) -> dict[str, Any]:
    return {
        "case_id": case_state.case_id,
        "status": case_state.status,
        "summary": case_state.summary,
        "requirements": [{"id": item.id, "status": item.status} for item in case_state.requirements],
        "evidence": [
            {
                "id": item.id,
                "type": item.type,
                "credibility": item.credibility,
                "should_accept": item.review_result.get("should_accept") if item.review_result else None,
            }
            for item in case_state.evidence_items[-12:]
        ],
        "missing_materials": list(case_state.missing_materials or []),
        "risk_flags": list(case_state.risk_flags or [])[-12:],
        "next_questions": list(case_state.next_questions or [])[-8:],
    }


def _summary_for_compactor(value: Any, max_chars: int = 260) -> str:
    text = " ".join(str(value or "").split())
    for marker in (" | ", " --- ", " ## ", " **当前进度**", " **匹配审查**"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
            break
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def _artifact_refs(session: dict[str, Any], turns: list[dict[str, Any]]) -> list[str]:
    refs = list(session.get("artifact_refs") or [])
    for turn in turns:
        ref = turn.get("user_message_ref")
        if ref:
            refs.append(str(ref))
    return _unique_strings(refs)


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
