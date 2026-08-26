from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from agents import FunctionTool, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded
from agents.memory import SQLiteSession
from agents.run_state import RunState
from pydantic import BaseModel

from app.agents.manager import CaseManagerAgentFactory, MANAGER_PROMPT
from app.agents.thinking import manager_tool_loop_thinking_type
from app.agents.patch_builder.deterministic import reduce_review_to_patch
from app.agents.registry import RoleRegistry
from app.compiler_runtime.runtime import (
    CompilerSupervisionPause,
    CompilerRunCheckpoint,
    EvidenceCompilerRuntime,
    PROMPT_VERSIONS,
    attachment_source_admission,
    compiler_trace_metadata,
    prepare_sources,
    prepared_sources_from_checkpoint,
)
from app.compiler_runtime.consumer import derive_consumer_packet, render_consumer_report
from app.compiler_runtime.transcript import ModelTranscriptHooks
from app.config import Settings, get_settings
from app.context import ContextManager
from app.harness import HarnessRuntime, HarnessRunState
from app.llm import LlmClient, ModelCallRecord
from app.observability.langfuse_tracer import LangfuseTracer, safe_role_input, safe_role_output
from app.observability.model_metrics import mark_first_model_output, mark_model_started, model_ttft_ms
from app.observability.openai_trace_bridge import sdk_run_summary
from app.runtime.agents_sdk import build_run_config, close_run_config_client, run_agent_sync
from app.runtime.checkpoints import RuntimeCheckpointStore
from app.runtime.context_assembler import ContextAssembler, context_budget
from app.runtime.context_partition import build_context_packet, usage_from_result as extract_usage_from_result, with_usage_metrics
from app.runtime.patch_normalizer import PatchNormalizer
from app.runtime.policy_gate import PolicyGate, requires_materials_advice
from app.runtime.recovery_policy import RecoveryPolicy
from app.runtime.retry import is_transient_llm_error
from app.runtime.supervisor_contract import CAPABILITY_CARDS, sorted_specialist_tool_specs
from app.runtime.tool_runtime import ToolRuntime
from app.runtime.trace_recorder import TraceRecorder
from app.session_manager import SessionManager
from app.skills import SkillRegistry
from app.state.case_store import CaseStore
from app.state.attachment_manifest import trusted_sources_for_evidence
from app.state.schemas import (
    AgentTurnRequest,
    AgentTurnResponse,
    EvidenceReviewResult,
    PolicyCheck,
    SupervisorDecision,
)
from app.domain.invoice_requirements import (
    AUTO_DERIVED_COMPILER_REQUIREMENTS,
    default_requirement_required,
    is_known_requirement,
    requirement_kind,
    requirement_label,
)
from app.tools.catalog import ToolCatalog
from app.tools.file_workspace import FileWorkspace, report_paths_for_run


