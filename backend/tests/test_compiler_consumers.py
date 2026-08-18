from __future__ import annotations

from app.compiler_runtime import (
    CheckAssessment,
    Claim,
    EvidenceIR,
    ProofNode,
    ProofPlan,
    ReviewArtifact,
    compile_review_artifact,
)
from app.context import _case_brief, _report_case_state
from app.evals.oracle import complete_claim_consistency_errors
from app.guards import enforce_case_state_consistency
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
        policy_hash="sha256:policy",
        compiler_version="test",
        model="fixture",
    )
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
    report_state = _report_case_state(case_state)

    assert "invoice=SUPPORTED" in brief
    assert report_state["compiled_proof"]["decisions"][0]["status"] == "SUPPORTED"
    assert report_state["review_artifact"]["evidence_ir"]["claims"][0]["quote"] == "Invoice INV-1"
    assert report_state["evidence_items"] == [
        {
            "id": "ev.invoice",
            "type": "invoice",
            "source": "attachment",
            "credibility": "high",
            "source_fingerprint": "sha256:invoice",
        }
    ]


def test_complete_claim_guards_use_decision_root_and_grounded_claims() -> None:
    case_state = _case_with_supported_runtime_proof()

    assert enforce_case_state_consistency("status = ready_for_report", case_state) == "status = ready_for_report"
    assert complete_claim_consistency_errors(case_state) == []
