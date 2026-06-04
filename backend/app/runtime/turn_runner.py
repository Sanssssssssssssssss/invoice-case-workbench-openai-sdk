from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from agents import FunctionTool, RunConfig
from agents.run_state import RunState
from pydantic import BaseModel

from app.agents.manager import CaseManagerAgentFactory, MANAGER_PROMPT
from app.agents.registry import RoleRegistry
from app.config import Settings, get_settings
from app.context import ContextManager
from app.harness import HarnessRuntime, HarnessRunState
from app.llm import LlmClient, ModelCallRecord
from app.observability.langfuse_tracer import LangfuseTracer, safe_role_input, safe_role_output
from app.observability.openai_trace_bridge import sdk_run_summary
from app.runtime.agents_sdk import build_run_config, run_agent_sync
from app.runtime.checkpoints import RuntimeCheckpointStore
from app.runtime.context_assembler import ContextAssembler, context_budget
from app.runtime.evidence_recovery import recover_text_direct_review
from app.runtime.patch_normalizer import PatchNormalizer
from app.runtime.policy_gate import PolicyGate, infer_reviewer_mode, requires_materials_advice
from app.runtime.recovery_policy import RecoveryPolicy
from app.runtime.retry import is_transient_llm_error
from app.runtime.supervisor_contract import CAPABILITY_CARDS, SPECIALIST_TOOL_DESCRIPTIONS, SPECIALIST_TOOL_MODELS
from app.runtime.tool_runtime import ToolRuntime
from app.runtime.trace_recorder import TraceRecorder
from app.session_manager import SessionManager
from app.skills import SkillRegistry
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, AgentTurnResponse, PolicyCheck, SupervisorDecision
from app.tools.catalog import ToolCatalog
from app.tools.file_workspace import FileWorkspace, report_paths_for_run


ROLE_TARGETS = {"materials_advisor", "evidence_reviewer", "case_patch_writer", "report_writer"}


@dataclass
class ManagerRunOutcome:
    final_output: str = ""
    interruptions: list[Any] | None = None
    sdk_state: str = ""
    raw_result: Any | None = None


class ManagerRunner(Protocol):
    def run(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state: HarnessRunState,
        planner_context: dict[str, Any],
        manager_input: dict[str, Any],
    ) -> ManagerRunOutcome:
        ...


class SdkManagerRunner:
    def run(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state: HarnessRunState,
        planner_context: dict[str, Any],
        manager_input: dict[str, Any],
    ) -> ManagerRunOutcome:
        _ = planner_context
        tools = runner.sdk_tools(state=state, request=request, planner_context=planner_context)
        manager = runner.manager_factory.build(tools, metadata={"case_id": state.case_id, "run_id": state.run_id})
        result = run_agent_sync(
            manager,
            json.dumps(manager_input, ensure_ascii=False, default=str),
            max_turns=max(1, state.max_steps - state.step_count + 2),
            run_config=runner.run_config(state),
        )
        runner.record_manager_model_call(state, manager_input, result)
        interruptions = list(getattr(result, "interruptions", []) or [])
        sdk_state = result.to_state().to_string() if interruptions else ""
        return ManagerRunOutcome(
            final_output=str(getattr(result, "final_output", "") or ""),
            interruptions=interruptions,
            sdk_state=sdk_state,
            raw_result=result,
        )

    def resume(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state: HarnessRunState,
        sdk_state: str,
        approved: bool,
        reason: str = "",
    ) -> ManagerRunOutcome:
        planner_context = runner.context_assembler.build_planner_context(request, state)
        tools = runner.sdk_tools(state=state, request=request, planner_context=planner_context)
        manager = runner.manager_factory.build(tools, metadata={"case_id": state.case_id, "run_id": state.run_id})
        run_state = _await(RunState.from_string(manager, sdk_state, context_override={}))
        interruptions = list(run_state.get_interruptions())
        if not interruptions:
            raise RuntimeError("No pending approval items are available for this run.")
        for item in interruptions:
            if approved:
                run_state.approve(item)
            else:
                run_state.reject(item, rejection_message=reason or "User rejected this tool call.")
        result = run_agent_sync(
            run_state,
            max_turns=max(1, state.max_steps - state.step_count + 2),
            run_config=runner.run_config(state),
        )
        runner.record_manager_model_call(state, {"approval_resume": approved, "reason": reason}, result)
        new_interruptions = list(getattr(result, "interruptions", []) or [])
        return ManagerRunOutcome(
            final_output=str(getattr(result, "final_output", "") or ""),
            interruptions=new_interruptions,
            sdk_state=result.to_state().to_string() if new_interruptions else "",
            raw_result=result,
        )


class AgentRuntime:
    def __init__(
        self,
        store: CaseStore | None = None,
        llm: LlmClient | None = None,
        manager_runner: ManagerRunner | None = None,
    ) -> None:
        self.runner = TurnRunner(store=store, llm=llm, manager_runner=manager_runner)

    def run_turn(self, request: AgentTurnRequest) -> AgentTurnResponse:
        return self.runner.run_turn(request)

    def resume_approval(self, case_id: str, run_id: str, approved: bool, reason: str = "") -> AgentTurnResponse:
        return self.runner.resume_approval(case_id, run_id, approved=approved, reason=reason)

    def resume_run(self, case_id: str, run_id: str) -> dict[str, Any]:
        return self.runner.resume_run(case_id, run_id)


