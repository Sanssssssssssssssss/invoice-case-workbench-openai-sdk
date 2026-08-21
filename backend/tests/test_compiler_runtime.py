from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.exceptions import ModelBehaviorError, UserError
from pydantic import ValidationError

from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import (
    CheckAssessment,
    CompilationDiagnostic,
    CompiledProof,
    EvidenceIR,
    ProofObligation,
    ProofNode,
    ProofPlan,
    StrongStatusLink,
)
from app.compiler_runtime.policy import requirement_context
from app.compiler_runtime.runtime import (
    EXECUTOR_MAX_TURNS,
    PROMPT_VERSIONS,
    EvidenceCompilerRuntime,
    ExecutorSummary,
    VerificationBatch,
    _artifact,
    _completion_hook,
    _configured_policy_values,
    _derived_execution_status,
    attachment_source_admission,
    expand_active_requirements,
    policy_excerpt_for,
    prepare_sources,
    _planning_extraction_summary,
    _planning_source_catalog,
    _review_result,
    _retryable_checks,
    _sandbox_tools,
    _sandbox_proof_material_hash,
)
from app.compiler_runtime.sandbox import EvidenceSandbox
from app.compiler_runtime.signatures import proof_signature_for
from app.config import Settings
from app.llm import LlmClient
from app.runtime.patch_normalizer import compact_case_patch_for_write
from app.state.schemas import EvidenceReviewResult


def test_executor_prompt_forbids_submitting_aggregate_nodes() -> None:
    prompt = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "executor.md"
    ).read_text(encoding="utf-8")

    assert "only for nodes whose kind is CHECK" in prompt
    assert "Never submit ALL or ANY nodes" in prompt
    assert "Never submit ALL, ANY, or NOT nodes" not in prompt


def test_executor_prompt_exact_hash_matches_versioned_fixture() -> None:
    prompt_path = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "executor.md"
    )
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "compiler_prompt_hashes.json").read_text(
            encoding="utf-8"
        )
    )

    actual = hashlib.sha256(prompt_path.read_text(encoding="utf-8").encode()).hexdigest()
    assert actual == fixture["executor.md"]


def test_task_compiler_prompt_exact_hash_matches_versioned_fixture() -> None:
    prompt_path = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "task_compiler.md"
    )
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "compiler_prompt_hashes.json").read_text(
            encoding="utf-8"
        )
    )

    actual = hashlib.sha256(prompt_path.read_text(encoding="utf-8").encode()).hexdigest()
    assert actual == fixture["task_compiler.md"]


def test_verifier_prompt_exact_hash_matches_versioned_fixture() -> None:
    prompt_path = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "verifier.md"
    )
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "compiler_prompt_hashes.json").read_text(
            encoding="utf-8"
        )
    )

    actual = hashlib.sha256(prompt_path.read_text(encoding="utf-8").encode()).hexdigest()
    assert actual == fixture["verifier.md"]


def test_task_compiler_prompt_forbids_check_to_check_edges() -> None:
    prompt = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "task_compiler.md"
    ).read_text(encoding="utf-8")

    assert "depends_on` must be exactly `[]`" in prompt
    assert "never create a CHECK-to-CHECK edge" in prompt
    assert "Include `version: \"1\"`" in prompt
    assert "Every root must directly establish the supplied Requirement `proof_target` in the same polarity" in prompt
    assert "Every CHECK must reference at least one active Requirement" in prompt
    assert "Keep each CHECK atomic" in prompt
    assert "A document or evidence Requirement checks only" in prompt
    assert "never in a standalone CHECK with empty requirement_refs" in prompt
    assert "Fold both configured and unconfigured values into the substantive CHECK" in prompt
    assert "Only when an active Requirement explicitly targets system-provenance traceability" in prompt
    assert "never add an unrelated provenance CHECK" in prompt
    assert "ALL, ANY, and NOT" not in prompt


def test_task_compiler_arithmetic_facets_use_independent_upstream_premises() -> None:
    prompt = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "task_compiler.md"
    ).read_text(encoding="utf-8")

    assert "semantically and evidentially orthogonal" in prompt
    assert "independent source-grounded premises" in prompt
    assert "stated amount, rate, basis, quantity, or applicable relationship" in prompt
    assert "basis or applicability is absent" in prompt
    assert "separately answerable gap that can remain NOT_FOUND" in prompt
    assert "Reconstruct a final aggregate from independently established upstream values" in prompt


def test_task_compiler_arithmetic_facets_forbid_circular_aggregate_proof() -> None:
    prompt = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "task_compiler.md"
    ).read_text(encoding="utf-8")

    assert "Never derive an upstream component by rearranging the same reported aggregate" in prompt
    assert "circular proof and duplicate attribution" in prompt
    assert "compare it with the reported aggregate only at terminal reconciliation" in prompt
    assert "Do not feed a possibly wrong reported aggregate back into upstream component checks" in prompt
    assert "VAT" not in prompt
    assert "subtotal plus or minus" not in prompt
    assert "four separate CHECKs" not in prompt


def test_verifier_requires_a_grounded_comparison_baseline() -> None:
    prompt = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "verifier.md"
    ).read_text(encoding="utf-8")

    assert "comparison baseline itself" in prompt
    assert "General resemblance without that baseline is NOT_FOUND, never SUPPORTED" in prompt
    assert "Absence of a baseline is also not evidence of mismatch" in prompt
    assert "Arithmetic and reconciliation strong classifications must rely on submitted CalculationWitness ids" in prompt
    assert "Never change an operand" in prompt
    assert "Do not perform free-form or mental arithmetic" in prompt
    assert "genuinely provide partial evidence" in prompt
    assert "examined_source_ids records coverage, not relevance" in prompt
    assert "Generic extraction limitations about authenticity" in prompt
    assert "do not refute that source role" in prompt
    assert "finish the source comparison and any calculation in reason before choosing status" in prompt
    assert "then copy that same value into the status field" in prompt
    assert "Semantic applicability is distinct from arithmetic" in prompt


def test_verifier_schema_places_reason_before_status() -> None:
    fields = list(CheckAssessment.model_json_schema()["properties"])

    assert fields.index("reason") < fields.index("status")
    assert fields[-1] == "status"


def test_strong_status_link_accepts_only_typed_polarity_not_naked_values() -> None:
    properties = StrongStatusLink.model_json_schema()["properties"]

    assert set(properties) == {"witness_id", "true_status"}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        StrongStatusLink.model_validate(
            {
                "witness_id": "witness.terminal",
                "true_status": "CONTRADICTED",
                "result": True,
                "tolerance": "0.01",
            }
        )


def test_source_traceability_target_is_system_provenance_not_authenticity() -> None:
    requirement = requirement_context(["source_traceability"])[0]
    prompt = (
        Path(__file__).parents[1]
        / "app"
        / "compiler_runtime"
        / "prompts"
        / "task_compiler.md"
    ).read_text(encoding="utf-8")

    assert requirement["label"] == "上传附件来源链可追溯"
    assert requirement["capability_hint"] == "system_provenance"
    assert requirement["target_predicate_hint"] == "source.upload_provenance_traceable"
    assert "never turn it into a claim that the business document is genuine" in prompt


def test_invoice_arithmetic_guidance_spans_plan_execution_and_verification() -> None:
    prompt_root = Path(__file__).parents[1] / "app" / "compiler_runtime" / "prompts"
    requirement = requirement_context(["invoice_calculation_valid"])[0]
    compiler = (prompt_root / "task_compiler.md").read_text(encoding="utf-8")
    executor = (prompt_root / "executor.md").read_text(encoding="utf-8")
    verifier = (prompt_root / "verifier.md").read_text(encoding="utf-8")

    assert requirement["capability_hint"] == "invoice_arithmetic"
    signature = proof_signature_for("invoice_calculation_valid")
    assert signature is not None
    assert [facet.id for facet in signature.facets] == [
        "line_extensions",
        "subtotal_aggregation",
        "stated_components",
        "final_total",
    ]
    assert "A ProofSignature is a type constraint, not a plan template" in compiler
    assert "The number, wording, sharing, and ALL/ANY arrangement of CHECKs remain your decision" in compiler
    assert "never reduce calculation validity to field presence" in compiler.lower()
    assert PROMPT_VERSIONS["task_compiler"] == "typed_task_compiler_v10"
    assert "Claims are append-only and existing Claim content is immutable" in executor
    assert "later unrelated Claims are allowed" in executor
    assert PROMPT_VERSIONS["executor"] == "typed_evidence_executor_v8"
    assert PROMPT_VERSIONS["verifier"] == "typed_fine_verifier_v13"
    assert "never bind a cross-Claim semantic relationship" in executor
    assert "only check_id, a facet_ref declared on that CHECK, an operation, and typed refs" in executor
    assert "For every declared facet whose minimum proof kinds include WITNESS" in executor
    assert "`GREATER_THAN` means exactly `refs[0] > refs[1]`" in executor
    assert "equality returns false" in executor
    assert "Arithmetic and reconciliation strong classifications must rely on submitted CalculationWitness ids" in verifier
    assert "Return it in strong_status_links" in verifier
    assert "only `witness_id` and the semantic `true_status`" in verifier
    assert "`true_status` is counterfactual" in verifier
    assert "must not be copied from the current final classification" in verifier
    assert "false result conflates equality with exceedance" in verifier
    assert "When `repair_feedback` is present" in verifier
    assert "never flip `status` or `true_status` merely to silence the diagnostic" in verifier
    assert "A replayable multiplication with an unsupported business base remains NOT_FOUND" in verifier


def test_invoice_arithmetic_plan_contract_keeps_recomputations_atomic() -> None:
    check_ids = [
        "check.line_extensions",
        "check.subtotal",
        "check.adjustments",
        "check.final_total",
    ]
    plan = ProofPlan.model_validate(
        {
            "plan_id": "plan.invoice-arithmetic",
            "version": "1",
            "objective": "Verify the invoice's internal arithmetic.",
            "active_requirement_ids": ["invoice_calculation_valid"],
            "policy_refs": ["invoice_calculation_rounding_tolerance"],
            "roots": {"invoice_calculation_valid": "root.invoice_arithmetic"},
            "nodes": [
                {
                    "id": check_id,
                    "kind": "CHECK",
                    "statement": statement,
                    "requirement_refs": ["invoice_calculation_valid"],
                    "policy_refs": ["invoice_calculation_rounding_tolerance"],
                }
                for check_id, statement in zip(
                    check_ids,
                    (
                        "Applicable line extensions equal quantity times unit price.",
                        "Printed line totals sum to the printed subtotal.",
                        "Printed taxes, discounts, and charges reconcile to their stated bases.",
                        "The printed final total reconciles to subtotal plus or minus adjustments.",
                    ),
                    strict=True,
                )
            ]
            + [{"id": "root.invoice_arithmetic", "kind": "ALL", "depends_on": check_ids}],
        }
    )

    nodes = {node.id: node for node in plan.nodes}
    assert nodes[plan.roots["invoice_calculation_valid"]].depends_on == check_ids
    assert all(nodes[check_id].kind == "CHECK" and not nodes[check_id].depends_on for check_id in check_ids)


def test_task_compiler_rejects_missing_signature_facet_before_executor(
    tmp_path,
    monkeypatch,
) -> None:
    invalid_plan = ProofPlan(
        plan_id="plan.incomplete-arithmetic",
        objective="Review invoice arithmetic.",
        active_requirement_ids=["invoice_calculation_valid"],
        policy_refs=["invoice_calculation_rounding_tolerance"],
        roots={"invoice_calculation_valid": "check.lines-only"},
        nodes=[
            ProofNode(
                id="check.lines-only",
                kind="CHECK",
                statement="Every printed line extension is arithmetically consistent.",
                requirement_refs=["invoice_calculation_valid"],
                policy_refs=["invoice_calculation_rounding_tolerance"],
                facet_refs=["line_extensions"],
            )
        ],
    )
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    captured: dict[str, object] = {}

    def fake_phase(**kwargs):
        captured.update(kwargs["payload"])
        return invalid_plan

    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    with pytest.raises(ValueError, match="Required facet"):
        runtime.compile_task(
            active_requirement_ids=["invoice_calculation_valid"],
            policy_excerpt=policy_excerpt_for(["invoice_calculation_valid"]),
            source_catalog=[{"kind": "invoice", "characters": 100}],
        )

    signatures = captured["proof_signatures"]
    assert len(signatures) == 1
    assert set(signatures[0]) == {
        "signature_id",
        "version",
        "requirement_id",
        "root_composition",
        "required_policy_refs",
        "facets",
    }


