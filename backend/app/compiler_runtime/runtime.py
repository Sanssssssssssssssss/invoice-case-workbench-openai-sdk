from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agents import Agent, FunctionTool, ModelSettings, ToolsToFinalOutputResult
from agents.exceptions import MaxTurnsExceeded
from pydantic import BaseModel, ConfigDict, Field

from app.agents.thinking import model_extra_body_for_thinking, temperature_for_thinking
from app.config import Settings
from app.domain.invoice_requirements import default_requirement_required, requirement_evidence_type
from app.llm import LlmClient, ModelCallRecord
from app.runtime.agents_sdk import FencedJsonOutputSchema, build_run_config, run_agent_sync
from app.runtime.context_partition import usage_from_result
from app.runtime.retry import is_transient_llm_error

from .kernel import compile_review_artifact
from .policy import (
    expand_active_requirements,
    policy_excerpt_for,
    policy_hash,
    required_policy_refs,
    requirement_context,
)
from .models import (
    CheckAssessment,
    CompiledProof,
    EvidenceIR,
    ProofPlan,
    ReviewArtifact,
    explicit_final_statuses,
)
from .sandbox import EvidenceSandbox, SourceRecord


COMPILER_VERSION = "evidence_compiler_runtime_v2"
PROMPT_VERSIONS = {
    "task_compiler": "task_compiler_v8",
    "executor": "evidence_executor_v4",
    "verifier": "fine_verifier_v9",
}
_PROMPT_ROOT = Path(__file__).with_name("prompts")
_TRACE_METADATA = {
    "prompt_version": COMPILER_VERSION,
    "prompt_file": "backend/app/compiler_runtime/prompts/",
    "output_model": "EvidenceReviewResult",
    "context_policy": ["active_requirement_ids", "source_catalog", "extraction_summary", "supervisor_task"],
    "max_retries": 1,
    "allowed_tools": ["list_sources", "read_source", "bind_claim", "submit_check"],
    "side_effects": "none",
    "owner": "route_policy",
    "guard_policy": ["proof_plan_schema", "source_hook", "fine_verifier", "proof_kernel"],
    "fallback_policy": "fail_closed",
    "runtime": "evidence_compiler_runtime",
    "agent_as_tool": False,
}
_EVIDENCE_TYPES = {
    "invoice",
    "purchase_order",
    "goods_receipt",
    "vendor_record",
    "duplicate_payment_check",
    "process_log",
    "clear_invoice_event",
    "payment_terms",
    "policy_excerpt",
    "bpi_event_log",
    "user_statement",
}
_EXCLUDED_SOURCE_STATUSES = {"error", "excluded", "quarantined"}
_EXCLUDED_SOURCE_CLASSIFICATIONS = {
    "cross_case",
    "cross_case_sample",
    "excluded",
    "irrelevant",
    "mixed_case",
    "mixed_case_document",
    "out_of_scope_reference",
    "policy_guidance",
    "prompt_injection",
    "quarantined",
    "wrong_workflow",
}


def compiler_trace_metadata() -> dict[str, Any]:
    """Return the Runtime-owned trace contract without consulting RoleRegistry."""

    return {
        **_TRACE_METADATA,
        "context_policy": list(_TRACE_METADATA["context_policy"]),
        "allowed_tools": list(_TRACE_METADATA["allowed_tools"]),
        "guard_policy": list(_TRACE_METADATA["guard_policy"]),
    }


def attachment_source_admission(item: Mapping[str, Any]) -> tuple[bool, str]:
    """Admit a newly read attachment only when its runtime source boundary is explicit."""

    if str(item.get("status") or "").strip().lower() != "success":
        return False, "attachment_status_not_success"
    manifest_status = str(item.get("manifest_status") or "").strip().lower()
    if manifest_status in _EXCLUDED_SOURCE_STATUSES:
        return False, f"manifest_status_{manifest_status}"
    classification = _source_attribute(item, "classification").lower()
    if classification in _EXCLUDED_SOURCE_CLASSIFICATIONS:
        return False, f"classification_{classification}"
    if _optional_bool(_source_attribute(item, "should_accept")) is False:
        return False, "source_explicitly_not_accepted"
    if any(_true_flag(_source_attribute(item, key)) for key in ("excluded", "quarantined", "cross_case")):
        return False, "source_explicitly_excluded"
    if not any(
        isinstance(item.get(key), str) and bool(str(item.get(key)).strip())
        for key in ("source_content", "body_markdown", "content")
    ):
        return False, "source_content_unreadable"
    return True, "admitted"


class _RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutorSummary(_RuntimeModel):
    completed_check_ids: list[str] = Field(default_factory=list)
    unresolved_check_ids: list[str] = Field(default_factory=list)
    summary: str = ""


class VerificationBatch(_RuntimeModel):
    assessments: list[CheckAssessment]


class _ListSourcesInput(_RuntimeModel):
    pass


class _ReadSourceInput(_RuntimeModel):
    source_id: str


class _BindClaimInput(_RuntimeModel):
    subject: str
    predicate: str
    value: Any
    source_id: str
    quote: str
    locator: str | int
    confidence: str = "medium"
    attributes: dict[str, Any] = Field(default_factory=dict)
    claim_id: str = ""


class _SubmitCheckInput(_RuntimeModel):
    check_id: str
    claim_ids: list[str] = Field(default_factory=list)
    note: str = ""
    submission_id: str = ""


@dataclass(frozen=True)
class PreparedSource:
    record: SourceRecord
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CompilerRunResult:
    artifact: ReviewArtifact
    proof: CompiledProof
    review_result: dict[str, Any]
    retry_count: int


