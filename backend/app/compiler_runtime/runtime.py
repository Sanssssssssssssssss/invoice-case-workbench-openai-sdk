from __future__ import annotations

import copy
import hashlib
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agents import Agent, FunctionTool, ModelSettings, ToolsToFinalOutputResult
from agents.exceptions import MaxTurnsExceeded, UserError
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
    ExecutionStatus,
    ProofPlan,
    ReviewArtifact,
    explicit_final_statuses,
)
from .proof_terms import (
    CalculationOperation,
    CalculationWitness,
    ProofTermRef,
    SemanticBindingProposal,
)
from .sandbox import EvidenceSandbox, SourceRecord
from .signatures import (
    PlanConformanceGate,
    proof_signature_for,
    proof_signature_hash_for,
)


COMPILER_VERSION = "typed_evidence_compiler_runtime_v8"
EXECUTOR_MAX_TURNS = 10
PROMPT_VERSIONS = {
    "task_compiler": "typed_task_compiler_v10",
    "executor": "typed_evidence_executor_v8",
    "verifier": "typed_fine_verifier_v13",
}
_PROMPT_ROOT = Path(__file__).with_name("prompts")
_TRACE_METADATA = {
    "prompt_version": COMPILER_VERSION,
    "prompt_file": "backend/app/compiler_runtime/prompts/",
    "output_model": "EvidenceReviewResult",
    "context_policy": ["active_requirement_ids", "source_catalog", "extraction_summary", "supervisor_task"],
    "max_retries": 1,
    "allowed_tools": [
        "list_sources",
        "read_source",
        "bind_claim",
        "compute_witness",
        "submit_check",
    ],
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
    execution_status: ExecutionStatus = "COMPLETED"


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


class _ComputeWitnessInput(_RuntimeModel):
    check_id: str
    facet_ref: str
    operation: CalculationOperation
    refs: list[ProofTermRef]


class _SubmitCheckInput(_RuntimeModel):
    check_id: str
    claim_ids: list[str] = Field(default_factory=list)
    binding_proposals: list[SemanticBindingProposal] = Field(default_factory=list)
    witness_ids: list[str] = Field(default_factory=list)
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
            "proof_signatures": _active_proof_signatures(requirement_ids),
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
        signatures = [
            signature
            for requirement_id in requirement_ids
            if (signature := proof_signature_for(requirement_id)) is not None
        ]
        try:
            PlanConformanceGate(signatures).validate(plan)
        except ValueError:
            self._progress_error(
                "task_compiler",
                "Proof Plan 没有满足 Requirement 的最小 ProofSignature。",
            )
            raise
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
        requested_focus = _unique(focus_check_ids)
        unknown_focus = sorted(set(requested_focus) - set(check_ids))
        if unknown_focus:
            raise ValueError(
                f"Executor focus references checks outside the ProofPlan: {unknown_focus}"
            )
        is_continuation = sandbox is not None
        sandbox = sandbox or EvidenceSandbox(
            sources=source_records,
            allowed_check_ids=check_ids,
            allowed_check_facets={
                node.id: node.facet_refs for node in plan.nodes if node.kind == "CHECK"
            },
            allowed_check_policy_refs={
                node.id: node.policy_refs for node in plan.nodes if node.kind == "CHECK"
            },
            policy_values=_configured_policy_values(policy_excerpt),
            policy_snapshot_hash=policy_hash(policy_excerpt),
            evidence_ir=EvidenceIR(
                source_ids=sorted(item.source_id for item in source_records),
                source_fingerprints={
                    item.record.source_id: str(item.metadata["source_fingerprint"])
                    for item in prepared_sources
                },
            ),
        )
        rollback_sandbox = sandbox
        if requested_focus:
            # Focused execution is speculative until its CHECK ownership is
            # validated. Never let a rejected repair mutate the frozen chain.
            sandbox = copy.deepcopy(sandbox)
        payload = {
            "proof_plan": plan.model_dump(mode="json"),
            "proof_signatures": _active_proof_signatures(plan.active_requirement_ids),
            "calculation_operation_protocol": _calculation_operation_protocol(),
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
            "focus_check_ids": requested_focus,
            "already_admitted_claims": sandbox.evidence_ir.model_dump(mode="json")["claims"],
            "prior_binding_proposals": [
                item.model_dump(mode="json") for item in sandbox.binding_proposals
            ],
            "prior_calculation_witnesses": [
                item.model_dump(mode="json") for item in sandbox.calculation_witnesses
            ],
            "prior_submissions": [
                {
                    "check_id": item.check_id,
                    "claim_ids": list(item.claim_ids),
                    "binding_ids": list(item.binding_ids),
                    "witness_ids": list(item.witness_ids),
                }
                for item in sandbox.submissions
            ],
            "hook_feedback": list(hook_feedback),
        }
        target_check_ids = requested_focus or check_ids
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
            write_scope = (
                sandbox.focused_writes(requested_focus)
                if requested_focus
                else nullcontext()
            )
            with write_scope:
                summary = self._run_phase(
                    name="executor",
                    prompt_file="executor.md",
                    payload=payload,
                    output_type=ExecutorSummary,
                    tools=_sandbox_tools(sandbox, progress_sink=self._sandbox_progress),
                    max_turns=EXECUTOR_MAX_TURNS,
                    tool_use_behavior=_completion_hook(
                        sandbox,
                        target_check_ids,
                        prior_submission_counts=submission_counts,
                    ),
                )
        except UserError:
            submitted = {
                check_id
                for check_id in target_check_ids
                if sum(1 for item in sandbox.submissions if item.check_id == check_id)
                > submission_counts.get(check_id, 0)
            }
            if not submitted:
                raise
            summary = ExecutorSummary(
                completed_check_ids=sorted(submitted),
                unresolved_check_ids=sorted(set(target_check_ids) - submitted),
                summary=(
                    "Executor stopped on an SDK UserError after preserving newly accepted "
                    "CHECK submissions; unsubmitted checks remain unresolved."
                ),
                execution_status="PARTIAL",
            )
        except MaxTurnsExceeded:
            submitted = {
                check_id
                for check_id in target_check_ids
                if sum(1 for item in sandbox.submissions if item.check_id == check_id)
                > submission_counts.get(check_id, 0)
            }
            if not submitted:
                if is_continuation:
                    summary = ExecutorSummary(
                        completed_check_ids=[],
                        unresolved_check_ids=sorted(target_check_ids),
                        summary=(
                            "Executor retry exhausted its turn budget without new proof terms; "
                            "the previously compiled artifact remains available."
                        ),
                        execution_status="PARTIAL",
                    )
                    self._progress(
                        "model_thinking",
                        stage="executor",
                        status="partial",
                        action="Evidence Worker 重试未产生新的证明工件",
                        public_reason="已保留首轮 Artifact，不把重试耗尽伪装成业务缺料。",
                        unresolved_check_count=len(target_check_ids),
                    )
                else:
                    self._progress_error(
                        "executor",
                        "Evidence Worker 超出轮次预算，且没有留下可提交的部分工件。",
                    )
                    raise
            else:
                summary = ExecutorSummary(
                    completed_check_ids=sorted(submitted),
                    unresolved_check_ids=sorted(set(target_check_ids) - submitted),
                    summary="Executor turn budget exhausted; admitted work was preserved and missing checks remain unresolved.",
                    execution_status=(
                        "COMPLETED" if set(target_check_ids) <= submitted else "PARTIAL"
                    ),
                )
        unknown = sorted(
            (set(summary.completed_check_ids) | set(summary.unresolved_check_ids)) - set(check_ids)
        )
        if unknown:
            self._progress_error("executor", "Worker 提交了 Proof Plan 之外的检查。")
            raise ValueError(f"Executor referenced checks outside the ProofPlan: {unknown}")
        outside_focus = sorted(
            (set(summary.completed_check_ids) | set(summary.unresolved_check_ids))
            - set(target_check_ids)
        )
        scope_violation = (
            _focused_candidate_scope_violation(
                rollback_sandbox,
                sandbox,
                focused_check_ids=requested_focus,
            )
            if requested_focus
            else None
        )
        if requested_focus and (outside_focus or scope_violation is not None):
            details = dict(scope_violation or {})
            if outside_focus:
                details["summary_check_ids_outside_focus"] = outside_focus
            self._progress(
                "model_thinking",
                stage="executor",
                status="boundary_violation",
                action="Evidence Worker 聚焦修复越过了 CHECK 写入边界",
                public_reason=(
                    "候选沙箱已整体丢弃；原 Artifact、非聚焦 CHECK 与既有证明工件保持冻结。"
                ),
                violation_code="FOCUSED_CHECK_SCOPE_VIOLATION",
                focused_check_ids=requested_focus,
                **details,
            )
            return (
                ExecutorSummary(
                    completed_check_ids=[],
                    unresolved_check_ids=sorted(target_check_ids),
                    summary=(
                        "Focused Executor candidate crossed its CHECK ownership boundary; "
                        "the candidate was discarded transactionally."
                    ),
                    execution_status="PARTIAL",
                ),
                rollback_sandbox,
            )
        self._progress(
            "model_thinking",
            stage="executor",
            status=("partial" if summary.execution_status == "PARTIAL" else "completed"),
            action=(
                "Evidence Worker 已保留本轮部分证据工作"
                if summary.execution_status == "PARTIAL"
                else "Evidence Worker 已完成本轮证据工作"
            ),
            public_reason=(
                "已采纳的合法工件将继续交给独立 Verifier；未提交 CHECK 保持 unresolved。"
                if summary.execution_status == "PARTIAL"
                else "已采纳的 Claim 与检查提交已冻结，等待独立 Verifier 核查。"
            ),
            source_count=len(sandbox.source_records),
            read_source_count=len(sandbox.read_source_ids),
            claim_count=len(sandbox.evidence_ir.claims),
            submitted_check_count=len({item.check_id for item in sandbox.submissions}),
            unresolved_check_count=len(summary.unresolved_check_ids),
            execution_status=summary.execution_status,
        )
        return summary, sandbox

    def verify(
        self,
        *,
        plan: ProofPlan,
        sandbox: EvidenceSandbox,
        policy_excerpt: dict[str, Any],
        focus_check_ids: Sequence[str] = (),
        repair_feedback: Sequence[dict[str, Any]] = (),
    ) -> list[CheckAssessment]:
        all_check_ids = [node.id for node in plan.nodes if node.kind == "CHECK"]
        requested_check_ids = _unique(focus_check_ids)
        unknown_focus = sorted(set(requested_check_ids) - set(all_check_ids))
        if unknown_focus:
            raise ValueError(
                f"Fine Verifier focus references checks outside the ProofPlan: {unknown_focus}"
            )
        if repair_feedback and not requested_check_ids:
            raise ValueError("Fine Verifier repair_feedback requires explicit focus_check_ids")
        feedback_check_ids = {
            str(item.get("check_id") or "").strip()
            for item in repair_feedback
            if str(item.get("check_id") or "").strip()
        }
        if not feedback_check_ids.issubset(set(requested_check_ids)):
            raise ValueError(
                "Fine Verifier repair_feedback references checks outside focus_check_ids"
            )
        target_check_ids = set(requested_check_ids or all_check_ids)
        self._progress(
            "model_started",
            stage="fine_verifier",
            status="started",
            action="Fine Verifier 正在逐项核查原子命题",
            public_reason="Verifier 只读取检查、Claim、原始引用与 Policy，不沿用 Worker 的最终判断。",
            check_count=len(target_check_ids),
            claim_count=len(sandbox.evidence_ir.claims),
            focused_repair=bool(requested_check_ids),
        )
        claims = {claim.id: claim for claim in sandbox.evidence_ir.claims}
        submitted_refs = _submitted_claim_refs(sandbox)
        submitted_binding_refs = _submitted_binding_refs(sandbox)
        submitted_witness_refs = _submitted_witness_refs(sandbox)
        bindings = {item.id: item for item in sandbox.binding_proposals}
        witnesses = {item.id: item for item in sandbox.calculation_witnesses}
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
            if node.kind != "CHECK" or node.id not in target_check_ids:
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
                    "submitted_binding_refs": submitted_binding_refs.get(node.id, []),
                    "candidate_binding_proposals": [
                        bindings[binding_id].model_dump(mode="json")
                        for binding_id in submitted_binding_refs.get(node.id, [])
                        if binding_id in bindings
                    ],
                    "submitted_witness_refs": submitted_witness_refs.get(node.id, []),
                    "candidate_calculation_witnesses": [
                        witnesses[witness_id].model_dump(mode="json")
                        for witness_id in submitted_witness_refs.get(node.id, [])
                        if witness_id in witnesses
                    ],
                }
            )
        payload = {
            "checks": checks,
            "proof_signatures": _active_proof_signatures(plan.active_requirement_ids),
            "calculation_operation_protocol": _calculation_operation_protocol(),
            "strong_status_link_protocol": _strong_status_link_protocol(),
            "focus_check_ids": requested_check_ids,
            "repair_feedback": list(repair_feedback),
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
            unknown_accepted_bindings = sorted(
                set(assessment.accepted_binding_ids)
                - set(submitted_binding_refs.get(assessment.check_id, []))
            )
            unknown_accepted_witnesses = sorted(
                set(assessment.accepted_witness_ids)
                - set(submitted_witness_refs.get(assessment.check_id, []))
            )
            if unknown_accepted_bindings or unknown_accepted_witnesses:
                self._progress(
                    "model_thinking",
                    stage="fine_verifier",
                    status="boundary_violation",
                    action="Verifier 返回了越权的 Proof Term 引用",
                    public_reason=(
                        "越权引用将保留在 Artifact 中，由 Proof Kernel 显式降级为 NOT_FOUND。"
                    ),
                    check_id=assessment.check_id,
                    unknown_binding_ids=unknown_accepted_bindings,
                    unknown_witness_ids=unknown_accepted_witnesses,
                )
            explicit_statuses = explicit_final_statuses(assessment.reason)
            if (
                not requested_check_ids
                and len(explicit_statuses) == 1
                and assessment.status not in explicit_statuses
            ):
                corrected_status = next(iter(explicit_statuses))
                corrections.append(
                    {
                        "check_id": assessment.check_id,
                        "from": assessment.status,
                        "to": corrected_status,
                    }
                )
                assessment = assessment.model_copy(update={"status": corrected_status})
            polarity_violations = _strong_status_link_boundary_violations(
                assessment,
                witnesses,
            )
            if polarity_violations:
                self._progress(
                    "model_thinking",
                    stage="fine_verifier",
                    status="boundary_violation",
                    action="Verifier 的终端 Witness 极性协议不一致",
                    public_reason=(
                        "错误链接将原样保留，由 Proof Kernel 显式降级；Runtime 不会从当前结果或说明文字反推极性。"
                    ),
                    check_id=assessment.check_id,
                    violation_code="STRONG_STATUS_LINK_POLARITY_CONFLICT",
                    polarity_violations=polarity_violations,
                )
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
        executor_summary, sandbox = self.execute_plan(
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
            submitted_binding_refs=_submitted_binding_refs(sandbox),
            submitted_witness_refs=_submitted_witness_refs(sandbox),
            policy_excerpt=policy_excerpt,
            model=self.settings.llm_model,
            sandbox=sandbox,
            execution_status=(
                "PARTIAL"
                if executor_summary.execution_status == "PARTIAL"
                else _derived_execution_status(plan, sandbox, assessments)
            ),
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

        retry_count = 0
        verifier_repair_attempted: set[str] = set()
        (
            assessments,
            artifact,
            proof,
            verifier_repair_ids,
        ) = self._repair_terminal_status_mismatches_once(
            plan=plan,
            policy_excerpt=policy_excerpt,
            requirement_requiredness=requiredness,
            sandbox=sandbox,
            assessments=assessments,
            artifact=artifact,
            proof=proof,
            exclude_check_ids=verifier_repair_attempted,
        )
        if verifier_repair_ids:
            verifier_repair_attempted.update(verifier_repair_ids)
            retry_count += 1

        terminal_executor_repair_attempted: set[str] = set()
        (
            sandbox,
            assessments,
            artifact,
            proof,
            terminal_executor_repair_ids,
        ) = self._repair_missing_terminal_witnesses_once(
            plan=plan,
            prepared_sources=prepared_sources,
            policy_excerpt=policy_excerpt,
            requirement_requiredness=requiredness,
            sandbox=sandbox,
            assessments=assessments,
            artifact=artifact,
            proof=proof,
            exclude_check_ids=terminal_executor_repair_attempted,
        )
        if terminal_executor_repair_ids:
            terminal_executor_repair_attempted.update(terminal_executor_repair_ids)
            retry_count += 1

        # The focused Executor can add the missing terminal correctly while its
        # focused Verifier still links the boolean result to the wrong strong
        # status.  Route that newly produced, Verifier-owned diagnostic once;
        # never auto-flip a status and never retry the same CHECK again.
        (
            assessments,
            artifact,
            proof,
            verifier_repair_ids,
        ) = self._repair_terminal_status_mismatches_once(
            plan=plan,
            policy_excerpt=policy_excerpt,
            requirement_requiredness=requiredness,
            sandbox=sandbox,
            assessments=assessments,
            artifact=artifact,
            proof=proof,
            exclude_check_ids=verifier_repair_attempted,
        )
        if verifier_repair_ids:
            verifier_repair_attempted.update(verifier_repair_ids)
            retry_count += 1

        blocking_check_ids = sorted(
            {item.check_id for item in proof.obligations if item.blocking}
        )
        retryable = _retryable_checks(
            plan,
            [
                check_id
                for check_id in blocking_check_ids
                if check_id not in verifier_repair_attempted
                and check_id not in terminal_executor_repair_attempted
            ],
            policy_excerpt,
        )
        if retryable:
            before_sandbox_hash = _sandbox_proof_material_hash(sandbox)
            retry_summary, sandbox = self.execute_plan(
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
            retry_count += 1
            if (
                _sandbox_proof_material_hash(sandbox) != before_sandbox_hash
            ):
                assessments = self.verify(plan=plan, sandbox=sandbox, policy_excerpt=policy_excerpt)
                artifact = _artifact(
                    plan=plan,
                    evidence_ir=sandbox.evidence_ir,
                    assessments=assessments,
                    submitted_claim_refs=_submitted_claim_refs(sandbox),
                    submitted_binding_refs=_submitted_binding_refs(sandbox),
                    submitted_witness_refs=_submitted_witness_refs(sandbox),
                    policy_excerpt=policy_excerpt,
                    model=self.settings.llm_model,
                    sandbox=sandbox,
                    execution_status=(
                        "PARTIAL"
                        if retry_summary.execution_status == "PARTIAL"
                        else _derived_execution_status(plan, sandbox, assessments)
                    ),
                )
                self._progress(
                    "model_started",
                    stage="proof_kernel",
                    status="started",
                    action="Proof Kernel 正在重新聚合新增证据",
                    public_reason="主动验证产生了新的有效 Proof Term，Kernel 将重新计算 DecisionProof。",
                    assessment_count=len(assessments),
                    retry=1,
                )
                proof = compile_review_artifact(
                    artifact,
                    requirement_requiredness=requiredness,
                )
                self._emit_kernel_completed(proof)

        # A blocking retry performs a full verification and can itself expose a
        # polarity mismatch. Keep owner routing explicit and bounded per CHECK.
        (
            assessments,
            artifact,
            proof,
            verifier_repair_ids,
        ) = self._repair_terminal_status_mismatches_once(
            plan=plan,
            policy_excerpt=policy_excerpt,
            requirement_requiredness=requiredness,
            sandbox=sandbox,
            assessments=assessments,
            artifact=artifact,
            proof=proof,
            exclude_check_ids=verifier_repair_attempted,
        )
        if verifier_repair_ids:
            verifier_repair_attempted.update(verifier_repair_ids)
            retry_count += 1

        # A blocking-evidence retry can expose a typed closure gap that did not
        # exist in the first Kernel pass (for example, it may add a numeric
        # Witness but omit the required boolean terminal). Give that Executor-
        # owned diagnostic the same single focused opportunity, never a second
        # attempt for checks already handled above.
        (
            sandbox,
            assessments,
            artifact,
            proof,
            terminal_executor_repair_ids,
        ) = self._repair_missing_terminal_witnesses_once(
            plan=plan,
            prepared_sources=prepared_sources,
            policy_excerpt=policy_excerpt,
            requirement_requiredness=requiredness,
            sandbox=sandbox,
            assessments=assessments,
            artifact=artifact,
            proof=proof,
            exclude_check_ids=terminal_executor_repair_attempted,
        )
        if terminal_executor_repair_ids:
            terminal_executor_repair_attempted.update(terminal_executor_repair_ids)
            retry_count += 1

        # The post-blocking terminal repair has the same Executor -> Verifier ->
        # Kernel boundary as the initial one, so it receives the same one-shot
        # Verifier-owner handoff when it creates a polarity mismatch.
        (
            assessments,
            artifact,
            proof,
            verifier_repair_ids,
        ) = self._repair_terminal_status_mismatches_once(
            plan=plan,
            policy_excerpt=policy_excerpt,
            requirement_requiredness=requiredness,
            sandbox=sandbox,
            assessments=assessments,
            artifact=artifact,
            proof=proof,
            exclude_check_ids=verifier_repair_attempted,
        )
        if verifier_repair_ids:
            verifier_repair_attempted.update(verifier_repair_ids)
            retry_count += 1

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

    def _repair_terminal_status_mismatches_once(
        self,
        *,
        plan: ProofPlan,
        policy_excerpt: dict[str, Any],
        requirement_requiredness: Mapping[str, bool],
        sandbox: EvidenceSandbox,
        assessments: Sequence[CheckAssessment],
        artifact: ReviewArtifact,
        proof: CompiledProof,
        exclude_check_ids: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[
        list[CheckAssessment],
        ReviewArtifact,
        CompiledProof,
        list[str],
    ]:
        """Give Verifier-owned terminal polarity mismatches one focused repair.

        The caller owns the per-CHECK attempt ledger. A failed repair preserves
        the frozen Artifact and Kernel result; this method never changes a
        status or StrongStatusLink itself.
        """

        repair_ids = sorted(
            {
                item.node_id
                for item in proof.diagnostics
                if item.code == "TERMINAL_WITNESS_STATUS_MISMATCH"
                and item.node_id
                and item.node_id not in exclude_check_ids
            }
        )
        current_assessments = list(assessments)
        if not repair_ids:
            return current_assessments, artifact, proof, []

        previous_assessments = {
            item.check_id: item
            for item in current_assessments
            if item.check_id in repair_ids
        }
        repair_feedback = [
            {
                "check_id": diagnostic.node_id,
                "diagnostic_code": diagnostic.code,
                "kernel_message": diagnostic.message,
                **(
                    {
                        "previous_assessment": previous_assessments[
                            diagnostic.node_id
                        ].model_dump(mode="json")
                    }
                    if diagnostic.node_id in previous_assessments
                    else {}
                ),
            }
            for diagnostic in proof.diagnostics
            if diagnostic.code == "TERMINAL_WITNESS_STATUS_MISMATCH"
            and diagnostic.node_id in repair_ids
        ]
        self._progress(
            "model_thinking",
            stage="fine_verifier",
            status="repair_started",
            action="Fine Verifier 正在修复终端 Witness 极性协议",
            public_reason=(
                "只重新核查 Kernel 标记为极性不一致的 CHECK，不改写 Witness 或业务证据。"
            ),
            focused_check_ids=repair_ids,
            repair_attempt=1,
        )
        try:
            repaired_assessments = self.verify(
                plan=plan,
                sandbox=sandbox,
                policy_excerpt=policy_excerpt,
                focus_check_ids=repair_ids,
                repair_feedback=repair_feedback,
            )
        except Exception as exc:
            self._progress(
                "model_thinking",
                stage="fine_verifier",
                status="repair_failed",
                action="Fine Verifier 极性修复失败，保留原始 NOT_FOUND",
                public_reason=(
                    f"{type(exc).__name__}: 修复调用没有产生可接受输出；不会自动翻转状态或链接。"
                ),
                focused_check_ids=repair_ids,
                repair_attempt=1,
            )
            return current_assessments, artifact, proof, repair_ids

        candidate_assessments = _merge_assessments(
            current_assessments,
            repaired_assessments,
        )
        candidate_artifact = _artifact(
            plan=plan,
            evidence_ir=sandbox.evidence_ir,
            assessments=candidate_assessments,
            submitted_claim_refs=_submitted_claim_refs(sandbox),
            submitted_binding_refs=_submitted_binding_refs(sandbox),
            submitted_witness_refs=_submitted_witness_refs(sandbox),
            policy_excerpt=policy_excerpt,
            model=self.settings.llm_model,
            sandbox=sandbox,
            execution_status=(
                "PARTIAL"
                if artifact.execution_status == "PARTIAL"
                else _derived_execution_status(plan, sandbox, candidate_assessments)
            ),
        )
        self._progress(
            "model_started",
            stage="proof_kernel",
            status="started",
            action="Proof Kernel 正在核验 Verifier 极性修复",
            public_reason=(
                "修复输出已重新封装为 Artifact；最终状态仍只由 Kernel 投影。"
            ),
            assessment_count=len(candidate_assessments),
            repair_attempt=1,
        )
        candidate_proof = compile_review_artifact(
            candidate_artifact,
            requirement_requiredness=requirement_requiredness,
        )
        self._emit_kernel_completed(candidate_proof)
        return (
            candidate_assessments,
            candidate_artifact,
            candidate_proof,
            repair_ids,
        )

    def _repair_missing_terminal_witnesses_once(
        self,
        *,
        plan: ProofPlan,
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any],
        requirement_requiredness: Mapping[str, bool],
        sandbox: EvidenceSandbox,
        assessments: Sequence[CheckAssessment],
        artifact: ReviewArtifact,
        proof: CompiledProof,
        exclude_check_ids: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[
        EvidenceSandbox,
        list[CheckAssessment],
        ReviewArtifact,
        CompiledProof,
        list[str],
    ]:
        """Give Executor-owned terminal gaps one transactional focused repair.

        The caller controls the per-CHECK one-attempt budget.  This method either
        commits a fully re-verified and recompiled candidate or returns the
        original frozen chain unchanged.
        """

        repair_ids = sorted(
            {
                item.node_id
                for item in proof.diagnostics
                if item.code == "TERMINAL_WITNESS_REQUIRED"
                and item.node_id
                and item.node_id not in exclude_check_ids
            }
        )
        current_assessments = list(assessments)
        if not repair_ids:
            return sandbox, current_assessments, artifact, proof, []

        previous_assessments = {
            item.check_id: item for item in current_assessments if item.check_id in repair_ids
        }
        repair_feedback = [
            {
                "check_id": diagnostic.node_id,
                "diagnostic_code": diagnostic.code,
                "kernel_message": diagnostic.message,
                **(
                    {
                        "previous_assessment": previous_assessments[
                            diagnostic.node_id
                        ].model_dump(mode="json")
                    }
                    if diagnostic.node_id in previous_assessments
                    else {}
                ),
            }
            for diagnostic in proof.diagnostics
            if diagnostic.code == "TERMINAL_WITNESS_REQUIRED"
            and diagnostic.node_id in repair_ids
        ]
        self._progress(
            "model_thinking",
            stage="executor",
            status="terminal_repair_started",
            action="Evidence Worker 正在补全缺失的终端 Witness",
            public_reason=(
                "只重开 Kernel 标记为缺少布尔终端的 CHECK；不会改写其他检查或自动制造结论。"
            ),
            focused_check_ids=repair_ids,
            repair_attempt=1,
        )

        # Work on a private copy so an exception or no-op cannot leak partial
        # Claims/Witnesses into the frozen Artifact or later reporting path.
        candidate_sandbox = copy.deepcopy(sandbox)
        before_hash = _sandbox_proof_material_hash(candidate_sandbox)
        try:
            _summary, candidate_sandbox = self.execute_plan(
                plan=plan,
                prepared_sources=prepared_sources,
                policy_excerpt=policy_excerpt,
                sandbox=candidate_sandbox,
                focus_check_ids=repair_ids,
                hook_feedback=repair_feedback,
            )
            if _sandbox_proof_material_hash(candidate_sandbox) == before_hash:
                self._progress(
                    "model_thinking",
                    stage="executor",
                    status="terminal_repair_no_change",
                    action="Evidence Worker 未补充新的终端证明工件",
                    public_reason="保留原始 NOT_FOUND；无新 Proof Material 时不重跑 Verifier 或 Kernel。",
                    focused_check_ids=repair_ids,
                    repair_attempt=1,
                )
                return sandbox, current_assessments, artifact, proof, repair_ids

            repaired_assessments = self.verify(
                plan=plan,
                sandbox=candidate_sandbox,
                policy_excerpt=policy_excerpt,
                focus_check_ids=repair_ids,
                repair_feedback=repair_feedback,
            )
            candidate_assessments = _merge_assessments(
                current_assessments,
                repaired_assessments,
            )
            candidate_artifact = _artifact(
                plan=plan,
                evidence_ir=candidate_sandbox.evidence_ir,
                assessments=candidate_assessments,
                submitted_claim_refs=_submitted_claim_refs(candidate_sandbox),
                submitted_binding_refs=_submitted_binding_refs(candidate_sandbox),
                submitted_witness_refs=_submitted_witness_refs(candidate_sandbox),
                policy_excerpt=policy_excerpt,
                model=self.settings.llm_model,
                sandbox=candidate_sandbox,
                execution_status=_derived_execution_status(
                    plan,
                    candidate_sandbox,
                    candidate_assessments,
                ),
            )
            self._progress(
                "model_started",
                stage="proof_kernel",
                status="started",
                action="Proof Kernel 正在核验新增的终端 Witness",
                public_reason=(
                    "新增工件已由聚焦 Verifier 核查并重新封装；最终状态仍只由 Kernel 投影。"
                ),
                assessment_count=len(candidate_assessments),
                repair_attempt=1,
            )
            candidate_proof = compile_review_artifact(
                candidate_artifact,
                requirement_requiredness=requirement_requiredness,
            )
        except Exception as exc:
            self._progress(
                "model_thinking",
                stage="executor",
                status="terminal_repair_failed",
                action="终端 Witness 修复失败，保留原始 NOT_FOUND",
                public_reason=(
                    f"{type(exc).__name__}: 修复链没有形成可接受的新 Artifact；原始证据与结论保持冻结。"
                ),
                focused_check_ids=repair_ids,
                repair_attempt=1,
            )
            return sandbox, current_assessments, artifact, proof, repair_ids

        self._emit_kernel_completed(candidate_proof)
        return (
            candidate_sandbox,
            candidate_assessments,
            candidate_artifact,
            candidate_proof,
            repair_ids,
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
    submitted_binding_refs: Mapping[str, Sequence[str]] | None = None,
    submitted_witness_refs: Mapping[str, Sequence[str]] | None = None,
    policy_excerpt: dict[str, Any],
    model: str,
    sandbox: EvidenceSandbox | None = None,
    execution_status: ExecutionStatus = "COMPLETED",
) -> ReviewArtifact:
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        proof_signature_hash=proof_signature_hash_for(plan.active_requirement_ids),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_ir.content_hash(),
        assessments=assessments,
        binding_proposals=list(sandbox.binding_proposals) if sandbox is not None else [],
        calculation_witnesses=list(sandbox.calculation_witnesses) if sandbox is not None else [],
        submitted_claim_refs={
            check_id: _unique(claim_ids)
            for check_id, claim_ids in sorted(submitted_claim_refs.items())
        },
        submitted_binding_refs={
            check_id: _unique(binding_ids)
            for check_id, binding_ids in sorted((submitted_binding_refs or {}).items())
        },
        submitted_witness_refs={
            check_id: _unique(witness_ids)
            for check_id, witness_ids in sorted((submitted_witness_refs or {}).items())
        },
        policy_hash=policy_hash(policy_excerpt),
        resolved_policy_terms={
            ref_id: value
            for ref_id, value in (
                sandbox.resolved_policy_terms
                if sandbox is not None
                else _configured_policy_values(policy_excerpt)
            ).items()
            if ref_id in plan.policy_refs
        },
        unconfigured_policy_refs=_unconfigured_policy_refs(plan, policy_excerpt),
        execution_status=execution_status,
        compiler_version=COMPILER_VERSION,
        model=model,
        prompt_versions=PROMPT_VERSIONS,
    )
    return artifact.model_copy(update={"artifact_hash": artifact.content_hash()})


def _submitted_claim_refs(sandbox: EvidenceSandbox) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for submission in sandbox.submissions:
        result.setdefault(submission.check_id, [])
        result[submission.check_id].extend(submission.claim_ids)
    return {
        check_id: _unique(claim_ids)
        for check_id, claim_ids in sorted(result.items())
    }


def _submitted_binding_refs(sandbox: EvidenceSandbox) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for submission in sandbox.submissions:
        result.setdefault(submission.check_id, [])
        result[submission.check_id].extend(submission.binding_ids)
    return {
        check_id: _unique(binding_ids)
        for check_id, binding_ids in sorted(result.items())
    }


def _submitted_witness_refs(sandbox: EvidenceSandbox) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for submission in sandbox.submissions:
        result.setdefault(submission.check_id, [])
        result[submission.check_id].extend(submission.witness_ids)
    return {
        check_id: _unique(witness_ids)
        for check_id, witness_ids in sorted(result.items())
    }


def _sandbox_proof_material_hash(sandbox: EvidenceSandbox) -> str:
    return _hash(
        {
            "evidence_ir": sandbox.evidence_ir.model_dump(mode="json"),
            "binding_proposals": [
                item.model_dump(mode="json") for item in sandbox.binding_proposals
            ],
            "calculation_witnesses": [
                item.model_dump(mode="json") for item in sandbox.calculation_witnesses
            ],
            "submitted_claim_refs": _submitted_claim_refs(sandbox),
            "submitted_binding_refs": _submitted_binding_refs(sandbox),
            "submitted_witness_refs": _submitted_witness_refs(sandbox),
        }
    )


def _focused_candidate_scope_violation(
    before: EvidenceSandbox,
    after: EvidenceSandbox,
    *,
    focused_check_ids: Sequence[str],
) -> dict[str, Any] | None:
    """Validate a focused candidate without assigning global Claims by fiat.

    CHECK-owned tools are blocked in ``EvidenceSandbox``. Claims intentionally
    remain globally typed observations, so every Claim created during a focused
    run must be reachable from a newly submitted focused CHECK (directly,
    through a submitted Binding, or through a submitted Witness DAG).
    """

    focused = set(focused_check_ids)
    before_claims = list(before.evidence_ir.claims)
    after_claims = list(after.evidence_ir.claims)
    before_bindings = list(before.binding_proposals)
    after_bindings = list(after.binding_proposals)
    before_witnesses = list(before.calculation_witnesses)
    after_witnesses = list(after.calculation_witnesses)
    before_submissions = list(before.submissions)
    after_submissions = list(after.submissions)

    frozen_prefixes = {
        "claim": (before_claims, after_claims),
        "binding": (before_bindings, after_bindings),
        "witness": (before_witnesses, after_witnesses),
        "submission": (before_submissions, after_submissions),
    }
    changed_prefixes = sorted(
        kind
        for kind, (frozen, candidate) in frozen_prefixes.items()
        if len(candidate) < len(frozen) or candidate[: len(frozen)] != frozen
    )
    if changed_prefixes:
        return {
            "scope_error": "PREEXISTING_PROOF_MATERIAL_CHANGED",
            "changed_material_kinds": changed_prefixes,
        }

    new_claims = after_claims[len(before_claims) :]
    new_bindings = after_bindings[len(before_bindings) :]
    new_witnesses = after_witnesses[len(before_witnesses) :]
    new_submissions = after_submissions[len(before_submissions) :]

    outside_focus = sorted(
        {
            item.check_id
            for item in [*new_bindings, *new_witnesses, *new_submissions]
            if item.check_id not in focused
        }
    )
    if outside_focus:
        return {
            "scope_error": "NON_FOCUSED_PROOF_MATERIAL_ADDED",
            "non_focused_check_ids": outside_focus,
        }

    binding_by_id = {item.id: item for item in after_bindings}
    witness_by_id = {item.id: item for item in after_witnesses}
    owned_claim_ids: set[str] = set()
    owned_binding_ids: set[str] = set()
    owned_witness_ids: set[str] = set()

    def admit_ref(ref: ProofTermRef) -> None:
        if ref.kind == "CLAIM":
            owned_claim_ids.add(ref.ref_id)
            return
        if ref.kind != "WITNESS" or ref.ref_id in owned_witness_ids:
            return
        witness = witness_by_id.get(ref.ref_id)
        if witness is None:
            return
        owned_witness_ids.add(witness.id)
        for operand in witness.operands:
            admit_ref(operand.ref)

    for submission in new_submissions:
        if submission.check_id not in focused:
            continue
        owned_claim_ids.update(submission.claim_ids)
        for binding_id in submission.binding_ids:
            binding = binding_by_id.get(binding_id)
            if binding is None:
                continue
            owned_binding_ids.add(binding.id)
            for ref in binding.term_refs:
                admit_ref(ref)
        for witness_id in submission.witness_ids:
            admit_ref(ProofTermRef(kind="WITNESS", ref_id=witness_id))

    orphan_claim_ids = sorted({item.id for item in new_claims} - owned_claim_ids)
    orphan_binding_ids = sorted({item.id for item in new_bindings} - owned_binding_ids)
    orphan_witness_ids = sorted({item.id for item in new_witnesses} - owned_witness_ids)
    if orphan_claim_ids or orphan_binding_ids or orphan_witness_ids:
        return {
            "scope_error": "UNOWNED_FOCUSED_PROOF_MATERIAL",
            "orphan_claim_ids": orphan_claim_ids,
            "orphan_binding_ids": orphan_binding_ids,
            "orphan_witness_ids": orphan_witness_ids,
        }
    return None


def _derived_execution_status(
    plan: ProofPlan,
    sandbox: EvidenceSandbox,
    assessments: Sequence[CheckAssessment],
) -> ExecutionStatus:
    check_ids = {node.id for node in plan.nodes if node.kind == "CHECK"}
    submitted_ids = {item.check_id for item in sandbox.submissions}
    assessed_ids = {item.check_id for item in assessments}
    if check_ids and check_ids <= submitted_ids and check_ids <= assessed_ids:
        return "COMPLETED"
    if submitted_ids or assessed_ids:
        return "PARTIAL"
    return "FAILED"


def _calculation_operation_protocol() -> dict[str, dict[str, Any]]:
    """Expose existing deterministic-engine semantics next to model inputs."""

    return {
        "GREATER_THAN": {
            "ordered_semantics": "refs[0] > refs[1]",
            "equality_result": False,
            "symmetric": False,
        }
    }


def _strong_status_link_protocol() -> dict[str, str]:
    """Describe the typed polarity field without adding a business rule."""

    return {
        "true_status": (
            "the CHECK classification that would follow if the linked boolean Witness "
            "replayed to true; it is not the current classification"
        ),
        "false_status": "the opposite strong classification, derived by the Proof Kernel",
    }


def _merge_assessments(
    current: Sequence[CheckAssessment],
    replacements: Sequence[CheckAssessment],
) -> list[CheckAssessment]:
    """Replace focused Verifier outputs without disturbing full-plan ordering."""

    current_ids = {item.check_id for item in current}
    replacement_by_id = {item.check_id: item for item in replacements}
    if len(replacement_by_id) != len(replacements):
        raise ValueError("Focused Fine Verifier returned duplicate CHECK assessments")
    unknown = sorted(set(replacement_by_id) - current_ids)
    if unknown:
        raise ValueError(
            f"Focused Fine Verifier returned checks outside the original batch: {unknown}"
        )
    return [replacement_by_id.get(item.check_id, item) for item in current]


def _strong_status_link_boundary_violations(
    assessment: CheckAssessment,
    witnesses: Mapping[str, CalculationWitness],
) -> list[dict[str, Any]]:
    """Detect definite polarity conflicts while preserving verifier output.

    This is deliberately narrower than Kernel validation.  It never invents a
    polarity or edits an assessment: it only makes an already-structured
    contradiction observable before the Kernel performs the authoritative
    fail-closed projection.
    """

    if assessment.status not in {"SUPPORTED", "CONTRADICTED"}:
        return []

    accepted = set(assessment.accepted_witness_ids)
    observed: list[dict[str, Any]] = []
    for link in assessment.strong_status_links:
        witness = witnesses.get(link.witness_id)
        if (
            witness is None
            or link.witness_id not in accepted
            or witness.check_id != assessment.check_id
            or not isinstance(witness.result, bool)
        ):
            continue
        mapped_status = (
            link.true_status
            if witness.result
            else ("CONTRADICTED" if link.true_status == "SUPPORTED" else "SUPPORTED")
        )
        observed.append(
            {
                "witness_id": link.witness_id,
                "witness_result": witness.result,
                "true_status": link.true_status,
                "mapped_status": mapped_status,
            }
        )

    if assessment.status == "SUPPORTED":
        return [item for item in observed if item["mapped_status"] == "CONTRADICTED"]
    if observed and not any(item["mapped_status"] == "CONTRADICTED" for item in observed):
        return observed
    return []


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

    async def compute_witness_tool(_context: Any, raw: str) -> str:
        data = _ComputeWitnessInput.model_validate_json(raw)
        return _observed_tool(
            "compute_witness",
            lambda: sandbox.compute_witness(**data.model_dump()),
        )

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
        _function_tool(
            "bind_claim",
            "Append one fact directly observed in an exact source quote to EvidenceIR; never encode a semantic relation or calculation.",
            _BindClaimInput,
            bind_claim,
        ),
        _function_tool(
            "compute_witness",
            "Compute a deterministic Decimal witness from ordered typed refs; no values or results are accepted. GREATER_THAN means refs[0] > refs[1], is not symmetric, and returns false at equality.",
            _ComputeWitnessInput,
            compute_witness_tool,
        ),
        _function_tool(
            "submit_check",
            "Submit candidate Claim refs, semantic binding proposals, Witness refs, and remaining questions for one CHECK; this is not a verdict.",
            _SubmitCheckInput,
            submit_check,
        ),
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
        unresolved = sorted(
            check_id
            for check_id, item in latest.items()
            if not (item.claim_ids or item.binding_ids or item.witness_ids)
        )
        return ToolsToFinalOutputResult(
            is_final_output=True,
            final_output=ExecutorSummary(
                completed_check_ids=sorted(expected - set(unresolved)),
                unresolved_check_ids=unresolved,
                summary="Every executable CHECK was submitted; the Runtime completion hook stopped the worker.",
                execution_status="COMPLETED",
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


def _active_proof_signatures(requirement_ids: Sequence[str]) -> list[dict[str, Any]]:
    return [
        signature.model_dump(mode="json")
        for requirement_id in requirement_ids
        if (signature := proof_signature_for(requirement_id)) is not None
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


def _configured_policy_values(policy_excerpt: Mapping[str, Any]) -> dict[str, Any]:
    """Lower policy envelopes to the tiny numeric shape accepted by proof_terms.

    Business metadata (scope/note) is intentionally not executable. The
    document_currency alias is retained here and resolved against concrete
    monetary operands by EvidenceSandbox immediately before computation.
    """

    raw_values = policy_excerpt.get("values")
    values = raw_values if isinstance(raw_values, Mapping) else {}
    configured: dict[str, Any] = {}
    for ref_id, envelope in values.items():
        if not isinstance(envelope, Mapping) or envelope.get("configured") is not True:
            continue
        raw = envelope.get("value")
        if isinstance(raw, Mapping):
            if "amount" in raw:
                configured[str(ref_id)] = {
                    "value": raw.get("amount"),
                    "currency": str(raw.get("currency") or "").strip(),
                    "unit": str(raw.get("unit") or "").strip(),
                }
                continue
            if "value" in raw:
                configured[str(ref_id)] = {
                    "value": raw.get("value"),
                    "currency": str(raw.get("currency") or "").strip(),
                    "unit": str(raw.get("unit") or "").strip(),
                }
                continue
        configured[str(ref_id)] = raw
    return configured


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
