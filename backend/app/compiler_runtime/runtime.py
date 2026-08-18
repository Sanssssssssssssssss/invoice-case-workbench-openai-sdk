from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agents import Agent, FunctionTool, ModelSettings, ToolsToFinalOutputResult
from agents.exceptions import MaxTurnsExceeded
from pydantic import BaseModel, ConfigDict, Field

from app.agents.thinking import model_extra_body_for_thinking, temperature_for_thinking
from app.config import Settings
from app.llm import LlmClient, ModelCallRecord
from app.runtime.agents_sdk import FencedJsonOutputSchema, build_run_config, run_agent_sync
from app.runtime.context_partition import usage_from_result
from app.runtime.reasoning_capture import extract_reasoning_from_result

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
)
from .sandbox import EvidenceSandbox, SourceRecord


COMPILER_VERSION = "evidence_compiler_runtime_v2"
PROMPT_VERSIONS = {
    "task_compiler": "task_compiler_v4",
    "executor": "evidence_executor_v2",
    "verifier": "fine_verifier_v4",
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
    ) -> None:
        self.llm = llm
        self.settings = settings or llm.settings
        self.hooks = hooks

    def compile_task(
        self,
        *,
        active_requirement_ids: Sequence[str],
        policy_excerpt: dict[str, Any],
        source_catalog: Sequence[dict[str, Any]],
        extraction_summary: Sequence[dict[str, Any]] = (),
    ) -> ProofPlan:
        requirement_ids = _unique(active_requirement_ids)
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
            raise ValueError("Task Compiler changed the ordered active requirement set")
        expected_policy_refs = sorted(required_policy_refs(requirement_ids))
        if sorted(plan.policy_refs) != expected_policy_refs:
            raise ValueError(
                f"Task Compiler policy coverage mismatch: expected={expected_policy_refs}, got={sorted(plan.policy_refs)}"
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
                tools=_sandbox_tools(sandbox),
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
            raise ValueError(f"Executor referenced checks outside the ProofPlan: {unknown}")
        return summary, sandbox

    def verify(
        self,
        *,
        plan: ProofPlan,
        sandbox: EvidenceSandbox,
        policy_excerpt: dict[str, Any],
    ) -> list[CheckAssessment]:
        claims = {claim.id: claim for claim in sandbox.evidence_ir.claims}
        submitted_refs = _submitted_claim_refs(sandbox)
        sources = [
            {
                "source_id": source.source_id,
                "title": source.title,
                "kind": source.kind,
                "content": source.content,
            }
            for source in sandbox.source_records
        ]
        visible_source_ids = {item["source_id"] for item in sources}
        admitted_source_ids = set(sandbox.evidence_ir.source_ids)
        if visible_source_ids != admitted_source_ids:
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
            raise ValueError(
                f"Fine Verifier must assess every CHECK exactly once: expected={sorted(expected)}, got={sorted(actual)}"
            )
        return batch.assessments

    def run(
        self,
        *,
        active_requirement_ids: Sequence[str],
        prepared_sources: Sequence[PreparedSource],
        policy_excerpt: dict[str, Any] | None = None,
        extraction_summary: Sequence[dict[str, Any]] = (),
    ) -> CompilerRunResult:
        active_ids = expand_active_requirements(active_requirement_ids)
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
        proof = compile_review_artifact(artifact)

        blocking_check_ids = sorted({item.check_id for item in proof.obligations})
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
                proof = compile_review_artifact(artifact)

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
        started = time.perf_counter()
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
        run_config = build_run_config(
            self.settings,
            workflow_name=f"invoice_agent.compiler.{name}",
            trace_metadata={
                "role": name,
                "prompt_version": PROMPT_VERSIONS[name if name != "fine_verifier" else "verifier"],
                "compiler_version": COMPILER_VERSION,
            },
            timeout_seconds=self.settings.evidence_reviewer_timeout_seconds,
        )
        input_text = json.dumps(payload, ensure_ascii=False, default=str)
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
            reasoning = extract_reasoning_from_result(result)
            self.llm.calls.append(
                ModelCallRecord(
                    role=name,
                    model=model,
                    prompt_version=PROMPT_VERSIONS[name if name != "fine_verifier" else "verifier"],
                    input_preview=input_text[:1400],
                    output_preview=raw[:1400],
                    system_prompt=prompt,
                    payload=payload,
                    raw_response=raw,
                    usage=usage,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    content_chars=len(raw),
                    runtime="evidence_compiler_runtime",
                    reasoning_excerpt=reasoning.text if reasoning else "",
                    reasoning_chars=reasoning.chars if reasoning else 0,
                    reasoning_chunks=reasoning.chunks if reasoning else 0,
                    thinking_type="disabled",
                    reasoning_source=reasoning.source if reasoning else "",
                )
            )
            return parsed
        except Exception as exc:
            if self.hooks is not None and hasattr(self.hooks, "record_error"):
                self.hooks.record_error(exc)
            self.llm.calls.append(
                ModelCallRecord(
                    role=name,
                    model=model,
                    prompt_version=PROMPT_VERSIONS[name if name != "fine_verifier" else "verifier"],
                    input_preview=input_text[:1400],
                    output_preview="",
                    error=f"{type(exc).__name__}: {exc}",
                    system_prompt=prompt,
                    payload=payload,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    runtime="evidence_compiler_runtime",
                )
            )
            raise


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
        result.append(
            PreparedSource(
                record=SourceRecord(source_id=source_id, title=title, kind=kind, content=content),
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


def _sandbox_tools(sandbox: EvidenceSandbox) -> list[FunctionTool]:
    async def list_sources(_context: Any, raw: str) -> str:
        _ListSourcesInput.model_validate_json(raw or "{}")
        return _tool_json(sandbox.list_sources())

    async def read_source(_context: Any, raw: str) -> str:
        data = _ReadSourceInput.model_validate_json(raw)
        return _tool_json(sandbox.read_source(data.source_id))

    async def bind_claim(_context: Any, raw: str) -> str:
        data = _BindClaimInput.model_validate_json(raw)
        return _tool_json(sandbox.bind_claim(**data.model_dump()))

    async def submit_check(_context: Any, raw: str) -> str:
        data = _SubmitCheckInput.model_validate_json(raw)
        return _tool_json(sandbox.submit_check(**data.model_dump()))

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
    decisions = {item.requirement_id: item for item in proof.decisions}
    assessments = {item.check_id: item for item in artifact.assessments}
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
        for requirement_id, decision in decisions.items():
            referenced = any(
                source_id in assessments[check_id].source_ids
                for check_id in (
                    decision.supporting_check_ids
                    + decision.contradicting_check_ids
                    + decision.unresolved_check_ids
                )
                if check_id in assessments
            )
            if not referenced:
                continue
            if decision.status == "SUPPORTED":
                supports.append(
                    {
                        "requirement": requirement_id,
                        "support_level": "full",
                        "quoted_text": claims[0].quote if claims else "",
                    }
                )
            elif decision.status == "CONTRADICTED":
                conflicts.append(
                    {
                        "type": "proof_contradiction",
                        "requirement": requirement_id,
                        "severity": "high",
                        "description": decision.stop_reason,
                        "quoted_text": claims[0].quote if claims else "",
                        "affected_evidence_ids": [source_id],
                    }
                )
        evidence_type = prepared.record.kind if prepared.record.kind in _EVIDENCE_TYPES else "unknown"
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
    contradicted = sorted(item.requirement_id for item in proof.decisions if item.status == "CONTRADICTED")
    supported = [
        {"requirement": item.requirement_id, "support_level": "full", "quoted_text": ""}
        for item in proof.decisions
        if item.status == "SUPPORTED"
    ]
    accepted_items = [item for item in evidence_items if item["review_result"]["should_accept"]]
    accepted_credibility = [str(item["credibility"]) for item in accepted_items]
    overall_credibility = (
        "high"
        if accepted_credibility and all(value == "high" for value in accepted_credibility)
        else "low"
        if accepted_credibility and all(value == "low" for value in accepted_credibility)
        else "medium"
    )
    return {
        "mode": "review",
        "source_doc_id": ",".join(item.record.source_id for item in prepared_sources),
        "evidence_type": "unknown",
        "credibility": overall_credibility,
        "extracted_fields": {},
        "extraction_result": {},
        # Grounded quotes prove provenance to this run, not legal originality or authenticity.
        "source_traceability": "unclear",
        "support_level": "full" if supported and not obligations else "partial" if evidence_items else "none",
        "risk_flags": contradicted,
        "should_accept": bool(accepted_items),
        "reason": f"Compiled {len(proof.decisions)} requirement proof(s) from {len(sandbox.evidence_ir.claims)} grounded claim(s).",
        "supports": supported,
        "conflicts": [],
        "evidence_cards": cards,
        "suggested_patch": {
            "add_evidence": evidence_items,
            "risk_flags": contradicted,
            "next_questions": obligations,
            "evidence_cards": cards,
        },
        "reply_to_user": "Evidence review compiled into source-grounded proof checks.",
    }


def _source_text(item: dict[str, Any]) -> str:
    lines = [
        f"SOURCE: {item.get('name') or item.get('attachment_id') or 'attachment'}",
        f"ATTACHMENT_ID: {item.get('attachment_id') or ''}",
    ]
    body = str(item.get("body_markdown") or item.get("content") or "")
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