def test_compute_witness_tool_schema_accepts_refs_but_no_raw_values_or_results() -> None:
    sandbox = EvidenceSandbox(
        sources=[],
        allowed_check_ids=["check.total"],
        allowed_check_facets={"check.total": ["final_total"]},
    )
    tool = next(item for item in _sandbox_tools(sandbox) if item.name == "compute_witness")
    properties = tool.params_json_schema["properties"]

    assert set(properties) == {"check_id", "facet_ref", "operation", "refs"}
    assert not {"value", "result", "tolerance", "formula"}.intersection(properties)
    assert "refs[0] > refs[1]" in tool.description
    assert "returns false at equality" in tool.description


def test_executor_payload_exposes_ordered_boolean_operation_protocol(
    tmp_path,
    monkeypatch,
) -> None:
    captured: dict = {}
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))

    def fake_phase(**kwargs):
        captured.update(kwargs["payload"])
        return ExecutorSummary(
            unresolved_check_ids=["check.vendor"],
            summary="No source was supplied.",
            execution_status="PARTIAL",
        )

    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    runtime.execute_plan(
        plan=_plan(),
        prepared_sources=[],
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert captured["calculation_operation_protocol"] == {
        "GREATER_THAN": {
            "ordered_semantics": "refs[0] > refs[1]",
            "equality_result": False,
            "symmetric": False,
        }
    }


def test_compute_witness_emits_observable_tool_trace() -> None:
    source = prepare_sources(
        [{"attachment_id": "att-total", "content": "Total EUR 10.00."}]
    )[0].record
    sandbox = EvidenceSandbox(
        sources=[source],
        allowed_check_ids=["check.total"],
        allowed_check_facets={"check.total": ["final_total"]},
    )
    sandbox.read_source(source.source_id)
    claim_id = sandbox.bind_claim(
        subject="invoice:1",
        predicate="printed_total",
        value="10.00",
        source_id=source.source_id,
        quote="Total EUR 10.00.",
        locator="line 1",
        attributes={"currency": "EUR"},
    )["claim"]["id"]
    events: list[tuple[str, dict | None]] = []
    tool = next(
        item
        for item in _sandbox_tools(
            sandbox,
            progress_sink=lambda name, result: events.append((name, result)),
        )
        if item.name == "compute_witness"
    )

    result = json.loads(
        asyncio.run(
            tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "check_id": "check.total",
                        "facet_ref": "final_total",
                        "operation": "SUM",
                        "refs": [{"kind": "CLAIM", "ref_id": claim_id}],
                    }
                ),
            )
        )
    )

    assert result["ok"] is True
    assert events[0] == ("compute_witness", None)
    assert events[1][0] == "compute_witness"
    assert events[1][1]["witness"]["result"] == "10.00"


def test_real_rounding_policy_is_lowered_without_executable_business_metadata() -> None:
    excerpt = policy_excerpt_for(["invoice_calculation_valid"])
    values = _configured_policy_values(excerpt)

    assert values == {
        "invoice_calculation_rounding_tolerance": {
            "value": "0.01",
            "currency": "",
            "unit": "document_currency",
        }
    }

    plan = ProofPlan(
        plan_id="plan.policy-hash",
        objective="Capture one configured arithmetic policy term.",
        active_requirement_ids=["invoice_calculation_valid"],
        policy_refs=["invoice_calculation_rounding_tolerance"],
        roots={"invoice_calculation_valid": "check.total"},
        nodes=[
            ProofNode(
                id="check.total",
                kind="CHECK",
                statement="The total is within the configured rounding tolerance.",
                requirement_refs=["invoice_calculation_valid"],
                policy_refs=["invoice_calculation_rounding_tolerance"],
                facet_refs=["final_total"],
            )
        ],
    )
    artifact = _artifact(
        plan=plan,
        evidence_ir=EvidenceIR(),
        assessments=[],
        submitted_claim_refs={},
        policy_excerpt=excerpt,
        model="test-model",
    )
    assert artifact.resolved_policy_terms == values
    assert artifact.artifact_hash == artifact.content_hash()
    tampered = artifact.model_copy(
        update={
            "resolved_policy_terms": {
                "invoice_calculation_rounding_tolerance": {
                    "value": "999",
                    "currency": "",
                    "unit": "document_currency",
                }
            }
        }
    )
    assert tampered.content_hash() != artifact.artifact_hash


def _settings(tmp_path) -> Settings:
    return Settings(
        workspace_root=tmp_path / "cases",
        storage_root=tmp_path / "storage",
        session_db_path=tmp_path / "storage" / "sessions.sqlite",
        memory_db_path=tmp_path / "storage" / "memory.sqlite",
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
        llm_api_key="test-only",
        llm_base_url="https://api.deepseek.com",
    )


def _plan() -> ProofPlan:
    return ProofPlan(
        plan_id="plan.vendor",
        objective="Verify the supplied vendor source exists.",
        active_requirement_ids=["vendor_identity"],
        roots={"vendor_identity": "check.vendor"},
        nodes=[
            ProofNode(
                id="check.vendor",
                kind="CHECK",
                statement="A vendor identity is grounded in the supplied source.",
                requirement_refs=["vendor_identity"],
            )
        ],
    )


def _sources():
    return prepare_sources(
        [
            {
                "attachment_id": "att_vendor",
                "name": "vendor.md",
                "content_kind": "vendor_record",
                "content": "Vendor V-100 is ACTIVE.",
                "original_ref": "attachments/vendor.md",
            }
        ]
    )


def _two_check_plan() -> ProofPlan:
    return ProofPlan(
        plan_id="plan.executor-user-error",
        objective="Verify that both source checks are completed.",
        active_requirement_ids=["vendor_identity"],
        roots={"vendor_identity": "root.all"},
        nodes=[
            ProofNode(
                id="check.one",
                kind="CHECK",
                statement="The first source check is complete.",
                requirement_refs=["vendor_identity"],
            ),
            ProofNode(
                id="check.two",
                kind="CHECK",
                statement="The second source check is complete.",
                requirement_refs=["vendor_identity"],
            ),
            ProofNode(
                id="root.all",
                kind="ALL",
                depends_on=["check.one", "check.two"],
            ),
        ],
    )


def test_executor_user_error_after_valid_submission_continues_as_partial(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    plan = _two_check_plan()
    phase_calls: list[str] = []
    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def fake_phase(**kwargs):
        phase_calls.append(kwargs["name"])
        if kwargs["name"] == "executor":
            submit = next(tool for tool in kwargs["tools"] if tool.name == "submit_check")
            result = json.loads(
                asyncio.run(
                    submit.on_invoke_tool(
                        None,
                        json.dumps(
                            {
                                "check_id": "check.one",
                                "note": "valid work admitted before the SDK error",
                            }
                        ),
                    )
                )
            )
            assert result["ok"] is True
            raise UserError("invalid structured tool call after valid work")
        return VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id=check_id,
                    examined_source_ids=[],
                    missing_fact="a source-grounded fact",
                    gap_code="SOURCE_MISSING",
                    status="NOT_FOUND",
                )
                for check_id in ("check.one", "check.two")
            ]
        )

    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=[],
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
        requirement_requiredness={"vendor_identity": False},
    )

    assert phase_calls == ["executor", "fine_verifier"]
    assert result.artifact.execution_status == "PARTIAL"
    assert result.artifact.submitted_claim_refs == {"check.one": []}
    assert result.proof.decision_for("vendor_identity").status == "NOT_FOUND"


def test_executor_user_error_before_first_submission_is_reraised(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    sandbox = EvidenceSandbox(sources=[], allowed_check_ids=["check.vendor"])
    failure = UserError("executor failed before submitting work")
    monkeypatch.setattr(runtime, "_run_phase", lambda **_kwargs: (_ for _ in ()).throw(failure))

    with pytest.raises(UserError) as caught:
        runtime.execute_plan(
            plan=_plan(),
            prepared_sources=[],
            policy_excerpt=policy_excerpt_for(["vendor_identity"]),
            sandbox=sandbox,
        )

    assert caught.value is failure
    assert not sandbox.submissions


def test_submit_check_extra_field_is_not_admitted_or_counted_as_success() -> None:
    sandbox = EvidenceSandbox(sources=[], allowed_check_ids=["check.vendor"])
    events: list[tuple[str, dict | None]] = []
    submit = next(
        tool
        for tool in _sandbox_tools(
            sandbox,
            progress_sink=lambda name, result: events.append((name, result)),
        )
        if tool.name == "submit_check"
    )
    valid = json.loads(
        asyncio.run(
            submit.on_invoke_tool(
                None,
                json.dumps({"check_id": "check.vendor", "note": "valid"}),
            )
        )
    )
    assert valid["ok"] is True
    successful_events = list(events)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        asyncio.run(
            submit.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "check_id": "check.vendor",
                        "note": "must not be admitted",
                        "status": "SUPPORTED",
                    }
                ),
            )
        )

    assert len(sandbox.submissions) == 1
    assert sandbox.submissions[0].note == "valid"
    assert events == successful_events


def test_executor_does_not_recover_model_behavior_error_after_submission(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    sandbox = EvidenceSandbox(sources=[], allowed_check_ids=["check.vendor"])
    failure = ModelBehaviorError("model output was malformed")

    def fake_phase(**kwargs):
        submit = next(tool for tool in kwargs["tools"] if tool.name == "submit_check")
        asyncio.run(
            submit.on_invoke_tool(
                None,
                json.dumps({"check_id": "check.vendor", "note": "valid"}),
            )
        )
        raise failure

    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    with pytest.raises(ModelBehaviorError) as caught:
        runtime.execute_plan(
            plan=_plan(),
            prepared_sources=[],
            policy_excerpt=policy_excerpt_for(["vendor_identity"]),
            sandbox=sandbox,
        )

    assert caught.value is failure
    assert len(sandbox.submissions) == 1


def test_verifier_overreach_is_preserved_for_kernel_diagnosis(tmp_path, monkeypatch) -> None:
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    plan = _plan()
    prepared = _sources()[0]
    sandbox = EvidenceSandbox(
        sources=[prepared.record],
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[prepared.record.source_id],
            source_fingerprints={
                prepared.record.source_id: hashlib.sha256(
                    prepared.record.content.encode("utf-8")
                ).hexdigest()
            },
        ),
    )
    sandbox.submit_check(check_id="check.vendor", note="source missing")
    monkeypatch.setattr(
        runtime,
        "_run_phase",
        lambda **_kwargs: VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id="check.vendor",
                    accepted_witness_ids=["witness-not-submitted"],
                    examined_source_ids=[prepared.record.source_id],
                    reason="Insufficient evidence. Final classification: NOT_FOUND",
                    missing_fact="a grounded vendor identity",
                    gap_code="SOURCE_MISSING",
                    status="NOT_FOUND",
                )
            ]
        ),
    )

    assessments = runtime.verify(
        plan=plan,
        sandbox=sandbox,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert assessments[0].accepted_witness_ids == ["witness-not-submitted"]


def test_sandbox_material_hash_changes_when_only_a_witness_is_added() -> None:
    record = prepare_sources(
        [{"attachment_id": "att-number", "content": "Amount EUR 10.00."}]
    )[0].record
    sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=["check.total"],
        allowed_check_facets={"check.total": ["final_total"]},
    )
    sandbox.read_source(record.source_id)
    claim_id = sandbox.bind_claim(
        subject="invoice:1",
        predicate="amount",
        value="10.00",
        source_id=record.source_id,
        quote="Amount EUR 10.00.",
        locator="line 1",
        attributes={"currency": "EUR"},
    )["claim"]["id"]
    before = _sandbox_proof_material_hash(sandbox)

    result = sandbox.compute_witness(
        check_id="check.total",
        facet_ref="final_total",
        operation="SUM",
        refs=[{"kind": "CLAIM", "ref_id": claim_id}],
    )

    assert result["ok"] is True
    assert _sandbox_proof_material_hash(sandbox) != before


