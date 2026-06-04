from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.context import ContextManager
from app.harness import (
    MAX_ACTION_INPUT_CHARS,
    MAX_ACTION_PATCH_CHARS,
    MAX_FINAL_ANSWER_CHARS,
    HarnessRuntime,
    HarnessRunState,
)
from app.llm import LlmClient
from app.session_manager import SessionManager
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest


class ContextAssembler:
    def __init__(
        self,
        *,
        store: CaseStore,
        llm: LlmClient,
        harness: HarnessRuntime,
        context: ContextManager,
        sessions: SessionManager,
        planner_prompt: str,
        planner_prompt_file: str = "backend/app/agents/planner/prompt.md",
    ) -> None:
        self.store = store
        self.llm = llm
        self.harness = harness
        self.context = context
        self.sessions = sessions
        self.planner_prompt = planner_prompt
        self.planner_prompt_file = planner_prompt_file

    def load_context(self, request: AgentTurnRequest, run_id: str | None = None) -> HarnessRunState:
        case_id = self.store.validate_case_id(request.case_id)
        self.store.ensure_case_dirs(case_id)
        self.store.load(case_id)
        state = self.harness.begin_run(case_id, current_goal=request.message[:240], run_id=run_id)
        self.harness.set_phase(state, "session_started")
        turn = self.sessions.append_user_turn(
            case_id,
            request.message,
            [item.model_dump() for item in request.attachments],
            state.run_id,
        )
        state.session_id = f"{case_id}:main"
        state.turn_id = str(turn.get("turn_id") or "")
        state.user_message_summary = str(turn.get("user_message_summary") or "")
        state.user_message_artifact_ref = str(turn.get("user_message_ref") or "")
        state.user_message_for_planner = state.user_message_summary if state.user_message_artifact_ref else request.message
        state.current_goal = state.user_message_for_planner[:240]
        self.compact_context_before_run(state, request)
        return state

    def build_planner_context(self, request: AgentTurnRequest, state: HarnessRunState) -> dict[str, Any]:
        case_state = self.store.load(state.case_id)
        session = self.sessions.load_session(state.case_id)
        planner_context = self.context.build_planner_context(
            state=state,
            case_state=case_state,
            session=session,
            attachments=[item.model_dump() for item in request.attachments],
        )
        self.context.write_context_manifest(
            state,
            target="planner",
            context_payload={"user_message": state.user_message_for_planner, "context_pack": planner_context},
            included=list(planner_context.keys()) + ["user_message"],
            excluded=[
                "raw attachment content",
                "full role result",
                "full RAG snippets",
                "full report markdown",
                "full case_state evidence content",
                "long pasted user message",
            ],
            model=self.llm.settings.llm_model,
            prompt_file=self.planner_prompt_file,
            system_prompt=self.planner_prompt,
            budget=context_budget(state),
            raw_leak_checks=["raw_attachment_content", "long_user_message", "full_report_markdown"],
            compact_triggered=state.session_compacted,
        )
        return planner_context

    def hydrate_role_input(self, state: HarnessRunState, role: str, payload: dict[str, Any], user_message: str) -> dict[str, Any]:
        current_state = self.store.load(state.case_id)
        return self.context.build_role_context(
            role=role,
            state=state,
            payload=payload,
            user_message=state.user_message_for_planner or user_message,
            case_state=current_state,
        )

    def compact_context_before_run(self, state: HarnessRunState, request: AgentTurnRequest) -> bool:
        session = self.sessions.load_session(state.case_id)
        case_state = self.store.load(state.case_id)
        estimated = estimate_pre_run_context_chars(
            request=request,
            session=session,
            case_state=case_state,
            planner_prompt=self.planner_prompt,
        )
        state.pre_run_context_estimate_chars = estimated
        context_char_limit = int(self.llm.settings.context_char_limit)
        state.pre_run_context_limit_chars = context_char_limit
        try:
            result = self.sessions.compact_before_run(
                state.case_id,
                force=False,
                reason="PreRunContextBudget",
                estimated_context_chars=estimated,
                context_char_limit=context_char_limit,
            )
        except Exception as exc:
            self.harness.record_observation(state, self.context.record_error(kind="session", name="pre_run_context_compaction", exc=exc))
            return False
        if not result:
            return False
        state.session_compacted = True
        self.harness.record_observation(
            state,
            {
                "kind": "session",
                "name": "pre_run_context_compaction",
                "summary": result.get("session_summary", ""),
                "key_facts": list(result.get("stable_case_facts") or [])[:8],
                "risks": list(result.get("risks") or [])[:8],
                "missing_items": [],
                "next_action_hint": "",
                "must_preserve_refs": list(result.get("artifact_refs_to_keep") or [])[:8],
                "context_budget": {
                    "estimated_chars": estimated,
                    "limit": context_char_limit,
                },
            },
        )
        return True


def context_budget(state: HarnessRunState) -> dict[str, int]:
    return {
        "max_steps": state.max_steps,
        "step_count": state.step_count,
        "action_input_max_chars": MAX_ACTION_INPUT_CHARS,
        "action_patch_max_chars": MAX_ACTION_PATCH_CHARS,
        "final_answer_max_chars": MAX_FINAL_ANSWER_CHARS,
        "pre_run_context_char_limit": state.pre_run_context_limit_chars,
        "pre_run_context_estimate_chars": state.pre_run_context_estimate_chars,
    }


def estimate_pre_run_context_chars(
    *,
    request: AgentTurnRequest,
    session: dict[str, Any],
    case_state: Any,
    planner_prompt: str,
) -> int:
    recent_turns = list(session.get("turns") or [])[-6:]
    attachment_budget = _attachment_budget([item.model_dump() for item in request.attachments])
    payload = {
        "system_prompts": {
            "planner": planner_prompt,
        },
        "user_message": request.message,
        "attachment_budget": attachment_budget,
        "session_summary": session.get("session_summary") or "",
        "recent_turns": [
            {
                "user": item.get("user_message_summary") or item.get("user_message") or "",
                "assistant": item.get("assistant_summary") or "",
                "attachments": item.get("attachments") or [],
            }
            for item in recent_turns
        ],
        "case_state": case_state.model_dump() if hasattr(case_state, "model_dump") else case_state,
    }
    attachment_chars = sum(int(item.get("estimated_chars") or 0) for item in attachment_budget)
    return len(json.dumps(payload, ensure_ascii=False, default=str)) + attachment_chars


def _attachment_budget(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in attachments:
        path = Path(str(item.get("path") or ""))
        rows.append(
            {
                "name": item.get("name") or path.name,
                "path": str(path),
                "content_type": item.get("content_type") or "",
                "estimated_chars": _path_size_estimate(path),
            }
        )
    return rows


def _path_size_estimate(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if path.is_dir():
            total = 0
            for child in list(path.rglob("*"))[:80]:
                if child.is_file():
                    total += int(child.stat().st_size)
            return total
    except OSError:
        return 0
    return 0