class EvidenceCompilerRuntime:
    """A small plan -> act -> verify loop over an in-memory evidence sandbox."""

    def __init__(
        self,
        llm: LlmClient,
        *,
        hooks: Any | None = None,
        settings: Settings | None = None,
        progress_sink: Callable[[str, dict[str, Any], str], None] | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or llm.settings
        self.hooks = hooks
        self.progress_sink = progress_sink

    def compile_task(
        self,
        *,
        active_requirement_ids: Sequence[str],
        policy_excerpt: dict[str, Any],
        source_catalog: Sequence[dict[str, Any]],
        extraction_summary: Sequence[dict[str, Any]] = (),
    ) -> ProofPlan:
        requirement_ids = _unique(active_requirement_ids)
        self._progress(
            "model_started",
            stage="task_compiler",
            status="started",
            action="正在把审核目标编译成可核查的 Proof Plan",
            public_reason="先明确检查边界和完成条件，再让 Worker 阅读证据。",
            requirement_count=len(requirement_ids),
            source_count=len(source_catalog),
        )
        payload = {
            "active_requirements": requirement_context(requirement_ids),
            "policy": policy_excerpt,
            "source_catalog": _planning_source_catalog(source_catalog),
            "extraction_summary": _planning_extraction_summary(extraction_summary),
            "required_output": {
                "active_requirement_ids": requirement_ids,
                "policy_refs": sorted(required_policy_refs(requirement_ids)),
            },
        }
        plan = self._run_phase(
            name="task_compiler",
            prompt_file="task_compiler.md",
            payload=payload,
            output_type=ProofPlan,
            max_turns=1,
        )
        if plan.active_requirement_ids != requirement_ids:
            self._progress_error("task_compiler", "Proof Plan 改变了活动 Requirement 范围。")
            raise ValueError("Task Compiler changed the ordered active requirement set")
        expected_policy_refs = sorted(required_policy_refs(requirement_ids))
        if sorted(plan.policy_refs) != expected_policy_refs:
            self._progress_error("task_compiler", "Proof Plan 没有完整覆盖适用 Policy。")
            raise ValueError(
                f"Task Compiler policy coverage mismatch: expected={expected_policy_refs}, got={sorted(plan.policy_refs)}"
            )
        self._progress(
            "model_thinking",
            stage="task_compiler",
            status="completed",
            action="Proof Plan 已通过结构校验",
            public_reason="活动 Requirement、Policy 引用和无环结构均已覆盖。",
            requirement_count=len(requirement_ids),
            root_count=len(plan.roots),
            check_count=sum(1 for node in plan.nodes if node.kind == "CHECK"),
        )
        return plan

    def execute_plan(
        self,
        *,
        plan: ProofPlan,
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any],
        sandbox: EvidenceSandbox | None = None,
        focus_check_ids: Sequence[str] = (),
        hook_feedback: Sequence[dict[str, Any]] = (),
    ) -> tuple[ExecutorSummary, EvidenceSandbox]:
        source_records = [item.record for item in prepared_sources]
        check_ids = [node.id for node in plan.nodes if node.kind == "CHECK"]
        sandbox = sandbox or EvidenceSandbox(
            sources=source_records,
            allowed_check_ids=check_ids,
            evidence_ir=EvidenceIR(
                source_ids=sorted(item.source_id for item in source_records),
                source_fingerprints={
                    item.record.source_id: str(item.metadata["source_fingerprint"])
                    for item in prepared_sources
                },
            ),
        )
        payload = {
            "proof_plan": plan.model_dump(mode="json"),
            "policy": policy_excerpt,
            "source_catalog": [
                {
                    "source_id": item.source_id,
                    "title": item.title,
                    "kind": item.kind,
                    "characters": len(item.content),
                }
                for item in sandbox.source_records
            ],
            "focus_check_ids": _unique(focus_check_ids),
            "already_admitted_claims": sandbox.evidence_ir.model_dump(mode="json")["claims"],
            "prior_submissions": [
                {
                    "check_id": item.check_id,
                    "claim_ids": list(item.claim_ids),
                }
                for item in sandbox.submissions
            ],
            "hook_feedback": list(hook_feedback),
        }
        target_check_ids = _unique(focus_check_ids) or check_ids
        self._progress(
            "model_started",
            stage="executor",
            status="started",
            action="Evidence Worker 正在按 Plan 读取来源并绑定 Claim",
            public_reason="Worker 只能通过证据沙箱读取来源、绑定事实和提交检查。",
            source_count=len(source_records),
            target_check_count=len(target_check_ids),
            existing_claim_count=len(sandbox.evidence_ir.claims),
        )
        submission_counts = {
            check_id: sum(1 for item in sandbox.submissions if item.check_id == check_id)
            for check_id in target_check_ids
        }
        try:
            summary = self._run_phase(
                name="executor",
                prompt_file="executor.md",
                payload=payload,
                output_type=ExecutorSummary,
                tools=_sandbox_tools(sandbox, progress_sink=self._sandbox_progress),
                max_turns=6,
                tool_use_behavior=_completion_hook(
                    sandbox,
                    target_check_ids,
                    prior_submission_counts=submission_counts,
                ),
            )
        except MaxTurnsExceeded:
            submitted = {item.check_id for item in sandbox.submissions}
            summary = ExecutorSummary(
                completed_check_ids=sorted(submitted),
                unresolved_check_ids=sorted(set(check_ids) - submitted),
                summary="Executor turn budget exhausted; admitted work was preserved and missing checks remain unresolved.",
            )
        unknown = sorted(
            (set(summary.completed_check_ids) | set(summary.unresolved_check_ids)) - set(check_ids)
        )
        if unknown:
            self._progress_error("executor", "Worker 提交了 Proof Plan 之外的检查。")
            raise ValueError(f"Executor referenced checks outside the ProofPlan: {unknown}")
        self._progress(
            "model_thinking",
            stage="executor",
            status="completed",
            action="Evidence Worker 已完成本轮证据工作",
            public_reason="已采纳的 Claim 与检查提交已冻结，等待独立 Verifier 核查。",
            source_count=len(sandbox.source_records),
            read_source_count=len(sandbox.read_source_ids),
            claim_count=len(sandbox.evidence_ir.claims),
            submitted_check_count=len({item.check_id for item in sandbox.submissions}),
            unresolved_check_count=len(summary.unresolved_check_ids),
        )
        return summary, sandbox

    def verify(
        self,
        *,
        plan: ProofPlan,
        sandbox: EvidenceSandbox,
        policy_excerpt: dict[str, Any],
    ) -> list[CheckAssessment]:
        self._progress(
            "model_started",
            stage="fine_verifier",
            status="started",
            action="Fine Verifier 正在逐项核查原子命题",
            public_reason="Verifier 只读取检查、Claim、原始引用与 Policy，不沿用 Worker 的最终判断。",
            check_count=sum(1 for node in plan.nodes if node.kind == "CHECK"),
            claim_count=len(sandbox.evidence_ir.claims),
        )
        claims = {claim.id: claim for claim in sandbox.evidence_ir.claims}
        submitted_refs = _submitted_claim_refs(sandbox)
        sources = [
            {
                "source_id": source.source_id,
                "title": source.title,
                "kind": source.kind,
                "content": source.content,
                "system_provenance": dict(source.provenance),
            }
            for source in sandbox.source_records
        ]
        visible_source_ids = {item["source_id"] for item in sources}
        admitted_source_ids = set(sandbox.evidence_ir.source_ids)
        if visible_source_ids != admitted_source_ids:
            self._progress_error("fine_verifier", "Verifier 的来源快照与 Evidence IR 不一致。")
            raise ValueError(
                "Fine Verifier source snapshot mismatch: "
                f"missing={sorted(admitted_source_ids - visible_source_ids)}, "
                f"extra={sorted(visible_source_ids - admitted_source_ids)}"
            )
        checks = []
        for node in plan.nodes:
            if node.kind != "CHECK":
                continue
            candidate_ids = submitted_refs.get(node.id, [])
            checks.append(
                {
                    **node.model_dump(mode="json"),
                    "submitted_claim_refs": candidate_ids,
                    "candidate_claims": [
                        claims[claim_id].model_dump(mode="json")
                        for claim_id in candidate_ids
                        if claim_id in claims
                    ],
                }
            )
        payload = {
            "checks": checks,
            "sources": sources,
            "policy": policy_excerpt,
        }
        batch = self._run_phase(
            name="fine_verifier",
            prompt_file="verifier.md",
            payload=payload,
            output_type=VerificationBatch,
            max_turns=1,
        )
        expected = {item["id"] for item in checks}
        actual = [item.check_id for item in batch.assessments]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            self._progress_error("fine_verifier", "Verifier 没有对每个 CHECK 恰好核查一次。")
            raise ValueError(
                f"Fine Verifier must assess every CHECK exactly once: expected={sorted(expected)}, got={sorted(actual)}"
            )
        reconciled: list[CheckAssessment] = []
        corrections: list[dict[str, str]] = []
        for assessment in batch.assessments:
            explicit_statuses = explicit_final_statuses(assessment.reason)
            if len(explicit_statuses) == 1 and assessment.status not in explicit_statuses:
                corrected_status = next(iter(explicit_statuses))
                corrections.append(
                    {
                        "check_id": assessment.check_id,
                        "from": assessment.status,
                        "to": corrected_status,
                    }
                )
                assessment = assessment.model_copy(update={"status": corrected_status})
            reconciled.append(assessment)
        if corrections:
            batch = batch.model_copy(update={"assessments": reconciled})
            self._progress(
                "model_thinking",
                stage="fine_verifier",
                status="reconciled",
                action="Verifier 的显式终态已与结构化状态对齐",
                public_reason="只对齐模型自己明确写出的最终分类，不重新解释业务证据。",
                corrections=corrections,
            )
        counts = {
            status.lower(): sum(1 for item in batch.assessments if item.status == status)
            for status in ("SUPPORTED", "CONTRADICTED", "NOT_FOUND")
        }
        self._progress(
            "model_thinking",
            stage="fine_verifier",
            status="completed",
            action="Fine Verifier 已完成全部原子检查",
            public_reason="三态结果已经引用完整性校验，可交给 Proof Kernel 聚合。",
            check_count=len(batch.assessments),
            supported_count=counts["supported"],
            contradicted_count=counts["contradicted"],
            not_found_count=counts["not_found"],
        )
        return batch.assessments

    def run(
        self,
        *,
        active_requirement_ids: Sequence[str],
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any] | None = None,
        extraction_summary: Sequence[dict[str, Any]] = (),
        requirement_requiredness: Mapping[str, bool] | None = None,
    ) -> CompilerRunResult:
        active_ids = expand_active_requirements(active_requirement_ids)
        requiredness = {
            requirement_id: bool(
                (requirement_requiredness or {}).get(
                    requirement_id,
                    default_requirement_required(requirement_id),
                )
            )
            for requirement_id in active_ids
        }
        policy_excerpt = policy_excerpt or policy_excerpt_for(active_ids)
        plan = self.compile_task(
            active_requirement_ids=active_ids,
            policy_excerpt=policy_excerpt,
            source_catalog=[
                {
                    "source_id": item.record.source_id,
                    "title": item.record.title,
                    "kind": item.record.kind,
                    "characters": len(item.record.content),
                }
                for item in prepared_sources
            ],
            extraction_summary=extraction_summary,
        )
        _, sandbox = self.execute_plan(
            plan=plan,
            prepared_sources=prepared_sources,
            policy_excerpt=policy_excerpt,
        )
        assessments = self.verify(plan=plan, sandbox=sandbox, policy_excerpt=policy_excerpt)
        artifact = _artifact(
            plan=plan,
            evidence_ir=sandbox.evidence_ir,
            assessments=assessments,
            submitted_claim_refs=_submitted_claim_refs(sandbox),
            policy_excerpt=policy_excerpt,
            model=self.settings.llm_model,
        )
        self._progress(
            "model_started",
            stage="proof_kernel",
            status="started",
            action="Proof Kernel 正在聚合三态检查结果",
            public_reason="Kernel 只验证引用与传播三值逻辑，不重新解释业务语义。",
            assessment_count=len(assessments),
        )
        proof = compile_review_artifact(
            artifact,
            requirement_requiredness=requiredness,
        )
        self._emit_kernel_completed(proof)

        blocking_check_ids = sorted(
            {item.check_id for item in proof.obligations if item.blocking}
        )
        retryable = _retryable_checks(plan, blocking_check_ids, policy_excerpt)
        retry_count = 0
        if retryable:
            before_evidence_hash = sandbox.evidence_ir.content_hash()
            before_submitted_refs = _submitted_claim_refs(sandbox)
            _, sandbox = self.execute_plan(
                plan=plan,
                prepared_sources=prepared_sources,
                policy_excerpt=policy_excerpt,
                sandbox=sandbox,
                focus_check_ids=retryable,
                hook_feedback=[
                    {
                        "check_id": item.check_id,
                        "missing_fact": item.missing_fact,
                        "reason": item.reason,
                    }
                    for item in assessments
                    if item.check_id in retryable
                ],
            )
            retry_count = 1
            if (
                sandbox.evidence_ir.content_hash() != before_evidence_hash
                or _submitted_claim_refs(sandbox) != before_submitted_refs
            ):
                assessments = self.verify(plan=plan, sandbox=sandbox, policy_excerpt=policy_excerpt)
                artifact = _artifact(
                    plan=plan,
                    evidence_ir=sandbox.evidence_ir,
                    assessments=assessments,
                    submitted_claim_refs=_submitted_claim_refs(sandbox),
                    policy_excerpt=policy_excerpt,
                    model=self.settings.llm_model,
                )
                self._progress(
                    "model_started",
                    stage="proof_kernel",
                    status="started",
                    action="Proof Kernel 正在重新聚合新增证据",
                    public_reason="主动验证产生了新的有效 Claim，Kernel 将重新计算 DecisionProof。",
                    assessment_count=len(assessments),
                    retry=1,
                )
                proof = compile_review_artifact(
                    artifact,
                    requirement_requiredness=requiredness,
                )
                self._emit_kernel_completed(proof)

        return CompilerRunResult(
            artifact=artifact,
            proof=proof,
            review_result=_review_result(
                prepared_sources=prepared_sources,
                sandbox=sandbox,
                artifact=artifact,
                proof=proof,
            ),
            retry_count=retry_count,
        )

    def _run_phase(
        self,
        *,
        name: str,
        prompt_file: str,
        payload: dict[str, Any],
        output_type: type[BaseModel],
        tools: Sequence[FunctionTool] = (),
        max_turns: int,
        tool_use_behavior: Any = "run_llm_again",
    ) -> Any:
        if not self.llm.available:
            raise RuntimeError("LLM_API_KEY is required for Evidence Compiler execution")
        prompt = (_PROMPT_ROOT / prompt_file).read_text(encoding="utf-8")
        model = self.settings.llm_model
        agent = Agent(
            name=name,
            instructions=prompt,
            model=model,
            model_settings=ModelSettings(
                temperature=temperature_for_thinking(model, self.settings.llm_temperature, "disabled"),
                extra_body=model_extra_body_for_thinking(model, "disabled"),
            ),
            tools=list(tools),
            output_type=FencedJsonOutputSchema(output_type, strict_json_schema=False),
            tool_use_behavior=tool_use_behavior,
        )
        input_text = json.dumps(payload, ensure_ascii=False, default=str)
        prompt_version = PROMPT_VERSIONS[name if name != "fine_verifier" else "verifier"]
        failed_record: ModelCallRecord | None = None
        for attempt in range(2):
            # A failed SDK run closes its client. Build a fresh config so the one
            # visible transport retry cannot reuse a broken connection pool.
            run_config = build_run_config(
                self.settings,
                workflow_name=f"invoice_agent.compiler.{name}",
                trace_metadata={
                    "role": name,
                    "prompt_version": prompt_version,
                    "compiler_version": COMPILER_VERSION,
                    "transport_attempt": attempt + 1,
                },
                timeout_seconds=self.settings.evidence_reviewer_timeout_seconds,
            )
            attempt_started = time.perf_counter()
            try:
                result = run_agent_sync(
                    agent,
                    input_text,
                    max_turns=max_turns,
                    hooks=self.hooks,
                    run_config=run_config,
                )
                parsed = result.final_output
                if not isinstance(parsed, output_type):
                    parsed = output_type.model_validate(parsed)
                raw = parsed.model_dump_json()
                usage = usage_from_result(result)
                if failed_record is not None:
                    failed_record.recovered_by = "compiler_transport_retry_success"
                self.llm.calls.append(
                    ModelCallRecord(
                        role=name,
                        model=model,
                        prompt_version=prompt_version,
                        input_preview=input_text[:1400],
                        output_preview=raw[:1400],
                        system_prompt=prompt,
                        payload=payload,
                        raw_response=raw,
                        usage=usage,
                        latency_ms=round((time.perf_counter() - attempt_started) * 1000, 2),
                        content_chars=len(raw),
                        retry_of=f"{name}:transport_attempt_1" if attempt else "",
                        runtime="evidence_compiler_runtime",
                        thinking_type="disabled",
                    )
                )
                return parsed
            except Exception as exc:
                if self.hooks is not None and hasattr(self.hooks, "record_error"):
                    self.hooks.record_error(exc)
                failed_record = ModelCallRecord(
                    role=name,
                    model=model,
                    prompt_version=prompt_version,
                    input_preview=input_text[:1400],
                    output_preview="",
                    error=f"{type(exc).__name__}: {exc}",
                    system_prompt=prompt,
                    payload=payload,
                    latency_ms=round((time.perf_counter() - attempt_started) * 1000, 2),
                    runtime="evidence_compiler_runtime",
                )
                self.llm.calls.append(failed_record)
                if attempt or not is_transient_llm_error(exc):
                    self._progress(
                        "model_thinking",
                        stage=name,
                        status="error",
                        action=f"{name} 阶段失败",
                        public_reason=f"{type(exc).__name__}: 结构化模型调用没有产出可接受结果。",
                    )
                    raise
                time.sleep(0.25)

        raise RuntimeError(f"Compiler phase {name} exhausted its transport attempts")

    def _emit_kernel_completed(self, proof: CompiledProof) -> None:
        self._progress(
            "model_thinking",
            stage="proof_kernel",
            status="completed",
            action="DecisionProof 已生成",
            public_reason="每个 Requirement 已得到 SUPPORTED、CONTRADICTED 或 NOT_FOUND 结果。",
            supported_count=sum(1 for item in proof.decisions if item.status == "SUPPORTED"),
            contradicted_count=sum(1 for item in proof.decisions if item.status == "CONTRADICTED"),
            not_found_count=sum(1 for item in proof.decisions if item.status == "NOT_FOUND"),
            blocking_obligation_count=sum(1 for item in proof.obligations if item.blocking),
        )

    def _sandbox_progress(self, tool: str, result: dict[str, Any] | None) -> None:
        if result is None:
            self._progress(
                "tool_started",
                stage="executor",
                status="started",
                action=f"Evidence Worker 调用 {tool}",
                public_reason="沙箱正在校验这一步是否满足来源与引用边界。",
                tool=tool,
            )
            return
        accepted = result.get("ok") is True
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = str(error.get("code") or "")
        self._progress(
            "tool_finished",
            stage="executor",
            status="completed" if accepted else "rejected",
            action=(f"{tool} 已完成" if accepted else f"证据 Hook 拒绝了 {tool}"),
            public_reason=str(
                error.get("message")
                or ("沙箱已接受这一步。" if accepted else "这一步没有进入 Evidence IR。")
            ),
            tool=tool,
            hook_code=code,
        )

    def _progress(
        self,
        kind: str,
        *,
        stage: str,
        status: str,
        action: str,
        public_reason: str,
        **counts: Any,
    ) -> None:
        if self.progress_sink is None:
            return
        payload = {
            "role": stage,
            "stage": stage,
            "status": status,
            "action": action,
            "public_reason": public_reason,
            **counts,
        }
        try:
            self.progress_sink(kind, payload, action)
        except Exception:
            return

    def _progress_error(self, stage: str, public_reason: str) -> None:
        self._progress(
            "model_thinking",
            stage=stage,
            status="error",
            action=f"{stage} 未通过 Runtime 校验",
            public_reason=public_reason,
        )