def test_execution_status_is_derived_from_final_full_plan_coverage() -> None:
    plan = ProofPlan(
        plan_id="plan.two-checks",
        objective="Review two independent facts.",
        active_requirement_ids=["vendor_identity"],
        roots={"vendor_identity": "root.all"},
        nodes=[
            ProofNode(
                id="check.one",
                kind="CHECK",
                statement="The first fact is present.",
                requirement_refs=["vendor_identity"],
            ),
            ProofNode(
                id="check.two",
                kind="CHECK",
                statement="The second fact is present.",
                requirement_refs=["vendor_identity"],
            ),
            ProofNode(id="root.all", kind="ALL", depends_on=["check.one", "check.two"]),
        ],
    )
    sandbox = EvidenceSandbox(
        sources=[],
        allowed_check_ids=["check.one", "check.two"],
    )
    assessments = [
        CheckAssessment(
            check_id=check_id,
            missing_fact="source fact",
            gap_code="SOURCE_MISSING",
            status="NOT_FOUND",
        )
        for check_id in ("check.one", "check.two")
    ]
    sandbox.submit_check(check_id="check.one", note="missing")
    assert _derived_execution_status(plan, sandbox, assessments) == "PARTIAL"

    sandbox.submit_check(check_id="check.two", note="missing")
    assert _derived_execution_status(plan, sandbox, assessments) == "COMPLETED"


def test_compiler_phase_retries_one_transient_connection_failure_with_fresh_client(
    tmp_path,
    monkeypatch,
) -> None:
    class APIConnectionError(Exception):
        pass

    expected = _plan()
    attempts: list[object] = []
    run_configs: list[object] = []

    def fake_build_run_config(*_args, **_kwargs):
        config = object()
        run_configs.append(config)
        return config

    def fake_run_agent_sync(*_args, **kwargs):
        attempts.append(kwargs["run_config"])
        if len(attempts) == 1:
            raise APIConnectionError("Connection error.")
        return SimpleNamespace(final_output=expected, raw_responses=[])

    monkeypatch.setattr("app.compiler_runtime.runtime.build_run_config", fake_build_run_config)
    monkeypatch.setattr("app.compiler_runtime.runtime.run_agent_sync", fake_run_agent_sync)
    monkeypatch.setattr("app.compiler_runtime.runtime.time.sleep", lambda _seconds: None)

    llm = LlmClient(_settings(tmp_path))
    runtime = EvidenceCompilerRuntime(llm)
    actual = runtime._run_phase(  # noqa: SLF001
        name="task_compiler",
        prompt_file="task_compiler.md",
        payload={"test": "transport retry"},
        output_type=ProofPlan,
        max_turns=1,
    )

    assert actual == expected
    assert attempts == run_configs
    assert len(run_configs) == 2
    assert llm.calls[0].error == "APIConnectionError: Connection error."
    assert llm.calls[0].recovered_by == "compiler_transport_retry_success"
    assert llm.calls[1].retry_of == "task_compiler:transport_attempt_1"


def test_compiler_stages_emit_public_progress_before_and_after_work(tmp_path, monkeypatch) -> None:
    events: list[tuple[str, dict, str]] = []
    runtime = EvidenceCompilerRuntime(
        LlmClient(_settings(tmp_path)),
        progress_sink=lambda kind, payload, summary: events.append((kind, payload, summary)),
    )

    def fake_phase(**kwargs):
        name = kwargs["name"]
        assert events[-1][1]["stage"] == name
        assert events[-1][1]["status"] == "started"
        if name == "task_compiler":
            return _plan()
        if name == "executor":
            assert kwargs["max_turns"] == EXECUTOR_MAX_TURNS == 10
            return ExecutorSummary(
                completed_check_ids=[],
                unresolved_check_ids=["check.vendor"],
                summary="nothing admitted",
            )
        return VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id="check.vendor",
                    status="NOT_FOUND",
                    examined_source_ids=[_sources()[0].record.source_id],
                    missing_fact="grounded vendor identity",
                )
            ]
        )

    monkeypatch.setattr(runtime, "_run_phase", fake_phase)
    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
        requirement_requiredness={"vendor_identity": False},
    )

    assert result.proof.decision_for("vendor_identity").status == "NOT_FOUND"
    stage_statuses = [
        (payload["stage"], payload["status"])
        for _kind, payload, _summary in events
        if payload.get("stage") in {"task_compiler", "executor", "fine_verifier", "proof_kernel"}
    ]
    for stage in ("task_compiler", "executor", "fine_verifier", "proof_kernel"):
        assert (stage, "started") in stage_statuses
        assert (stage, "completed") in stage_statuses
        assert stage_statuses.index((stage, "started")) < stage_statuses.index((stage, "completed"))
    assert all("reasoning_excerpt" not in payload for _kind, payload, _summary in events)


def test_sandbox_hook_rejection_is_emitted_without_source_text() -> None:
    source = _sources()[0].record
    sandbox = EvidenceSandbox(sources=[source], allowed_check_ids=["check.vendor"])
    events: list[tuple[str, dict | None]] = []
    tools = _sandbox_tools(sandbox, progress_sink=lambda tool, result: events.append((tool, result)))
    bind = next(tool for tool in tools if tool.name == "bind_claim")

    result = json.loads(
        asyncio.run(
            bind.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "subject": "vendor:V-100",
                        "predicate": "status",
                        "value": "ACTIVE",
                        "source_id": source.source_id,
                        "quote": "Vendor V-100 is ACTIVE.",
                        "locator": "BODY",
                    }
                ),
            )
        )
    )

    assert result["error"]["code"] == "SOURCE_NOT_READ"
    assert events[0] == ("bind_claim", None)
    assert events[1][1]["error"]["code"] == "SOURCE_NOT_READ"
    assert source.content not in json.dumps(events, ensure_ascii=False)