class TurnRunner:
    def __init__(
        self,
        store: CaseStore | None = None,
        llm: LlmClient | None = None,
        manager_runner: ManagerRunner | None = None,
    ) -> None:
        self.settings: Settings = get_settings()
        self.store = store or CaseStore(self.settings.workspace_root)
        self.llm = llm or LlmClient(self.settings)
        self.observability = LangfuseTracer.from_settings(self.llm.settings)
        self.llm.set_tracer(self.observability)
        self.harness = HarnessRuntime(self.store, max_steps=self.settings.max_steps)
        self.sessions = SessionManager(self.store, self.llm)
        self.context = ContextManager(self.store, self.llm, self.sessions.sessions, self.sessions.memory)
        self.tools = ToolCatalog(FileWorkspace(self.store), content_resolver=self.context.resolve_content_ref)
        self.tool_runtime = ToolRuntime(self.tools, self.harness, self.context, tracer=self.observability)
        self.trace_recorder = TraceRecorder(
            store=self.store,
            llm=self.llm,
            harness=self.harness,
            sessions=self.sessions,
            tracer=self.observability,
        )
        self.context_assembler = ContextAssembler(
            store=self.store,
            llm=self.llm,
            harness=self.harness,
            context=self.context,
            sessions=self.sessions,
            planner_prompt=MANAGER_PROMPT,
            planner_prompt_file="backend/app/agents/planner/prompt.md",
        )
        self.policy_gate = PolicyGate(store=self.store, context=self.context, tool_catalog=self.tools)
        self.recovery = RecoveryPolicy(store=self.store, harness=self.harness, context=self.context)
        self.patch_normalizer = PatchNormalizer()
        self.roles = RoleRegistry(self.llm)
        self.skills = SkillRegistry()
        self.manager_factory = CaseManagerAgentFactory(self.settings)
        self.manager_runner = manager_runner or SdkManagerRunner()
        self.checkpoints = RuntimeCheckpointStore(self.store)

    def run_turn(self, request: AgentTurnRequest) -> AgentTurnResponse:
        if not request.message.strip():
            raise ValueError("message is required")
        self.llm.calls.clear()
        case_id = self.store.validate_case_id(request.case_id)
        run_id = self.harness.new_run_id()
        with self.observability.start_turn(
            case_id=case_id,
            run_id=run_id,
            session_id=f"{case_id}:main",
            message_summary=request.message,
            attachments=request.attachments,
            max_steps=self.harness.max_steps,
            model=self.llm.settings.llm_model,
            workspace_root_hash=_workspace_root_hash(self.store),
        ) as turn:
            state = self.context_assembler.load_context(request, run_id=run_id)
            state.observability.update(turn.to_dict())
            try:
                return self._run_until_final(request, state)
            finally:
                self.harness.write_trace(state)

    def resume_approval(self, case_id: str, run_id: str, approved: bool, reason: str = "") -> AgentTurnResponse:
        state, request, sdk_state, interruptions = self.checkpoints.load(case_id, run_id)
        if not sdk_state:
            raise FileNotFoundError(run_id)
        with self.observability.start_turn(
            case_id=state.case_id,
            run_id=state.run_id,
            session_id=state.session_id or f"{state.case_id}:main",
            turn_id=state.turn_id,
            message_summary=f"approval_resume approved={approved}",
            attachments=request.attachments,
            max_steps=self.harness.max_steps,
            model=self.llm.settings.llm_model,
            workspace_root_hash=_workspace_root_hash(self.store),
        ) as turn:
            state.observability.update(turn.to_dict())
            self._record_approval_decision(state, interruptions, approved=approved, reason=reason)
            if hasattr(self.manager_runner, "resume"):
                outcome = self.manager_runner.resume(  # type: ignore[attr-defined]
                    runner=self,
                    request=request,
                    state=state,
                    sdk_state=sdk_state,
                    approved=approved,
                    reason=reason,
                )
                return self._handle_manager_outcome(request, state, outcome)
            return self._run_until_final(request, state)

    def resume_run(self, case_id: str, run_id: str) -> dict[str, Any]:
        state, _request, _sdk_state, _interruptions = self.checkpoints.load(case_id, run_id)
        return {
            "case_id": state.case_id,
            "run_id": state.run_id,
            "phase": state.phase,
            "completed_at": state.completed_at,
            "final_answer": state.final_answer,
            "status": "completed" if state.completed_at else state.phase,
        }

    def _run_until_final(self, request: AgentTurnRequest, state: HarnessRunState) -> AgentTurnResponse:
        while not state.completed_at and state.step_count < state.max_steps:
            planner_context = self.context_assembler.build_planner_context(request, state)
            forced = self._deterministic_policy_continuation(request, state)
            if forced:
                name, payload = forced
                self.harness.append_debug_event(
                    state,
                    kind="runtime_policy",
                    name="deterministic_continuation",
                    payload={"tool": name, "input": payload},
                    summary=f"Runtime continued required policy action: {name}",
                    parent_event_id=state.last_action_event_id,
                    caused_by_event_id=state.last_action_event_id,
                )
                result = self.invoke_manager_tool(
                    state=state,
                    request=request,
                    planner_context=planner_context,
                    name=name,
                    payload=payload,
                )
                if result.get("status") == "approval_required":
                    return self._waiting_approval_response(request, state, "", [result])
                continue
            runtime_final = self._deterministic_final_after_patch(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            manager_input = self._manager_input(request, state, planner_context)
            try:
                outcome = self.manager_runner.run(
                    runner=self,
                    request=request,
                    state=state,
                    planner_context=planner_context,
                    manager_input=manager_input,
                )
            except Exception as exc:
                self._record_manager_failure(state, manager_input, exc)
                self.harness.finalize_run(state, _manager_failure_answer(exc))
                return self.trace_recorder.finalize_turn(state)
            response = self._handle_manager_outcome(request, state, outcome)
            if state.completed_at or state.phase == "waiting_approval":
                return response
        if not state.final_answer:
            self.harness.finalize_run(state, self.harness.step_limit_answer(state))
        return self.trace_recorder.finalize_turn(state)

    def _deterministic_policy_continuation(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
    ) -> tuple[str, dict[str, Any]] | None:
        if request.attachments and not _latest_observation(state, kind="tool", name="read_attachment"):
            return "read_attachment", {}
        if _latest_observation(state, kind="tool", name="read_attachment"):
            if not self.context.last_evidence_reviewer_result(state, mode=("review", "repair")):
                if not _latest_observation_error(state, kind="role", name="evidence_reviewer"):
                    return "evidence_reviewer", {"mode": "review"}
        if self.context.last_evidence_reviewer_result(state, mode=("review", "repair")):
            if not self.context.last_role_result(state, name="case_patch_writer"):
                if not _latest_observation_error(state, kind="role", name="case_patch_writer"):
                    return "case_patch_writer", {}
        if self.context.last_role_result(state, name="case_patch_writer"):
            if not _latest_observation(state, kind="tool", name="write_case_patch"):
                return "write_case_patch", {}
        return None

    def _deterministic_final_after_patch(self, request: AgentTurnRequest, state: HarnessRunState) -> str:
        if not _latest_observation(state, kind="tool", name="write_case_patch"):
            return ""
        if _report_requested_message(request.message) or requires_materials_advice(request.message):
            return ""
        return _runtime_final_answer(self.store.load(state.case_id), state)

    def _handle_manager_outcome(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        outcome: ManagerRunOutcome,
    ) -> AgentTurnResponse:
        interruptions = list(outcome.interruptions or [])
        if interruptions:
            return self._waiting_approval_response(request, state, outcome.sdk_state, interruptions)
        final_text = str(outcome.final_output or "").strip()
        if final_text:
            return self._finalize_manager_answer(request, state, final_text)
        if not state.final_answer:
            self.harness.finalize_run(state, self.harness.step_limit_answer(state))
        return self.trace_recorder.finalize_turn(state)

    def _finalize_manager_answer(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        final_text: str,
    ) -> AgentTurnResponse:
        _ = request
        decision = SupervisorDecision(
            action="final_answer",
            final_answer=final_text,
            reason="case_manager final output",
            confidence=0.7,
        )
        self.harness.record_supervisor_decision(state, decision)
        self.recovery.handle_final_answer(state, decision)
        if state.final_answer:
            self.trace_recorder.persist_trace_checkpoint(state, decision)
            return self.trace_recorder.finalize_turn(state)
        self.trace_recorder.persist_trace_checkpoint(state, decision)
        recovered_text = _runtime_final_answer(self.store.load(state.case_id), state)
        recovered = SupervisorDecision(
            action="final_answer",
            final_answer=recovered_text,
            reason="runtime final_answer recovery after guard feedback",
            confidence=0.9,
        )
        self.harness.record_supervisor_decision(state, recovered)
        self.harness.append_debug_event(
            state,
            kind="runtime_recovery",
            name="final_answer_guard_rewrite",
            payload={
                "source": "case_state",
                "guard_feedback": self.context.last_runtime_feedback(state),
                "final_answer_chars": len(recovered_text),
            },
            summary="Recovered final answer from case_state after guard feedback.",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.recovery.handle_final_answer(state, recovered)
        if not state.final_answer:
            state.final_answer = recovered_text
        self.trace_recorder.persist_trace_checkpoint(state, recovered)
        return self.trace_recorder.finalize_turn(state)

    def _finalize_runtime_policy_answer(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        final_text: str,
    ) -> AgentTurnResponse:
        _ = request
        decision = SupervisorDecision(
            action="final_answer",
            final_answer=final_text,
            reason="runtime_policy final from case_state",
            confidence=0.9,
        )
        self.harness.record_supervisor_decision(state, decision)
        self.harness.append_debug_event(
            state,
            kind="runtime_policy",
            name="deterministic_final_answer",
            payload={"source": "case_state", "final_answer_chars": len(final_text)},
            summary="Runtime finalized from case_state after required patch write.",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.recovery.handle_final_answer(state, decision)
        if not state.final_answer:
            state.final_answer = final_text
        self.trace_recorder.persist_trace_checkpoint(state, decision)
        return self.trace_recorder.finalize_turn(state)

    def sdk_tools(
        self,
        *,
        state: HarnessRunState,
        request: AgentTurnRequest,
        planner_context: dict[str, Any],
    ) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        for name, input_model in SPECIALIST_TOOL_MODELS.items():
            tools.append(self._sdk_tool(name, SPECIALIST_TOOL_DESCRIPTIONS[name], input_model, state, request, planner_context))
        for tool_card in self.tools.visible_tools():
            name = str(tool_card.get("name") or "")
            if not name:
                continue
            spec = self.tools.get(name)
            tools.append(
                self._sdk_tool(
                    name,
                    str(tool_card.get("description") or name),
                    spec.capability.input_model,
                    state,
                    request,
                    planner_context,
                    needs_approval=self._tool_needs_approval,
                )
            )
        return tools

    def invoke_manager_tool(
        self,
        *,
        state: HarnessRunState,
        request: AgentTurnRequest,
        planner_context: dict[str, Any],
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if state.step_count >= state.max_steps:
            return {"status": "blocked", "reason": "step_limit", "message": self.harness.step_limit_answer(state)}
        decision = _decision_for_tool(name, payload)
        check = self.policy_gate.check(
            request=request,
            state=state,
            decision=decision,
            planner_context=planner_context,
        )
        if not check.allowed:
            self._record_policy_rejected_decision(state, decision, check)
            self._record_policy_check(state, decision, check)
            self._record_policy_block(state, check)
            self.trace_recorder.persist_trace_checkpoint(state, decision)
            return {"status": "blocked", "policy_check": check.model_dump(mode="json")}
        self.harness.record_supervisor_decision(state, decision)
        self._record_policy_check(state, decision, check)
        if check.requires_approval and not self._approval_was_granted(state, decision.target):
            self.harness.set_phase(state, "waiting_approval")
            self.harness.write_trace(state)
            return {"status": "approval_required", "policy_check": check.model_dump(mode="json")}
        if decision.action == "delegate_agent":
            result = self._call_specialist(state, request, planner_context, decision)
        elif decision.action == "write_case_patch":
            result = self._write_case_patch(state, decision)
        else:
            result = self._call_tool(state, request, decision)
        self.trace_recorder.persist_trace_checkpoint(state, decision)
        return result

    def run_config(self, state: HarnessRunState) -> RunConfig:
        return build_run_config(
            self.settings,
            workflow_name="invoice_agent.case_manager",
            trace_metadata={
                "case_id": state.case_id,
                "run_id": state.run_id,
                "runtime": "openai_agents_sdk",
                "timeout_seconds": self.settings.timeout_for_role("planner"),
            },
            timeout_seconds=self.settings.timeout_for_role("planner"),
        )

    def record_manager_model_call(self, state: HarnessRunState, manager_input: dict[str, Any], result: Any) -> None:
        usage = _usage_from_result(result)
        output_preview = _manager_output_preview(result)
        record = ModelCallRecord(
            role="planner",
            model=self.settings.llm_model,
            prompt_version="case_manager_agents_sdk_v1",
            input_preview=json.dumps(manager_input, ensure_ascii=False, default=str)[:1400],
            output_preview=output_preview,
            system_prompt=MANAGER_PROMPT,
            payload=manager_input,
            raw_response=output_preview,
            usage=usage,
            content_chars=len(output_preview),
        )
        self.llm.calls.append(record)
        self.trace_recorder.record_model_call_debug(state)

    def _record_manager_failure(self, state: HarnessRunState, manager_input: dict[str, Any], exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        self.llm.calls.append(
            ModelCallRecord(
                role="planner",
                model=self.settings.llm_model,
                prompt_version="case_manager_agents_sdk_v1",
                input_preview=json.dumps(manager_input, ensure_ascii=False, default=str)[:1400],
                output_preview="",
                error=error,
                system_prompt=MANAGER_PROMPT,
                payload=manager_input,
            )
        )
        self.trace_recorder.record_model_call_debug(state)
        self.harness.record_observation(state, self.context.record_error(kind="model", name="case_manager", exc=exc))

    def _sdk_tool(
        self,
        name: str,
        description: str,
        input_model: type[BaseModel],
        state: HarnessRunState,
        request: AgentTurnRequest,
        planner_context: dict[str, Any],
        *,
        needs_approval: Any = False,
    ) -> FunctionTool:
        async def invoke(_tool_context: Any, raw_input: str) -> str:
            try:
                parsed = _parse_tool_input(raw_input, input_model)
                result = self.invoke_manager_tool(
                    state=state,
                    request=request,
                    planner_context=planner_context,
                    name=name,
                    payload=parsed,
                )
                return json.dumps(result, ensure_ascii=False, default=str)
            except Exception as exc:
                self.harness.record_observation(state, self.context.record_error(kind="tool", name=name, exc=exc))
                return json.dumps(
                    {"status": "error", "error": {"type": type(exc).__name__, "message": str(exc)}},
                    ensure_ascii=False,
                    default=str,
                )

        async def approval(_context: Any, raw_args: dict[str, Any], _tool_name: str) -> bool:
            if not callable(needs_approval):
                return bool(needs_approval)
            return bool(needs_approval(name, raw_args, state))

        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=input_model.model_json_schema(),
            on_invoke_tool=invoke,
            strict_json_schema=True,
            needs_approval=approval if callable(needs_approval) else bool(needs_approval),
        )

    def _tool_needs_approval(self, name: str, raw_args: dict[str, Any], state: HarnessRunState) -> bool:
        _ = raw_args
        if self._approval_was_granted(state, name):
            return False
        try:
            return self.tools.get(name).approval_mode == "always"
        except KeyError:
            return False

    def _call_specialist(
        self,
        state: HarnessRunState,
        request: AgentTurnRequest,
        planner_context: dict[str, Any],
        decision: SupervisorDecision,
    ) -> dict[str, Any]:
        role = decision.target
        payload = dict(decision.input or {})
        if role == "materials_advisor" and payload.get("question_focus"):
            payload["user_question"] = payload.get("question_focus")
        if role == "evidence_reviewer":
            payload["mode"] = infer_reviewer_mode(payload, state)
        role_capability = self.roles.trace_metadata(role)
        role_input = self.context_assembler.hydrate_role_input(state, role, payload, request.message)
        role_input["supervisor_task"] = supervisor_task(decision, state)
        self.context.write_context_manifest(
            state,
            target=f"role:{role}",
            context_payload=role_input,
            included=list(role_input.keys()),
            excluded=["unrelated artifacts", "supervisor transcript", "full report markdown unless needed"],
            blocked_raw_content=role != "evidence_reviewer",
            model=self.llm.settings.llm_model,
            prompt_file=self.roles.prompt_file(role),
            system_prompt=self.roles.prompt(role),
            budget=context_budget(state),
            raw_leak_checks=["unrelated_artifacts", "supervisor_transcript"],
            compact_triggered=state.session_compacted,
            metadata={"role_capability": role_capability},
        )
        with self.observability.span(
            f"agent.{role}",
            input=safe_role_input(role, role_input),
            metadata={"role_capability": role_capability, "step_count": state.step_count},
            as_type="agent",
        ) as span:
            try:
                result = self.roles.call(role, role_input)
                _mark_prior_specialist_errors_recovered(
                    state,
                    self.llm.calls,
                    role=role,
                    recovered_by="specialist_retry_success",
                )
                self.trace_recorder.record_model_call_debug(state)
                if role == "evidence_reviewer" and self._record_reviewer_mode_mismatch(state, role, role_input, result, role_capability):
                    span.update(
                        output=safe_role_output(result, error="evidence_reviewer mode mismatch"),
                        level="WARNING",
                        status_message="evidence_reviewer mode mismatch",
                    )
                    return {"status": "blocked", "reason": "role_mode_mismatch"}
                for rag_debug in self.context.pop_pending_rag_debug(state, role):
                    self.harness.append_debug_event(
                        state,
                        kind="rag_guidance",
                        name=role,
                        payload=rag_debug,
                        summary=f"RAG guidance for {role}: {rag_debug.get('status', '')}; sources={len(rag_debug.get('source_ids') or [])}",
                        parent_event_id=state.last_action_event_id,
                        caused_by_event_id=state.last_action_event_id,
                    )
                self.harness.record_role_call(state, role, role_input, result, capability=role_capability)
                observation = self.context.record_result(state, kind="role", name=role, result=result)
                self.harness.record_observation(state, observation)
                self._update_phase_after_role(state, role, result)
                span.update(output=safe_role_output(result))
                return _manager_success("role", role, observation=observation)
            except Exception as exc:
                recovery = self._recover_evidence_reviewer_timeout(state, role, role_input, role_capability, exc)
                if not recovery:
                    recovery = self._recover_case_patch_writer_failure(state, role, role_input, role_capability, exc)
                if recovery:
                    span.update(
                        output=safe_role_output(recovery, error=f"recovered after {type(exc).__name__}"),
                        level="WARNING",
                        status_message=f"recovered after {type(exc).__name__}",
                    )
                    return _manager_success("role", role, observation=_latest_observation(state, kind="role", name=role), result=recovery)
                self.harness.record_role_call(state, role, role_input, {}, error=f"{type(exc).__name__}: {exc}", capability=role_capability)
                self.harness.record_observation(state, self.context.record_error(kind="role", name=role, exc=exc))
                span.update(output=safe_role_output({}, error=f"{type(exc).__name__}: {exc}"), level="ERROR", status_message=str(exc))
                return {"status": "error", "role": role, "error": {"type": type(exc).__name__, "message": str(exc)}}

    def _recover_evidence_reviewer_timeout(
        self,
        state: HarnessRunState,
        role: str,
        role_input: dict[str, Any],
        role_capability: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any] | None:
        if role != "evidence_reviewer":
            return None
        if str(role_input.get("mode") or "").strip().lower() != "review":
            return None
        if not _is_recoverable_specialist_error(exc):
            return None
        artifact_ref = str(_latest_observation(state, kind="tool", name="read_attachment").get("artifact_ref") or "")
        if not artifact_ref:
            return None
        try:
            attachment_batch = self.context.artifacts.read(state.case_id, artifact_ref)
        except Exception:
            return None
        recovery = recover_text_direct_review(attachment_batch, error=exc)
        if not recovery:
            return None
        capability = dict(role_capability)
        capability["runtime_recovery"] = (
            "text_direct_review_after_specialist_timeout"
            if _is_timeout_error(exc)
            else "text_direct_review_after_specialist_transient_failure"
        )
        _mark_latest_model_call_recovered(
            self.llm.calls,
            role=role,
            recovered_by=capability["runtime_recovery"],
            exc=exc,
        )
        self.harness.append_debug_event(
            state,
            kind="runtime_recovery",
            name="evidence_reviewer_text_direct",
            payload={
                "role": role,
                "mode": role_input.get("mode", ""),
                "source_artifact_ref": artifact_ref,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "evidence_count": len((recovery.get("suggested_patch") or {}).get("add_evidence") or []),
            },
            summary="Recovered evidence_reviewer review from text-direct attachment extraction after timeout.",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.harness.record_role_call(state, role, role_input, recovery, capability=capability)
        observation = self.context.record_result(state, kind="role", name=role, result=recovery)
        observation["runtime_recovery"] = capability["runtime_recovery"]
        self.harness.record_observation(state, observation)
        self._update_phase_after_role(state, role, recovery)
        return recovery

    def _recover_case_patch_writer_failure(
        self,
        state: HarnessRunState,
        role: str,
        role_input: dict[str, Any],
        role_capability: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any] | None:
        if role != "case_patch_writer" or not _is_recoverable_specialist_error(exc):
            return None
        reviewer_result = self.context.last_evidence_reviewer_result(state, mode=("review", "repair"))
        suggested_updates = reviewer_result.get("suggested_patch") if isinstance(reviewer_result, dict) else None
        if not isinstance(suggested_updates, dict) or not _case_updates_have_content(suggested_updates):
            return None

        recovery_name = "case_patch_writer_from_reviewer_suggested_patch_after_transient_failure"
        writer_patch = {
            "patch_type": "update_case",
            "case_updates": suggested_updates,
            "audit_note": "Recovered from evidence_reviewer.suggested_patch after case_patch_writer transient failure.",
        }
        writer_patch = self.patch_normalizer.preserve_reviewer_quote_fields(writer_patch, reviewer_result)
        writer_patch = self.patch_normalizer.compact_for_write(writer_patch)
        capability = dict(role_capability)
        capability["runtime_recovery"] = recovery_name
        _mark_latest_model_call_recovered(self.llm.calls, role=role, recovered_by=recovery_name, exc=exc)
        self.harness.append_debug_event(
            state,
            kind="runtime_recovery",
            name="case_patch_writer_suggested_patch",
            payload={
                "role": role,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "recovered_by": recovery_name,
                "evidence_count": len(suggested_updates.get("add_evidence") or suggested_updates.get("evidence_items") or []),
                "risk_count": len(suggested_updates.get("risk_flags") or []),
            },
            summary="Recovered case_patch_writer output from evidence_reviewer suggested_patch after transient failure.",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.harness.record_role_call(state, role, role_input, writer_patch, capability=capability)
        role_observation = self.context.record_result(state, kind="role", name=role, result=writer_patch)
        role_observation["runtime_recovery"] = recovery_name
        self.harness.record_observation(state, role_observation)
        self._update_phase_after_role(state, role, writer_patch)

        write_decision = SupervisorDecision(
            action="write_case_patch",
            target="write_case_patch",
            input={"source": "runtime_recovery", "recovered_from": "case_patch_writer_transient_failure"},
            reason="runtime_recovery:case_patch_writer_transient_failure",
            confidence=1.0,
        )
        self.harness.record_supervisor_decision(state, write_decision)
        try:
            result = self.tool_runtime.call(state, "write_case_patch", {"patch": writer_patch}, internal=True)
        except Exception as write_exc:
            self.tool_runtime.call_and_record_error(state, "write_case_patch", {"patch": writer_patch}, write_exc)
            return None
        observation = _latest_observation(state, kind="tool", name="write_case_patch")
        return _manager_success("tool", "write_case_patch", observation=observation, result=result)

    def _call_tool(self, state: HarnessRunState, request: AgentTurnRequest, decision: SupervisorDecision) -> dict[str, Any]:
        payload = self._tool_payload_defaults(decision.target, dict(decision.input or {}), state)
        if decision.target == "read_attachment":
            payload = _normalize_current_attachment_read_payload(payload, request)
        try:
            result = self.tool_runtime.call(state, decision.target, payload, attachments=request.attachments)
            observation = _latest_observation(state, kind="tool", name=decision.target)
            return _manager_success("tool", decision.target, observation=observation, result=result)
        except Exception as exc:
            self.tool_runtime.call_and_record_error(state, decision.target, payload, exc)
            return {"status": "error", "tool": decision.target, "error": {"type": type(exc).__name__, "message": str(exc)}}

    def _write_case_patch(self, state: HarnessRunState, decision: SupervisorDecision) -> dict[str, Any]:
        writer_patch = self.context.last_role_result(state, name="case_patch_writer")
        if not writer_patch:
            exc = PermissionError("write_case_patch requires a successful case_patch_writer observation in the current run")
            self.harness.record_observation(state, self.context.record_error(kind="tool", name="write_case_patch", exc=exc))
            return {"status": "error", "tool": "write_case_patch", "error": {"type": type(exc).__name__, "message": str(exc)}}
        patch = self.patch_normalizer.preserve_reviewer_quote_fields(
            writer_patch,
            self.context.last_evidence_reviewer_result(state, mode=("review", "repair")),
        )
        patch = self.patch_normalizer.compact_for_write(patch)
        try:
            result = self.tool_runtime.call(state, "write_case_patch", {"patch": patch}, internal=True)
            observation = _latest_observation(state, kind="tool", name="write_case_patch")
            return _manager_success("tool", "write_case_patch", observation=observation, result=result)
        except Exception as exc:
            self.tool_runtime.call_and_record_error(state, "write_case_patch", {"patch": patch}, exc)
            return {"status": "error", "tool": "write_case_patch", "error": {"type": type(exc).__name__, "message": str(exc)}}

    def _waiting_approval_response(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        sdk_state: str,
        interruptions: list[Any],
    ) -> AgentTurnResponse:
        payloads = [_approval_payload(item) for item in interruptions]
        for payload in payloads:
            self.harness.append_debug_event(
                state,
                kind="approval_interrupt",
                name=str(payload.get("tool") or "tool"),
                payload={"approval_payload": payload, "risk_level": payload.get("risk_level", "")},
                summary=f"approval required: {payload.get('tool', '')}",
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
        self.harness.set_phase(state, "waiting_approval")
        self.checkpoints.save(state=state, request=request, sdk_state=sdk_state, interruptions=payloads)
        self.harness.write_trace(state)
        case_state = self.store.load(state.case_id)
        trace = state.compact_trace()
        trace["status"] = "waiting_approval"
        trace["interrupts"] = payloads
        return AgentTurnResponse(
            case_id=state.case_id,
            reply="这个操作需要你确认后才能继续。",
            case_state=case_state,
            trace=trace,
        )

    def _record_approval_decision(
        self,
        state: HarnessRunState,
        interruptions: list[dict[str, Any]],
        *,
        approved: bool,
        reason: str,
    ) -> None:
        name = "approved" if approved else "rejected"
        for item in interruptions or [{"tool": ""}]:
            tool = str(item.get("tool") or "")
            self.harness.append_debug_event(
                state,
                kind="approval_decision",
                name=name,
                payload={"tool": tool, "approved": approved, "reason": reason, "risk_level": item.get("risk_level", "")},
                summary=f"approval {name}: {tool}",
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
            self.harness.record_observation(
                state,
                {
                    "kind": "approval",
                    "name": name,
                    "summary": f"User {name} {tool}.",
                    "key_facts": [f"tool={tool}", f"risk_level={item.get('risk_level', '')}", f"reason={reason}"],
                    "risks": [],
                    "missing_items": [],
                    "next_action_hint": "dispatch" if approved else "final_answer",
                    "must_preserve_refs": [],
                },
            )

    def _record_policy_check(self, state: HarnessRunState, decision: SupervisorDecision, check: PolicyCheck) -> None:
        self.harness.append_debug_event(
            state,
            kind="policy_check",
            name=check.error_type or "allow",
            payload={"decision": decision.model_dump(exclude_none=True), "policy_check": check.model_dump(mode="json")},
            summary="allowed" if check.allowed else f"blocked: {check.error_type}",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )

    def _record_policy_block(self, state: HarnessRunState, check: PolicyCheck) -> None:
        self.harness.record_observation(
            state,
            {
                "kind": "policy",
                "name": check.error_type,
                "summary": check.feedback_to_supervisor,
                "key_facts": list(check.recommended_constraints),
                "risks": [],
                "missing_items": [],
                "next_action_hint": "; ".join(check.recommended_constraints),
                "must_preserve_refs": [],
                "policy_feedback": check.model_dump(mode="json"),
            },
        )

    def _record_policy_rejected_decision(self, state: HarnessRunState, decision: SupervisorDecision, check: PolicyCheck) -> None:
        event = self.harness.append_debug_event(
            state,
            kind="supervisor_decision_blocked",
            name=decision.action,
            payload={
                "decision": decision.model_dump(exclude_none=True),
                "policy_error_type": check.error_type,
                "policy_feedback": check.feedback_to_supervisor,
            },
            summary=f"blocked before step; action={decision.action}; target={decision.target or '-'}; policy={check.error_type or '-'}",
            parent_event_id=state.last_planner_model_event_id,
            caused_by_event_id=state.last_planner_model_event_id,
        )
        state.last_action_event_id = str(event.get("event_id") or "")

    def _approval_was_granted(self, state: HarnessRunState, tool: str) -> bool:
        for observation in reversed(getattr(state, "observations", []) or []):
            if observation.get("kind") != "approval" or observation.get("name") != "approved":
                continue
            facts = " ".join(str(item) for item in observation.get("key_facts") or [])
            if f"tool={tool}" in facts:
                return True
        return False

    def _manager_input(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        planner_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "user_message": state.user_message_for_planner or request.message,
            "context_pack": planner_context,
            "step_count": state.step_count,
            "capability_cards": {
                "specialists": CAPABILITY_CARDS["specialists"],
                "tools": self.tools.visible_tools(),
                "skills": self.skills.visible_manifests(),
            },
        }

    def _tool_payload_defaults(self, tool: str, payload: dict[str, Any], state: HarnessRunState) -> dict[str, Any]:
        if tool == "write_case_file":
            markdown_path, _ = report_paths_for_run(state.started_at)
            payload.setdefault("relative_path", markdown_path)
            if not payload.get("content") and not payload.get("content_ref"):
                payload["content_ref"] = "last_role:report_writer.markdown"
        elif tool == "render_pdf":
            markdown_path, pdf_path = report_paths_for_run(state.started_at)
            payload.setdefault("markdown_path", markdown_path)
            payload.setdefault("pdf_path", pdf_path)
        return payload

    def _record_reviewer_mode_mismatch(
        self,
        state: HarnessRunState,
        role: str,
        role_input: dict[str, Any],
        result: dict[str, Any],
        role_capability: dict[str, Any],
    ) -> bool:
        expected = str(role_input.get("mode") or "").strip().lower()
        observed = str((result or {}).get("mode") or "").strip().lower()
        if expected not in {"extract", "review", "repair"} or not observed or observed == expected:
            return False
        message = f"evidence_reviewer returned mode={observed} for requested mode={expected}."
        self.harness.record_role_call(state, role, role_input, result, error=message, capability=role_capability)
        self.harness.record_observation(
            state,
            {
                "kind": "policy",
                "name": "role_mode_mismatch",
                "summary": message,
                "key_facts": [f"requested_mode={expected}", f"returned_mode={observed}"],
                "risks": [],
                "missing_items": [],
                "next_action_hint": f"delegate_agent:evidence_reviewer_{expected}",
                "must_preserve_refs": [],
                "policy_feedback": {
                    "allowed": False,
                    "error_type": "role_mode_mismatch",
                    "feedback_to_supervisor": message,
                    "recommended_constraints": [
                        f"Call evidence_reviewer again with input.mode={expected}.",
                        "Do not send the mismatched result to case_patch_writer.",
                    ],
                    "risk_level": "read",
                    "requires_approval": False,
                },
            },
        )
        return True

    def _update_phase_after_role(self, state: HarnessRunState, role: str, result: dict[str, Any] | None = None) -> None:
        if role == "evidence_reviewer":
            mode = str((result or {}).get("mode") or "review")
            self.harness.set_phase(state, "evidence_extracted" if mode == "extract" else "evidence_reviewed")
        elif role == "case_patch_writer":
            self.harness.set_phase(state, "patch_ready")
        elif role == "report_writer":
            self.harness.set_phase(state, "report_drafted")


def _manager_success(
    kind: str,
    name: str,
    *,
    observation: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "success", kind: name}
    if observation:
        payload["observation"] = _manager_observation(observation)
        if observation.get("artifact_ref"):
            payload["artifact_ref"] = observation.get("artifact_ref")
        if observation.get("next_action_hint"):
            payload["next_action_hint"] = observation.get("next_action_hint")
    preview = _manager_result_preview(name, result)
    if preview is not None:
        payload["result_preview"] = preview
    elif observation and observation.get("artifact_ref"):
        payload["result_storage"] = "full_result_stored_in_artifact_ref"
    return payload


def _manager_observation(observation: dict[str, Any]) -> dict[str, Any]:
    safe = {
        "kind": observation.get("kind", ""),
        "name": observation.get("name", ""),
        "summary": observation.get("summary", ""),
        "key_facts": _bounded_list(observation.get("key_facts"), limit=12, item_chars=220),
        "risks": _bounded_list(observation.get("risks"), limit=8, item_chars=240),
        "missing_items": _bounded_list(observation.get("missing_items"), limit=8, item_chars=220),
        "next_action_hint": observation.get("next_action_hint", ""),
        "must_preserve_refs": _bounded_list(observation.get("must_preserve_refs"), limit=12, item_chars=220),
        "artifact_ref": observation.get("artifact_ref", ""),
    }
    if observation.get("reviewer_mode"):
        safe["reviewer_mode"] = observation.get("reviewer_mode")
    return {key: value for key, value in safe.items() if value not in ("", [], {}, None)}


def _manager_result_preview(name: str, result: dict[str, Any] | None) -> Any | None:
    if not isinstance(result, dict):
        return None
    if name in {"read_attachment", "write_case_patch", "write_case_file", "render_pdf"}:
        return None
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= 2200:
        return result
    return _compact_json_value(result, max_chars=2200)


def _compact_json_value(value: Any, *, max_chars: int) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return value
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            if len(json.dumps(compacted, ensure_ascii=False, default=str)) >= max_chars:
                compacted["_truncated"] = True
                break
            compacted[key] = _compact_json_value(item, max_chars=max(240, max_chars // 3))
        return compacted
    if isinstance(value, list):
        items: list[Any] = []
        for item in value[:12]:
            items.append(_compact_json_value(item, max_chars=max(240, max_chars // 4)))
            if len(json.dumps(items, ensure_ascii=False, default=str)) >= max_chars:
                items.append({"_truncated": True})
                break
        return items
    return str(value)[:max_chars] + ("..." if len(str(value)) > max_chars else "")


def _bounded_list(value: Any, *, limit: int, item_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:item_chars] for item in value[:limit] if str(item).strip()]


def _latest_observation(state: HarnessRunState, *, kind: str, name: str) -> dict[str, Any]:
    for observation in reversed(getattr(state, "observations", []) or []):
        if not isinstance(observation, dict):
            continue
        if observation.get("kind") == kind and observation.get("name") == name:
            return observation
    return {}


def _latest_observation_error(state: HarnessRunState, *, kind: str, name: str) -> dict[str, Any]:
    observation = _latest_observation(state, kind=kind, name=name)
    error = observation.get("error") if isinstance(observation, dict) else None
    return error if isinstance(error, dict) else {}


def _normalize_current_attachment_read_payload(payload: dict[str, Any], request: AgentTurnRequest) -> dict[str, Any]:
    if not list(request.attachments or []):
        return payload
    if str(payload.get("attachment_id") or "").strip() or str(payload.get("original_ref") or "").strip():
        return payload
    return {}


def supervisor_task(decision: SupervisorDecision | None, state: HarnessRunState) -> dict[str, Any]:
    latest_policy_feedback: dict[str, Any] = {}
    for observation in reversed(getattr(state, "observations", []) or []):
        if isinstance(observation, dict) and isinstance(observation.get("policy_feedback"), dict):
            latest_policy_feedback = dict(observation.get("policy_feedback") or {})
            break
    if decision is None:
        return {
            "action": "",
            "target": "",
            "mode": "",
            "reason": "",
            "short_plan": [],
            "plan_progress": "",
            "input": {},
            "policy_feedback": latest_policy_feedback,
        }
    decision_input = dict(decision.input or {})
    return {
        "action": decision.action,
        "target": decision.target,
        "mode": str(decision_input.get("mode") or ""),
        "reason": decision.reason,
        "short_plan": list(decision.short_plan or []),
        "plan_progress": decision.plan_progress,
        "input": decision_input,
        "policy_feedback": latest_policy_feedback,
    }


def _decision_for_tool(name: str, payload: dict[str, Any]) -> SupervisorDecision:
    if name in ROLE_TARGETS:
        return SupervisorDecision(action="delegate_agent", target=name, input=dict(payload or {}), reason=f"sdk_tool:{name}")
    if name == "write_case_patch":
        return SupervisorDecision(action="write_case_patch", target="write_case_patch", input=dict(payload or {}), reason="sdk_tool:write_case_patch")
    return SupervisorDecision(action="call_tool", target=name, input=dict(payload or {}), reason=f"sdk_tool:{name}")


def _parse_tool_input(raw_input: str, model: type[BaseModel]) -> dict[str, Any]:
    if not raw_input:
        return model().model_dump()
    try:
        data = json.loads(raw_input)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return model.model_validate(data).model_dump()


def _approval_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        tool = str(item.get("tool") or item.get("name") or "tool")
        args = item.get("input") if isinstance(item.get("input"), dict) else {}
        input_preview = json.dumps(args, ensure_ascii=False, default=str)[:800]
        return {
            "type": "tool_approval",
            "tool": tool,
            "risk_level": str(item.get("risk_level") or "read"),
            "input_preview": input_preview,
            "input_sha256": hashlib.sha256(input_preview.encode("utf-8")).hexdigest(),
            "reason": str(item.get("reason") or "This action requires approval."),
        }
    tool = str(getattr(item, "tool_name", None) or getattr(item, "name", None) or "tool")
    raw_args = getattr(item, "arguments", None) or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
    except Exception:
        args = {}
    input_preview = json.dumps(args, ensure_ascii=False, default=str)[:800]
    return {
        "type": "tool_approval",
        "tool": tool,
        "risk_level": "read",
        "input_preview": input_preview,
        "input_sha256": hashlib.sha256(input_preview.encode("utf-8")).hexdigest(),
        "reason": "This action requires approval.",
    }


def _manager_output_preview(result: Any) -> str:
    return json.dumps(sdk_run_summary(result), ensure_ascii=False, default=str)


def _usage_from_result(result: Any) -> dict[str, Any]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for response in getattr(result, "raw_responses", []) or []:
        usage = getattr(response, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            continue
        totals["prompt_tokens"] += int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        totals["completion_tokens"] += int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        totals["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
    return {key: value for key, value in totals.items() if value}


def _runtime_final_answer(case_state: Any, state: HarnessRunState) -> str:
    evidence_text = _case_evidence_text(case_state)
    risk_text = " ".join(str(item) for item in getattr(case_state, "risk_flags", []) or [])
    combined = f"{evidence_text}\n{risk_text}"
    profile = getattr(case_state, "case_profile", {}) or {}
    profile = profile if isinstance(profile, dict) else {}
    invoice_no = str(profile.get("invoice_number") or "") or _first_match(combined, r"\bINV-[A-Z0-9-]+\b")
    supplier = str(profile.get("supplier") or "") or _labeled_value(
        combined,
        (
            "Supplier legal name",
            "Search supplier",
            "Supplier checked",
            "Supplier",
            "供应商",
        ),
    )
    if not supplier:
        supplier = _first_match(combined, r"\b[A-Z][A-Za-z0-9 .&-]{2,80}(?:Ltd\.|Limited|Inc\.|LLC|GmbH)\b")
    supplier = _clean_supplier_name(supplier)
    amount_value = str(profile.get("amount_total") or "")
    currency = str(profile.get("currency") or "")
    amount = _join_nonempty([amount_value, currency], " ") if amount_value else ""
    if not amount:
        amount = _first_match(
            combined,
            r"(?:CNY|USD|EUR|GBP|RMB)\s*[\d,]+(?:\.\d{2})?|[\d,]+(?:\.\d{2})?\s*(?:CNY|USD|EUR|GBP|RMB)",
        )
    payment_doc = _first_match(combined, r"\bPAY-[A-Z0-9-]+\b")
    clearing_doc = _first_match(combined, r"\bCLR-[A-Z0-9-]+\b")
    conflict_ids = _requirement_ids(case_state, "conflict")
    missing_ids = _requirement_ids(case_state, "missing")
    weak_ids = _requirement_ids(case_state, "weak")
    satisfied_ids = _requirement_ids(case_state, "satisfied") + _requirement_ids(case_state, "accepted")
    risk_lower = risk_text.lower()
    duplicate_risk = bool(
        payment_doc
        or clearing_doc
        or "duplicate_payment_screen" in conflict_ids
        or "duplicate_payment_risk" in risk_lower
        or "重复付款风险" in risk_text
    )
    duplicate_checked = bool(
        "duplicate_payment_screen" in set(satisfied_ids + weak_ids + conflict_ids)
        or "duplicate_payment_check" in combined
        or "重复付款" in combined
    )

    lines: list[str] = []
    if duplicate_risk:
        lines.append("本轮本地材料审查已经记录到案卷，重点结论：重复付款检查命中风险。")
    elif duplicate_checked:
        lines.append("本轮本地材料审查已经记录到案卷，重点结论：当前材料未显示重复付款命中。")
    else:
        lines.append("本轮本地材料审查已经记录到案卷，下面是当前案卷状态。")
    facts = _join_nonempty(
        [
            f"发票 {invoice_no}" if invoice_no else "",
            f"供应商 {supplier}" if supplier else "",
            f"金额 {amount}" if amount else "",
        ],
        "；",
    )
    if facts:
        lines.append(f"- 识别到的核心对象：{facts}。")
    duplicate_facts = _join_nonempty(
        [
            f"历史付款记录 {payment_doc}" if payment_doc else "",
            f"清账凭证 {clearing_doc}" if clearing_doc else "",
        ],
        "；",
    )
    if duplicate_facts:
        lines.append(f"- 重复付款线索：{duplicate_facts}。")
    lines.append(
        "- 当前案卷状态："
        f"status={getattr(case_state, 'status', '') or '-'}；"
        f"evidence={len(getattr(case_state, 'evidence_items', []) or [])}；"
        f"satisfied={_csv(satisfied_ids)}；"
        f"conflict={_csv(conflict_ids)}；"
        f"missing={_csv(missing_ids)}；"
        f"weak={_csv(weak_ids)}。"
    )
    risks = [str(item).strip() for item in getattr(case_state, "risk_flags", []) or [] if str(item).strip()]
    if risks:
        lines.append(f"- 风险标记：{_safe_text(risks[0], max_chars=260)}")
    questions = [str(item).strip() for item in getattr(case_state, "next_questions", []) or [] if str(item).strip()]
    if questions:
        lines.append(f"- 待核对问题：{_safe_text(questions[0], max_chars=260)}")
    elif duplicate_risk:
        lines.append("- 待核对问题：核对历史付款记录、清账凭证与当前发票的清账关系、金额和供应商是否对应。")
    if duplicate_risk:
        lines.append("结论：在上述冲突解释清楚前，duplicate_payment_screen 保持 conflict；不要把该项写成 satisfied。")
    elif str(getattr(case_state, "status", "") or "") == "ready_for_report" and not missing_ids and not weak_ids and not conflict_ids:
        lines.append("结论：case_state=ready_for_report；可继续生成本地报告草稿。")
    else:
        lines.append("结论：请按当前 missing、weak、conflict 项继续补料或复核；不要把 weak 项写成 satisfied。")
    return "\n".join(lines)


def _case_evidence_text(case_state: Any) -> str:
    rows: list[str] = []
    for item in list(getattr(case_state, "evidence_items", []) or [])[:20]:
        if not item:
            continue
        parts = [
            getattr(item, "summary", ""),
            getattr(item, "content", ""),
            getattr(item, "reviewer_notes", ""),
            json.dumps(getattr(item, "metadata", {}) or {}, ensure_ascii=False, default=str),
            json.dumps([getattr(support, "requirement", "") for support in getattr(item, "supports", []) or []], ensure_ascii=False),
            json.dumps(getattr(item, "conflicts", []) or [], ensure_ascii=False, default=str),
        ]
        rows.append(" ".join(str(part) for part in parts if part))
    return "\n".join(rows)


def _requirement_ids(case_state: Any, status: str) -> list[str]:
    return [
        str(getattr(item, "id", "") or "")
        for item in getattr(case_state, "requirements", []) or []
        if str(getattr(item, "status", "") or "") == status and str(getattr(item, "id", "") or "")
    ][:12]


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, str(text or ""), flags=re.I)
    return match.group(0).strip() if match else ""


def _labeled_value(text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*:\s*([^\n\r|;]+?)(?:\s+-\s+|;|,\s*(?:Amount|Date|Result|Supplier ID|Invoice ID|Buyer|PO|GRN)\s*:|$)",
            str(text or ""),
            flags=re.I,
        )
        if match:
            value = match.group(1).strip(" ;")
            if value:
                return value
    return ""


def _clean_supplier_name(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    company = re.search(r"\b[A-Z][A-Za-z0-9 .&'-]{2,100}?(?:Ltd\.|Limited|Inc\.|LLC|GmbH)(?=$|[\s,;\"'\\}\]])", text)
    if company:
        return company.group(0).strip(" ,;\"'\\")
    text = re.split(
        r"\s*(?:;|,\s*\"?(?:Amount|Date|Result|Supplier ID|Invoice ID|Buyer|PO|GRN|status|source_quote|source_locator|locator|crop_path|confidence|field|value)\"?\s*:)",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    text = re.split(r"\s+运行时", text, maxsplit=1)[0]
    text = text.strip(" ,;\"'\\")
    if re.search(r"\bLtd$", text):
        text += "."
    return text


def _join_nonempty(values: list[str], separator: str) -> str:
    return separator.join(value for value in values if value)


def _csv(values: list[str]) -> str:
    return ", ".join(values[:10]) if values else "-"


def _safe_text(value: str, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def _is_timeout_error(exc: Exception) -> bool:
    error_text = f"{type(exc).__name__}: {exc}".lower()
    return "timeout" in error_text or "timed out" in error_text


def _is_recoverable_specialist_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return _is_timeout_error(exc) or is_transient_llm_error(exc) or "overloaded" in text


def _case_updates_have_content(updates: dict[str, Any]) -> bool:
    for field in (
        "requirements",
        "remove_requirements",
        "add_evidence",
        "evidence_items",
        "risk_flags",
        "next_questions",
        "evidence_cards",
    ):
        if updates.get(field):
            return True
    for field in ("summary", "conversation_summary", "case_profile", "next_action_hint", "reply_brief"):
        if updates.get(field) is not None:
            return True
    return False


def _report_requested_message(message: str) -> bool:
    text = str(message or "").lower()
    return any(term in text for term in ("生成报告", "最终报告", "导出报告", "渲染pdf", "生成 pdf", "pdf report", "report", "final report"))


def _mark_latest_model_call_recovered(
    calls: list[ModelCallRecord],
    *,
    role: str,
    recovered_by: str,
    exc: Exception,
) -> None:
    error_type = type(exc).__name__
    error_text = str(exc)
    for record in reversed(calls):
        if record.role != role or not record.error:
            continue
        if error_type not in record.error and error_text not in record.error:
            continue
        record.recovered_by = recovered_by
        record.output_preview = f"Recovered by {recovered_by} after {error_type}."
        record.raw_response = json.dumps(
            {"recovered_by": recovered_by, "original_error_type": error_type},
            ensure_ascii=False,
        )
        record.finish_reason = f"recovered_by:{recovered_by}"
        record.error = ""
        return


def _mark_prior_specialist_errors_recovered(
    state: HarnessRunState,
    calls: list[ModelCallRecord],
    *,
    role: str,
    recovered_by: str,
) -> None:
    for record in calls:
        if record.role != role or not record.error:
            continue
        original_error = record.error
        record.recovered_by = recovered_by
        record.output_preview = f"Recovered by later successful {role} call."
        record.raw_response = json.dumps(
            {"recovered_by": recovered_by, "original_error": original_error[:240]},
            ensure_ascii=False,
        )
        record.finish_reason = f"recovered_by:{recovered_by}"
        record.error = ""
    for call in getattr(state, "role_calls", []) or []:
        if call.get("role") != role or not call.get("error"):
            continue
        call["recovered_by"] = recovered_by
        capability = call.setdefault("capability", {})
        if isinstance(capability, dict):
            capability["recovered_by"] = recovered_by
        call["error"] = ""


def _workspace_root_hash(store: CaseStore) -> str:
    return hashlib.sha256(str(store.workspace_root).encode("utf-8")).hexdigest()[:12]


def _manager_failure_answer(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return "本轮模型调用超时，我已停止继续执行并保留当前 case 与 trace。请稍后重试，或先补充/上传材料后再继续。"
    return "本轮 case manager 调用失败，我已停止继续执行并保留当前 case 与 trace。请稍后重试。"


def _use_responses_api(provider: str, base_url: str) -> bool:
    if str(provider or "").lower() != "openai":
        return False
    return str(base_url or "").rstrip("/") == "https://api.openai.com/v1"


def _await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return asyncio.run(value)
    return value