def prepare_sources(items: Iterable[Mapping[str, Any]]) -> list[PreparedSource]:
    """Lower attachment extraction records into readable, run-local text sources."""

    result: list[PreparedSource] = []
    for index, raw in enumerate(items):
        item = dict(raw)
        already_persisted = bool(item.get("already_persisted"))
        supplied_source_id = str(item.get("source_id") or "").strip()
        supplied_content = item.get("source_content")
        supplied_fingerprint = str(item.get("source_fingerprint") or "").strip()
        if already_persisted:
            missing = [
                name
                for name, present in (
                    ("source_id", bool(supplied_source_id)),
                    ("source_content", isinstance(supplied_content, str) and bool(supplied_content.strip())),
                    ("source_fingerprint", bool(supplied_fingerprint)),
                )
                if not present
            ]
            if missing:
                raise ValueError(
                    f"Persisted source at index {index} is missing stable fields: {missing}"
                )
        identity = str(
            item.get("attachment_id")
            or item.get("original_ref")
            or item.get("name")
            or f"source_{index + 1}"
        )
        content = str(supplied_content) if already_persisted else str(supplied_content or "") or _source_text(item)
        if not content.strip():
            continue
        actual_fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if already_persisted and supplied_fingerprint != actual_fingerprint:
            raise ValueError(
                f"Persisted source at index {index} does not match its source_fingerprint"
            )
        source_id = supplied_source_id or f"evc_{_hash({'identity': identity, 'content': content})[:16]}"
        title = str(item.get("name") or identity)
        kind = str(item.get("evidence_type") or item.get("type") or item.get("content_kind") or "unknown")
        classification = _source_attribute(item, "classification") or "unclear"
        credibility = _normalize_credibility(_source_attribute(item, "credibility"))
        provenance = {
            "runtime_admission": "admitted",
            "attachment_id": str(item.get("attachment_id") or ""),
            "original_ref": str(item.get("original_ref") or ""),
            "source_sha256": str(item.get("sha256") or ""),
            "content_sha256": actual_fingerprint,
            "extraction_ref": str(item.get("extraction_ref") or ""),
            "extraction_sha256": str(item.get("extraction_sha256") or ""),
            "preview_paths": list(item.get("preview_paths") or []),
            "scope": "system_chain_of_custody_only_not_real_world_authenticity",
        }
        provenance = {key: value for key, value in provenance.items() if value not in ("", [], None)}
        result.append(
            PreparedSource(
                record=SourceRecord(
                    source_id=source_id,
                    title=title,
                    kind=kind,
                    content=content,
                    provenance=provenance,
                ),
                metadata={
                    "attachment_id": str(item.get("attachment_id") or ""),
                    "original_ref": str(item.get("original_ref") or ""),
                    "source_filename": title,
                    "preview_paths": list(item.get("preview_paths") or []),
                    "extraction_ref": str(item.get("extraction_ref") or ""),
                    "source_doc_id": source_id,
                    "classification": classification,
                    "credibility": credibility,
                    "should_accept": _optional_bool(_source_attribute(item, "should_accept")),
                    "manifest_status": str(item.get("manifest_status") or ""),
                    "source_status": str(item.get("status") or ""),
                    "source": str(item.get("source") or ("attachment" if not already_persisted else "")),
                    "already_persisted": already_persisted,
                    "source_fingerprint": supplied_fingerprint or actual_fingerprint,
                },
            )
        )
    by_id: dict[str, PreparedSource] = {}
    for item in result:
        source_id = item.record.source_id
        existing = by_id.get(source_id)
        if existing is None:
            by_id[source_id] = item
            continue
        if existing.record.content != item.record.content:
            raise ValueError(f"Source id {source_id!r} identifies conflicting content")
        if item.metadata.get("already_persisted") and not existing.metadata.get("already_persisted"):
            by_id[source_id] = item
    return [by_id[key] for key in sorted(by_id)]