def test_focused_executor_cannot_write_another_check_or_leak_unowned_claim(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[tuple[str, dict, str]] = []
    runtime = EvidenceCompilerRuntime(
        LlmClient(_settings(tmp_path)),
        progress_sink=lambda kind, payload, action: events.append((kind, payload, action)),
    )
    plan = _two_check_plan()
    prepared = _sources()
    source = prepared[0].record
    frozen = EvidenceSandbox(
        sources=[source],
        allowed_check_ids=["check.one", "check.two"],
    )
    frozen.read_source(source.source_id)
    tool_results: dict[str, dict] = {}

    def fake_phase(**kwargs):
        tools = {item.name: item for item in kwargs["tools"]}
        tool_results["bind"] = json.loads(
            asyncio.run(
                tools["bind_claim"].on_invoke_tool(
                    None,
                    json.dumps(
                        {
                            "subject": "vendor:V-100",
                            "predicate": "status",
                            "value": "ACTIVE",
                            "source_id": source.source_id,
                            "quote": "Vendor V-100 is ACTIVE.",
                            "locator": "line 1",
                        }
                    ),
                )
            )
        )
        tool_results["submit_other"] = json.loads(
            asyncio.run(
                tools["submit_check"].on_invoke_tool(
                    None,
                    json.dumps(
                        {
                            "check_id": "check.two",
                            "claim_ids": [tool_results["bind"]["claim"]["id"]],
                            "note": "hostile attempt to upgrade the frozen CHECK",
                        }
                    ),
                )
            )
        )
        return ExecutorSummary(
            completed_check_ids=[],
            unresolved_check_ids=["check.one"],
            summary="hostile focused candidate",
            execution_status="PARTIAL",
        )

    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    summary, result = runtime.execute_plan(
        plan=plan,
        prepared_sources=prepared,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
        sandbox=frozen,
        focus_check_ids=["check.one"],
    )

    assert tool_results["bind"]["ok"] is True
    assert tool_results["submit_other"]["ok"] is False
    assert tool_results["submit_other"]["error"]["code"] == "CHECK_OUTSIDE_FOCUS"
    assert result is frozen
    assert result.evidence_ir.claims == []
    assert result.submissions == ()
    assert summary.completed_check_ids == []
    assert summary.unresolved_check_ids == ["check.one"]
    boundary = next(
        payload
        for _kind, payload, _action in events
        if payload.get("violation_code") == "FOCUSED_CHECK_SCOPE_VIOLATION"
    )
    assert boundary["scope_error"] == "UNOWNED_FOCUSED_PROOF_MATERIAL"
    assert boundary["orphan_claim_ids"] == [tool_results["bind"]["claim"]["id"]]


def test_focused_repair_of_a_cannot_upgrade_frozen_b(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    plan = _two_check_plan()
    prepared = _sources()
    source = prepared[0].record
    hostile_submit_results: list[dict] = []

    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def invoke(tools, name: str, payload: dict) -> dict:
        tool = next(item for item in tools if item.name == name)
        return json.loads(
            asyncio.run(tool.on_invoke_tool(None, json.dumps(payload)))
        )

    def fake_phase(**kwargs):
        assert kwargs["name"] == "executor"
        focus = kwargs["payload"]["focus_check_ids"]
        tools = kwargs["tools"]
        if not focus:
            invoke(tools, "read_source", {"source_id": source.source_id})
            for check_id in ("check.one", "check.two"):
                assert invoke(
                    tools,
                    "submit_check",
                    {"check_id": check_id, "note": "initially unresolved"},
                )["ok"] is True
            return ExecutorSummary(
                completed_check_ids=["check.one", "check.two"],
                summary="initial submissions",
            )

        assert focus == ["check.one"]
        claim = invoke(
            tools,
            "bind_claim",
            {
                "subject": "vendor:V-100",
                "predicate": "status",
                "value": "ACTIVE",
                "source_id": source.source_id,
                "quote": "Vendor V-100 is ACTIVE.",
                "locator": "line 1",
            },
        )["claim"]
        hostile_submit_results.append(
            invoke(
                tools,
                "submit_check",
                {
                    "check_id": "check.two",
                    "claim_ids": [claim["id"]],
                    "note": "attempted frozen-CHECK upgrade",
                },
            )
        )
        assert invoke(
            tools,
            "submit_check",
            {
                "check_id": "check.one",
                "claim_ids": [claim["id"]],
                "note": "valid focused repair",
            },
        )["ok"] is True
        return ExecutorSummary(
            completed_check_ids=["check.one"],
            summary="focused repair",
        )

    def fake_verify(**kwargs):
        if kwargs.get("focus_check_ids"):
            claim_ids = [item.id for item in kwargs["sandbox"].evidence_ir.claims]
            return [
                CheckAssessment(
                    check_id="check.one",
                    claim_ids=claim_ids,
                    source_ids=[source.source_id],
                    examined_source_ids=[source.source_id],
                    reason="A was repaired",
                    status="SUPPORTED",
                )
            ]
        return [
            CheckAssessment(
                check_id=check_id,
                missing_fact=f"evidence for {check_id}",
                reason="frozen initial NOT_FOUND",
                status="NOT_FOUND",
            )
            for check_id in ("check.one", "check.two")
        ]

    terminal_gap = CompiledProof(
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_REQUIRED",
                node_id="check.one",
                message="A needs one terminal",
            )
        ]
    )
    compiled = iter([terminal_gap, CompiledProof()])
    monkeypatch.setattr(runtime, "_run_phase", fake_phase)
    monkeypatch.setattr(runtime, "verify", fake_verify)
    monkeypatch.setattr(
        "app.compiler_runtime.runtime.compile_review_artifact",
        lambda *_args, **_kwargs: next(compiled),
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=prepared,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert hostile_submit_results[0]["ok"] is False
    assert hostile_submit_results[0]["error"]["code"] == "CHECK_OUTSIDE_FOCUS"
    assessment_by_id = {item.check_id: item for item in result.artifact.assessments}
    assert assessment_by_id["check.one"].status == "SUPPORTED"
    assert assessment_by_id["check.two"].status == "NOT_FOUND"
    assert assessment_by_id["check.two"].reason == "frozen initial NOT_FOUND"
    assert result.artifact.submitted_claim_refs["check.two"] == []
    assert len(result.artifact.submitted_claim_refs["check.one"]) == 1
    assert result.retry_count == 1


def test_task_compiler_planning_context_is_independent_of_source_identity() -> None:
    first_catalog = [
        {"source_id": "source.secret-a", "title": "invoice-a.pdf", "kind": "invoice", "characters": 120},
        {"source_id": "source.secret-b", "title": "invoice-b.pdf", "kind": "invoice", "characters": 80},
    ]
    renamed_catalog = [
        {"source_id": "renamed-1", "title": "x.pdf", "kind": "invoice", "characters": 120},
        {"source_id": "renamed-2", "title": "y.pdf", "kind": "invoice", "characters": 80},
    ]
    first_extraction = [
        {
            "attachment_id": "att-secret",
            "name": "invoice-a.pdf",
            "content_kind": "invoice",
            "available_fields": ["total", "currency"],
            "warnings": ["low contrast"],
        }
    ]
    renamed_extraction = [
        {
            "attachment_id": "renamed-att",
            "name": "x.pdf",
            "content_kind": "invoice",
            "available_fields": ["currency", "total"],
            "warnings": ["different warning text"],
        }
    ]

    assert _planning_source_catalog(first_catalog) == _planning_source_catalog(renamed_catalog)
    assert _planning_extraction_summary(first_extraction) == _planning_extraction_summary(renamed_extraction)
    assert _planning_source_catalog(first_catalog) == [
        {"kind": "invoice", "count": 2, "total_characters": 200}
    ]


class _ScriptedRuntime(EvidenceCompilerRuntime):
    def __init__(self, llm: LlmClient, *, resolve_on_retry: bool) -> None:
        super().__init__(llm)
        self.resolve_on_retry = resolve_on_retry
        self.execute_calls = 0
        self.verify_calls = 0

    def compile_task(self, **_kwargs):
        return _plan()

    def execute_plan(self, *, plan, prepared_sources, policy_excerpt, sandbox=None, focus_check_ids=(), hook_feedback=()):
        del policy_excerpt, hook_feedback
        self.execute_calls += 1
        if sandbox is None:
            record = prepared_sources[0].record
            sandbox = EvidenceSandbox(
                sources=[record],
                allowed_check_ids=["check.vendor"],
                evidence_ir=EvidenceIR(
                    source_ids=[record.source_id],
                    source_fingerprints={
                        record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
                    },
                ),
            )
            sandbox.read_source(record.source_id)
        if self.execute_calls == 1 or self.resolve_on_retry:
            record = prepared_sources[0].record
            sandbox.bind_claim(
                subject="vendor:V-100",
                predicate="status",
                value="ACTIVE" if self.execute_calls == 1 else "identity_confirmed",
                source_id=record.source_id,
                quote="Vendor V-100 is ACTIVE.",
                locator="line 3",
                confidence="high",
            )
            sandbox.submit_check(
                check_id="check.vendor",
                claim_ids=[item.id for item in sandbox.evidence_ir.claims],
                note="grounded source read",
            )
        return (
            ExecutorSummary(
                completed_check_ids=[] if focus_check_ids else ["check.vendor"],
                unresolved_check_ids=list(focus_check_ids),
                summary="scripted executor",
            ),
            sandbox,
        )

    def verify(self, *, plan, sandbox, policy_excerpt):
        del plan, policy_excerpt
        self.verify_calls += 1
        claim_ids = [item.id for item in sandbox.evidence_ir.claims]
        if self.verify_calls == 1 and self.resolve_on_retry:
            return [
                CheckAssessment(
                    check_id="check.vendor",
                    status="NOT_FOUND",
                    examined_source_ids=list(sandbox.evidence_ir.source_ids),
                    missing_fact="a second grounded identity fact",
                )
            ]
        return [
            CheckAssessment(
                check_id="check.vendor",
                status="SUPPORTED",
                claim_ids=claim_ids,
                source_ids=[sandbox.evidence_ir.source_ids[0]],
                examined_source_ids=list(sandbox.evidence_ir.source_ids),
                reason="the vendor identity is directly grounded",
            )
        ]


class _DecisiveAnyRuntime(EvidenceCompilerRuntime):
    def __init__(self, llm: LlmClient) -> None:
        super().__init__(llm)
        self.execute_calls = 0

    def compile_task(self, **_kwargs):
        return ProofPlan(
            plan_id="plan.decisive-any",
            objective="Stop when one independently sufficient branch is supported.",
            active_requirement_ids=["vendor_identity"],
            roots={"vendor_identity": "root.any"},
            nodes=[
                ProofNode(
                    id="check.primary",
                    kind="CHECK",
                    statement="The primary source establishes the vendor identity.",
                    requirement_refs=["vendor_identity"],
                ),
                ProofNode(
                    id="check.alternative",
                    kind="CHECK",
                    statement="An alternative source establishes the vendor identity.",
                    requirement_refs=["vendor_identity"],
                ),
                ProofNode(id="root.any", kind="ANY", depends_on=["check.primary", "check.alternative"]),
            ],
        )

    def execute_plan(self, *, plan, prepared_sources, policy_excerpt, sandbox=None, **_kwargs):
        del plan, policy_excerpt
        self.execute_calls += 1
        if sandbox is None:
            record = prepared_sources[0].record
            sandbox = EvidenceSandbox(
                sources=[record],
                allowed_check_ids=["check.primary", "check.alternative"],
                evidence_ir=EvidenceIR(
                    source_ids=[record.source_id],
                    source_fingerprints={
                        record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
                    },
                ),
            )
            sandbox.read_source(record.source_id)
            bound = sandbox.bind_claim(
                subject="vendor:V-100",
                predicate="identity",
                value="V-100",
                source_id=record.source_id,
                quote="Vendor V-100 is ACTIVE.",
                locator="line 5",
                confidence="high",
            )
            sandbox.submit_check(
                check_id="check.primary",
                claim_ids=[bound["claim"]["id"]],
            )
            sandbox.submit_check(check_id="check.alternative", note="no alternative source")
        return ExecutorSummary(summary="scripted decisive ANY"), sandbox

    def verify(self, *, plan, sandbox, policy_excerpt):
        del plan, policy_excerpt
        claim = sandbox.evidence_ir.claims[0]
        return [
            CheckAssessment(
                check_id="check.primary",
                status="SUPPORTED",
                claim_ids=[claim.id],
                source_ids=[claim.source_id],
                examined_source_ids=list(sandbox.evidence_ir.source_ids),
            ),
            CheckAssessment(
                check_id="check.alternative",
                status="NOT_FOUND",
                examined_source_ids=list(sandbox.evidence_ir.source_ids),
                missing_fact="an alternative source",
            ),
        ]


def test_prepare_sources_is_content_addressed_and_preserves_exact_text() -> None:
    first = _sources()[0]
    second = prepare_sources(
        [
            {
                "attachment_id": "att_vendor",
                "name": "vendor.md",
                "content_kind": "vendor_record",
                "content": "Vendor V-100 is BLOCKED.",
            }
        ]
    )[0]

    assert "Vendor V-100 is ACTIVE." in first.record.content
    assert first.record.source_id != second.record.source_id
    assert first.metadata["attachment_id"] == "att_vendor"


def test_prepare_sources_exposes_safe_system_provenance_without_absolute_paths() -> None:
    prepared = prepare_sources(
        [
            {
                "attachment_id": "att_invoice",
                "name": "invoice.pdf",
                "path": r"C:\\private\\case\\invoice.pdf",
                "original_ref": "attachments/originals/invoice.pdf",
                "sha256": "upload-sha256",
                "extraction_ref": "evidence/extractions/att_invoice.json",
                "extraction_sha256": "extraction-sha256",
                "preview_paths": ["evidence/previews/invoice_p1.png"],
                "content": "Invoice INV-42",
            }
        ]
    )[0]

    provenance = prepared.record.provenance
    assert provenance["runtime_admission"] == "admitted"
    assert provenance["attachment_id"] == "att_invoice"
    assert provenance["original_ref"] == "attachments/originals/invoice.pdf"
    assert provenance["source_sha256"] == "upload-sha256"
    assert provenance["content_sha256"] == hashlib.sha256(
        prepared.record.content.encode("utf-8")
    ).hexdigest()
    assert provenance["scope"] == "system_chain_of_custody_only_not_real_world_authenticity"
    assert "C:\\private" not in json.dumps(provenance)


def test_prepare_sources_prefers_full_extracted_text_over_compact_markdown() -> None:
    full_text = "opening\n" + ("invoice line\n" * 400) + "TAIL FACT INV-999"

    prepared = prepare_sources(
        [
            {
                "attachment_id": "att_long_invoice",
                "name": "long-invoice.pdf",
                "content_kind": "pdf",
                "body_markdown": "short preview only",
                "content": full_text,
            }
        ]
    )[0]

    assert "TAIL FACT INV-999" in prepared.record.content
    assert len(prepared.record.content) > len(full_text)


def test_attachment_source_admission_requires_success_scope_and_readable_content() -> None:
    valid = {
        "status": "success",
        "manifest_status": "active",
        "content": "Invoice INV-1",
    }

    assert attachment_source_admission(valid) == (True, "admitted")
    assert attachment_source_admission({**valid, "status": "error"}) == (
        False,
        "attachment_status_not_success",
    )
    assert attachment_source_admission({**valid, "manifest_status": "quarantined"}) == (
        False,
        "manifest_status_quarantined",
    )
    assert attachment_source_admission({**valid, "manifest_status": "excluded"}) == (
        False,
        "manifest_status_excluded",
    )
    assert attachment_source_admission(
        {**valid, "metadata": {"classification": "cross_case_sample"}}
    ) == (False, "classification_cross_case_sample")
    assert attachment_source_admission({**valid, "cross_case": True}) == (
        False,
        "source_explicitly_excluded",
    )
    assert attachment_source_admission({**valid, "content": ""}) == (
        False,
        "source_content_unreadable",
    )


def test_prepare_sources_deduplicates_identical_source_ids() -> None:
    source = {
        "source_id": "source.same",
        "source_content": "Vendor V-100 is ACTIVE.",
        "name": "vendor.md",
    }

    prepared = prepare_sources([source, dict(source)])

    assert len(prepared) == 1
    assert prepared[0].record.source_id == "source.same"


def test_prepare_sources_rejects_conflicting_content_for_one_source_id() -> None:
    with pytest.raises(ValueError, match="identifies conflicting content"):
        prepare_sources(
            [
                {"source_id": "source.same", "source_content": "Vendor V-100 is ACTIVE."},
                {"source_id": "source.same", "source_content": "Vendor V-100 is BLOCKED."},
            ]
        )


def test_policy_activation_and_unconfigured_values_stay_declarative() -> None:
    active = expand_active_requirements(["invoice", "purchase_order", "goods_receipt"])
    policy = policy_excerpt_for(active)

    assert "three_way_amount_match" in active
    assert policy["values"]["amount_tolerance_percent"] == {"configured": True, "value": "2"}
    duplicate_policy = policy_excerpt_for(["no_active_duplicate"])
    assert duplicate_policy["values"]["duplicate_search_window"] == {
        "configured": False,
        "value": None,
    }
    calculation_policy = policy_excerpt_for(["invoice_calculation_valid"])
    assert calculation_policy["values"]["invoice_calculation_rounding_tolerance"] == {
        "configured": True,
        "value": {
            "amount": "0.01",
            "unit": "document_currency",
            "scope": "invoice_internal_arithmetic_rounding",
            "note": "Absolute rounding allowance for one invoice's internal arithmetic; this is not the three-way matching percentage tolerance.",
        },
    }


def test_policy_expands_requirement_premises_in_stable_order() -> None:
    expected = ["vendor_identity_active", "vendor_identity"]

    assert expand_active_requirements(["vendor_identity_active"]) == expected
    assert expand_active_requirements(["vendor_identity_active"]) == expected
    assert expand_active_requirements(["vendor_identity_active", "vendor_identity_active"]) == expected


def test_prepare_sources_preserves_persisted_source_identity_exactly() -> None:
    source_content = "Vendor V-100 is ACTIVE.\nEffective 2026-08-01."
    persisted = {
        "source_id": "evc_persisted_vendor",
        "source_content": source_content,
        "source_fingerprint": hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
        "already_persisted": True,
        "name": "vendor.md",
        "evidence_type": "vendor_record",
        "content": "this fallback must not replace persisted source_content",
    }

    first = prepare_sources([persisted])[0]
    replay = prepare_sources([dict(persisted)])[0]

    assert first == replay
    assert first.record.source_id == persisted["source_id"]
    assert first.record.content == persisted["source_content"]
    assert first.metadata["source_fingerprint"] == persisted["source_fingerprint"]
    assert first.metadata["already_persisted"] is True


@pytest.mark.parametrize("missing_field", ["source_id", "source_content", "source_fingerprint"])
def test_prepare_sources_rejects_incomplete_persisted_identity(missing_field: str) -> None:
    persisted = {
        "source_id": "evc_persisted_vendor",
        "source_content": "Vendor V-100 is ACTIVE.",
        "source_fingerprint": "sha256:persisted-vendor-v1",
        "already_persisted": True,
    }
    persisted[missing_field] = ""

    with pytest.raises(ValueError, match=missing_field):
        prepare_sources([persisted])


def test_runtime_finishes_supported_case_without_retry(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=False)

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert result.proof.decision_for("vendor_identity").status == "SUPPORTED"
    assert result.retry_count == 0
    assert runtime.execute_calls == 1
    assert runtime.verify_calls == 1
    EvidenceReviewResult.model_validate(result.review_result)
    evidence = result.review_result["suggested_patch"]["add_evidence"][0]
    assert evidence["credibility"] == "medium"
    assert evidence["metadata"]["classification"] == "business_evidence"
    assert evidence["review_result"]["should_accept"] is True
    assert result.review_result["source_traceability"] == "original_document"


def test_runtime_projects_file_format_to_policy_declared_evidence_type(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=False)
    prepared = prepare_sources(
        [
            {
                "attachment_id": "att_vendor_image",
                "name": "vendor-record.png",
                "content_kind": "image",
                "content": "Vendor V-100 is ACTIVE.",
            }
        ]
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=prepared,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    evidence = result.review_result["suggested_patch"]["add_evidence"][0]
    assert evidence["type"] == "vendor_record"
    assert evidence["review_result"]["evidence_type"] == "vendor_record"


def test_review_result_preserves_source_quality_and_does_not_accept_unused_source(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=False)
    sources = _sources() + prepare_sources(
        [
            {
                "attachment_id": "att_process",
                "name": "process.log",
                "content": "Workflow event observed.",
                "classification": "process_only",
                "credibility": "low",
            }
        ]
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=sources,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    by_name = {
        item["metadata"]["source_filename"]: item
        for item in result.review_result["suggested_patch"]["add_evidence"]
    }
    unused = by_name["process.log"]
    assert unused["credibility"] == "low"
    assert unused["metadata"]["classification"] == "process_only"
    assert unused["review_result"]["should_accept"] is False


def test_review_result_accepts_grounded_source_submitted_to_unresolved_check(tmp_path) -> None:
    prepared = _sources()
    record = prepared[0].record
    sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={
                record.source_id: prepared[0].metadata["source_fingerprint"]
            },
        ),
    )
    sandbox.read_source(record.source_id)
    bound = sandbox.bind_claim(
        subject="vendor:V-100",
        predicate="status",
        value="ACTIVE",
        source_id=record.source_id,
        quote="Vendor V-100 is ACTIVE.",
        locator="BODY",
        confidence="high",
    )
    sandbox.submit_check(
        check_id="check.vendor",
        claim_ids=[bound["claim"]["id"]],
    )
    plan = _plan()
    policy = policy_excerpt_for(["vendor_identity"])
    artifact = _artifact(
        plan=plan,
        evidence_ir=sandbox.evidence_ir,
        assessments=[
            CheckAssessment(
                check_id="check.vendor",
                status="NOT_FOUND",
                examined_source_ids=[record.source_id],
                missing_fact="a configured verification policy",
            )
        ],
        submitted_claim_refs={"check.vendor": [bound["claim"]["id"]]},
        policy_excerpt=policy,
        model="fixture",
    )

    review = _review_result(
        prepared_sources=prepared,
        sandbox=sandbox,
        artifact=artifact,
        proof=compile_review_artifact(artifact),
    )

    evidence = review["suggested_patch"]["add_evidence"][0]
    assert evidence["review_result"]["should_accept"] is True
    assert evidence["supports"] == []


def test_review_result_projects_each_requirement_own_claim_quote() -> None:
    prepared = prepare_sources(
        [
            {
                "attachment_id": "att_invoice",
                "name": "invoice.txt",
                "content_kind": "invoice",
                "content": "Invoice number: INV-42\nTotal amount: EUR 125.00",
            }
        ]
    )
    record = prepared[0].record
    plan = ProofPlan(
        plan_id="plan.invoice-fields",
        objective="Verify two invoice fields.",
        active_requirement_ids=["invoice_number", "amount_total"],
        roots={
            "invoice_number": "check.invoice_number",
            "amount_total": "check.amount_total",
        },
        nodes=[
            ProofNode(
                id="check.invoice_number",
                kind="CHECK",
                statement="The invoice number is present.",
                requirement_refs=["invoice_number"],
            ),
            ProofNode(
                id="check.amount_total",
                kind="CHECK",
                statement="The total amount is present.",
                requirement_refs=["amount_total"],
            ),
        ],
    )
    sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=[node.id for node in plan.nodes],
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={
                record.source_id: prepared[0].metadata["source_fingerprint"],
            },
        ),
    )
    sandbox.read_source(record.source_id)
    number_claim = sandbox.bind_claim(
        subject="invoice:INV-42",
        predicate="invoice_number",
        value="INV-42",
        source_id=record.source_id,
        quote="Invoice number: INV-42",
        locator="lines 1-1",
        confidence="high",
    )["claim"]
    amount_claim = sandbox.bind_claim(
        subject="invoice:INV-42",
        predicate="amount_total",
        value={"amount": 125, "currency": "EUR"},
        source_id=record.source_id,
        quote="Total amount: EUR 125.00",
        locator="lines 2-2",
        confidence="high",
    )["claim"]
    sandbox.submit_check(check_id="check.invoice_number", claim_ids=[number_claim["id"]])
    sandbox.submit_check(check_id="check.amount_total", claim_ids=[amount_claim["id"]])
    assessments = [
        CheckAssessment(
            check_id="check.invoice_number",
            status="SUPPORTED",
            claim_ids=[number_claim["id"]],
            source_ids=[record.source_id],
            examined_source_ids=[record.source_id],
        ),
        CheckAssessment(
            check_id="check.amount_total",
            status="SUPPORTED",
            claim_ids=[amount_claim["id"]],
            source_ids=[record.source_id],
            examined_source_ids=[record.source_id],
        ),
    ]
    policy = policy_excerpt_for(plan.active_requirement_ids)
    artifact = _artifact(
        plan=plan,
        evidence_ir=sandbox.evidence_ir,
        assessments=assessments,
        submitted_claim_refs={
            "check.invoice_number": [number_claim["id"]],
            "check.amount_total": [amount_claim["id"]],
        },
        policy_excerpt=policy,
        model="fixture",
    )

    review = _review_result(
        prepared_sources=prepared,
        sandbox=sandbox,
        artifact=artifact,
        proof=compile_review_artifact(artifact),
    )

    supports = {
        item["requirement"]: item["quoted_text"]
        for item in review["suggested_patch"]["add_evidence"][0]["supports"]
    }
    assert supports == {
        "invoice_number": "Invoice number: INV-42",
        "amount_total": "Total amount: EUR 125.00",
    }


