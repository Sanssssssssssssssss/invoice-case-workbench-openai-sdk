from __future__ import annotations

import json
from typing import Any

from app.harness import HarnessRuntime, HarnessRunState
from app.llm import LlmClient
from app.observability.langfuse_tracer import LangfuseTracer
from app.observability.model_metrics import summarize_model_metrics
from app.runtime.checkpoints import RuntimeCheckpointStore
from app.session_manager import SessionManager
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnResponse, SupervisorDecision


class TraceRecorder:
    def __init__(
        self,
        *,
        store: CaseStore,
        llm: LlmClient,
        harness: HarnessRuntime,
        sessions: SessionManager,
        tracer: LangfuseTracer | None = None,
    ) -> None:
        self.store = store
        self.llm = llm
        self.harness = harness
        self.sessions = sessions
        self.tracer = tracer or LangfuseTracer.disabled()

    def record_model_call_debug(self, state: HarnessRunState) -> None:
        compact_calls = [item.to_dict() for item in self.llm.calls]
        state.observability["model_metrics"] = summarize_model_metrics(compact_calls)
        self.harness.record_model_calls(
            state,
            compact_calls,
            [item.to_debug_dict() for item in self.llm.calls],
        )

    def persist_trace_checkpoint(self, state: HarnessRunState, action: SupervisorDecision | None) -> None:
        self.record_model_call_debug(state)
        self.harness.persist_trace_checkpoint(state, action)

    def finalize_turn(self, state: HarnessRunState) -> AgentTurnResponse:
        RuntimeCheckpointStore(self.store).clear(state.case_id, state.run_id)
        case_state = self.store.load(state.case_id)
        self.record_model_call_debug(state)
        self.harness.finalize_run(state)
        self.tracer.finish_turn(
            final_answer=state.final_answer,
            case_status=case_state.status,
            step_count=state.step_count,
            phase=state.phase,
            feedback_summary=state.compact_trace().get("feedback_summary") or {},
            report_paths=_report_paths(state),
            guard_retry_count=_guard_retry_count(state),
        )
        self.tracer.flush()
        self.harness.write_trace(state)
        reply = state.final_answer or "Agent run stopped without a final answer. Inspect trace for planner observations."
        if state.turn_id:
            self.sessions.append_assistant_turn(state.case_id, state.turn_id, reply, state.run_id)
        return AgentTurnResponse(
            case_id=state.case_id,
            reply=reply,
            case_state=case_state,
            trace=state.compact_trace(),
        )


def _report_paths(state: HarnessRunState) -> list[str]:
    paths: list[str] = []
    for call in state.tool_calls:
        if call.get("tool") not in {"write_case_file", "render_pdf"} or call.get("error"):
            continue
        data = _loads(call.get("result_preview"))
        for key in ("relative_path", "markdown_path", "pdf_path", "path"):
            value = data.get(key)
            if value:
                paths.append(str(value))
    return _unique(paths)


def _guard_retry_count(state: HarnessRunState) -> int:
    count = 0
    for observation in state.observations:
        if not isinstance(observation, dict):
            continue
        if observation.get("kind") in {"runtime", "guard"} and observation.get("runtime_feedback"):
            count += 1
    return count


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