def _artifact(
    *,
    plan: ProofPlan,
    evidence_ir: EvidenceIR,
    assessments: list[CheckAssessment],
    submitted_claim_refs: Mapping[str, Sequence[str]],
    policy_excerpt: dict[str, Any],
    model: str,
) -> ReviewArtifact:
    return ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_ir.content_hash(),
        assessments=assessments,
        submitted_claim_refs={
            check_id: _unique(claim_ids)
            for check_id, claim_ids in sorted(submitted_claim_refs.items())
        },
        policy_hash=policy_hash(policy_excerpt),
        unconfigured_policy_refs=_unconfigured_policy_refs(plan, policy_excerpt),
        compiler_version=COMPILER_VERSION,
        model=model,
        prompt_versions=PROMPT_VERSIONS,
    )


def _submitted_claim_refs(sandbox: EvidenceSandbox) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for submission in sandbox.submissions:
        result.setdefault(submission.check_id, [])
        result[submission.check_id].extend(submission.claim_ids)
    return {
        check_id: _unique(claim_ids)
        for check_id, claim_ids in sorted(result.items())
    }


def _sandbox_tools(
    sandbox: EvidenceSandbox,
    *,
    progress_sink: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> list[FunctionTool]:
    async def list_sources(_context: Any, raw: str) -> str:
        _ListSourcesInput.model_validate_json(raw or "{}")
        return _observed_tool("list_sources", sandbox.list_sources)

    async def read_source(_context: Any, raw: str) -> str:
        data = _ReadSourceInput.model_validate_json(raw)
        return _observed_tool("read_source", lambda: sandbox.read_source(data.source_id))

    async def bind_claim(_context: Any, raw: str) -> str:
        data = _BindClaimInput.model_validate_json(raw)
        return _observed_tool("bind_claim", lambda: sandbox.bind_claim(**data.model_dump()))

    async def submit_check(_context: Any, raw: str) -> str:
        data = _SubmitCheckInput.model_validate_json(raw)
        return _observed_tool("submit_check", lambda: sandbox.submit_check(**data.model_dump()))

    def _observed_tool(name: str, invoke: Callable[[], dict[str, Any]]) -> str:
        if progress_sink is not None:
            progress_sink(name, None)
        result = invoke()
        if progress_sink is not None:
            progress_sink(name, result)
        return _tool_json(result)

    return [
        _function_tool("list_sources", "List the evidence sources available in this run.", _ListSourcesInput, list_sources),
        _function_tool("read_source", "Read one source by source_id before binding claims.", _ReadSourceInput, read_source),
        _function_tool("bind_claim", "Append one source-grounded fact or semantic relation to EvidenceIR.", _BindClaimInput, bind_claim),
        _function_tool("submit_check", "Submit candidate claim references and remaining questions for one CHECK; this is not a verdict.", _SubmitCheckInput, submit_check),
    ]


def _completion_hook(
    sandbox: EvidenceSandbox,
    check_ids: Sequence[str],
    *,
    prior_submission_counts: Mapping[str, int] | None = None,
) -> Any:
    expected = set(check_ids)
    baseline = dict(prior_submission_counts or {})

    def complete(_context: Any, _tool_results: Any) -> ToolsToFinalOutputResult:
        submissions_by_check: dict[str, list[Any]] = {check_id: [] for check_id in expected}
        for item in sandbox.submissions:
            if item.check_id in expected:
                submissions_by_check[item.check_id].append(item)
        if any(
            len(submissions_by_check[check_id]) <= int(baseline.get(check_id, 0))
            for check_id in expected
        ):
            return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
        latest = {check_id: items[-1] for check_id, items in submissions_by_check.items()}
        unresolved = sorted(check_id for check_id, item in latest.items() if not item.claim_ids)
        return ToolsToFinalOutputResult(
            is_final_output=True,
            final_output=ExecutorSummary(
                completed_check_ids=sorted(expected - set(unresolved)),
                unresolved_check_ids=unresolved,
                summary="Every executable CHECK was submitted; the Runtime completion hook stopped the worker.",
            ),
        )

    return complete


def _function_tool(name: str, description: str, model: type[BaseModel], callback: Any) -> FunctionTool:
    return FunctionTool(
        name=name,
        description=description,
        params_json_schema=model.model_json_schema(),
        on_invoke_tool=callback,
        strict_json_schema=False,
    )


def _review_result(
    *,
    prepared_sources: Sequence[PreparedSource],
    sandbox: EvidenceSandbox,
    artifact: ReviewArtifact,
    proof: CompiledProof,
) -> dict[str, Any]:
    claims_by_source: dict[str, list[Any]] = {}
    for claim in sandbox.evidence_ir.claims:
        claims_by_source.setdefault(claim.source_id, []).append(claim)
    claims_by_id = {claim.id: claim for claim in sandbox.evidence_ir.claims}
    decisions = {item.requirement_id: item for item in proof.decisions}
    node_results = {item.node_id: item for item in proof.node_results}
    submitted_claim_ids = {
        claim_id
        for submission in sandbox.submissions
        for claim_id in submission.claim_ids
    }
    evidence_items: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for prepared in prepared_sources:
        if prepared.metadata.get("already_persisted"):
            continue
        source_id = prepared.record.source_id
        claims = claims_by_source.get(source_id, [])
        grounded = any(claim.id in submitted_claim_ids for claim in claims)
        explicit_accept = prepared.metadata.get("should_accept")
        should_accept = grounded and explicit_accept is not False
        classification = str(prepared.metadata.get("classification") or "unclear").strip().lower()
        if classification == "unclear" and should_accept:
            classification = "business_evidence"
        credibility = _normalize_credibility(prepared.metadata.get("credibility"))
        supports: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        supported_evidence_types: set[str] = set()
        for requirement_id, decision in decisions.items():
            referenced_claims = [
                claim
                for claim in _decision_claims(decision, node_results, claims_by_id)
                if claim.source_id == source_id
            ]
            if not referenced_claims:
                continue
            quoted_text = "\n".join(_unique(claim.quote for claim in referenced_claims))
            if decision.status == "SUPPORTED":
                declared_evidence_type = requirement_evidence_type(requirement_id)
                if declared_evidence_type:
                    supported_evidence_types.add(declared_evidence_type)
                supports.append(
                    {
                        "requirement": requirement_id,
                        "support_level": "full",
                        "quoted_text": quoted_text,
                    }
                )
            elif decision.status == "CONTRADICTED":
                conflicts.append(
                    {
                        "type": "proof_contradiction",
                        "requirement": requirement_id,
                        "severity": "high",
                        "description": decision.stop_reason,
                        "quoted_text": quoted_text,
                        "affected_evidence_ids": [source_id],
                    }
                )
        evidence_type = prepared.record.kind if prepared.record.kind in _EVIDENCE_TYPES else "unknown"
        if evidence_type == "unknown" and len(supported_evidence_types) == 1:
            candidate_type = next(iter(supported_evidence_types))
            if candidate_type in _EVIDENCE_TYPES:
                evidence_type = candidate_type
        item = {
            "id": source_id,
            "type": evidence_type,
            "credibility": credibility,
            "summary": f"Compiler-reviewed source with {len(claims)} grounded claim(s).",
            "source": str(prepared.metadata.get("source") or "attachment"),
            "content": prepared.record.content,
            "review_result": {
                "should_accept": should_accept,
                "reason": "Source has grounded Claims submitted to a proof check." if should_accept else "Source was not submitted to a proof check.",
                "evidence_type": evidence_type,
            },
            "supports": supports,
            "conflicts": conflicts,
            "quoted_text": _unique([claim.quote for claim in claims]),
            "reviewer_notes": "Admitted through read-before-bind provenance hooks.",
            "metadata": {
                **prepared.metadata,
                "classification": classification,
                "compiler_source_sha256": str(prepared.metadata["source_fingerprint"]),
                "claim_ids": [claim.id for claim in claims],
            },
        }
        evidence_items.append(item)
        cards.append(
            {
                "id": source_id,
                "title": prepared.record.title,
                "summary": item["summary"],
                "claim_ids": item["metadata"]["claim_ids"],
                "should_accept": should_accept,
            }
        )
    obligations = _unique([item.missing_fact for item in proof.obligations if item.missing_fact])
    has_blocking_obligations = any(item.blocking for item in proof.obligations)
    contradicted = sorted(item.requirement_id for item in proof.decisions if item.status == "CONTRADICTED")
    supported: list[dict[str, Any]] = []
    proof_conflicts: list[dict[str, Any]] = []
    for decision in proof.decisions:
        referenced_claims = _decision_claims(decision, node_results, claims_by_id)
        quoted_text = "\n".join(_unique(claim.quote for claim in referenced_claims))
        source_ids = _unique(claim.source_id for claim in referenced_claims)
        if decision.status == "SUPPORTED":
            supported.append(
                {
                    "requirement": decision.requirement_id,
                    "support_level": "full",
                    "quoted_text": quoted_text,
                }
            )
        elif decision.status == "CONTRADICTED":
            proof_conflicts.append(
                {
                    "type": "proof_contradiction",
                    "requirement": decision.requirement_id,
                    "severity": "high",
                    "description": decision.stop_reason,
                    "quoted_text": quoted_text,
                    "affected_evidence_ids": source_ids,
                }
            )
    accepted_items = [item for item in evidence_items if item["review_result"]["should_accept"]]
    accepted_credibility = [str(item["credibility"]) for item in accepted_items]
    overall_credibility = (
        "high"
        if accepted_credibility and all(value == "high" for value in accepted_credibility)
        else "low"
        if accepted_credibility and all(value == "low" for value in accepted_credibility)
        else "medium"
    )
    accepted_types = _unique(
        str(item.get("type") or "")
        for item in accepted_items
        if str(item.get("type") or "") != "unknown"
    )
    overall_evidence_type = accepted_types[0] if len(accepted_types) == 1 else "unknown"
    traceability = _review_traceability(accepted_items)
    return {
        "mode": "review",
        "source_doc_id": ",".join(item.record.source_id for item in prepared_sources),
        "evidence_type": overall_evidence_type,
        "credibility": overall_credibility,
        "extracted_fields": {},
        "extraction_result": {},
        "source_traceability": traceability,
        "support_level": "full" if supported and not has_blocking_obligations else "partial" if evidence_items else "none",
        "risk_flags": contradicted,
        "should_accept": bool(accepted_items),
        "reason": f"Compiled {len(proof.decisions)} requirement proof(s) from {len(sandbox.evidence_ir.claims)} grounded claim(s).",
        "supports": supported,
        "conflicts": proof_conflicts,
        "evidence_cards": cards,
        "suggested_patch": {
            "add_evidence": evidence_items,
            "risk_flags": contradicted,
            "next_questions": obligations,
            "evidence_cards": cards,
        },
        "reply_to_user": "Evidence review compiled into source-grounded proof checks.",
    }


def _decision_claims(
    decision: Any,
    node_results: Mapping[str, Any],
    claims_by_id: Mapping[str, Any],
) -> list[Any]:
    check_ids = (
        decision.supporting_check_ids
        if decision.status == "SUPPORTED"
        else decision.contradicting_check_ids
        if decision.status == "CONTRADICTED"
        else decision.unresolved_check_ids
    )
    claim_ids = _unique(
        claim_id
        for check_id in check_ids
        for claim_id in getattr(node_results.get(check_id), "claim_ids", [])
    )
    return [claims_by_id[claim_id] for claim_id in claim_ids if claim_id in claims_by_id]


def _review_traceability(accepted_items: Sequence[Mapping[str, Any]]) -> str:
    if not accepted_items:
        return "unclear"
    sources = {str(item.get("source") or "") for item in accepted_items}
    if sources == {"attachment"}:
        return "original_document"
    if sources == {"rag"}:
        return "rag_guidance"
    if sources == {"user_message"}:
        return "user_statement"
    return "unclear"


def _source_text(item: dict[str, Any]) -> str:
    lines = [
        f"SOURCE: {item.get('name') or item.get('attachment_id') or 'attachment'}",
        f"ATTACHMENT_ID: {item.get('attachment_id') or ''}",
    ]
    # Attachment dossiers keep a compact Markdown preview alongside the full
    # extracted text. The Worker must receive the full text when it is present.
    body = str(item.get("content") or item.get("body_markdown") or "")
    if body:
        lines.extend(["", "BODY:", body])
    for heading, key in (
        ("FIELDS", "field_inventory"),
        ("BLOCKS", "block_crops"),
        ("PAGES", "page_summaries"),
        ("VISUAL", "visual_check"),
        ("QUALITY", "quality_notes"),
        ("WARNINGS", "warnings"),
    ):
        value = item.get(key)
        if value not in (None, "", [], {}):
            lines.extend(["", f"{heading}:", json.dumps(value, ensure_ascii=False, indent=2, default=str)])
    return "\n".join(lines)


def _planning_source_catalog(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Give planning source shape without binding the plan to files or run-local IDs."""

    grouped: dict[str, dict[str, int | str]] = {}
    for item in items:
        kind = str(item.get("kind") or "unknown").strip() or "unknown"
        entry = grouped.setdefault(kind, {"kind": kind, "count": 0, "total_characters": 0})
        entry["count"] = int(entry["count"]) + 1
        entry["total_characters"] = int(entry["total_characters"]) + max(
            0, int(item.get("characters") or 0)
        )
    return [grouped[kind] for kind in sorted(grouped)]


def _planning_extraction_summary(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate extraction shape; case facts remain work for the sandboxed Executor."""

    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        kind = str(item.get("content_kind") or "unknown").strip() or "unknown"
        entry = grouped.setdefault(
            kind,
            {"content_kind": kind, "document_count": 0, "available_fields": set(), "warning_count": 0},
        )
        entry["document_count"] += 1
        entry["available_fields"].update(_unique(item.get("available_fields") or []))
        entry["warning_count"] += len(item.get("warnings") or [])
    return [
        {
            **entry,
            "available_fields": sorted(entry["available_fields"]),
        }
        for _kind, entry in sorted(grouped.items())
    ]


def _retryable_checks(
    plan: ProofPlan,
    unresolved_check_ids: Sequence[str],
    policy_excerpt: dict[str, Any],
) -> list[str]:
    checks = {node.id: node for node in plan.nodes if node.kind == "CHECK"}
    unconfigured = set(_unconfigured_policy_refs(plan, policy_excerpt))
    return [
        check_id
        for check_id in unresolved_check_ids
        if check_id in checks
        and not set(checks[check_id].policy_refs).intersection(unconfigured)
    ]


def _unconfigured_policy_refs(
    plan: ProofPlan,
    policy_excerpt: Mapping[str, Any],
) -> list[str]:
    raw_values = policy_excerpt.get("values")
    values = raw_values if isinstance(raw_values, Mapping) else {}
    return sorted(
        policy_ref
        for policy_ref in plan.policy_refs
        if not isinstance(values.get(policy_ref), Mapping)
        or values[policy_ref].get("configured") is not True
    )


def _tool_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _source_attribute(item: Mapping[str, Any], key: str) -> str:
    for container in (
        item,
        item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {},
        item.get("review_result") if isinstance(item.get("review_result"), Mapping) else {},
    ):
        value = container.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _true_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _optional_bool(value: Any) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _normalize_credibility(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"low", "medium", "high"} else "medium"


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