def test_review_result_uses_only_contradicting_leaf_sources_for_a_conflict() -> None:
    prepared = prepare_sources(
        [
            {
                "source_id": "source.support",
                "name": "support.txt",
                "content_kind": "invoice",
                "source_content": "Invoice identity is INV-42.",
            },
            {
                "source_id": "source.conflict",
                "name": "conflict.txt",
                "content_kind": "invoice",
                "source_content": "Configured baseline is contradicted.",
            },
        ]
    )
    records = [item.record for item in prepared]
    plan = ProofPlan(
        plan_id="plan.mixed-polarity",
        objective="Require both invoice identity and baseline conformance.",
        active_requirement_ids=["invoice"],
        roots={"invoice": "root.invoice"},
        nodes=[
            ProofNode(
                id="check.identity",
                kind="CHECK",
                statement="The invoice identity is grounded.",
                requirement_refs=["invoice"],
            ),
            ProofNode(
                id="check.baseline",
                kind="CHECK",
                statement="The invoice conforms to the baseline.",
                requirement_refs=["invoice"],
            ),
            ProofNode(
                id="root.invoice",
                kind="ALL",
                depends_on=["check.identity", "check.baseline"],
            ),
        ],
    )
    sandbox = EvidenceSandbox(
        sources=records,
        allowed_check_ids=["check.identity", "check.baseline"],
        evidence_ir=EvidenceIR(
            source_ids=[item.source_id for item in records],
            source_fingerprints={
                item.record.source_id: item.metadata["source_fingerprint"]
                for item in prepared
            },
        ),
    )
    for record in records:
        sandbox.read_source(record.source_id)
    support_claim = sandbox.bind_claim(
        subject="invoice:INV-42",
        predicate="identity",
        value="INV-42",
        source_id="source.support",
        quote="Invoice identity is INV-42.",
        locator="line 1",
        confidence="high",
    )["claim"]
    conflict_claim = sandbox.bind_claim(
        subject="invoice:INV-42",
        predicate="baseline_conformance",
        value=False,
        source_id="source.conflict",
        quote="Configured baseline is contradicted.",
        locator="line 1",
        confidence="high",
    )["claim"]
    sandbox.submit_check(check_id="check.identity", claim_ids=[support_claim["id"]])
    sandbox.submit_check(check_id="check.baseline", claim_ids=[conflict_claim["id"]])
    examined = [item.source_id for item in records]
    policy = policy_excerpt_for(["invoice"])
    artifact = _artifact(
        plan=plan,
        evidence_ir=sandbox.evidence_ir,
        assessments=[
            CheckAssessment(
                check_id="check.identity",
                status="SUPPORTED",
                claim_ids=[support_claim["id"]],
                source_ids=["source.support"],
                examined_source_ids=examined,
            ),
            CheckAssessment(
                check_id="check.baseline",
                status="CONTRADICTED",
                claim_ids=[conflict_claim["id"]],
                source_ids=["source.conflict"],
                examined_source_ids=examined,
            ),
        ],
        submitted_claim_refs={
            "check.identity": [support_claim["id"]],
            "check.baseline": [conflict_claim["id"]],
        },
        policy_excerpt=policy,
        model="fixture",
    )
    proof = compile_review_artifact(artifact)

    review = _review_result(
        prepared_sources=prepared,
        sandbox=sandbox,
        artifact=artifact,
        proof=proof,
    )

    assert proof.decision_for("invoice").status == "CONTRADICTED"
    assert review["conflicts"][0]["quoted_text"] == "Configured baseline is contradicted."
    assert review["conflicts"][0]["affected_evidence_ids"] == ["source.conflict"]
    evidence = {item["id"]: item for item in review["suggested_patch"]["add_evidence"]}
    assert evidence["source.support"]["conflicts"] == []
    assert evidence["source.conflict"]["conflicts"][0]["quoted_text"] == (
        "Configured baseline is contradicted."
    )


