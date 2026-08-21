from __future__ import annotations

import pytest

from app.compiler_runtime import (
    CheckAssessment,
    Claim,
    EvidenceIR,
    ProofNode,
    ProofPlan,
    ReviewArtifact,
    compile_review_artifact,
)
from app.compiler_runtime.consumer import derive_consumer_packet
from app.compiler_runtime.signatures import proof_signature_hash_for
from app.context import _case_brief
from app.evals.oracle import complete_claim_consistency_errors
from app.guards import (
    CaseStateConsistencyError,
    enforce_case_state_consistency,
    enforce_report_proof_consistency,
)
from app.state.schemas import CaseState, EvidenceItem, Requirement


def _case_with_supported_runtime_proof() -> CaseState:
    plan = ProofPlan(
        plan_id="plan.invoice",
        objective="Establish whether the invoice source supports the requirement.",
        active_requirement_ids=["invoice"],
        roots={"invoice": "check.invoice"},
        nodes=[
            ProofNode(
                id="check.invoice",
                kind="CHECK",
                statement="An invoice source is present and grounded.",
                requirement_refs=["invoice"],
            )
        ],
    )
    evidence_ir = EvidenceIR(
        source_ids=["ev.invoice"],
        source_fingerprints={"ev.invoice": "sha256:invoice"},
        claims=[
            Claim(
                id="claim.invoice_number",
                subject="invoice:INV-1",
                predicate="invoice_number",
                value="INV-1",
                source_id="ev.invoice",
                quote="Invoice INV-1",
                locator="line 1",
                confidence="high",
            )
        ],
    )
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_ir.content_hash(),
        assessments=[
            CheckAssessment(
                check_id="check.invoice",
                status="SUPPORTED",
                claim_ids=["claim.invoice_number"],
                source_ids=["ev.invoice"],
                examined_source_ids=["ev.invoice"],
                reason="The source directly identifies the invoice.",
            )
        ],
        submitted_claim_refs={"check.invoice": ["claim.invoice_number"]},
        proof_signature_hash=proof_signature_hash_for(plan.active_requirement_ids),
        policy_hash="sha256:policy",
        compiler_version="test",
        model="fixture",
    )
    artifact = artifact.model_copy(update={"artifact_hash": artifact.content_hash()})
    return CaseState(
        case_id="case.compiler.consumer",
        requirements=[Requirement(id="invoice", label="Invoice", status="accepted")],
        evidence_items=[
            EvidenceItem(
                id="ev.invoice",
                type="invoice",
                source="attachment",
                credibility="high",
                content="Invoice INV-1",
            )
        ],
        review_artifact=artifact,
        compiled_proof=compile_review_artifact(artifact),
    )


def test_context_consumes_runtime_proof_without_legacy_fields() -> None:
    case_state = _case_with_supported_runtime_proof()

    brief = _case_brief(case_state)
    report_state = derive_consumer_packet(case_state).model_dump(mode="json")

    assert "invoice=SUPPORTED" in brief
    assert report_state["root_decisions"][0]["status"] == "SUPPORTED"
    assert report_state["reportability"] == "FULL"
    assert report_state["review_complete"] is True
    assert report_state["decision_ready"] is True
    assert "review_artifact" not in report_state
    assert "plan" not in report_state
    assert "assessments" not in report_state
    assert report_state["source_fingerprints"] == {"ev.invoice": "sha256:invoice"}
    assert report_state["claims"] == [
        {
            "id": "claim.invoice_number",
            "subject": "invoice:INV-1",
            "predicate": "invoice_number",
            "value": "INV-1",
            "source_id": "ev.invoice",
            "quote": "Invoice INV-1",
            "locator": "line 1",
            "confidence": "high",
            "currency": "",
            "unit": "",
        }
    ]


def test_complete_claim_guards_use_decision_root_and_grounded_claims() -> None:
    case_state = _case_with_supported_runtime_proof()

    assert enforce_case_state_consistency("status = ready_for_report", case_state) == "status = ready_for_report"
    assert complete_claim_consistency_errors(case_state) == []


def test_report_guard_requires_each_canonical_contradiction_and_rejects_no_conflict() -> None:
    case_state = _case_with_supported_runtime_proof()
    assessment = case_state.review_artifact.assessments[0].model_copy(
        update={"status": "CONTRADICTED"}
    )
    case_state.review_artifact = case_state.review_artifact.model_copy(
        update={"assessments": [assessment]}
    )
    case_state.review_artifact = case_state.review_artifact.model_copy(
        update={"artifact_hash": case_state.review_artifact.content_hash()}
    )
    case_state.compiled_proof = compile_review_artifact(case_state.review_artifact)

    with pytest.raises(CaseStateConsistencyError, match="no conflict"):
        enforce_report_proof_consistency("invoice 未发现冲突。", case_state)
    with pytest.raises(CaseStateConsistencyError, match="omits contradicted requirements"):
        enforce_report_proof_consistency("已发现一个证据冲突。", case_state)

    report = "Requirement invoice：证据明确反驳该项要求。"
    assert enforce_report_proof_consistency(report, case_state) == report