ROLE_TARGETS = {"materials_advisor", "evidence_reviewer", "case_patch_writer", "report_writer"}
MANAGER_PROMPT_VERSION = "supervisor_planner_v2.13_plan_checkpoint"
_SAFE_FINAL_ANSWER_STOP = (
    "本轮最终回复未通过安全校验，因此未提供业务结论。请查看当前案件状态和运行记录。"
)


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
        manager = runner.manager_factory.build(tools, metadata=_manager_metadata(runner, state))
        started = time.perf_counter()
        session = runner.manager_session(state.case_id)
        try:
            result = run_agent_sync(
                manager,
                json.dumps(manager_input, ensure_ascii=False, default=str),
                max_turns=max(1, state.max_steps - state.step_count + 2),
                run_config=runner.run_config(state),
                hooks=_transcript_hooks_for(runner, state),
                session=session,
            )
        finally:
            session.close()
        runner.record_manager_model_call(state, manager_input, result, latency_ms=_elapsed_ms(started))
        interruptions = list(getattr(result, "interruptions", []) or [])
        sdk_state = result.to_state().to_string() if interruptions else ""
        return ManagerRunOutcome(
            final_output=str(getattr(result, "final_output", "") or ""),
            interruptions=interruptions,
            sdk_state=sdk_state,
            raw_result=result,
        )

    async def run_streamed(
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
        manager = runner.manager_factory.build(tools, metadata=_manager_metadata(runner, state))
        run_config = runner.run_config(state)
        session = runner.manager_session(state.case_id)
        try:
            started = time.perf_counter()
            result = Runner.run_streamed(
                manager,
                json.dumps(manager_input, ensure_ascii=False, default=str),
                max_turns=max(1, state.max_steps - state.step_count + 2),
                run_config=run_config,
                hooks=_transcript_hooks_for(runner, state),
                session=session,
            )
            mark_model_started(state.observability, "planner")
            runner.emit_stream_event(
                "model_started",
                {"role": "planner", "model": runner.settings.llm_model, "step_count": state.step_count},
                summary="Case manager model call started.",
            )
            async for event in result.stream_events():
                runner.record_sdk_stream_event(state, event)
            runner.record_manager_model_call(state, manager_input, result, latency_ms=_elapsed_ms(started))
            return _manager_outcome_from_result(result)
        finally:
            await close_run_config_client(run_config)
            session.close()

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
        manager = runner.manager_factory.build(tools, metadata=_manager_metadata(runner, state))
        run_state = _await(RunState.from_string(manager, sdk_state, context_override={}))
        interruptions = list(run_state.get_interruptions())
        if not interruptions:
            raise RuntimeError("No pending approval items are available for this run.")
        for item in interruptions:
            if approved:
                run_state.approve(item)
            else:
                run_state.reject(item, rejection_message=reason or "User rejected this tool call.")
        started = time.perf_counter()
        session = runner.manager_session(state.case_id)
        try:
            result = run_agent_sync(
                manager,
                run_state,
                max_turns=max(1, state.max_steps - state.step_count + 2),
                run_config=runner.run_config(state),
                hooks=_transcript_hooks_for(runner, state),
                session=session,
            )
        finally:
            session.close()
        runner.record_manager_model_call(
            state,
            {"approval_resume": approved, "reason": reason},
            result,
            latency_ms=_elapsed_ms(started),
        )
        new_interruptions = list(getattr(result, "interruptions", []) or [])
        return ManagerRunOutcome(
            final_output=str(getattr(result, "final_output", "") or ""),
            interruptions=new_interruptions,
            sdk_state=result.to_state().to_string() if new_interruptions else "",
            raw_result=result,
        )

    async def resume_streamed(
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
        manager = runner.manager_factory.build(tools, metadata=_manager_metadata(runner, state))
        loaded_state = RunState.from_string(manager, sdk_state, context_override={})
        run_state = await loaded_state if hasattr(loaded_state, "__await__") else loaded_state
        interruptions = list(run_state.get_interruptions())
        if not interruptions:
            raise RuntimeError("No pending approval items are available for this run.")
        for item in interruptions:
            if approved:
                run_state.approve(item)
            else:
                run_state.reject(item, rejection_message=reason or "User rejected this tool call.")
        run_config = runner.run_config(state)
        session = runner.manager_session(state.case_id)
        try:
            started = time.perf_counter()
            result = Runner.run_streamed(
                manager,
                run_state,
                max_turns=max(1, state.max_steps - state.step_count + 2),
                run_config=run_config,
                hooks=_transcript_hooks_for(runner, state),
                session=session,
            )
            mark_model_started(state.observability, "planner")
            runner.emit_stream_event(
                "model_started",
                {"role": "planner", "model": runner.settings.llm_model, "approval_resume": approved},
                summary="Case manager approval resume started.",
            )
            async for event in result.stream_events():
                runner.record_sdk_stream_event(state, event)
            runner.record_manager_model_call(
                state,
                {"approval_resume": approved, "reason": reason},
                result,
                latency_ms=_elapsed_ms(started),
            )
            return _manager_outcome_from_result(result)
        finally:
            await close_run_config_client(run_config)
            session.close()


def _manager_metadata(runner: TurnRunner, state: HarnessRunState) -> dict[str, Any]:
    context = getattr(runner, "context", None)
    partition = context.prompt_partition_metadata(state, "planner") if context is not None else {}
    return {
        "case_id": state.case_id,
        "run_id": state.run_id,
        "prompt_partition": partition,
    }


def _transcript_hooks_for(runner: Any, state: HarnessRunState) -> Any | None:
    factory = getattr(runner, "transcript_hooks", None)
    return factory(state) if callable(factory) else None


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

    async def run_turn_streamed(
        self,
        request: AgentTurnRequest,
        *,
        run_id: str,
        event_sink: Any | None = None,
    ) -> AgentTurnResponse:
        return await self.runner.run_turn_streamed(request, run_id=run_id, event_sink=event_sink)

    def resume_approval(self, case_id: str, run_id: str, approved: bool, reason: str = "") -> AgentTurnResponse:
        return self.runner.resume_approval(case_id, run_id, approved=approved, reason=reason)

    async def resume_approval_streamed(
        self,
        case_id: str,
        run_id: str,
        approved: bool,
        reason: str = "",
        *,
        event_sink: Any | None = None,
    ) -> AgentTurnResponse:
        return await self.runner.resume_approval_streamed(case_id, run_id, approved=approved, reason=reason, event_sink=event_sink)

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
            tool_catalog=self.tools,
            planner_prompt=MANAGER_PROMPT,
            planner_prompt_file="backend/app/agents/planner/prompt.md",
            hooks_for_state=self.transcript_hooks,
        )
        self.policy_gate = PolicyGate(store=self.store, context=self.context, tool_catalog=self.tools)
        self.recovery = RecoveryPolicy(store=self.store, harness=self.harness, context=self.context)
        self.patch_normalizer = PatchNormalizer()
        self.roles = RoleRegistry(self.llm)
        self.skills = SkillRegistry()
        self.manager_factory = CaseManagerAgentFactory(self.settings)
        self.manager_runner = manager_runner or SdkManagerRunner()
        self.checkpoints = RuntimeCheckpointStore(self.store)
        self._stream_emit: Any | None = None
        self._run_lock_guard = threading.Lock()
        self._run_locks: dict[str, threading.RLock] = {}
        self._transcript_hooks: dict[str, ModelTranscriptHooks] = {}

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
            hooks_token = self.llm.bind_runtime_hooks(self.transcript_hooks(state))
            try:
                return self._run_until_final(request, state)
            finally:
                try:
                    self._flush_transcript(state)
                    self.harness.write_trace(state)
                finally:
                    self.llm.reset_runtime_hooks(hooks_token)

    async def run_turn_streamed(
        self,
        request: AgentTurnRequest,
        *,
        run_id: str,
        event_sink: Any | None = None,
    ) -> AgentTurnResponse:
        if not request.message.strip():
            raise ValueError("message is required")
        self.llm.calls.clear()
        prior_emit = self._stream_emit
        self._stream_emit = event_sink
        state: HarnessRunState | None = None
        lag_stop: asyncio.Event | None = None
        lag_task: asyncio.Task[None] | None = None
        case_id = self.store.validate_case_id(request.case_id)
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
            try:
                started = time.perf_counter()
                state = await asyncio.to_thread(self.context_assembler.load_context, request, run_id=run_id)
                state.observability.update(turn.to_dict())
                hooks_token = self.llm.bind_runtime_hooks(self.transcript_hooks(state))
                self._record_timing(state, "context_loaded", started)
                self._init_event_loop_lag(state)
                lag_stop = asyncio.Event()
                lag_task = asyncio.create_task(self._monitor_event_loop_lag(state, lag_stop))
                self.emit_stream_event(
                    "context_loaded",
                    {
                        "case_id": state.case_id,
                        "run_id": state.run_id,
                        "session_compacted": state.session_compacted,
                        "pre_run_context_estimate_chars": state.pre_run_context_estimate_chars,
                        "pre_run_context_limit_chars": state.pre_run_context_limit_chars,
                    },
                    summary="Case context loaded.",
                )
                return await self._run_until_final_streamed(request, state)
            finally:
                try:
                    if lag_stop is not None:
                        lag_stop.set()
                    if lag_task is not None:
                        await lag_task
                    if state is not None:
                        self._flush_transcript(state)
                    if state is not None and not state.completed_at and state.phase != "waiting_approval":
                        started = time.perf_counter()
                        self.harness.write_trace(state)
                        self._record_timing(state, "trace_write_finally", started)
                finally:
                    if state is not None:
                        if "hooks_token" in locals():
                            self.llm.reset_runtime_hooks(hooks_token)
                    self._stream_emit = prior_emit

    def resume_approval(self, case_id: str, run_id: str, approved: bool, reason: str = "") -> AgentTurnResponse:
        state, request, sdk_state, interruptions = self.checkpoints.load(case_id, run_id)
        self.harness.reconcile_debug_sequence(state)
        state.pending_approvals = []
        hooks_token = self.llm.bind_runtime_hooks(self.transcript_hooks(state))
        try:
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
                deterministic_report_approval = self._should_resume_report_approval_deterministically(
                    request,
                    state,
                    interruptions,
                )
                self._record_approval_decision(state, interruptions, approved=approved, reason=reason)
                if deterministic_report_approval or not sdk_state:
                    return self._resume_runtime_policy_approval(request, state, approved=approved, reason=reason)
                if hasattr(self.manager_runner, "resume"):
                    outcome = self.manager_runner.resume(  # type: ignore[attr-defined]
                        runner=self,
                        request=request,
                        state=state,
                        sdk_state=sdk_state,
                        approved=approved,
                        reason=reason,
                    )
                    response = self._handle_manager_outcome(request, state, outcome)
                    return response or self._run_until_final(request, state)
                return self._run_until_final(request, state)
        finally:
            try:
                self._flush_transcript(state)
                self.harness.write_trace(state)
            finally:
                self.llm.reset_runtime_hooks(hooks_token)

    async def resume_approval_streamed(
        self,
        case_id: str,
        run_id: str,
        *,
        approved: bool,
        reason: str = "",
        event_sink: Any | None = None,
    ) -> AgentTurnResponse:
        prior_emit = self._stream_emit
        self._stream_emit = event_sink
        lag_stop: asyncio.Event | None = None
        lag_task: asyncio.Task[None] | None = None
        state, request, sdk_state, interruptions = self.checkpoints.load(case_id, run_id)
        self.harness.reconcile_debug_sequence(state)
        state.pending_approvals = []
        hooks_token = self.llm.bind_runtime_hooks(self.transcript_hooks(state))
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
            try:
                state.observability.update(turn.to_dict())
                self._init_event_loop_lag(state)
                lag_stop = asyncio.Event()
                lag_task = asyncio.create_task(self._monitor_event_loop_lag(state, lag_stop))
                self.emit_stream_event(
                    "approval_decision",
                    {"case_id": state.case_id, "run_id": state.run_id, "approved": approved},
                    summary="Approval decision received.",
                )
                deterministic_report_approval = self._should_resume_report_approval_deterministically(
                    request,
                    state,
                    interruptions,
                )
                self._record_approval_decision(state, interruptions, approved=approved, reason=reason)
                if deterministic_report_approval or not sdk_state:
                    return await self._resume_runtime_policy_approval_streamed(request, state, approved=approved, reason=reason)
                if hasattr(self.manager_runner, "resume_streamed"):
                    outcome = await self.manager_runner.resume_streamed(  # type: ignore[attr-defined]
                        runner=self,
                        request=request,
                        state=state,
                        sdk_state=sdk_state,
                        approved=approved,
                        reason=reason,
                    )
                    response = self._handle_manager_outcome(request, state, outcome)
                    return response or await self._run_until_final_streamed(request, state)
                return await self._run_until_final_streamed(request, state)
            finally:
                try:
                    if lag_stop is not None:
                        lag_stop.set()
                    if lag_task is not None:
                        await lag_task
                    self._flush_transcript(state)
                    self.harness.write_trace(state)
                finally:
                    self.llm.reset_runtime_hooks(hooks_token)
                    self._stream_emit = prior_emit

    def _should_resume_report_approval_deterministically(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        interruptions: list[dict[str, Any]],
    ) -> bool:
        if len(interruptions) != 1:
            return False
        tool = str(interruptions[0].get("tool") or "")
        if tool not in {"write_case_file", "render_pdf"}:
            return False
        continuation = self._deterministic_policy_continuation(request, state)
        return continuation is not None and continuation[0] == tool

    def _resume_runtime_policy_approval(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        *,
        approved: bool,
        reason: str,
    ) -> AgentTurnResponse:
        if approved:
            return self._run_until_final(request, state)
        tool = _latest_approval_tool(state)
        self.harness.finalize_run(state, _approval_rejection_answer(tool))
        return self.trace_recorder.finalize_turn(state)

    async def _resume_runtime_policy_approval_streamed(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        *,
        approved: bool,
        reason: str,
    ) -> AgentTurnResponse:
        if approved:
            return await self._run_until_final_streamed(request, state)
        _ = reason
        tool = _latest_approval_tool(state)
        self.harness.finalize_run(state, _approval_rejection_answer(tool))
        return self.trace_recorder.finalize_turn(state)

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
        while not state.completed_at:
            manager_rewrite_pending = bool(state.observability.pop("_manager_final_rewrite_pending", False))
            forced = None if manager_rewrite_pending else self._deterministic_policy_continuation(request, state)
            if state.step_count >= state.max_steps and not manager_rewrite_pending and not forced:
                break
            planner_context = self.context_assembler.build_planner_context(request, state)
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
                    runtime_policy=True,
                )
                if result.get("status") == "approval_required":
                    return self._waiting_approval_response(request, state, "", [result])
                if result.get("status") != "blocked":
                    continue
            runtime_final = "" if manager_rewrite_pending else self._deterministic_final_after_report(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            runtime_final = "" if manager_rewrite_pending else self._deterministic_final_after_patch(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            runtime_final = "" if manager_rewrite_pending else self._deterministic_final_after_materials_advice(request, state)
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
                report_write_waiting = self._recover_report_write_after_manager_max_turns(
                    request,
                    state,
                    planner_context,
                    exc,
                )
                if report_write_waiting is not None:
                    return report_write_waiting
                if self._continue_after_manager_failure(request, state, exc):
                    continue
                self.harness.finalize_run(state, _manager_failure_answer(exc))
                return self.trace_recorder.finalize_turn(state)
            response = self._handle_manager_outcome(request, state, outcome)
            if response is None:
                continue
            if state.completed_at or state.phase == "waiting_approval":
                return response
        if not state.final_answer:
            runtime_final = self._deterministic_final_after_report(request, state) or self._deterministic_final_after_patch(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            self.harness.finalize_run(state, self.harness.step_limit_answer(state))
        return self.trace_recorder.finalize_turn(state)

    async def _run_until_final_streamed(self, request: AgentTurnRequest, state: HarnessRunState) -> AgentTurnResponse:
        while not state.completed_at:
            manager_rewrite_pending = bool(state.observability.pop("_manager_final_rewrite_pending", False))
            forced = None if manager_rewrite_pending else self._deterministic_policy_continuation(request, state)
            if state.step_count >= state.max_steps and not manager_rewrite_pending and not forced:
                break
            started = time.perf_counter()
            planner_context = await asyncio.to_thread(self.context_assembler.build_planner_context, request, state)
            self._record_timing(state, "planner_context", started)
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
                    runtime_policy=True,
                )
                if result.get("status") == "approval_required":
                    return self._waiting_approval_response(request, state, "", [result])
                if result.get("status") != "blocked":
                    continue
            runtime_final = "" if manager_rewrite_pending else self._deterministic_final_after_report(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            runtime_final = "" if manager_rewrite_pending else self._deterministic_final_after_patch(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            runtime_final = "" if manager_rewrite_pending else self._deterministic_final_after_materials_advice(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            manager_input = self._manager_input(request, state, planner_context)
            try:
                if hasattr(self.manager_runner, "run_streamed"):
                    outcome = await self.manager_runner.run_streamed(  # type: ignore[attr-defined]
                        runner=self,
                        request=request,
                        state=state,
                        planner_context=planner_context,
                        manager_input=manager_input,
                    )
                else:
                    outcome = self.manager_runner.run(
                        runner=self,
                        request=request,
                        state=state,
                        planner_context=planner_context,
                        manager_input=manager_input,
                    )
            except Exception as exc:
                self._record_manager_failure(state, manager_input, exc)
                report_write_waiting = self._recover_report_write_after_manager_max_turns(
                    request,
                    state,
                    planner_context,
                    exc,
                )
                if report_write_waiting is not None:
                    return report_write_waiting
                if self._continue_after_manager_failure(request, state, exc):
                    continue
                self.harness.finalize_run(state, _manager_failure_answer(exc))
                return self.trace_recorder.finalize_turn(state)
            response = self._handle_manager_outcome(request, state, outcome)
            if response is None:
                continue
            if state.completed_at or state.phase == "waiting_approval":
                return response
        if not state.final_answer:
            runtime_final = self._deterministic_final_after_report(request, state) or self._deterministic_final_after_patch(request, state)
            if runtime_final:
                return self._finalize_runtime_policy_answer(request, state, runtime_final)
            self.harness.finalize_run(state, self.harness.step_limit_answer(state))
        return self.trace_recorder.finalize_turn(state)

    def _deterministic_policy_continuation(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
    ) -> tuple[str, dict[str, Any]] | None:
        if request.attachments and not _latest_observation(state, kind="tool", name="read_attachment"):
            return "read_attachment", {}
        if _latest_observation(state, kind="tool", name="read_attachment") or _latest_attachment_batch_artifact_ref(state, self.store):
            if not self.context.last_evidence_reviewer_result(state, mode="review"):
                if not _latest_observation_error(state, kind="role", name="evidence_reviewer"):
                    selected = list(state.observability.get("active_requirement_ids") or [])
                    if selected:
                        return "evidence_reviewer", {"mode": "review", "active_requirement_ids": selected}
                    # The current user turn may intentionally narrow or replace an
                    # older case scope. Let Manager interpret that request before
                    # falling back to requirements already stored on the case.
                    return None
        reviewer_index = _latest_observation_index(state, kind="role", name="evidence_reviewer")
        writer_index = _latest_observation_index(state, kind="role", name="case_patch_writer")
        write_index = _latest_observation_index(state, kind="tool", name="write_case_patch")
        if reviewer_index > writer_index and not _latest_observation_error(state, kind="role", name="case_patch_writer"):
            return "case_patch_writer", {}
        if writer_index > write_index:
            return "write_case_patch", {}
        if _report_requested_message(request.message):
            report = self.context.last_role_result(state, name="report_writer")
            if not report:
                if self.store.load(state.case_id).status != "ready_for_report":
                    return None
                return "report_writer", {}
            if not _latest_observation(state, kind="tool", name="write_case_file"):
                return "write_case_file", {}
            if not _markdown_only_report_message(request.message) and not _latest_observation(state, kind="tool", name="render_pdf"):
                return "render_pdf", {}
        return None

    def _deterministic_final_after_patch(self, request: AgentTurnRequest, state: HarnessRunState) -> str:
        if not _latest_observation(state, kind="tool", name="write_case_patch"):
            return ""
        if _report_requested_message(request.message):
            case_state = self.store.load(state.case_id)
            return "" if case_state.status == "ready_for_report" else _runtime_final_answer(case_state, state)
        if requires_materials_advice(request.message):
            return ""
        return _runtime_final_answer(self.store.load(state.case_id), state)

    def _deterministic_final_after_report(self, request: AgentTurnRequest, state: HarnessRunState) -> str:
        if not _report_requested_message(request.message):
            return ""
        if not _latest_observation(state, kind="tool", name="write_case_file"):
            return ""
        markdown_path, pdf_path = report_paths_for_run(state.started_at)
        if _markdown_only_report_message(request.message):
            delivery = ["报告已生成。", f"- Markdown：{markdown_path}"]
        else:
            if not _latest_observation(state, kind="tool", name="render_pdf"):
                return ""
            delivery = [
                "报告已生成。",
                f"- PDF（主要交付）：{pdf_path}",
                f"- Markdown（辅助源文件）：{markdown_path}",
            ]
        proof = getattr(self.store.load(state.case_id), "compiled_proof", None)
        decisions = list(getattr(proof, "decisions", []) or [])
        counts = {
            status: sum(1 for item in decisions if str(getattr(item, "status", "") or "") == status)
            for status in ("SUPPORTED", "CONTRADICTED", "NOT_FOUND")
        }
        delivery.append(
            "- 证据结论："
            f"SUPPORTED {counts['SUPPORTED']}；"
            f"CONTRADICTED {counts['CONTRADICTED']}；"
            f"NOT_FOUND {counts['NOT_FOUND']}。"
        )
        persisted_markdown = self._persisted_report_markdown(state, markdown_path)
        summary = _canonical_report_summary(persisted_markdown)
        if summary:
            delivery.append(f"- 报告摘要：{summary}")
        conflict = next((item for item in decisions if item.status == "CONTRADICTED"), None)
        unresolved = next((item for item in decisions if item.status == "NOT_FOUND"), None)
        if conflict is not None:
            delivery.append(f"- 首个已证实冲突：{conflict.requirement_id}。")
        elif unresolved is not None:
            obligation = next(
                (item for item in list(getattr(proof, "obligations", []) or []) if item.requirement_id == unresolved.requirement_id),
                None,
            )
            detail = f"（{obligation.missing_fact}）" if obligation is not None else ""
            delivery.append(f"- 首个未决项：{unresolved.requirement_id}{detail}。")
        delivery.append("以上是证据状态，不等于业务批准或拒绝。")
        return "\n".join(delivery)

    def _persisted_report_markdown(
        self,
        state: HarnessRunState,
        relative_path: str,
    ) -> str:
        successful_write = any(
            isinstance(call, dict)
            and call.get("tool") == "write_case_file"
            and not str(call.get("error") or "").strip()
            for call in list(getattr(state, "tool_calls", []) or [])
        )
        if not successful_write:
            return ""
        try:
            path = self.store.resolve_case_path(state.case_id, relative_path)
            if not path.is_file():
                return ""
            return path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return ""

    def _deterministic_final_after_materials_advice(self, request: AgentTurnRequest, state: HarnessRunState) -> str:
        if not requires_materials_advice(request.message):
            return ""
        advisor = self.context.last_role_result(state, name="materials_advisor")
        if not isinstance(advisor, dict):
            return ""
        return str(advisor.get("answer") or "").strip()

    def _handle_manager_outcome(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        outcome: ManagerRunOutcome,
    ) -> AgentTurnResponse | None:
        interruptions = list(outcome.interruptions or [])
        if interruptions:
            return self._waiting_approval_response(request, state, outcome.sdk_state, interruptions)
        final_text = str(outcome.final_output or "").strip()
        if final_text:
            report_delivery = self._deterministic_final_after_report(request, state)
            if report_delivery:
                return self._finalize_runtime_policy_answer(request, state, report_delivery)
            return self._finalize_manager_answer(request, state, final_text)
        if not state.final_answer:
            self.harness.finalize_run(state, self.harness.step_limit_answer(state))
        return self.trace_recorder.finalize_turn(state)

    def _continue_after_manager_failure(self, request: AgentTurnRequest, state: HarnessRunState, exc: Exception) -> bool:
        if not _is_recoverable_specialist_error(exc):
            return False
        attempts = int(state.observability.get("manager_failure_continuations") or 0)
        if attempts >= 1:
            return False
        if not (
            self._deterministic_policy_continuation(request, state)
            or self._deterministic_final_after_patch(request, state)
            or self._deterministic_final_after_report(request, state)
            or self._deterministic_final_after_materials_advice(request, state)
        ):
            return False
        state.observability["manager_failure_continuations"] = attempts + 1
        self.harness.append_debug_event(
            state,
            kind="runtime_recovery",
            name="manager_transient_continue",
            payload={
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "attempt": attempts + 1,
            },
            summary="Continuing deterministic runtime path after transient case manager failure.",
            parent_event_id=state.last_model_event_id,
            caused_by_event_id=state.last_model_event_id,
        )
        return True

    def _recover_report_write_after_manager_max_turns(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        planner_context: dict[str, Any],
        exc: Exception,
    ) -> AgentTurnResponse | None:
        if not isinstance(exc, MaxTurnsExceeded):
            return None
        report = self.context.last_role_result(state, name="report_writer")
        if not str(report.get("markdown") or "").strip():
            return None
        continuation = self._deterministic_policy_continuation(request, state)
        if continuation is None or continuation[0] != "write_case_file":
            return None
        name, payload = continuation
        self.harness.append_debug_event(
            state,
            kind="runtime_recovery",
            name="report_write_after_manager_max_turns",
            payload={"error": {"type": type(exc).__name__, "message": str(exc)}, "tool": name},
            summary="Starting deterministic report write approval after case manager max turns.",
            parent_event_id=state.last_model_event_id,
            caused_by_event_id=state.last_model_event_id,
        )
        result = self.invoke_manager_tool(
            state=state,
            request=request,
            planner_context=planner_context,
            name=name,
            payload=payload,
        )
        if result.get("status") != "approval_required":
            return None
        return self._waiting_approval_response(request, state, "", [result])

    def _finalize_manager_answer(
        self,
        request: AgentTurnRequest,
        state: HarnessRunState,
        final_text: str,
    ) -> AgentTurnResponse | None:
        if (
            _latest_observation(state, kind="tool", name="read_attachment")
            and not _latest_observation(state, kind="tool", name="write_case_patch")
        ):
            reviewer_error = _latest_observation_error(state, kind="role", name="evidence_reviewer")
            final_text = (
                "附件已读取，但本轮 evidence_reviewer 输出未通过结构校验；任何证据、Claim 或结论都没有写入案件。请重新发起材料审查。"
                if reviewer_error
                else _runtime_final_answer(self.store.load(state.case_id), state)
            )
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
        report_delivery = self._deterministic_final_after_report(request, state)
        if (
            _latest_observation(state, kind="guard", name="final_answer_internal_retry_instruction")
            and not state.observability.get("manager_final_rewrite_attempted")
            and not report_delivery
        ):
            state.observability["manager_final_rewrite_attempted"] = True
            state.observability["_manager_final_rewrite_pending"] = True
            self.harness.append_debug_event(
                state,
                kind="runtime_recovery",
                name="manager_final_answer_rewrite_requested",
                payload={"guard_feedback": self.context.last_runtime_feedback(state)},
                summary="Asked case manager to rewrite a leaked internal planning preamble.",
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
            return None
        if report_delivery:
            recovered_text, recovered_source = report_delivery, "canonical_report_delivery"
        else:
            recovered_text, recovered_source = self._guard_recovery_final_answer(request, state)
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
                "source": recovered_source,
                "guard_feedback": self.context.last_runtime_feedback(state),
                "final_answer_chars": len(recovered_text),
            },
            summary="Recovered a safe final answer after guard feedback.",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.recovery.handle_final_answer(state, recovered)
        checkpoint_decision = recovered
        if not state.final_answer:
            checkpoint_decision = self._validate_safe_stop_after_guard(state)
        self.trace_recorder.persist_trace_checkpoint(state, checkpoint_decision)
        return self.trace_recorder.finalize_turn(state)

    def _validate_safe_stop_after_guard(
        self,
        state: HarnessRunState,
    ) -> SupervisorDecision:
        decision = SupervisorDecision(
            action="final_answer",
            final_answer=_SAFE_FINAL_ANSWER_STOP,
            reason="fixed safe stop after final-answer guard rejection",
            confidence=1.0,
        )
        self.harness.append_debug_event(
            state,
            kind="runtime_recovery",
            name="final_answer_guard_safe_stop",
            payload={"final_answer_chars": len(_SAFE_FINAL_ANSWER_STOP)},
            summary="Validating a fixed safe stop after final-answer guard rejection.",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.recovery.handle_final_answer(state, decision)
        return decision

    def _guard_recovery_final_answer(self, request: AgentTurnRequest, state: HarnessRunState) -> tuple[str, str]:
        if requires_materials_advice(request.message):
            advisor = self.context.last_role_result(state, name="materials_advisor")
            answer = str(advisor.get("answer") or "").strip() if isinstance(advisor, dict) else ""
            if answer:
                return answer, "materials_advisor"
        return _runtime_final_answer(self.store.load(state.case_id), state), "case_state"

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
        checkpoint_decision = decision
        if not state.final_answer:
            checkpoint_decision = self._validate_safe_stop_after_guard(state)
        self.trace_recorder.persist_trace_checkpoint(state, checkpoint_decision)
        return self.trace_recorder.finalize_turn(state)

    def sdk_tools(
        self,
        *,
        state: HarnessRunState,
        request: AgentTurnRequest,
        planner_context: dict[str, Any],
    ) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        for name, description, input_model in sorted_specialist_tool_specs():
            tools.append(self._sdk_tool(name, description, input_model, state, request, planner_context))
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
        runtime_policy: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self.emit_stream_event(
            "tool_started",
            {"tool": name, "step_count": state.step_count, "phase": state.phase},
            summary=f"{name} started.",
        )
        if state.step_count >= state.max_steps and not runtime_policy:
            result = {"status": "blocked", "reason": "step_limit", "message": self.harness.step_limit_answer(state)}
            self._emit_tool_finished(state, name, result, started)
            return result
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
            result = {"status": "blocked", "policy_check": check.model_dump(mode="json")}
            self._emit_tool_finished(state, name, result, started)
            return result
        self.harness.record_supervisor_decision(state, decision)
        self._record_policy_check(state, decision, check)
        if check.requires_approval and not self._approval_was_granted(state, decision.target):
            self.harness.set_phase(state, "waiting_approval")
            self.harness.write_trace(state)
            result = {"status": "approval_required", "policy_check": check.model_dump(mode="json")}
            self._emit_tool_finished(state, name, result, started)
            return result
        if decision.action == "delegate_agent":
            result = self._call_specialist(state, request, planner_context, decision)
        elif decision.action == "write_case_patch":
            result = self._write_case_patch(state, decision)
        else:
            result = self._call_tool(state, request, decision)
        self.trace_recorder.persist_trace_checkpoint(state, decision)
        self._emit_tool_finished(state, name, result, started)
        return result

    def run_config(self, state: HarnessRunState) -> RunConfig:
        partition = self.context.prompt_partition_metadata(state, "planner")
        return build_run_config(
            self.settings,
            workflow_name="invoice_agent.case_manager",
            trace_metadata={
                "case_id": state.case_id,
                "run_id": state.run_id,
                "runtime": "openai_agents_sdk",
                "timeout_seconds": self.settings.timeout_for_role("planner"),
                **partition,
            },
            timeout_seconds=self.settings.timeout_for_role("planner"),
        )

    def manager_session(self, case_id: str) -> SQLiteSession:
        return SQLiteSession(f"{self.store.validate_case_id(case_id)}:manager", self.settings.session_db_path)

    def transcript_hooks(self, state: HarnessRunState) -> ModelTranscriptHooks:
        hook = self._transcript_hooks.get(state.run_id)
        if hook is None:
            hook = ModelTranscriptHooks(
                self.harness,
                state,
                prompt_versions={
                    "case_manager": MANAGER_PROMPT_VERSION,
                    "planner": MANAGER_PROMPT_VERSION,
                    "task_compiler": PROMPT_VERSIONS["task_compiler"],
                    "executor": PROMPT_VERSIONS["executor"],
                    "fine_verifier": PROMPT_VERSIONS["verifier"],
                    "materials_advisor": self.roles.prompt_version("materials_advisor"),
                    "report_writer": self.roles.prompt_version("report_writer"),
                },
                secret_values=[self.settings.llm_api_key or ""],
            )
            self._transcript_hooks[state.run_id] = hook
        return hook

    def _flush_transcript(self, state: HarnessRunState) -> None:
        hook = self._transcript_hooks.get(state.run_id)
        if hook is not None:
            hook.flush()

    def record_manager_model_call(
        self,
        state: HarnessRunState,
        manager_input: dict[str, Any],
        result: Any,
        *,
        latency_ms: float | None = None,
    ) -> None:
        usage = _usage_from_result(result)
        partition = with_usage_metrics(self.context.prompt_partition_metadata(state, "planner"), usage)
        self.context.update_context_manifest_usage(state, "planner", partition)
        output_preview = _manager_output_preview(result)
        record = ModelCallRecord(
            role="planner",
            model=self.settings.llm_model,
            prompt_version=MANAGER_PROMPT_VERSION,
            input_preview=json.dumps(manager_input, ensure_ascii=False, default=str)[:1400],
            output_preview=output_preview,
            system_prompt=MANAGER_PROMPT,
            payload=manager_input,
            raw_response=output_preview,
            usage=usage,
            latency_ms=latency_ms,
            ttft_ms=model_ttft_ms(state.observability, "planner"),
            content_chars=len(output_preview),
            thinking_type=manager_tool_loop_thinking_type(
                self.settings.llm_model,
                self.settings.manager_thinking_type,
            ),
            prompt_partition=partition,
        )
        self.llm.calls.append(record)
        self.trace_recorder.record_model_call_debug(state)

    def _sync_context_manifest_usage(self, state: HarnessRunState, target: str, role: str) -> None:
        for record in reversed(self.llm.calls):
            if record.role == role and record.prompt_partition:
                self.context.update_context_manifest_usage(state, target, record.prompt_partition)
                return

    def _sync_latest_model_ttft(self, state: HarnessRunState, role: str) -> None:
        ttft_ms = model_ttft_ms(state.observability, role)
        if ttft_ms is None:
            return
        for record in reversed(self.llm.calls):
            if record.role == role and record.ttft_ms is None:
                record.ttft_ms = ttft_ms
                return

    def emit_stream_event(self, kind: str, payload: dict[str, Any] | None = None, *, summary: str = "") -> None:
        if not callable(self._stream_emit):
            return
        try:
            self._stream_emit(kind, payload or {}, summary=summary)
        except Exception:
            return

    def record_sdk_stream_event(self, state: HarnessRunState, event: Any) -> None:
        event_type = str(getattr(event, "type", "") or "")
        if event_type == "agent_updated_stream_event":
            agent = getattr(event, "new_agent", None)
            role = _stream_agent_role(getattr(agent, "name", "") or "manager")
            state.observability["_active_stream_role"] = role
            mark_model_started(state.observability, role)
            self.emit_stream_event(
                "model_started",
                {"role": role, "model": self.settings.llm_model},
                summary="Agent updated.",
            )
            return
        if event_type == "raw_response_event":
            data = getattr(event, "data", None)
            data_type = str(getattr(data, "type", "") or (data.get("type") if isinstance(data, dict) else ""))
            if "reasoning" in data_type or "thinking" in data_type:
                role = str(state.observability.get("_active_stream_role") or "planner")
                mark_first_model_output(state.observability, role)
                self._emit_public_model_working(state, role)
            delta = _stream_text_delta(data)
            if delta:
                role = str(state.observability.get("_active_stream_role") or "planner")
                mark_first_model_output(state.observability, role)
                self.emit_stream_event(
                    "assistant_delta",
                    {"delta": delta, "role": "assistant", "step_count": state.step_count},
                    summary="delta",
                )
            return
        if event_type == "run_item_stream_event":
            name = str(getattr(event, "name", "") or "")
            item = getattr(event, "item", None)
            tool_name = _stream_item_tool_name(item)
            if name == "tool_called" and tool_name:
                self.emit_stream_event("tool_started", {"tool": tool_name, "source": "sdk"}, summary=f"{tool_name} called.")
            elif name == "tool_output" and tool_name:
                self.emit_stream_event("tool_finished", {"tool": tool_name, "source": "sdk", "status": "completed"}, summary=f"{tool_name} output.")
            elif name == "reasoning_item_created":
                role = str(state.observability.get("_active_stream_role") or "planner")
                mark_first_model_output(state.observability, role)
                self._emit_public_model_working(state, role)

    def _emit_public_model_working(self, state: HarnessRunState, role: str) -> None:
        emitted = state.observability.setdefault("_public_model_working", {})
        if not isinstance(emitted, dict):
            emitted = {}
            state.observability["_public_model_working"] = emitted
        key = f"{role}:{state.step_count}"
        if key in emitted:
            return
        emitted[key] = True
        payload = {
            "role": role,
            "model": self.settings.llm_model,
            "stage": role,
            "status": "running",
            "action": "正在分析当前上下文并准备下一步公开结果",
            "public_reason": "模型内部推理保持私密；这里只展示可审计的工作进度。",
            "step_count": state.step_count,
        }
        self.harness.append_debug_event(
            state,
            kind="model_thinking",
            name=role,
            payload=payload,
            summary=payload["action"],
            parent_event_id=state.last_model_event_id or state.last_action_event_id,
            caused_by_event_id=state.last_model_event_id or state.last_action_event_id,
        )
        self.emit_stream_event("model_thinking", payload, summary=payload["action"])

    def _record_timing(self, state: HarnessRunState, name: str, started: float) -> None:
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        timings = state.observability.setdefault("timings", [])
        if isinstance(timings, list):
            timings.append({"name": name, "duration_ms": duration_ms, "ts": time.time()})

    def _lock_for_run(self, run_id: str) -> threading.RLock:
        with self._run_lock_guard:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = threading.RLock()
                self._run_locks[run_id] = lock
            return lock

    def _init_event_loop_lag(self, state: HarnessRunState) -> None:
        state.observability.setdefault(
            "event_loop_lag",
            {"max_ms": 0, "mean_ms": 0, "samples": 0, "p95_ms": 0},
        )

    async def _monitor_event_loop_lag(self, state: HarnessRunState, stop: asyncio.Event, *, interval: float = 0.05) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval
        samples: list[float] = []
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                now = loop.time()
                lag_ms = max(0.0, (now - expected) * 1000)
                samples.append(lag_ms)
                expected = now + interval
                self._update_event_loop_lag(state, samples)
        if samples:
            self._update_event_loop_lag(state, samples)

    def _update_event_loop_lag(self, state: HarnessRunState, samples: list[float]) -> None:
        if not samples:
            return
        ordered = sorted(samples)
        p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
        state.observability["event_loop_lag"] = {
            "max_ms": int(max(ordered)),
            "mean_ms": int(sum(ordered) / len(ordered)),
            "samples": len(ordered),
            "p95_ms": int(ordered[p95_index]),
        }

    def _emit_tool_finished(self, state: HarnessRunState, name: str, result: dict[str, Any], started: float) -> None:
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        self._record_timing(state, f"tool:{name}", started)
        status = str(result.get("status") or ("error" if result.get("error") else "completed"))
        self.emit_stream_event(
            "tool_finished",
            {"tool": name, "status": status, "duration_ms": duration_ms, "phase": state.phase},
            summary=f"{name} {status}.",
        )

    def _record_manager_failure(self, state: HarnessRunState, manager_input: dict[str, Any], exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        partition = self.context.prompt_partition_metadata(state, "planner")
        self.llm.calls.append(
            ModelCallRecord(
                role="planner",
                model=self.settings.llm_model,
                prompt_version=MANAGER_PROMPT_VERSION,
                input_preview=json.dumps(manager_input, ensure_ascii=False, default=str)[:1400],
                output_preview="",
                error=error,
                system_prompt=MANAGER_PROMPT,
                payload=manager_input,
                prompt_partition=partition,
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
                run_lock = self._lock_for_run(state.run_id)

                def call_tool() -> dict[str, Any]:
                    with run_lock:
                        return self.invoke_manager_tool(
                            state=state,
                            request=request,
                            planner_context=planner_context,
                            name=name,
                            payload=parsed,
                        )

                result = await asyncio.to_thread(call_tool)
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
            payload["mode"] = "review"
            selected = list(dict.fromkeys(str(item or "").strip() for item in payload.get("active_requirement_ids") or [] if str(item or "").strip()))
            if not selected:
                selected = list(state.observability.get("active_requirement_ids") or [])
            unknown = [item for item in selected if not is_known_requirement(item)]
            if unknown:
                return {"status": "error", "role": role, "error": {"type": "ValueError", "message": f"Unknown active requirement ids: {unknown}"}}
            if selected:
                payload["active_requirement_ids"] = selected
                state.observability["active_requirement_ids"] = selected
        if role == "case_patch_writer":
            reviewer_result = self.context.last_evidence_reviewer_result(state, mode="review")
            if not reviewer_result:
                return {"status": "error", "role": role, "error": {"type": "ValueError", "message": "No reviewed evidence is available."}}
            result = reduce_review_to_patch(reviewer_result)
            selected = list(state.observability.get("active_requirement_ids") or [])
            if selected:
                existing = {
                    item.id
                    for item in self.store.load(state.case_id).requirements
                    if item.id not in AUTO_DERIVED_COMPILER_REQUIREMENTS
                }
                selected_set = set(selected)
                result["case_updates"]["requirements"] = [
                    {
                        "id": requirement_id,
                        "label": requirement_label(requirement_id),
                        "kind": requirement_kind(requirement_id),
                        "required": default_requirement_required(requirement_id),
                    }
                    for requirement_id in selected
                    if requirement_id not in AUTO_DERIVED_COMPILER_REQUIREMENTS
                ]
                result["case_updates"]["remove_requirements"] = sorted(existing - selected_set)
            capability = self.roles.trace_metadata(role)
            capability.update(
                runtime="deterministic_reducer",
                agent_as_tool=False,
                prompt_version="case_patch_reducer_v1",
                prompt_file="",
                fallback_policy="fail_closed",
            )
            role_input = {"role_result": reviewer_result, "supervisor_task": supervisor_task(decision, state)}
            self.harness.record_role_call(state, role, role_input, result, capability=capability)
            observation = self.context.record_result(state, kind="role", name=role, result=result)
            self.harness.record_observation(state, observation)
            self._update_phase_after_role(state, role, result)
            return _manager_success("role", role, observation=observation)
        if role == "evidence_reviewer":
            return self._call_evidence_compiler(state, request, payload, decision)
        if role == "report_writer":
            try:
                packet = derive_consumer_packet(self.store.load(state.case_id))
                role_input = {
                    "canonical_consumer_packet": packet.model_dump(mode="json"),
                    "user_request": request.message,
                }
                result = {"title": "final_report", "markdown": render_consumer_report(packet)}
                capability = self.roles.trace_metadata(role)
                capability.update(
                    runtime="deterministic_report_renderer",
                    agent_as_tool=False,
                    prompt_version="canonical_report_renderer_v1",
                    prompt_file="",
                    fallback_policy="fail_closed",
                )
                self.harness.record_role_call(state, role, role_input, result, capability=capability)
                observation = self.context.record_result(state, kind="role", name=role, result=result)
                self.harness.record_observation(state, observation)
                self._update_phase_after_role(state, role, result)
                return _manager_success("role", role, observation=observation)
            except Exception as exc:
                self.harness.record_role_call(state, role, {}, {}, error=f"{type(exc).__name__}: {exc}")
                self.harness.record_observation(state, self.context.record_error(kind="role", name=role, exc=exc))
                return {"status": "error", "role": role, "error": {"type": type(exc).__name__, "message": str(exc)}}
        role_capability = self.roles.trace_metadata(role)
        role_input = self.context_assembler.hydrate_role_input(state, role, payload, request.message)
        role_input["supervisor_task"] = supervisor_task(decision, state)
        role_prompt_version = self.roles.prompt_version(role)
        role_packet = build_context_packet(
            role=role,
            prompt_version=role_prompt_version,
            prompt_file=self.roles.prompt_file(role),
            system_prompt=self.roles.prompt(role),
            context_payload=role_input,
            settings=self.settings,
            output_model=self.roles.capability(role).output_model,
            role_contract=role_capability,
            tool_catalog=self.tools,
            skills=self.skills,
        )
        role_partition_metadata = role_packet.debug_metadata()
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
            partition_metadata=role_packet.manifest_metadata(),
        )
        with self.observability.span(
            f"agent.{role}",
            input=safe_role_input(role, role_input),
            metadata={"role_capability": role_capability, "step_count": state.step_count},
            as_type="agent",
        ) as span:
            specialist_started = time.perf_counter()
            try:
                stream_handler = self._specialist_stream_handler(state, role)
                self._emit_specialist_progress(state, role, status="started")
                if stream_handler is None:
                    result = self.roles.call(
                        role,
                        role_input,
                        prompt_partition=role_partition_metadata,
                        hooks=self.transcript_hooks(state),
                    )
                else:
                    result = self.roles.call(
                        role,
                        role_input,
                        on_stream=stream_handler,
                        prompt_partition=role_partition_metadata,
                        hooks=self.transcript_hooks(state),
                    )
                self._emit_specialist_progress(
                    state,
                    role,
                    status="completed",
                    duration_ms=max(0, int((time.perf_counter() - specialist_started) * 1000)),
                )
                _mark_prior_specialist_errors_recovered(
                    state,
                    self.llm.calls,
                    role=role,
                    recovered_by="specialist_retry_success",
                )
                self._sync_latest_model_ttft(state, role)
                self.trace_recorder.record_model_call_debug(state)
                self._sync_context_manifest_usage(state, f"role:{role}", role)
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
                return _manager_success(
                    "role",
                    role,
                    observation=observation,
                    result=result if role == "materials_advisor" else None,
                )
            except Exception as exc:
                self._emit_specialist_progress(
                    state,
                    role,
                    status="error",
                    duration_ms=max(0, int((time.perf_counter() - specialist_started) * 1000)),
                    error_type=type(exc).__name__,
                )
                self.harness.record_role_call(state, role, role_input, {}, error=f"{type(exc).__name__}: {exc}", capability=role_capability)
                self.harness.record_observation(state, self.context.record_error(kind="role", name=role, exc=exc))
                span.update(output=safe_role_output({}, error=f"{type(exc).__name__}: {exc}"), level="ERROR", status_message=str(exc))
                return {"status": "error", "role": role, "error": {"type": type(exc).__name__, "message": str(exc)}}

    def _call_evidence_compiler(
        self,
        state: HarnessRunState,
        request: AgentTurnRequest,
        payload: dict[str, Any],
        decision: SupervisorDecision,
    ) -> dict[str, Any]:
        role = "evidence_reviewer"
        state.observability.pop("_pending_review_artifact", None)
        case_state = self.store.load(state.case_id)
        requested_compiler_run_id = str(payload.get("compiler_run_id") or "").strip()
        resume_parent_run_id = ""
        resume_checkpoint: CompilerRunCheckpoint | None = None
        if requested_compiler_run_id:
            try:
                resume_parent_run_id, raw_checkpoint = self.checkpoints.latest_compiler(
                    state.case_id,
                    requested_compiler_run_id,
                )
                resume_checkpoint = CompilerRunCheckpoint.model_validate(raw_checkpoint)
                if resume_checkpoint.status != "running":
                    raise ValueError("Compiler run is not waiting for a recheck")
                plan_checkpoint = (
                    not resume_checkpoint.active_check_id
                    and not resume_checkpoint.completed_check_ids
                    and not resume_checkpoint.corrections
                )
                if not plan_checkpoint and (
                    not resume_checkpoint.corrections
                    or resume_checkpoint.corrections[-1].kind != "RECHECK"
                ):
                    raise ValueError("Compiler run has no pending RECHECK correction")
            except Exception as exc:
                self.harness.record_observation(
                    state,
                    self.context.record_error(kind="role", name=role, exc=exc),
                )
                return {
                    "status": "error",
                    "role": role,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
        requested = list(payload.get("active_requirement_ids") or [])
        existing = [
            item.id
            for item in case_state.requirements
            if item.id not in AUTO_DERIVED_COMPILER_REQUIREMENTS
        ]
        selected = (
            list(resume_checkpoint.artifact.plan.active_requirement_ids)
            if resume_checkpoint is not None
            else list(dict.fromkeys(requested if requested else existing))
        )
        if not selected:
            exc = ValueError("evidence_reviewer requires at least one active requirement")
            self.harness.record_observation(
                state,
                self.context.record_error(kind="role", name=role, exc=exc),
            )
            return {
                "status": "error",
                "role": role,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        state.observability["active_requirement_ids"] = selected

        attachment_items = [] if resume_checkpoint is not None else self.context.last_attachment_items(state)
        admitted_attachments: list[dict[str, Any]] = []
        rejected_attachments: list[dict[str, Any]] = []
        for index, item in enumerate(attachment_items):
            admitted, reason = attachment_source_admission(item)
            if admitted:
                admitted_attachments.append(dict(item))
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            rejected_attachments.append(
                {
                    "index": index,
                    "attachment_id": str(item.get("attachment_id") or ""),
                    "name": str(item.get("name") or ""),
                    "reason": reason,
                    "status": str(item.get("status") or ""),
                    "manifest_status": str(item.get("manifest_status") or ""),
                    "classification": str(item.get("classification") or metadata.get("classification") or ""),
                }
            )
        if resume_checkpoint is not None:
            prepared_sources = prepared_sources_from_checkpoint(resume_checkpoint)
            extraction_summary: list[dict[str, Any]] = []
        else:
            source_inputs = list(admitted_attachments)
            trusted_ids = set(
                trusted_sources_for_evidence(
                    self.store,
                    state.case_id,
                    case_state.evidence_items,
                )
            )
            source_inputs.extend(
                _persisted_evidence_sources(
                    [item for item in case_state.evidence_items if item.id in trusted_ids]
                )
            )
            prepared_sources = prepare_sources(source_inputs)
            extraction_summary = _compiler_extraction_summary(admitted_attachments)
        task_objective = state.user_message_for_planner or request.message
        source_admission = {
            "attachment_count": len(attachment_items),
            "admitted_attachment_count": len(admitted_attachments),
            "rejected_attachment_count": len(rejected_attachments),
            "prepared_source_count": len(prepared_sources),
            "restored_from_checkpoint": resume_checkpoint is not None,
            "rejected": rejected_attachments,
        }
        state.observability["compiler_source_admission"] = source_admission
        if rejected_attachments:
            self.harness.append_debug_event(
                state,
                kind="compiler_source_admission",
                name="evidence_compiler_runtime",
                payload=source_admission,
                summary=f"Rejected {len(rejected_attachments)} of {len(attachment_items)} new attachment source(s).",
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
        role_input = {
            "task_objective": task_objective,
            "active_requirement_ids": selected,
            "source_catalog": [
                {
                    "source_id": item.record.source_id,
                    "title": item.record.title,
                    "kind": item.record.kind,
                    "characters": len(item.record.content),
                    "already_persisted": bool(item.metadata.get("already_persisted")),
                }
                for item in prepared_sources
            ],
            "extraction_summary": extraction_summary,
            "supervisor_task": supervisor_task(decision, state),
            "compiler_run_id": requested_compiler_run_id,
            "compiler_revision": resume_checkpoint.revision if resume_checkpoint is not None else 1,
        }
        capability = compiler_trace_metadata()
        self.context.write_context_manifest(
            state,
            target=f"role:{role}",
            context_payload=role_input,
            included=list(role_input),
            excluded=["old contracts", "typed holes", "legacy semantic graph", "expected golden labels"],
            blocked_raw_content=False,
            model=self.llm.settings.llm_model,
            prompt_file="backend/app/compiler_runtime/prompts/",
            system_prompt="Task Compiler + sandbox Executor + Fine Verifier",
            budget=context_budget(state),
            raw_leak_checks=["expected_golden_labels"],
            compact_triggered=state.session_compacted,
            metadata={
                "role_capability": capability,
                "task_objective": task_objective,
            },
        )
        with self.observability.span(
            "agent.evidence_compiler_runtime",
            input=safe_role_input(role, role_input),
            metadata={
                "role_capability": capability,
                "step_count": state.step_count,
                "task_objective": task_objective,
            },
            as_type="agent",
        ) as span:
            try:
                runtime = EvidenceCompilerRuntime(
                    self.llm,
                    hooks=self.transcript_hooks(state),
                    settings=self.settings,
                    progress_sink=self._compiler_progress_sink(state),
                    executor_session_db_path=self.settings.session_db_path,
                )
                compiler_run_id = (
                    resume_checkpoint.compiler_run_id
                    if resume_checkpoint is not None
                    else f"compiler_{state.run_id.removeprefix('run_')}"
                )
                checkpoint_parent_run_id = resume_parent_run_id or state.run_id
                compiled = runtime.run(
                    task_objective=task_objective,
                    active_requirement_ids=selected,
                    prepared_sources=prepared_sources,
                    extraction_summary=extraction_summary,
                    requirement_requiredness={
                        item.id: item.required
                        for item in case_state.requirements
                    },
                    compiler_run_id=compiler_run_id,
                    checkpoint=resume_checkpoint,
                    checkpoint_sink=lambda checkpoint: self.checkpoints.save_compiler(
                        case_id=state.case_id,
                        run_id=checkpoint_parent_run_id,
                        compiler_run_id=checkpoint.compiler_run_id,
                        payload=checkpoint.model_dump(mode="json"),
                    ),
                )
                result = EvidenceReviewResult.model_validate(compiled.review_result).model_dump(mode="json")
                state.observability["_pending_review_artifact"] = compiled.artifact.model_dump(mode="json")
                state.observability["compiler_run"] = {
                    "compiler_run_id": compiler_run_id,
                    "revision": compiled.checkpoint.revision if compiled.checkpoint else 1,
                    "status": compiled.checkpoint.status if compiled.checkpoint else "completed",
                    "completed_check_ids": (
                        list(compiled.checkpoint.completed_check_ids) if compiled.checkpoint else []
                    ),
                    "plan_id": compiled.artifact.plan.plan_id,
                    "plan_hash": compiled.artifact.plan_hash,
                    "evidence_snapshot_hash": compiled.artifact.evidence_snapshot_hash,
                    "checks": len(compiled.artifact.assessments),
                    "claims": len(compiled.artifact.evidence_ir.claims),
                    "retry_count": compiled.retry_count,
                }
                _mark_prior_specialist_errors_recovered(
                    state,
                    self.llm.calls,
                    role=role,
                    recovered_by="compiler_runtime_success",
                )
                self.trace_recorder.record_model_call_debug(state)
                self.harness.record_role_call(state, role, role_input, result, capability=capability)
                observation = self.context.record_result(state, kind="role", name=role, result=result)
                self.harness.record_observation(state, observation)
                self._update_phase_after_role(state, role, result)
                span.update(output=safe_role_output(result))
                return _manager_success("role", role, observation=observation)
            except CompilerSupervisionPause as pause:
                state.observability.pop("_pending_review_artifact", None)
                payload = pause.payload
                plan_ready = str(payload.get("status") or "") == "plan_ready"
                receipt = {
                    "status": "paused",
                    "role": role,
                    "compiler_run_id": str(payload.get("compiler_run_id") or compiler_run_id),
                    "revision": int(payload.get("compiler_revision") or 1),
                    "active_check_id": str((payload.get("focused_check_ids") or [""])[0]),
                    "pause_reason": "PLAN_READY" if plan_ready else "CHECK_RETRY_EXHAUSTED",
                    "diagnostic_codes": list(payload.get("diagnostic_codes") or []),
                    "next_action_hint": "call_tool:inspect_compiler_run",
                }
                state.observability["compiler_run"] = receipt
                self.harness.record_role_call(state, role, role_input, receipt, capability=capability)
                self.harness.record_observation(
                    state,
                    {
                        "kind": "role",
                        "name": role,
                        "status": "paused",
                        "summary": (
                            "Compiler paused after saving a new Proof Plan for Supervisor inspection."
                            if plan_ready
                            else "Compiler paused after the active CHECK exhausted its bounded retry budget."
                        ),
                        "key_facts": [
                            f"compiler_run_id={receipt['compiler_run_id']}",
                            f"revision={receipt['revision']}",
                            f"active_check_id={receipt['active_check_id']}",
                        ],
                        "risks": (
                            [] if plan_ready else ["No candidate from the active CHECK was committed."]
                        ),
                        "missing_items": [],
                        "next_action_hint": receipt["next_action_hint"],
                        "must_preserve_refs": [receipt["compiler_run_id"]],
                    },
                )
                span.update(output=safe_role_output(receipt))
                return receipt
            except Exception as exc:
                state.observability.pop("_pending_review_artifact", None)
                receipt = {
                    "status": "error",
                    "role": role,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
                try:
                    _parent_run_id, saved = self.checkpoints.latest_compiler(
                        state.case_id,
                        compiler_run_id,
                    )
                except FileNotFoundError:
                    saved = None
                if saved is not None:
                    receipt.update(
                        {
                            "compiler_run_id": str(saved.get("compiler_run_id") or compiler_run_id),
                            "revision": int(saved.get("revision") or 1),
                            "active_check_id": str(saved.get("active_check_id") or ""),
                            "completed_check_ids": list(saved.get("completed_check_ids") or []),
                            "next_action_hint": "call_tool:inspect_compiler_run",
                        }
                    )
                    state.observability["compiler_run"] = receipt
                self.harness.record_role_call(
                    state,
                    role,
                    role_input,
                    {},
                    error=f"{type(exc).__name__}: {exc}",
                    capability=capability,
                )
                observation = self.context.record_error(kind="role", name=role, exc=exc)
                if saved is not None:
                    observation.update(
                        {
                            "status": "error",
                            "key_facts": [
                                f"compiler_run_id={receipt['compiler_run_id']}",
                                f"revision={receipt['revision']}",
                                f"active_check_id={receipt['active_check_id']}",
                            ],
                            "next_action_hint": receipt["next_action_hint"],
                            "must_preserve_refs": [receipt["compiler_run_id"]],
                        }
                    )
                self.harness.record_observation(state, observation)
                span.update(
                    output=safe_role_output({}, error=f"{type(exc).__name__}: {exc}"),
                    level="ERROR",
                    status_message=str(exc),
                )
                return receipt

    def _compiler_progress_sink(self, state: HarnessRunState) -> Any:
        def emit(kind: str, payload: dict[str, Any], summary: str) -> bool:
            stage = str(payload.get("stage") or payload.get("role") or "evidence_compiler")
            name = str(payload.get("tool") or stage)
            self.harness.append_debug_event(
                state,
                kind=kind,
                name=name,
                payload=payload,
                summary=summary,
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
            self.emit_stream_event(kind, payload, summary=summary)
            return str(payload.get("status") or "") in {"plan_ready", "frontier_rolled_back"}

        return emit

    def _emit_specialist_progress(
        self,
        state: HarnessRunState,
        role: str,
        *,
        status: str,
        duration_ms: int = 0,
        error_type: str = "",
    ) -> None:
        work = {
            "materials_advisor": "整理补料建议",
            "report_writer": "整理审核报告",
        }.get(role, "处理专门任务")
        label = {
            "materials_advisor": "材料顾问",
            "report_writer": "报告撰写员",
        }.get(role, role)
        action = {
            "started": f"{label}正在{work}",
            "completed": f"{label}已完成{work}",
            "error": f"{label}{work}失败",
        }.get(status, f"{label}正在工作")
        public_reason = {
            "materials_advisor": "正在根据当前案卷、证据缺口和适用规则生成可执行的补料清单。",
            "report_writer": "正在根据已验证的证据结论和来源链组织报告。",
        }.get(role, "正在根据当前任务上下文生成结构化结果。")
        payload = {
            "role": role,
            "stage": role,
            "status": status,
            "action": action,
            "public_reason": public_reason,
            "step_count": state.step_count,
        }
        if duration_ms:
            payload["duration_ms"] = duration_ms
        if error_type:
            payload["error_type"] = error_type
        self.harness.append_debug_event(
            state,
            kind="model_thinking",
            name=role,
            payload=payload,
            summary=action,
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.emit_stream_event("model_thinking", payload, summary=action)

    def _specialist_stream_handler(self, state: HarnessRunState, role: str) -> Any | None:
        if not callable(self._stream_emit):
            return None

        def handle(payload: dict[str, Any]) -> None:
            event = payload.get("event") if isinstance(payload, dict) else None
            if event is None:
                return
            state.observability["_active_stream_role"] = role
            self.record_sdk_stream_event(state, event)

        return handle

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
            self.context.last_evidence_reviewer_result(state, mode="review"),
        )
        patch = self.patch_normalizer.compact_for_write(patch)
        try:
            result = self.tool_runtime.call(state, "write_case_patch", {"patch": patch}, internal=True)
            state.observability.pop("_pending_review_artifact", None)
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
        state.pending_approvals = payloads
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
        self.emit_stream_event(
            "approval_required",
            {"case_id": state.case_id, "run_id": state.run_id, "interrupts": payloads},
            summary="Approval is required before continuing.",
        )
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
        state.pending_approvals = []

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
            payload.pop("content", None)
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
        suggested_patch = result.get("suggested_patch") if isinstance(result.get("suggested_patch"), dict) else {}
        if (
            expected == "extract"
            and observed == "review"
            and (result.get("extraction_result") or result.get("extracted_fields"))
            and not (suggested_patch.get("add_evidence") or suggested_patch.get("evidence_items"))
        ):
            result["mode"] = "extract"
            self.harness.append_debug_event(
                state,
                kind="runtime_recovery",
                name="evidence_reviewer_mode_normalized",
                payload={"requested_mode": expected, "returned_mode": observed},
                summary="Normalized extraction-shaped evidence_reviewer output to mode=extract.",
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
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
        if kind == "role" and name == "report_writer":
            payload["content_ref"] = "last_role:report_writer.markdown"
        elif observation.get("artifact_ref"):
            payload["artifact_ref"] = observation.get("artifact_ref")
        if observation.get("next_action_hint"):
            payload["next_action_hint"] = observation.get("next_action_hint")
    preview = _manager_result_preview(name, result)
    if preview is not None:
        payload["result_preview"] = preview
    elif observation and observation.get("artifact_ref") and not (kind == "role" and name == "report_writer"):
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
    if observation.get("kind") == "role" and observation.get("name") == "report_writer":
        safe.pop("artifact_ref", None)
        safe["content_ref"] = "last_role:report_writer.markdown"
    if observation.get("reviewer_mode"):
        safe["reviewer_mode"] = observation.get("reviewer_mode")
    return {key: value for key, value in safe.items() if value not in ("", [], {}, None)}


def _manager_result_preview(name: str, result: dict[str, Any] | None) -> Any | None:
    if not isinstance(result, dict):
        return None
    if name in {"read_attachment", "write_case_patch", "write_case_file", "render_pdf"}:
        return None
    if name in {"inspect_compiler_run", "recheck_compiler_check", "cancel_compiler_run"}:
        return _compact_json_value(result, max_chars=12000)
    if name == "materials_advisor":
        tasks = [item for item in result.get("tasks") or [] if isinstance(item, dict)]
        return {
            "answer_summary": " ".join(str(result.get("answer") or "").split())[:480],
            "tasks": [
                {
                    "requirement": str(item.get("requirement") or "")[:120],
                    "current_status": str(item.get("current_status") or "")[:80],
                    "task": str(item.get("task") or "")[:180],
                    "why_insufficient": str(item.get("why_insufficient") or "")[:240],
                }
                for item in tasks
            ],
            "missing_materials": [
                str(item)[:180] for item in result.get("missing_materials") or [] if str(item).strip()
            ],
            "next_questions": [
                str(item)[:240] for item in result.get("next_questions") or [] if str(item).strip()
            ],
        }
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


def _latest_observation_index(state: HarnessRunState, *, kind: str, name: str) -> int:
    observations = getattr(state, "observations", []) or []
    return next(
        (
            index
            for index in range(len(observations) - 1, -1, -1)
            if isinstance(observations[index], dict)
            and observations[index].get("kind") == kind
            and observations[index].get("name") == name
        ),
        -1,
    )


def _persisted_evidence_sources(evidence_items: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for evidence in evidence_items:
        metadata = getattr(evidence, "metadata", {}) or {}
        review_result = getattr(evidence, "review_result", {}) or {}
        content = str(getattr(evidence, "content", "") or "")
        if not content:
            content = "\n".join(str(item) for item in getattr(evidence, "quoted_text", []) or [])
        if not content.strip():
            continue
        evidence_id = str(getattr(evidence, "id", "") or "").strip()
        if not evidence_id:
            continue
        result.append(
            {
                "source_id": evidence_id,
                "source_content": content,
                "name": str(metadata.get("source_filename") or evidence_id),
                "evidence_type": str(getattr(evidence, "type", "") or "unknown"),
                "attachment_id": str(metadata.get("attachment_id") or ""),
                "original_ref": str(metadata.get("original_ref") or ""),
                "preview_paths": list(metadata.get("preview_paths") or []),
                "extraction_ref": str(metadata.get("extraction_ref") or ""),
                "classification": str(metadata.get("classification") or "unclear"),
                "credibility": str(getattr(evidence, "credibility", "") or "medium"),
                "should_accept": review_result.get("should_accept") if isinstance(review_result, dict) else None,
                "source": str(getattr(evidence, "source", "") or "attachment"),
                "already_persisted": True,
                "source_fingerprint": str(metadata.get("compiler_source_sha256") or ""),
            }
        )
    return result


def _compiler_extraction_summary(attachment_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose document shape to planning, not case answers or golden expectations."""

    result: list[dict[str, Any]] = []
    for item in attachment_items:
        fields = []
        for field in item.get("field_inventory") or []:
            if not isinstance(field, dict):
                continue
            name = str(field.get("field") or field.get("name") or "").strip()
            if name:
                fields.append(name)
        result.append(
            {
                "attachment_id": str(item.get("attachment_id") or ""),
                "name": str(item.get("name") or ""),
                "content_kind": str(item.get("content_kind") or item.get("type") or ""),
                "available_fields": list(dict.fromkeys(fields)),
                "warnings": [str(value) for value in (item.get("warnings") or []) if str(value).strip()],
            }
        )
    return result


def _latest_attachment_batch_artifact_ref(state: HarnessRunState, store: CaseStore) -> str:
    ref = str(_latest_observation(state, kind="tool", name="read_attachment").get("artifact_ref") or "").strip()
    if ref:
        return ref

    ref = str(state.observability.get("latest_attachment_batch_ref") or "").strip()
    if ref:
        return ref

    refs = state.observability.get("attachment_batch_refs")
    if isinstance(refs, list):
        for item in reversed(refs):
            ref = str(item or "").strip()
            if ref:
                return ref

    try:
        root = store.resolve_case_path(state.case_id, f"traces/artifacts/{state.run_id}")
    except Exception:
        return ""
    if not root.exists():
        return ""
    for path in sorted(root.glob("art_*_attachment_batch_read_attachment.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("artifact_type") == "attachment_batch" and data.get("name") == "read_attachment":
            return path.relative_to(store.case_dir(state.case_id)).as_posix()
    return ""


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
        policy_check = item.get("policy_check")
        if isinstance(policy_check, dict) and isinstance(policy_check.get("approval_payload"), dict):
            approval = dict(policy_check.get("approval_payload") or {})
            input_preview = str(approval.get("input_preview") or "{}")[:800]
            return {
                "type": "tool_approval",
                "tool": str(approval.get("tool") or item.get("tool") or item.get("name") or "tool"),
                "risk_level": str(approval.get("risk_level") or policy_check.get("risk_level") or "read"),
                "input_preview": input_preview,
                "input_sha256": str(approval.get("input_sha256") or hashlib.sha256(input_preview.encode("utf-8")).hexdigest()),
                "reason": str(approval.get("reason") or policy_check.get("approval_reason") or "This action requires approval."),
            }
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


def _manager_outcome_from_result(result: Any) -> ManagerRunOutcome:
    interruptions = list(getattr(result, "interruptions", []) or [])
    sdk_state = ""
    if interruptions:
        sdk_state = result.to_state().to_string()
    return ManagerRunOutcome(
        final_output=str(getattr(result, "final_output", "") or ""),
        interruptions=interruptions,
        sdk_state=sdk_state,
        raw_result=result,
    )


def _stream_text_delta(data: Any) -> str:
    event_type = str(getattr(data, "type", "") or "")
    if "reasoning" in event_type or "thinking" in event_type:
        return ""
    if event_type.endswith("output_text.delta") or event_type.endswith("text.delta"):
        return str(getattr(data, "delta", "") or "")
    if event_type.endswith("message.delta"):
        delta = getattr(data, "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
    choices = getattr(data, "choices", None)
    if choices:
        chunks: list[str] = []
        for choice in choices:
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None)
            if content:
                chunks.append(str(content))
        return "".join(chunks)
    return ""


def _stream_agent_role(agent_name: str) -> str:
    name = str(agent_name or "").strip().lower()
    if name in {"case_manager", "manager", "supervisor"}:
        return "planner"
    return name or "planner"


def _stream_item_tool_name(item: Any) -> str:
    raw_item = getattr(item, "raw_item", None) or item
    for attr in ("name", "tool_name"):
        value = getattr(raw_item, attr, None)
        if value:
            return str(value)
    call = getattr(raw_item, "function", None)
    value = getattr(call, "name", None)
    return str(value or "")


def _usage_from_result(result: Any) -> dict[str, Any]:
    return extract_usage_from_result(result)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _runtime_final_answer(case_state: Any, state: HarnessRunState) -> str:
    _ = state
    proof = getattr(case_state, "compiled_proof", None)
    decisions = list(getattr(proof, "decisions", []) or [])
    conflict_ids = _requirement_ids(case_state, "conflict")
    missing_ids = _requirement_ids(case_state, "missing")
    weak_ids = _requirement_ids(case_state, "weak")
    satisfied_ids = _requirement_ids(case_state, "satisfied") + _requirement_ids(case_state, "accepted")
    status = str(getattr(case_state, "status", "") or "")

    if decisions:
        lines = ["本轮 Evidence Compiler 结果已经写入案卷。"]
        for decision in decisions:
            subject = str(getattr(decision, "requirement_id", "") or "unnamed_requirement")
            decision_status = str(getattr(decision, "status", "") or "NOT_FOUND")
            reason = _safe_text(str(getattr(decision, "stop_reason", "") or ""), max_chars=220)
            lines.append(f"- {subject}={decision_status}；{reason or '无补充说明'}。")

        blocking = [
            item
            for item in getattr(proof, "obligations", []) or []
            if bool(getattr(item, "blocking", False))
        ]
        if blocking:
            lines.append("- 尚未完成的核查：")
            for obligation in blocking:
                identity = str(getattr(obligation, "id", "") or getattr(obligation, "check_id", "") or "unnamed_obligation")
                premise = _safe_text(str(getattr(obligation, "missing_fact", "") or "未说明缺失事实"), max_chars=260)
                lines.append(f"  - {identity}：{premise}")
        else:
            lines.append("- 尚未完成的核查：无。")
    else:
        lines = ["本轮本地材料审查已经记录到案卷，下面是当前案卷状态。"]

    lines.append(
        "- 当前案卷状态："
        f"status={status or '-'}；"
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

    if decisions:
        decision_requirements = {
            str(getattr(item, "requirement_id", "") or "")
            for item in decisions
            if str(getattr(item, "requirement_id", "") or "")
        }
        ordinary_blockers = {
            "missing": [item for item in missing_ids if item not in decision_requirements],
            "weak": [item for item in weak_ids if item not in decision_requirements],
            "conflict": [item for item in conflict_ids if item not in decision_requirements],
        }
        reportable_findings = [
            str(getattr(item, "requirement_id", "") or "unnamed_requirement")
            for item in decisions
            if str(getattr(item, "status", "") or "") == "CONTRADICTED"
        ]
        if reportable_findings:
            lines.append(f"可进入报告的已证实发现：{_csv(reportable_findings)}。")
        if status == "ready_for_report" and not any(ordinary_blockers.values()):
            lines.append("结论：所有活动 Requirement 已得到强结论，可进入报告。")
        elif any(str(getattr(item, "status", "") or "") == "NOT_FOUND" for item in decisions):
            lines.append("结论：仍有 NOT_FOUND 原子检查；应按未决核查继续取证，不能写成强结论。")
        elif any(ordinary_blockers.values()):
            lines.append(
                "结论：仍有未完成 Requirements："
                f"missing={_csv(ordinary_blockers['missing'])}；"
                f"weak={_csv(ordinary_blockers['weak'])}；"
                f"conflict={_csv(ordinary_blockers['conflict'])}。"
            )
        else:
            lines.append(f"结论：当前 case_state={status or '-'}，尚未 ready_for_report。")
        lines.append("SUPPORTED、CONTRADICTED、NOT_FOUND 仅表示证据状态，不代表业务批准或拒绝。")
    elif status == "ready_for_report" and not missing_ids and not weak_ids and not conflict_ids:
        lines.append("结论：case_state=ready_for_report；可继续生成本地报告草稿。")
    else:
        lines.append("结论：请按当前 missing、weak、conflict 项继续补料或复核；不要把 weak 项写成 satisfied。")
    return "\n".join(lines)


def _requirement_ids(case_state: Any, status: str) -> list[str]:
    return [
        str(getattr(item, "id", "") or "")
        for item in getattr(case_state, "requirements", []) or []
        if str(getattr(item, "status", "") or "") == status and str(getattr(item, "id", "") or "")
    ][:12]


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
    if any(marker in text for marker in ("modelbehaviorerror", "typeadapter", "validation error", "validationerror", "schema")):
        return False
    return _is_timeout_error(exc) or is_transient_llm_error(exc) or "overloaded" in text


def _report_requested_message(message: str) -> bool:
    text = str(message or "").lower()
    compact = re.sub(r"\s+", "", text)
    if any(
        term in text
        for term in (
            "不要生成报告",
            "不要报告",
            "不生成报告",
            "不需要报告",
            "无需生成报告",
            "无需报告",
            "不用生成报告",
            "不用报告",
            "别生成报告",
            "no report",
            "without report",
            "do not generate report",
            "don't generate report",
        )
    ) or any(term in compact for term in ("不要生成报告", "不要生成pdf", "不生成报告", "不需要报告", "无需报告", "不用生成报告", "不用报告", "别生成报告")):
        return False
    text = text.replace("ready_for_report", "")
    compact = compact.replace("ready_for_report", "")
    if re.search(r"(生成|撰写|写入|写|输出|导出|渲染|出具|制作).{0,16}(报告|pdf)", text, flags=re.I):
        return True
    if re.search(r"(我要|要|需要|给我|来一份).{0,8}(一份)?(报告|pdf)", text, flags=re.I):
        return True
    return any(
        term in text
        for term in (
            "生成报告",
            "最终报告",
            "导出报告",
            "渲染pdf",
            "生成 pdf",
            "pdf report",
            "final report",
            "generate report",
            "write report",
            "render report",
            "export report",
            "i need a report",
            "give me a report",
        )
    ) or any(term in compact for term in ("生成报告", "最终报告", "导出报告", "出具报告", "制作报告", "渲染pdf", "生成pdf", "导出pdf"))


def _markdown_only_report_message(message: str) -> bool:
    text = str(message or "").lower()
    return any(term in text for term in ("只要 markdown", "只要md", "不要 pdf", "不用 pdf", "markdown only", "md only"))


def _canonical_report_summary(markdown: str) -> str:
    """Extract one user-visible paragraph from the already guarded report.

    Its caller supplies the canonical Markdown persisted after the approved
    report write. Returning an empty string preserves deterministic delivery
    when that file is unavailable or lacks the standard heading.
    """

    text = str(markdown or "").replace("\r\n", "\n")
    match = re.search(r"(?im)^#{2,4}\s*(?:摘要结论|结论摘要|摘要)\s*$", text)
    if match is None:
        return ""
    paragraphs = text[match.end() :].strip().split("\n\n")
    for paragraph in paragraphs:
        candidate = " ".join(line.strip() for line in paragraph.splitlines() if line.strip())
        if not candidate or candidate.startswith("#") or candidate.startswith("|"):
            continue
        candidate = re.sub(r"[*_`]", "", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        return candidate[:600].rstrip()
    return ""


def _latest_approval_tool(state: HarnessRunState) -> str:
    for observation in reversed(getattr(state, "observations", []) or []):
        if not isinstance(observation, dict):
            continue
        if observation.get("kind") != "approval":
            continue
        for fact in observation.get("key_facts") or []:
            text = str(fact)
            if text.startswith("tool="):
                return text.split("=", 1)[1]
    return ""


def _approval_rejection_answer(tool: str) -> str:
    if tool == "render_pdf":
        return "已取消执行 render_pdf；Markdown 报告已写入，PDF 未生成。"
    if tool == "write_case_file":
        return "已取消执行 write_case_file；报告文件未写入。"
    return f"已取消执行 {tool}。" if tool else "已取消执行该操作。"


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