def test_flipkart_contradiction_is_consistent_across_proof_review_and_patch() -> None:
    baseline_ref = "invoice_template_baseline_ref"
    policy = policy_excerpt_for(["invoice_number", "template_match"])
    policy["values"][baseline_ref] = {
        "configured": True,
        "value": "flipkart_invoice_layout_v1",
    }
    prepared = prepare_sources(
        [
            {
                "attachment_id": "att_flipkart",
                "name": "FlipkartInvoice.pdf",
                "content_kind": "invoice",
                "content": "Invoice number: BLR_WFLD20151000982590\nLayout does not match the configured baseline.",
            }
        ]
    )
    record = prepared[0].record
    plan = ProofPlan(
        plan_id="plan.flipkart",
        objective="Verify invoice identity and configured template match.",
        active_requirement_ids=["invoice_number", "template_match"],
        policy_refs=[baseline_ref],
        roots={
            "invoice_number": "check.invoice_number",
            "template_match": "check.template_match",
        },
        nodes=[
            ProofNode(
                id="check.invoice_number",
                kind="CHECK",
                statement="The invoice number is present.",
                requirement_refs=["invoice_number"],
            ),
            ProofNode(
                id="check.template_match",
                kind="CHECK",
                statement="The invoice matches the configured template baseline.",
                requirement_refs=["template_match"],
                policy_refs=[baseline_ref],
                facet_refs=["baseline_comparison"],
            ),
        ],
    )
    sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=[node.id for node in plan.nodes],
        allowed_check_facets={"check.template_match": ["baseline_comparison"]},
        allowed_check_policy_refs={"check.template_match": [baseline_ref]},
        policy_values=_configured_policy_values(policy),
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={record.source_id: prepared[0].metadata["source_fingerprint"]},
        ),
    )
    sandbox.read_source(record.source_id)
    number = sandbox.bind_claim(
        subject="invoice:BLR_WFLD20151000982590",
        predicate="invoice_number",
        value="BLR_WFLD20151000982590",
        source_id=record.source_id,
        quote="Invoice number: BLR_WFLD20151000982590",
        locator="line 1",
        confidence="high",
    )["claim"]
    mismatch = sandbox.bind_claim(
        subject="invoice:BLR_WFLD20151000982590",
        predicate="template_match",
        value=False,
        source_id=record.source_id,
        quote="Layout does not match the configured baseline.",
        locator="line 2",
        confidence="high",
    )["claim"]
    sandbox.submit_check(check_id="check.invoice_number", claim_ids=[number["id"]])
    binding_id = "binding.flipkart.template_baseline"
    sandbox.submit_check(
        check_id="check.template_match",
        claim_ids=[mismatch["id"]],
        binding_proposals=[
            {
                "id": binding_id,
                "check_id": "check.template_match",
                "facet_ref": "baseline_comparison",
                "relation": "candidate_layout_compared_with_selected_baseline",
                "term_refs": [
                    {"kind": "CLAIM", "ref_id": mismatch["id"]},
                    {"kind": "POLICY", "ref_id": baseline_ref},
                ],
                "reason": "The mismatch claim explicitly refers to the selected configured baseline.",
            }
        ],
    )
    assessments = [
        CheckAssessment(
            check_id="check.invoice_number",
            status="SUPPORTED",
            claim_ids=[number["id"]],
            source_ids=[record.source_id],
            examined_source_ids=[record.source_id],
        ),
        CheckAssessment(
            check_id="check.template_match",
            status="CONTRADICTED",
            claim_ids=[mismatch["id"]],
                source_ids=[record.source_id],
                examined_source_ids=[record.source_id],
                accepted_binding_ids=[binding_id],
                reason="The configured template baseline is contradicted.",
            ),
        ]
    artifact = _artifact(
        plan=plan,
        evidence_ir=sandbox.evidence_ir,
        assessments=assessments,
        submitted_claim_refs={
            "check.invoice_number": [number["id"]],
            "check.template_match": [mismatch["id"]],
        },
        submitted_binding_refs={"check.template_match": [binding_id]},
        policy_excerpt=policy,
        model="fixture",
        sandbox=sandbox,
    )
    proof = compile_review_artifact(artifact)
    review = _review_result(
        prepared_sources=prepared,
        sandbox=sandbox,
        artifact=artifact,
        proof=proof,
    )

    expected_quote = "Layout does not match the configured baseline."
    evidence = review["suggested_patch"]["add_evidence"][0]
    assert proof.decision_for("template_match").status == "CONTRADICTED"
    assert review["risk_flags"] == ["template_match"]
    assert review["suggested_patch"]["risk_flags"] == review["risk_flags"]
    assert review["conflicts"][0]["requirement"] == "template_match"
    assert review["conflicts"][0]["quoted_text"] == expected_quote
    assert evidence["conflicts"][0]["quoted_text"] == expected_quote
    assert review["supports"][0]["quoted_text"] == "Invoice number: BLR_WFLD20151000982590"
    assert review["evidence_type"] == "invoice"
    assert review["source_traceability"] == "original_document"


def test_runtime_does_not_retry_unresolved_leaf_below_decisive_any_root(tmp_path) -> None:
    runtime = _DecisiveAnyRuntime(LlmClient(_settings(tmp_path)))

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert result.proof.decision_for("vendor_identity").status == "SUPPORTED"
    assert result.proof.obligations == []
    assert result.retry_count == 0
    assert runtime.execute_calls == 1


def test_runtime_does_not_retry_optional_not_found_requirement(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=True)

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
        requirement_requiredness={"vendor_identity": False},
    )

    assert result.proof.decision_for("vendor_identity").status == "NOT_FOUND"
    assert result.proof.obligations
    assert all(item.blocking is False for item in result.proof.obligations)
    assert result.retry_count == 0
    assert runtime.execute_calls == 1
    assert runtime.verify_calls == 1
    assert result.review_result["suggested_patch"]["next_questions"]


def test_verifier_receives_full_sources_and_only_per_check_submitted_claims(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = prepare_sources(
        [
            {"attachment_id": "att_vendor", "content": "Vendor V-100 is ACTIVE."},
            {"attachment_id": "att_note", "content": "Unrelated payment note."},
        ]
    )
    records = [item.record for item in prepared]
    sandbox = EvidenceSandbox(
        sources=records,
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[item.source_id for item in records],
            source_fingerprints={
                item.source_id: hashlib.sha256(item.content.encode("utf-8")).hexdigest()
                for item in records
            },
        ),
    )
    for record in records:
        sandbox.read_source(record.source_id)
    vendor_record = next(item for item in records if "Vendor V-100" in item.content)
    note_record = next(item for item in records if "Unrelated payment" in item.content)
    vendor_claim = sandbox.bind_claim(
        subject="vendor:V-100",
        predicate="status",
        value="ACTIVE",
        source_id=vendor_record.source_id,
        quote="Vendor V-100 is ACTIVE.",
        locator="line 5",
        confidence="high",
    )["claim"]
    sandbox.bind_claim(
        subject="note:1",
        predicate="text",
        value="unrelated",
        source_id=note_record.source_id,
        quote="Unrelated payment note.",
        locator="line 5",
        confidence="high",
    )
    sandbox.submit_check(check_id="check.vendor", claim_ids=[vendor_claim["id"]])
    captured: dict = {}

    def fake_phase(**kwargs):
        captured.update(kwargs["payload"])
        return VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id="check.vendor",
                    status="SUPPORTED",
                    claim_ids=[vendor_claim["id"]],
                    source_ids=[vendor_record.source_id],
                    examined_source_ids=sorted(item.source_id for item in records),
                )
            ]
        )

    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    runtime.verify(plan=_plan(), sandbox=sandbox, policy_excerpt=policy_excerpt_for(["vendor_identity"]))

    assert "claims" not in captured
    assert {item["source_id"]: item["content"] for item in captured["sources"]} == {
        item.source_id: item.content for item in records
    }
    check = captured["checks"][0]
    assert check["submitted_claim_refs"] == [vendor_claim["id"]]
    assert [item["id"] for item in check["candidate_claims"]] == [vendor_claim["id"]]
    assert captured["calculation_operation_protocol"]["GREATER_THAN"] == {
        "ordered_semantics": "refs[0] > refs[1]",
        "equality_result": False,
        "symmetric": False,
    }
    assert "would follow if the linked boolean Witness replayed to true" in captured[
        "strong_status_link_protocol"
    ]["true_status"]
    assert "not the current classification" in captured["strong_status_link_protocol"][
        "true_status"
    ]


def test_verifier_focused_repair_exposes_only_named_checks_and_feedback(
    tmp_path,
    monkeypatch,
) -> None:
    plan = _two_check_plan()
    sandbox = EvidenceSandbox(
        sources=[],
        allowed_check_ids=["check.one", "check.two"],
    )
    sandbox.submit_check(check_id="check.one", note="first submission")
    sandbox.submit_check(check_id="check.two", note="second submission")
    feedback = [
        {
            "check_id": "check.two",
            "diagnostic_code": "TERMINAL_WITNESS_STATUS_MISMATCH",
            "kernel_message": "ordered predicate maps to the opposite status",
        }
    ]
    captured: dict = {}

    def fake_phase(**kwargs):
        captured.update(kwargs["payload"])
        return VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id="check.two",
                    reason="Hostile stale prose. Final classification: CONTRADICTED",
                    missing_fact="a justified terminal polarity",
                    gap_code="WITNESS_MISSING",
                    status="NOT_FOUND",
                )
            ]
        )

    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    assessments = runtime.verify(
        plan=plan,
        sandbox=sandbox,
        policy_excerpt={},
        focus_check_ids=["check.two"],
        repair_feedback=feedback,
    )

    assert [item.check_id for item in assessments] == ["check.two"]
    assert assessments[0].status == "NOT_FOUND"
    assert [item["id"] for item in captured["checks"]] == ["check.two"]
    assert captured["focus_check_ids"] == ["check.two"]
    assert captured["repair_feedback"] == feedback


def test_verifier_false_terminal_link_conflict_is_observed_but_not_auto_repaired(
    tmp_path,
    monkeypatch,
) -> None:
    plan = ProofPlan(
        plan_id="plan.ordered-threshold",
        objective="Check one ordered threshold predicate.",
        active_requirement_ids=["vendor_identity"],
        policy_refs=["threshold"],
        roots={"vendor_identity": "check.threshold"},
        nodes=[
            ProofNode(
                id="check.threshold",
                kind="CHECK",
                statement="The observed measure is at most the configured threshold.",
                requirement_refs=["vendor_identity"],
                policy_refs=["threshold"],
                facet_refs=["measure"],
            )
        ],
    )
    sandbox = EvidenceSandbox(
        sources=[],
        allowed_check_ids=["check.threshold"],
        allowed_check_facets={"check.threshold": ["measure"]},
        allowed_check_policy_refs={"check.threshold": ["threshold"]},
        policy_values={"threshold": "1"},
        policy_snapshot_hash="policy-snapshot",
    )
    terminal = sandbox.compute_witness(
        check_id="check.threshold",
        facet_ref="measure",
        operation="GREATER_THAN",
        refs=[
            {"kind": "POLICY", "ref_id": "threshold"},
            {"kind": "POLICY", "ref_id": "threshold"},
        ],
    )["witness"]
    assert terminal["result"] is False
    sandbox.submit_check(
        check_id="check.threshold",
        witness_ids=[terminal["id"]],
    )
    events: list[tuple[str, dict, str]] = []
    runtime = EvidenceCompilerRuntime(
        LlmClient(_settings(tmp_path)),
        progress_sink=lambda kind, payload, action: events.append((kind, payload, action)),
    )

    def fake_phase(**_kwargs):
        return VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id="check.threshold",
                    accepted_witness_ids=[terminal["id"]],
                    strong_status_links=[
                        StrongStatusLink(
                            witness_id=terminal["id"],
                            true_status="CONTRADICTED",
                        )
                    ],
                    reason="The false predicate is treated as a contradiction. Final classification: CONTRADICTED",
                    status="CONTRADICTED",
                )
            ]
        )

    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    assessments = runtime.verify(plan=plan, sandbox=sandbox, policy_excerpt={})

    # Runtime must not manufacture semantic polarity from this run's desired outcome.
    assert assessments[0].strong_status_links[0].true_status == "CONTRADICTED"
    boundary = next(
        payload
        for _kind, payload, _action in events
        if payload.get("violation_code") == "STRONG_STATUS_LINK_POLARITY_CONFLICT"
    )
    assert boundary["polarity_violations"] == [
        {
            "witness_id": terminal["id"],
            "witness_result": False,
            "true_status": "CONTRADICTED",
            "mapped_status": "SUPPORTED",
        }
    ]


