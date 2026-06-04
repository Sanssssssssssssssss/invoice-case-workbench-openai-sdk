from __future__ import annotations

from typing import Any

from app.llm import LlmClient
from app.memory_service import MemoryService
from app.session_compactor import SessionCompactor
from app.state.session_repository import SessionRepository
from app.state.case_store import CaseStore


class SessionManager:
    def __init__(
        self,
        store: CaseStore,
        llm: LlmClient | None = None,
        repository: SessionRepository | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.store = store
        self.sessions = repository or SessionRepository(store)
        self.memory = memory or MemoryService(store)
        self.compactor = SessionCompactor(self.sessions, store, llm)

    def load_session(self, case_id: str) -> dict[str, Any]:
        return self.sessions.load_session(case_id)

    def append_user_turn(
        self,
        case_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        return self.sessions.append_user_turn(case_id, message, attachments, run_id)

    def append_assistant_turn(self, case_id: str, turn_id: str, reply: str, run_id: str = "") -> None:
        self.sessions.append_assistant_turn(case_id, turn_id, reply, run_id)

    def list_recent_turns(self, case_id: str, n: int = 3) -> list[dict[str, Any]]:
        return self.sessions.list_recent_turns(case_id, n=n)

    def rollback_turn(self, case_id: str, turn_id: str = "latest") -> dict[str, Any]:
        return self.sessions.rollback_turn(case_id, turn_id)

    def clear_session(self, case_id: str) -> None:
        self.sessions.clear_session(case_id)

    def compact_before_run(
        self,
        case_id: str,
        *,
        force: bool = False,
        reason: str = "",
        estimated_context_chars: int = 0,
        context_char_limit: int | None = None,
    ) -> dict[str, Any] | None:
        return self.compactor.compact_before_run(
            case_id,
            force=force,
            reason=reason,
            estimated_context_chars=estimated_context_chars,
            context_char_limit=context_char_limit,
        )