def test_verifier_reconciles_one_explicit_final_status_without_another_call(
    tmp_path,
    monkeypatch,
) -> None:
    prepared = _sources()
    record = prepared[0].record
    sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={
                record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    sandbox.read_source(record.source_id)
    claim = sandbox.bind_claim(
        subject="vendor:V-100",
        predicate="status",
        value="ACTIVE",
        source_id=record.source_id,
        quote="Vendor V-100 is ACTIVE.",
        locator="line 1",
        confidence="high",
    )["claim"]
    sandbox.submit_check(check_id="check.vendor", claim_ids=[claim["id"]])
    calls = 0

    def fake_phase(**_kwargs):
        nonlocal calls
        calls += 1
        return VerificationBatch(
            assessments=[
                CheckAssessment(
                    check_id="check.vendor",
                    claim_ids=[claim["id"]],
                    source_ids=[record.source_id],
                    examined_source_ids=[record.source_id],
                    reason="The evidence directly refutes the check. Final classification: CONTRADICTED",
                    status="SUPPORTED",
                )
            ]
        )

    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    monkeypatch.setattr(runtime, "_run_phase", fake_phase)

    assessments = runtime.verify(
        plan=_plan(),
        sandbox=sandbox,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert calls == 1
    assert assessments[0].status == "CONTRADICTED"


def test_runtime_retries_only_unresolved_checks_once(tmp_path) -> None:
    runtime = _ScriptedRuntime(LlmClient(_settings(tmp_path)), resolve_on_retry=True)

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=_sources(),
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert result.proof.decision_for("vendor_identity").status == "SUPPORTED"
    assert result.retry_count == 1
    assert runtime.execute_calls == 2
    assert runtime.verify_calls == 2
    assert len(result.artifact.evidence_ir.claims) == 2


def test_terminal_status_mismatch_routes_one_focused_verifier_repair(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_events: list[tuple[str, dict, str]] = []
    runtime = EvidenceCompilerRuntime(
        LlmClient(_settings(tmp_path)),
        progress_sink=lambda kind, payload, action: runtime_events.append(
            (kind, payload, action)
        ),
    )
    plan = _plan()
    sandbox = EvidenceSandbox(sources=[], allowed_check_ids=["check.vendor"])
    sandbox.submit_check(check_id="check.vendor", note="typed proof already exists")
    execute_calls = 0
    verify_calls: list[dict] = []

    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def fake_execute(**_kwargs):
        nonlocal execute_calls
        execute_calls += 1
        return (
            ExecutorSummary(
                completed_check_ids=["check.vendor"],
                summary="scripted typed proof",
            ),
            sandbox,
        )

    def fake_verify(**kwargs):
        verify_calls.append(kwargs)
        repaired = bool(kwargs.get("focus_check_ids"))
        return [
            CheckAssessment(
                check_id="check.vendor",
                reason="repaired assessment" if repaired else "original assessment",
                missing_fact="a terminal polarity",
                gap_code="WITNESS_MISSING",
                status="NOT_FOUND",
            )
        ]

    mismatch = CompiledProof(
        obligations=[
            ProofObligation(
                id="obligation.check.vendor",
                requirement_id="vendor_identity",
                check_id="check.vendor",
                missing_fact="re-evaluate terminal Witness polarity",
            )
        ],
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_STATUS_MISMATCH",
                node_id="check.vendor",
                message="terminal result maps to the opposite strong status",
            )
        ],
    )
    compiled = iter([mismatch, CompiledProof()])
    monkeypatch.setattr(runtime, "execute_plan", fake_execute)
    monkeypatch.setattr(runtime, "verify", fake_verify)
    monkeypatch.setattr(
        "app.compiler_runtime.runtime.compile_review_artifact",
        lambda *_args, **_kwargs: next(compiled),
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=[],
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert execute_calls == 1
    assert len(verify_calls) == 2
    assert "focus_check_ids" not in verify_calls[0]
    assert verify_calls[1]["focus_check_ids"] == ["check.vendor"]
    feedback = verify_calls[1]["repair_feedback"]
    assert feedback[0]["diagnostic_code"] == "TERMINAL_WITNESS_STATUS_MISMATCH"
    assert feedback[0]["previous_assessment"]["reason"] == "original assessment"
    assert result.artifact.assessments[0].reason == "repaired assessment"
    assert result.retry_count == 1
    assert any(payload.get("status") == "repair_started" for _, payload, _ in runtime_events)


def test_failed_terminal_status_repair_preserves_not_found_without_executor_retry(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_events: list[tuple[str, dict, str]] = []
    runtime = EvidenceCompilerRuntime(
        LlmClient(_settings(tmp_path)),
        progress_sink=lambda kind, payload, action: runtime_events.append(
            (kind, payload, action)
        ),
    )
    plan = _plan()
    sandbox = EvidenceSandbox(sources=[], allowed_check_ids=["check.vendor"])
    sandbox.submit_check(check_id="check.vendor", note="typed proof already exists")
    execute_calls = 0
    verify_calls = 0

    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def fake_execute(**_kwargs):
        nonlocal execute_calls
        execute_calls += 1
        return ExecutorSummary(completed_check_ids=["check.vendor"]), sandbox

    def fake_verify(**kwargs):
        nonlocal verify_calls
        verify_calls += 1
        if kwargs.get("focus_check_ids"):
            raise ModelBehaviorError("focused repair remained malformed")
        return [
            CheckAssessment(
                check_id="check.vendor",
                reason="original fail-closed assessment",
                missing_fact="a terminal polarity",
                gap_code="WITNESS_MISSING",
                status="NOT_FOUND",
            )
        ]

    mismatch = CompiledProof(
        obligations=[
            ProofObligation(
                id="obligation.check.vendor",
                requirement_id="vendor_identity",
                check_id="check.vendor",
                missing_fact="re-evaluate terminal Witness polarity",
            )
        ],
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_STATUS_MISMATCH",
                node_id="check.vendor",
                message="terminal result maps to the opposite strong status",
            )
        ],
    )
    monkeypatch.setattr(runtime, "execute_plan", fake_execute)
    monkeypatch.setattr(runtime, "verify", fake_verify)
    monkeypatch.setattr(
        "app.compiler_runtime.runtime.compile_review_artifact",
        lambda *_args, **_kwargs: mismatch,
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=[],
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert execute_calls == 1
    assert verify_calls == 2
    assert result.proof is mismatch
    assert result.artifact.assessments[0].reason == "original fail-closed assessment"
    assert result.retry_count == 1
    assert any(payload.get("status") == "repair_failed" for _, payload, _ in runtime_events)


def test_terminal_witness_required_routes_one_focused_executor_repair_without_blocking(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_events: list[tuple[str, dict, str]] = []
    runtime = EvidenceCompilerRuntime(
        LlmClient(_settings(tmp_path)),
        progress_sink=lambda kind, payload, action: runtime_events.append(
            (kind, payload, action)
        ),
    )
    plan = _plan()
    prepared = _sources()
    record = prepared[0].record
    initial_sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={
                record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    initial_sandbox.read_source(record.source_id)
    initial_sandbox.submit_check(check_id="check.vendor", note="numeric proof only")
    execute_calls: list[dict] = []
    verify_calls: list[dict] = []

    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def fake_execute(**kwargs):
        execute_calls.append(kwargs)
        candidate = kwargs.get("sandbox")
        if candidate is None:
            return ExecutorSummary(completed_check_ids=["check.vendor"]), initial_sandbox
        claim = candidate.bind_claim(
            claim_id="claim.terminal.repair",
            subject="vendor:V-100",
            predicate="status",
            value="ACTIVE",
            source_id=record.source_id,
            quote="Vendor V-100 is ACTIVE.",
            locator="line 1",
            confidence="high",
        )["claim"]
        candidate.submit_check(
            check_id="check.vendor",
            claim_ids=[claim["id"]],
            note="terminal proof material added",
        )
        return ExecutorSummary(completed_check_ids=["check.vendor"]), candidate

    def fake_verify(**kwargs):
        verify_calls.append(kwargs)
        if kwargs.get("focus_check_ids"):
            claim_ids = [item.id for item in kwargs["sandbox"].evidence_ir.claims]
            return [
                CheckAssessment(
                    check_id="check.vendor",
                    status="SUPPORTED",
                    claim_ids=claim_ids,
                    source_ids=[record.source_id],
                    examined_source_ids=[record.source_id],
                    reason="focused terminal repair verified",
                )
            ]
        return [
            CheckAssessment(
                check_id="check.vendor",
                status="NOT_FOUND",
                missing_fact="a replayable boolean terminal",
                gap_code="WITNESS_MISSING",
            )
        ]

    terminal_gap = CompiledProof(
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_REQUIRED",
                node_id="check.vendor",
                message="a replayable boolean terminal is required",
            )
        ]
    )
    compiled = iter([terminal_gap, CompiledProof()])
    monkeypatch.setattr(runtime, "execute_plan", fake_execute)
    monkeypatch.setattr(runtime, "verify", fake_verify)
    monkeypatch.setattr(
        "app.compiler_runtime.runtime.compile_review_artifact",
        lambda *_args, **_kwargs: next(compiled),
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=prepared,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert len(execute_calls) == 2
    assert execute_calls[1]["focus_check_ids"] == ["check.vendor"]
    assert execute_calls[1]["hook_feedback"][0]["diagnostic_code"] == (
        "TERMINAL_WITNESS_REQUIRED"
    )
    assert len(verify_calls) == 2
    assert verify_calls[1]["focus_check_ids"] == ["check.vendor"]
    assert result.artifact.assessments[0].reason == "focused terminal repair verified"
    assert result.retry_count == 1
    assert len(result.artifact.evidence_ir.claims) == 1
    assert initial_sandbox.evidence_ir.claims == []
    assert any(
        payload.get("status") == "terminal_repair_started"
        for _, payload, _ in runtime_events
    )


def test_terminal_executor_repair_that_exposes_status_mismatch_gets_one_verifier_repair(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    plan = _plan()
    prepared = _sources()
    record = prepared[0].record
    initial_sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={
                record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    initial_sandbox.read_source(record.source_id)
    initial_sandbox.submit_check(check_id="check.vendor", note="numeric proof only")
    verify_calls: list[dict] = []

    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def fake_execute(**kwargs):
        candidate = kwargs.get("sandbox")
        if candidate is None:
            return ExecutorSummary(completed_check_ids=["check.vendor"]), initial_sandbox
        claim = candidate.bind_claim(
            claim_id="claim.terminal.repair",
            subject="vendor:V-100",
            predicate="status",
            value="ACTIVE",
            source_id=record.source_id,
            quote="Vendor V-100 is ACTIVE.",
            locator="line 1",
            confidence="high",
        )["claim"]
        candidate.submit_check(
            check_id="check.vendor",
            claim_ids=[claim["id"]],
            note="terminal proof material added",
        )
        return ExecutorSummary(completed_check_ids=["check.vendor"]), candidate

    def fake_verify(**kwargs):
        verify_calls.append(kwargs)
        diagnostic_code = (
            kwargs.get("repair_feedback") or [{}]
        )[0].get("diagnostic_code")
        reason = {
            None: "initial NOT_FOUND",
            "TERMINAL_WITNESS_REQUIRED": "terminal exists but polarity is wrong",
            "TERMINAL_WITNESS_STATUS_MISMATCH": "terminal polarity repaired",
        }[diagnostic_code]
        return [
            CheckAssessment(
                check_id="check.vendor",
                status="NOT_FOUND" if diagnostic_code is None else "SUPPORTED",
                reason=reason,
                missing_fact="a replayable boolean terminal" if diagnostic_code is None else "",
            )
        ]

    terminal_gap = CompiledProof(
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_REQUIRED",
                node_id="check.vendor",
                message="a replayable boolean terminal is required",
            )
        ]
    )
    mismatch = CompiledProof(
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_STATUS_MISMATCH",
                node_id="check.vendor",
                message="terminal result maps to the opposite strong status",
            )
        ]
    )
    compiled = iter([terminal_gap, mismatch, CompiledProof()])
    monkeypatch.setattr(runtime, "execute_plan", fake_execute)
    monkeypatch.setattr(runtime, "verify", fake_verify)
    monkeypatch.setattr(
        "app.compiler_runtime.runtime.compile_review_artifact",
        lambda *_args, **_kwargs: next(compiled),
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=prepared,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert len(verify_calls) == 3
    assert verify_calls[1]["repair_feedback"][0]["diagnostic_code"] == (
        "TERMINAL_WITNESS_REQUIRED"
    )
    assert verify_calls[2]["repair_feedback"][0]["diagnostic_code"] == (
        "TERMINAL_WITNESS_STATUS_MISMATCH"
    )
    assert result.proof.diagnostics == []
    assert result.artifact.assessments[0].reason == "terminal polarity repaired"
    assert result.retry_count == 2


@pytest.mark.parametrize("repair_mode", ["no_change", "error"])
def test_terminal_witness_repair_no_change_or_error_fails_closed_and_is_not_retried(
    tmp_path,
    monkeypatch,
    repair_mode,
) -> None:
    runtime_events: list[tuple[str, dict, str]] = []
    runtime = EvidenceCompilerRuntime(
        LlmClient(_settings(tmp_path)),
        progress_sink=lambda kind, payload, action: runtime_events.append(
            (kind, payload, action)
        ),
    )
    plan = _plan()
    sandbox = EvidenceSandbox(sources=[], allowed_check_ids=["check.vendor"])
    sandbox.submit_check(check_id="check.vendor", note="numeric proof only")
    execute_calls: list[dict] = []
    verify_calls = 0

    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def fake_execute(**kwargs):
        execute_calls.append(kwargs)
        if kwargs.get("sandbox") is None:
            return ExecutorSummary(completed_check_ids=["check.vendor"]), sandbox
        if repair_mode == "error":
            raise ModelBehaviorError("focused Executor repair remained malformed")
        return ExecutorSummary(completed_check_ids=["check.vendor"]), kwargs["sandbox"]

    def fake_verify(**_kwargs):
        nonlocal verify_calls
        verify_calls += 1
        return [
            CheckAssessment(
                check_id="check.vendor",
                status="NOT_FOUND",
                missing_fact="a replayable boolean terminal",
                gap_code="WITNESS_MISSING",
                reason="original fail-closed assessment",
            )
        ]

    terminal_gap = CompiledProof(
        obligations=[
            ProofObligation(
                id="obligation.check.vendor",
                requirement_id="vendor_identity",
                check_id="check.vendor",
                missing_fact="a replayable boolean terminal",
            )
        ],
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_REQUIRED",
                node_id="check.vendor",
                message="a replayable boolean terminal is required",
            )
        ],
    )
    compile_calls = 0

    def fake_compile(*_args, **_kwargs):
        nonlocal compile_calls
        compile_calls += 1
        return terminal_gap

    monkeypatch.setattr(runtime, "execute_plan", fake_execute)
    monkeypatch.setattr(runtime, "verify", fake_verify)
    monkeypatch.setattr(
        "app.compiler_runtime.runtime.compile_review_artifact",
        fake_compile,
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=[],
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    # Initial execution plus one focused terminal attempt. The blocking retry
    # must not make a third call for the same CHECK.
    assert len(execute_calls) == 2
    assert execute_calls[1]["focus_check_ids"] == ["check.vendor"]
    assert verify_calls == 1
    assert compile_calls == 1
    assert result.proof is terminal_gap
    assert result.artifact.assessments[0].reason == "original fail-closed assessment"
    assert result.retry_count == 1
    expected_status = (
        "terminal_repair_no_change" if repair_mode == "no_change" else "terminal_repair_failed"
    )
    assert any(
        payload.get("status") == expected_status for _, payload, _ in runtime_events
    )


def test_blocking_retry_that_exposes_terminal_gap_gets_one_focused_executor_repair(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = EvidenceCompilerRuntime(LlmClient(_settings(tmp_path)))
    plan = _plan()
    prepared = _sources()
    record = prepared[0].record
    initial_sandbox = EvidenceSandbox(
        sources=[record],
        allowed_check_ids=["check.vendor"],
        evidence_ir=EvidenceIR(
            source_ids=[record.source_id],
            source_fingerprints={
                record.source_id: hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    initial_sandbox.read_source(record.source_id)
    execute_calls: list[dict] = []
    verify_calls: list[dict] = []

    monkeypatch.setattr(runtime, "compile_task", lambda **_kwargs: plan)

    def fake_execute(**kwargs):
        execute_calls.append(kwargs)
        candidate = kwargs.get("sandbox")
        if candidate is None:
            return ExecutorSummary(completed_check_ids=[]), initial_sandbox
        call_number = len(execute_calls)
        claim = candidate.bind_claim(
            claim_id=f"claim.retry.{call_number}",
            subject="vendor:V-100",
            predicate=f"retry_fact_{call_number}",
            value=f"fact-{call_number}",
            source_id=record.source_id,
            quote="Vendor V-100 is ACTIVE.",
            locator="line 1",
            confidence="high",
        )["claim"]
        candidate.submit_check(
            check_id="check.vendor",
            claim_ids=[item.id for item in candidate.evidence_ir.claims],
            note=f"retry material {call_number}",
        )
        return ExecutorSummary(completed_check_ids=["check.vendor"]), candidate

    def fake_verify(**kwargs):
        verify_calls.append(kwargs)
        diagnostic_code = (
            kwargs.get("repair_feedback") or [{}]
        )[0].get("diagnostic_code")
        claim_ids = [item.id for item in kwargs["sandbox"].evidence_ir.claims]
        return [
            CheckAssessment(
                check_id="check.vendor",
                status="SUPPORTED" if claim_ids else "NOT_FOUND",
                claim_ids=claim_ids,
                source_ids=[record.source_id] if claim_ids else [],
                examined_source_ids=[record.source_id],
                missing_fact="" if claim_ids else "initial evidence gap",
                reason={
                    "TERMINAL_WITNESS_REQUIRED": "terminal exists but polarity is wrong",
                    "TERMINAL_WITNESS_STATUS_MISMATCH": "terminal polarity repaired",
                }.get(diagnostic_code, "full verification"),
            )
        ]

    blocking_gap = CompiledProof(
        obligations=[
            ProofObligation(
                id="obligation.check.vendor",
                requirement_id="vendor_identity",
                check_id="check.vendor",
                missing_fact="initial evidence gap",
            )
        ]
    )
    terminal_gap = CompiledProof(
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_REQUIRED",
                node_id="check.vendor",
                message="a replayable boolean terminal is required",
            )
        ]
    )
    mismatch = CompiledProof(
        diagnostics=[
            CompilationDiagnostic(
                code="TERMINAL_WITNESS_STATUS_MISMATCH",
                node_id="check.vendor",
                message="terminal result maps to the opposite strong status",
            )
        ]
    )
    compiled = iter([blocking_gap, terminal_gap, mismatch, CompiledProof()])
    monkeypatch.setattr(runtime, "execute_plan", fake_execute)
    monkeypatch.setattr(runtime, "verify", fake_verify)
    monkeypatch.setattr(
        "app.compiler_runtime.runtime.compile_review_artifact",
        lambda *_args, **_kwargs: next(compiled),
    )

    result = runtime.run(
        active_requirement_ids=["vendor_identity"],
        prepared_sources=prepared,
        policy_excerpt=policy_excerpt_for(["vendor_identity"]),
    )

    assert len(execute_calls) == 3
    assert execute_calls[1]["focus_check_ids"] == ["check.vendor"]
    assert "diagnostic_code" not in execute_calls[1]["hook_feedback"][0]
    assert execute_calls[2]["focus_check_ids"] == ["check.vendor"]
    assert execute_calls[2]["hook_feedback"][0]["diagnostic_code"] == (
        "TERMINAL_WITNESS_REQUIRED"
    )
    assert len(verify_calls) == 4
    assert "focus_check_ids" not in verify_calls[1]
    assert verify_calls[2]["focus_check_ids"] == ["check.vendor"]
    assert verify_calls[2]["repair_feedback"][0]["diagnostic_code"] == (
        "TERMINAL_WITNESS_REQUIRED"
    )
    assert verify_calls[3]["focus_check_ids"] == ["check.vendor"]
    assert verify_calls[3]["repair_feedback"][0]["diagnostic_code"] == (
        "TERMINAL_WITNESS_STATUS_MISMATCH"
    )
    assert result.artifact.assessments[0].reason == "terminal polarity repaired"
    assert len(result.artifact.evidence_ir.claims) == 2
    assert result.retry_count == 3


def test_completion_hook_stops_only_after_every_check_is_submitted() -> None:
    source = _sources()[0].record
    sandbox = EvidenceSandbox(
        sources=[source],
        allowed_check_ids=["check.one", "check.two"],
        evidence_ir=EvidenceIR(
            source_ids=[source.source_id],
            source_fingerprints={
                source.source_id: hashlib.sha256(source.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    hook = _completion_hook(sandbox, ["check.one", "check.two"])

    sandbox.submit_check(check_id="check.one", note="missing")
    assert hook(None, []).is_final_output is False
    sandbox.submit_check(check_id="check.two", note="missing")
    result = hook(None, [])

    assert result.is_final_output is True
    assert result.final_output.unresolved_check_ids == ["check.one", "check.two"]


def test_retry_completion_hook_requires_a_new_submission_for_the_focus_check() -> None:
    source = _sources()[0].record
    sandbox = EvidenceSandbox(
        sources=[source],
        allowed_check_ids=["check.one", "check.two"],
        evidence_ir=EvidenceIR(
            source_ids=[source.source_id],
            source_fingerprints={
                source.source_id: hashlib.sha256(source.content.encode("utf-8")).hexdigest()
            },
        ),
    )
    sandbox.submit_check(check_id="check.one", note="missing")
    sandbox.submit_check(check_id="check.two", note="complete")
    hook = _completion_hook(
        sandbox,
        ["check.one"],
        prior_submission_counts={"check.one": 1},
    )

    sandbox.read_source(source.source_id)
    assert hook(None, []).is_final_output is False
    sandbox.submit_check(check_id="check.one", note="still missing")

    result = hook(None, [])
    assert result.is_final_output is True
    assert result.final_output.unresolved_check_ids == ["check.one"]


def test_unconfigured_policy_hole_is_not_retried_by_evidence_executor() -> None:
    plan = ProofPlan(
        plan_id="policy-hole",
        objective="Keep policy administration outside the evidence sandbox.",
        active_requirement_ids=["no_active_duplicate"],
        policy_refs=["duplicate_search_window"],
        roots={"no_active_duplicate": "check.window"},
        nodes=[
            ProofNode(
                id="check.window",
                kind="CHECK",
                statement="The duplicate search window is configured.",
                requirement_refs=["no_active_duplicate"],
                policy_refs=["duplicate_search_window"],
            )
        ],
    )

    assert _retryable_checks(
        plan,
        ["check.window"],
        policy_excerpt_for(["no_active_duplicate"]),
    ) == []
    artifact = _artifact(
        plan=plan,
        evidence_ir=EvidenceIR(),
        assessments=[],
        submitted_claim_refs={"check.window": []},
        policy_excerpt=policy_excerpt_for(["no_active_duplicate"]),
        model="fixture",
    )
    assert artifact.unconfigured_policy_refs == ["duplicate_search_window"]


def test_persisted_source_fails_closed_when_content_does_not_match_fingerprint() -> None:
    with pytest.raises(ValueError, match="does not match its source_fingerprint"):
        prepare_sources(
            [
                {
                    "already_persisted": True,
                    "source_id": "source.invoice",
                    "source_content": "truncated source",
                    "source_fingerprint": hashlib.sha256(b"original source").hexdigest(),
                }
            ]
        )


def test_compiler_source_content_is_not_truncated_before_persistence() -> None:
    content = "source text " * 100
    compact = compact_case_patch_for_write(
        {
            "case_updates": {
                "add_evidence": [
                    {
                        "content": content,
                        "metadata": {"compiler_source_sha256": hashlib.sha256(content.encode()).hexdigest()},
                    }
                ]
            }
        }
    )

    assert compact["case_updates"]["add_evidence"][0]["content"] == content
