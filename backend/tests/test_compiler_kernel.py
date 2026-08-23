from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.compiler_runtime import (
    CheckAssessment,
    Claim,
    EvidenceIR,
    ProofNode,
    ProofPlan,
    ReviewArtifact,
    StrongStatusLink,
    compile_review_artifact,
)
from app.compiler_runtime.policy import proof_decision_ready
from app.compiler_runtime.proof_terms import (
    CalculationRequest,
    ProofTermRef,
    SemanticBindingProposal,
    compute_witness,
)
from app.compiler_runtime.signatures import proof_signature_hash_for


def _plan(
    *,
    root_kind: str = "ALL",
    reverse: bool = False,
    policy_ref: str | None = None,
) -> ProofPlan:
    nodes = [
        ProofNode(
            id="check.invoice_total",
            kind="CHECK",
            statement="The invoice total is present and grounded in the invoice.",
            requirement_refs=["req.invoice_review"],
            policy_refs=[policy_ref] if policy_ref else [],
        ),
        ProofNode(
            id="check.currency",
            kind="CHECK",
            statement="The invoice currency is present and grounded in the invoice.",
            requirement_refs=["req.invoice_review"],
        ),
        ProofNode(
            id="root.invoice_review",
            kind=root_kind,
            depends_on=["check.invoice_total", "check.currency"],
        ),
    ]
    if reverse:
        nodes.reverse()
    return ProofPlan(
        plan_id="plan.invoice",
        objective="Establish whether the invoice has the required grounded fields.",
        active_requirement_ids=["req.invoice_review"],
        policy_refs=[policy_ref] if policy_ref else [],
        roots={"req.invoice_review": "root.invoice_review"},
        nodes=nodes,
    )


def _ir(*, reverse: bool = False, total_confidence: str = "high") -> EvidenceIR:
    claims = [
        Claim(
            id="claim.total",
            subject="invoice:INV-1",
            predicate="total",
            value="10500.00",
            source_id="source.invoice",
            quote="Total GBP 10,500.00",
            locator="page 1, totals box",
            confidence=total_confidence,
        ),
        Claim(
            id="claim.currency",
            subject="invoice:INV-1",
            predicate="currency",
            value="GBP",
            source_id="source.invoice",
            quote="GBP",
            locator="page 1, totals box",
            confidence="high",
        ),
    ]
    if reverse:
        claims.reverse()
    return EvidenceIR(
        source_ids=["source.invoice"],
        source_fingerprints={"source.invoice": "sha256:invoice-v1"},
        claims=claims,
    )


def _assessment(check_id: str, status: str) -> CheckAssessment:
    claim_id = "claim.total" if check_id.endswith("total") else "claim.currency"
    return CheckAssessment(
        check_id=check_id,
        status=status,
        claim_ids=[claim_id] if status != "NOT_FOUND" else [],
        source_ids=["source.invoice"] if status != "NOT_FOUND" else [],
        examined_source_ids=["source.invoice"],
        reason=f"verifier returned {status}",
        missing_fact="a grounded value is still required" if status == "NOT_FOUND" else "",
    )


def _artifact(
    assessments: list[CheckAssessment],
    *,
    plan: ProofPlan | None = None,
    evidence_ir: EvidenceIR | None = None,
    plan_hash: str | None = None,
    evidence_snapshot_hash: str | None = None,
    unconfigured_policy_refs: list[str] | None = None,
    submitted_claim_refs: dict[str, list[str]] | None = None,
) -> ReviewArtifact:
    plan = plan or _plan()
    evidence_ir = evidence_ir or _ir()
    if submitted_claim_refs is None:
        submitted_claim_refs = {
            node.id: []
            for node in plan.nodes
            if node.kind == "CHECK"
        }
        for assessment in assessments:
            submitted_claim_refs.setdefault(assessment.check_id, [])
            submitted_claim_refs[assessment.check_id].extend(assessment.claim_ids)
    unconfigured_policy_refs = unconfigured_policy_refs or []
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan_hash or plan.content_hash(),
        proof_signature_hash=proof_signature_hash_for(plan.active_requirement_ids),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_snapshot_hash or evidence_ir.content_hash(),
        assessments=assessments,
        submitted_claim_refs=submitted_claim_refs,
        policy_hash="policy-sha256",
        resolved_policy_terms={
            ref_id: "1"
            for ref_id in plan.policy_refs
            if ref_id not in unconfigured_policy_refs
        },
        unconfigured_policy_refs=unconfigured_policy_refs,
        compiler_version="test-1",
        model="fixture",
        prompt_versions={"task_compiler": "1", "verifier": "1"},
    )
    return artifact.model_copy(update={"artifact_hash": artifact.content_hash()})


_TYPED_FACETS = (
    "line_extensions",
    "subtotal_aggregation",
    "stated_components",
    "final_total",
)
_TYPED_POLICY = "invoice_calculation_rounding_tolerance"


def _typed_artifact(*, status: str = "SUPPORTED") -> ReviewArtifact:
    check_id = "check.typed"
    plan = ProofPlan(
        plan_id="plan.typed",
        objective="Compile the declared typed proof facets.",
        active_requirement_ids=["invoice_calculation_valid"],
        policy_refs=[_TYPED_POLICY],
        roots={"invoice_calculation_valid": check_id},
        nodes=[
            ProofNode(
                id=check_id,
                kind="CHECK",
                statement="The declared proof facets are established.",
                requirement_refs=["invoice_calculation_valid"],
                policy_refs=[_TYPED_POLICY],
                facet_refs=list(_TYPED_FACETS),
                semantic_role_refs=[
                    "COMPONENT_OBSERVATION",
                    "COMPONENT_APPLICABILITY",
                    "COMPONENT_RECONCILIATION",
                ],
            )
        ],
    )
    claim = Claim(
        id="claim.amount",
        subject="record:1",
        predicate="observed_amount",
        value="10.00",
        source_id="source.record",
        quote="Observed amount EUR 10.00",
        locator="page 1",
        confidence="high",
        attributes={"currency": "EUR"},
    )
    evidence_ir = EvidenceIR(
        source_ids=[claim.source_id],
        source_fingerprints={claim.source_id: "sha256:record-v1"},
        claims=[claim],
    )
    policy_hash = "sha256:typed-policy"
    resolved_policy_terms = {
        _TYPED_POLICY: {"value": "0.01", "currency": "EUR", "unit": ""}
    }
    intermediate_witnesses = [
        compute_witness(
            CalculationRequest(
                id=f"witness.{facet_ref}",
                check_id=check_id,
                facet_ref=facet_ref,
                operation="SUM",
                operands=[ProofTermRef(kind="CLAIM", ref_id=claim.id)],
            ),
            claims={claim.id: claim},
            witnesses={},
            policy_values=resolved_policy_terms,
            evidence_snapshot_hash=evidence_ir.source_snapshot_hash(),
            policy_snapshot_hash=policy_hash,
        )
        for facet_ref in _TYPED_FACETS
    ]
    terminal_witnesses = [
        compute_witness(
            CalculationRequest(
                id=f"witness.{facet_ref}.terminal",
                check_id=check_id,
                facet_ref=facet_ref,
                operation="GREATER_THAN",
                operands=[
                    ProofTermRef(kind="WITNESS", ref_id=f"witness.{facet_ref}"),
                    ProofTermRef(kind="POLICY", ref_id=_TYPED_POLICY),
                ],
            ),
            claims={claim.id: claim},
            witnesses={item.id: item for item in intermediate_witnesses},
            policy_values=resolved_policy_terms,
            evidence_snapshot_hash=evidence_ir.source_snapshot_hash(),
            policy_snapshot_hash=policy_hash,
        )
        for facet_ref in _TYPED_FACETS
    ]
    witnesses = [*intermediate_witnesses, *terminal_witnesses]
    binding = SemanticBindingProposal(
        id="binding.stated_components",
        check_id=check_id,
        facet_ref="stated_components",
        relation="observed terms are the stated components",
        term_refs=[ProofTermRef(kind="CLAIM", ref_id=claim.id)],
        reason="The proposal relates only admitted source observations.",
    )
    assessment = CheckAssessment(
        check_id=check_id,
        claim_ids=[claim.id],
        accepted_binding_ids=[binding.id],
        accepted_witness_ids=[item.id for item in witnesses],
        strong_status_links=[
            StrongStatusLink(
                witness_id=item.id,
                true_status=status,
            )
            for item in terminal_witnesses
        ],
        source_ids=[claim.source_id],
        examined_source_ids=[claim.source_id],
        reason=f"verifier returned {status}",
        status=status,
    )
    artifact = ReviewArtifact(
        plan=plan,
        plan_hash=plan.content_hash(),
        proof_signature_hash=proof_signature_hash_for(plan.active_requirement_ids),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_ir.content_hash(),
        assessments=[assessment],
        binding_proposals=[binding],
        calculation_witnesses=witnesses,
        submitted_claim_refs={check_id: [claim.id]},
        submitted_binding_refs={check_id: [binding.id]},
        submitted_witness_refs={check_id: [item.id for item in witnesses]},
        policy_hash=policy_hash,
        resolved_policy_terms=resolved_policy_terms,
        compiler_version="test-typed-1",
        model="fixture",
    )
    return _reseal(artifact)


def _reseal(artifact: ReviewArtifact, **updates: object) -> ReviewArtifact:
    changed = artifact.model_copy(update=updates, deep=True)
    return changed.model_copy(update={"artifact_hash": changed.content_hash()})


def test_plan_rejects_duplicate_node_ids() -> None:
    check = ProofNode(
        id="check.same",
        kind="CHECK",
        statement="A fact is grounded.",
        requirement_refs=["req.one"],
    )
    with pytest.raises(ValidationError, match="node ids must be unique"):
        ProofPlan(
            plan_id="duplicate",
            objective="Reject an ambiguous graph.",
            active_requirement_ids=["req.one"],
            roots={"req.one": "check.same"},
            nodes=[check, check],
        )


def test_plan_rejects_missing_root_dependency_and_cycle() -> None:
    with pytest.raises(ValidationError, match="roots must exactly cover"):
        ProofPlan(
            plan_id="missing-root",
            objective="Reject incomplete root coverage.",
            active_requirement_ids=["req.one"],
            roots={},
            nodes=[
                ProofNode(
                    id="check.one",
                    kind="CHECK",
                    statement="One fact exists.",
                    requirement_refs=["req.one"],
                )
            ],
        )

    with pytest.raises(ValidationError, match="unknown dependencies"):
        ProofPlan(
            plan_id="missing-dependency",
            objective="Reject dangling references.",
            active_requirement_ids=["req.one"],
            roots={"req.one": "root"},
            nodes=[ProofNode(id="root", kind="ALL", depends_on=["missing"])],
        )

    with pytest.raises(ValidationError, match="acyclic"):
        ProofPlan(
            plan_id="cycle",
            objective="Reject a cyclic graph.",
            active_requirement_ids=["req.one"],
            roots={"req.one": "all.one"},
            nodes=[
                ProofNode(id="all.one", kind="ALL", depends_on=["all.two"]),
                ProofNode(id="all.two", kind="ANY", depends_on=["all.one"]),
            ],
        )


def test_plan_requires_requirement_and_policy_coverage() -> None:
    with pytest.raises(ValidationError, match="req.two.*not covered"):
        ProofPlan(
            plan_id="requirement-coverage",
            objective="Cover every active requirement.",
            active_requirement_ids=["req.one", "req.two"],
            roots={"req.one": "check.one", "req.two": "check.one"},
            nodes=[
                ProofNode(
                    id="check.one",
                    kind="CHECK",
                    statement="One fact exists.",
                    requirement_refs=["req.one"],
                )
            ],
        )


def test_plan_rebuilds_missing_redundant_policy_index_from_checks() -> None:
    plan = ProofPlan(
        plan_id="policy-index",
        objective="Normalize the redundant top-level policy index.",
        active_requirement_ids=["req.one"],
        roots={"req.one": "check.one"},
        nodes=[
            ProofNode(
                id="check.one",
                kind="CHECK",
                statement="The configured policy value is available.",
                requirement_refs=["req.one"],
                policy_refs=["policy.one"],
            )
        ],
    )

    assert plan.policy_refs == ["policy.one"]

    with pytest.raises(ValidationError, match="policy.missing"):
        ProofPlan(
            plan_id="policy-coverage",
            objective="Cover every applicable policy reference.",
            active_requirement_ids=["req.one"],
            policy_refs=["policy.missing"],
            roots={"req.one": "check.one"},
            nodes=[
                ProofNode(
                    id="check.one",
                    kind="CHECK",
                    statement="One fact exists.",
                    requirement_refs=["req.one"],
                )
            ],
        )


def test_plan_and_ir_hashes_ignore_declaration_order() -> None:
    assert _plan().content_hash() == _plan(reverse=True).content_hash()
    assert _ir().content_hash() == _ir(reverse=True).content_hash()
    assert _ir().source_snapshot_hash() == _ir(reverse=True).source_snapshot_hash()


def test_ir_hash_tracks_source_content_fingerprints() -> None:
    changed = _ir().model_copy(
        update={"source_fingerprints": {"source.invoice": "sha256:invoice-v2"}}
    )

    assert _ir().content_hash() != changed.content_hash()
    assert _ir().source_snapshot_hash() != changed.source_snapshot_hash()


def test_source_snapshot_hash_has_a_fixed_domain_and_schema() -> None:
    payload = {
        "kind": "compiler_runtime.evidence_source_snapshot",
        "schema_version": "1",
        "source_ids": ["source.invoice"],
        "source_fingerprints": {"source.invoice": "sha256:invoice-v1"},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )

    assert _ir().source_snapshot_hash() == hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_ir_rejects_partial_source_fingerprint_coverage() -> None:
    with pytest.raises(ValidationError, match="must cover every source"):
        EvidenceIR(
            source_ids=["source.one", "source.two"],
            source_fingerprints={"source.one": "sha256:one"},
        )


def test_kernel_rejects_nonempty_sources_without_complete_fingerprints() -> None:
    evidence_ir = _ir().model_copy(update={"source_fingerprints": {}}, deep=True)
    artifact = _artifact(
        [
            _assessment("check.invoice_total", "SUPPORTED"),
            _assessment("check.currency", "SUPPORTED"),
        ],
        evidence_ir=evidence_ir,
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["INCOMPLETE_SOURCE_FINGERPRINTS"]


@pytest.mark.parametrize(
    ("root_kind", "statuses", "expected"),
    [
        ("ALL", ["SUPPORTED", "SUPPORTED"], "SUPPORTED"),
        ("ALL", ["SUPPORTED", "CONTRADICTED"], "CONTRADICTED"),
        ("ALL", ["SUPPORTED", "NOT_FOUND"], "NOT_FOUND"),
        ("ANY", ["CONTRADICTED", "SUPPORTED"], "SUPPORTED"),
        ("ANY", ["CONTRADICTED", "CONTRADICTED"], "CONTRADICTED"),
        ("ANY", ["CONTRADICTED", "NOT_FOUND"], "NOT_FOUND"),
    ],
)
def test_kernel_uses_pure_three_value_aggregation(
    root_kind: str,
    statuses: list[str],
    expected: str,
) -> None:
    plan = _plan(root_kind=root_kind)
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", statuses[0]),
                _assessment("check.currency", statuses[1]),
            ],
            plan=plan,
        )
    )

    assert proof.decision_for("req.invoice_review").status == expected
    assert bool(proof.obligations) is (expected == "NOT_FOUND")


def test_proof_node_rejects_removed_not_kind() -> None:
    with pytest.raises(ValidationError, match="CHECK|ALL|ANY"):
        ProofNode(id="root.not", kind="NOT", depends_on=["check.fact"])


def test_strong_assessment_with_invalid_refs_fails_closed_once() -> None:
    assessment = CheckAssessment(
        check_id="check.invoice_total",
        status="SUPPORTED",
        claim_ids=["claim.unknown"],
        source_ids=["source.invoice"],
        examined_source_ids=["source.invoice"],
        reason="unsupported strong conclusion",
    )
    proof = compile_review_artifact(
        _artifact([assessment, _assessment("check.currency", "SUPPORTED")])
    )

    decision = proof.decision_for("req.invoice_review")
    assert decision.status == "NOT_FOUND"
    assert decision.unresolved_check_ids == ["check.invoice_total"]
    assert [item.code for item in proof.diagnostics] == ["INVALID_CLAIM_REFERENCE"]
    assert [item.check_id for item in proof.obligations] == ["check.invoice_total"]


def test_explicit_status_reason_conflict_fails_closed_at_kernel_boundary() -> None:
    assessment = _assessment("check.invoice_total", "SUPPORTED").model_copy(
        update={
            "reason": (
                "The recomputed total differs from the printed total beyond tolerance, "
                "so Final classification: CONTRADICTED."
            )
        }
    )

    proof = compile_review_artifact(
        _artifact([assessment, _assessment("check.currency", "SUPPORTED")])
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [
        "ASSESSMENT_STATUS_REASON_CONFLICT"
    ]


def test_counterfactual_status_does_not_conflict_with_final_classification() -> None:
    assessment = _assessment("check.invoice_total", "SUPPORTED").model_copy(
        update={
            "reason": (
                "If the terminal witness were true, the CHECK is CONTRADICTED; "
                "its false result maps to SUPPORTED. Final classification: SUPPORTED"
            )
        }
    )

    proof = compile_review_artifact(
        _artifact([assessment, _assessment("check.currency", "SUPPORTED")])
    )

    assert proof.decision_for("req.invoice_review").status == "SUPPORTED"
    assert proof.diagnostics == []


def test_negated_status_word_is_not_treated_as_an_explicit_final_status() -> None:
    assessment = _assessment("check.invoice_total", "SUPPORTED").model_copy(
        update={"reason": "The evidence is not CONTRADICTED; it directly supports the check."}
    )

    proof = compile_review_artifact(
        _artifact([assessment, _assessment("check.currency", "SUPPORTED")])
    )

    assert proof.decision_for("req.invoice_review").status == "SUPPORTED"
    assert proof.diagnostics == []


def test_unconfigured_policy_forces_its_check_to_not_found() -> None:
    plan = _plan(policy_ref="policy.invoice_fields")
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "SUPPORTED"),
                _assessment("check.currency", "SUPPORTED"),
            ],
            plan=plan,
            unconfigured_policy_refs=["policy.invoice_fields"],
        )
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["POLICY_NOT_CONFIGURED"]
    assert [item.check_id for item in proof.obligations] == ["check.invoice_total"]
    result = next(item for item in proof.node_results if item.node_id == "check.invoice_total")
    assert result.claim_ids == ["claim.total"]
    assert result.source_ids == ["source.invoice"]


def test_optional_not_found_obligation_is_non_blocking() -> None:
    artifact = _artifact(
        [
            _assessment("check.invoice_total", "NOT_FOUND"),
            _assessment("check.currency", "SUPPORTED"),
        ]
    )

    proof = compile_review_artifact(
        artifact,
        requirement_requiredness={"req.invoice_review": False},
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert proof.decision_for("req.invoice_review").stop_reason == "optional evidence remains unresolved"
    assert proof.obligations
    assert all(item.blocking is False for item in proof.obligations)
    assert proof_decision_ready(proof)


def test_template_without_baseline_is_forced_not_found_even_if_verifier_supports() -> None:
    baseline_ref = "invoice_template_baseline_ref"
    plan = ProofPlan(
        plan_id="plan.invoice.template",
        objective="Establish the template-match Requirement without reversing its polarity.",
        active_requirement_ids=["template_match"],
        policy_refs=[baseline_ref],
        roots={"template_match": "check.template_match"},
        nodes=[
            ProofNode(
                id="check.template_match",
                kind="CHECK",
                statement="The invoice conforms to an admitted expected template baseline.",
                requirement_refs=["template_match"],
                policy_refs=[baseline_ref],
                facet_refs=["baseline_comparison"],
            )
        ],
    )
    evidence_ir = EvidenceIR(
        source_ids=["source.invoice"],
        source_fingerprints={"source.invoice": "sha256:invoice-v1"},
        claims=[
            Claim(
                id="claim.looks_like_invoice",
                subject="invoice:INV-1",
                predicate="looks_like_invoice",
                value=True,
                source_id="source.invoice",
                quote="Invoice INV-1",
                locator="page 1, heading",
                confidence="high",
            )
        ],
    )
    assessment = CheckAssessment(
        check_id="check.template_match",
        status="SUPPORTED",
        claim_ids=["claim.looks_like_invoice"],
        source_ids=["source.invoice"],
        examined_source_ids=["source.invoice"],
        reason="The document looks like an invoice. Final classification: SUPPORTED",
    )
    proof = compile_review_artifact(
        _artifact(
            [assessment],
            plan=plan,
            evidence_ir=evidence_ir,
            submitted_claim_refs={
                "check.template_match": ["claim.looks_like_invoice"]
            },
            unconfigured_policy_refs=[baseline_ref],
        ),
        requirement_requiredness={"template_match": False},
    )

    decision = proof.decision_for("template_match")
    assert decision.status == "NOT_FOUND"
    assert decision.root_node_id == "check.template_match"
    assert [item.code for item in proof.diagnostics] == ["POLICY_NOT_CONFIGURED"]
    assert proof.node_results[0].gap_code == "POLICY_UNCONFIGURED"
    assert proof.obligations[0].blocking is False
    assert proof_decision_ready(proof)


def _configured_template_binding_artifact(
    *,
    consume_required_policy: bool,
) -> ReviewArtifact:
    baseline_ref = "invoice_template_baseline_ref"
    check_id = "check.template_match"
    plan = ProofPlan(
        plan_id="plan.invoice.template.configured",
        objective="Compare the invoice with an admitted expected template baseline.",
        active_requirement_ids=["template_match"],
        policy_refs=[baseline_ref],
        roots={"template_match": check_id},
        nodes=[
            ProofNode(
                id=check_id,
                kind="CHECK",
                statement="The invoice conforms to the admitted expected template baseline.",
                requirement_refs=["template_match"],
                policy_refs=[baseline_ref],
                facet_refs=["baseline_comparison"],
            )
        ],
    )
    invoice_claim = Claim(
        id="claim.looks_like_invoice",
        subject="invoice:INV-1",
        predicate="looks_like_invoice",
        value=True,
        source_id="source.invoice",
        quote="Invoice INV-1",
        locator="page 1, heading",
        confidence="high",
    )
    baseline_claim = Claim(
        id="claim.expected_template",
        subject="template:baseline-v1",
        predicate="expected_layout",
        value="baseline-v1",
        source_id="source.baseline",
        quote="Expected invoice template baseline v1",
        locator="baseline record 1",
        confidence="high",
    )
    evidence_ir = EvidenceIR(
        source_ids=[invoice_claim.source_id, baseline_claim.source_id],
        source_fingerprints={
            invoice_claim.source_id: "sha256:invoice-v1",
            baseline_claim.source_id: "sha256:baseline-v1",
        },
        claims=[invoice_claim, baseline_claim],
    )
    term_refs = [ProofTermRef(kind="CLAIM", ref_id=invoice_claim.id)]
    if consume_required_policy:
        term_refs.extend(
            [
                ProofTermRef(kind="CLAIM", ref_id=baseline_claim.id),
                ProofTermRef(kind="POLICY", ref_id=baseline_ref),
            ]
        )
    binding = SemanticBindingProposal(
        id="binding.template_baseline",
        check_id=check_id,
        facet_ref="baseline_comparison",
        relation="the current invoice is compared with the selected baseline",
        term_refs=term_refs,
        reason="The proposed comparison names the proof terms it actually consumes.",
    )
    bound_source_ids = (
        [invoice_claim.source_id, baseline_claim.source_id]
        if consume_required_policy
        else [invoice_claim.source_id]
    )
    assessment = CheckAssessment(
        check_id=check_id,
        accepted_binding_ids=[binding.id],
        source_ids=bound_source_ids,
        examined_source_ids=[invoice_claim.source_id, baseline_claim.source_id],
        reason="The accepted Binding establishes the template comparison.",
        status="SUPPORTED",
    )
    artifact = _artifact(
        [assessment],
        plan=plan,
        evidence_ir=evidence_ir,
        submitted_claim_refs={check_id: [invoice_claim.id, baseline_claim.id]},
    )
    return _reseal(
        artifact,
        binding_proposals=[binding],
        submitted_binding_refs={check_id: [binding.id]},
    )


def test_configured_policy_declaration_without_consumption_fails_closed() -> None:
    proof = compile_review_artifact(
        _configured_template_binding_artifact(consume_required_policy=False)
    )

    assert proof.decision_for("template_match").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [
        "REQUIRED_POLICY_TERM_NOT_CONSUMED"
    ]
    result = proof.node_results[0]
    assert result.claim_ids == ["claim.looks_like_invoice"]
    assert result.binding_ids == ["binding.template_baseline"]
    assert result.source_ids == ["source.invoice"]


def test_binding_that_consumes_policy_and_baseline_claim_can_be_strong() -> None:
    proof = compile_review_artifact(
        _configured_template_binding_artifact(consume_required_policy=True)
    )

    assert proof.decision_for("template_match").status == "SUPPORTED"
    assert proof.diagnostics == []
    result = proof.node_results[0]
    assert result.claim_ids == ["claim.expected_template", "claim.looks_like_invoice"]
    assert result.binding_ids == ["binding.template_baseline"]
    assert result.source_ids == ["source.baseline", "source.invoice"]


def test_direct_contradiction_is_reportable() -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "CONTRADICTED"),
                _assessment("check.currency", "SUPPORTED"),
            ]
        )
    )

    assert proof.decision_for("req.invoice_review").status == "CONTRADICTED"
    assert proof_decision_ready(proof)


@pytest.mark.parametrize(
    ("confidence", "expected_status", "expected_diagnostic"),
    [
        ("low", "NOT_FOUND", "LOW_CONFIDENCE_CLAIM"),
        ("medium", "SUPPORTED", None),
        ("high", "SUPPORTED", None),
    ],
)
def test_strong_assessment_rejects_only_low_confidence_claims(
    confidence: str,
    expected_status: str,
    expected_diagnostic: str | None,
) -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "SUPPORTED"),
                _assessment("check.currency", "SUPPORTED"),
            ],
            evidence_ir=_ir(total_confidence=confidence),
        )
    )

    assert proof.decision_for("req.invoice_review").status == expected_status
    diagnostics = [item.code for item in proof.diagnostics]
    assert (expected_diagnostic in diagnostics) if expected_diagnostic else not diagnostics


def test_not_found_with_dangling_refs_is_diagnosed_and_remains_not_found() -> None:
    assessment = CheckAssessment(
        check_id="check.invoice_total",
        status="NOT_FOUND",
        claim_ids=["claim.unknown"],
        source_ids=["source.invoice"],
        examined_source_ids=["source.invoice"],
        missing_fact="the invoice total",
    )
    proof = compile_review_artifact(
        _artifact([assessment, _assessment("check.currency", "SUPPORTED")])
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["INVALID_CLAIM_REFERENCE"]
    assert [item.check_id for item in proof.obligations] == ["check.invoice_total"]


def test_missing_assessment_becomes_a_traceable_obligation() -> None:
    proof = compile_review_artifact(
        _artifact([_assessment("check.invoice_total", "SUPPORTED")])
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["MISSING_ASSESSMENT"]
    assert proof.obligations[0].missing_fact == _plan().nodes[1].statement


def test_unknown_leaf_is_not_blocking_after_decisive_contradiction() -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "CONTRADICTED"),
                _assessment("check.currency", "NOT_FOUND"),
            ]
        )
    )

    decision = proof.decision_for("req.invoice_review")
    assert decision.status == "CONTRADICTED"
    assert decision.unresolved_check_ids == ["check.currency"]
    assert decision.obligation_ids == []
    assert proof.obligations == []


@pytest.mark.parametrize(
    ("overrides", "diagnostic"),
    [
        ({"plan_hash": "stale-plan"}, "STALE_PLAN"),
        ({"evidence_snapshot_hash": "stale-evidence"}, "STALE_EVIDENCE_SNAPSHOT"),
    ],
)
def test_stale_artifact_hashes_fail_closed(overrides: dict[str, str], diagnostic: str) -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "SUPPORTED"),
                _assessment("check.currency", "SUPPORTED"),
            ],
            **overrides,
        )
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert diagnostic in [item.code for item in proof.diagnostics]
    assert len(proof.obligations) == 2


def test_assessment_must_cite_the_sources_of_its_claims() -> None:
    evidence_ir = EvidenceIR(
        source_ids=["source.invoice", "source.other"],
        source_fingerprints={
            "source.invoice": "sha256:invoice-v1",
            "source.other": "sha256:other-v1",
        },
        claims=_ir().claims,
    )
    assessment = CheckAssessment(
        check_id="check.invoice_total",
        status="SUPPORTED",
        claim_ids=["claim.total"],
        source_ids=["source.other"],
        examined_source_ids=["source.invoice", "source.other"],
    )
    currency = _assessment("check.currency", "SUPPORTED").model_copy(
        update={"examined_source_ids": ["source.invoice", "source.other"]}
    )
    proof = compile_review_artifact(
        _artifact(
            [assessment, currency],
            evidence_ir=evidence_ir,
        )
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["CLAIM_SOURCE_NOT_CITED"]


def test_strong_assessment_cannot_add_sources_not_used_by_its_claims() -> None:
    evidence_ir = EvidenceIR(
        source_ids=["source.invoice", "source.other"],
        source_fingerprints={
            "source.invoice": "sha256:invoice-v1",
            "source.other": "sha256:other-v1",
        },
        claims=_ir().claims,
    )
    assessment = _assessment("check.invoice_total", "SUPPORTED").model_copy(
        update={
            "source_ids": ["source.invoice", "source.other"],
            "examined_source_ids": ["source.invoice", "source.other"],
        }
    )
    currency = _assessment("check.currency", "SUPPORTED").model_copy(
        update={"examined_source_ids": ["source.invoice", "source.other"]}
    )

    proof = compile_review_artifact(
        _artifact([assessment, currency], evidence_ir=evidence_ir)
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["UNSUPPORTED_SOURCE_REFERENCE"]


def test_not_found_may_record_examined_sources_beyond_its_partial_claims() -> None:
    evidence_ir = EvidenceIR(
        source_ids=["source.invoice", "source.other"],
        source_fingerprints={
            "source.invoice": "sha256:invoice-v1",
            "source.other": "sha256:other-v1",
        },
        claims=_ir().claims,
    )
    assessment = CheckAssessment(
        check_id="check.invoice_total",
        status="NOT_FOUND",
        claim_ids=["claim.total"],
        source_ids=["source.invoice", "source.other"],
        examined_source_ids=["source.invoice", "source.other"],
        missing_fact="a comparable baseline",
    )
    currency = _assessment("check.currency", "SUPPORTED").model_copy(
        update={"examined_source_ids": ["source.invoice", "source.other"]}
    )

    proof = compile_review_artifact(
        _artifact([assessment, currency], evidence_ir=evidence_ir)
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert proof.diagnostics == []


def test_strong_assessment_requires_exact_source_coverage() -> None:
    incomplete = _assessment("check.invoice_total", "SUPPORTED").model_copy(
        update={"examined_source_ids": []}
    )

    proof = compile_review_artifact(
        _artifact([incomplete, _assessment("check.currency", "SUPPORTED")])
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["SOURCE_COVERAGE_INCOMPLETE"]
    assert [item.check_id for item in proof.obligations] == ["check.invoice_total"]


def test_assessment_cannot_borrow_claim_submitted_for_another_check() -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "SUPPORTED"),
                _assessment("check.currency", "SUPPORTED"),
            ],
            submitted_claim_refs={
                "check.invoice_total": [],
                "check.currency": ["claim.currency"],
            },
        )
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["UNSUBMITTED_CLAIM_REFERENCE"]


def test_check_without_executor_submission_fails_closed() -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "SUPPORTED"),
                _assessment("check.currency", "SUPPORTED"),
            ],
            submitted_claim_refs={"check.invoice_total": ["claim.total"]},
        )
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["MISSING_CHECK_SUBMISSION"]
    assert [item.check_id for item in proof.obligations] == ["check.currency"]


def _replace_typed_assessment(
    artifact: ReviewArtifact,
    **updates: object,
) -> ReviewArtifact:
    assessment = artifact.assessments[0].model_copy(update=updates, deep=True)
    return _reseal(artifact, assessments=[assessment])


def test_typed_supported_closes_every_declared_facet_and_projects_terms() -> None:
    artifact = _typed_artifact()

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "SUPPORTED"
    assert proof.diagnostics == []
    result = proof.node_results[0]
    assert result.claim_ids == ["claim.amount"]
    assert result.binding_ids == ["binding.stated_components"]
    assert result.witness_ids == sorted(item.id for item in artifact.calculation_witnesses)
    assert result.source_ids == ["source.record"]


def test_late_claim_does_not_stale_an_existing_witness() -> None:
    artifact = _typed_artifact()
    original_ir = artifact.evidence_ir
    late_claim = Claim(
        id="claim.late",
        subject="record:1",
        predicate="observed_status",
        value="open",
        source_id="source.record",
        quote="Status open",
        locator="page 1",
        confidence="high",
    )
    changed_ir = original_ir.model_copy(
        update={"claims": [*original_ir.claims, late_claim]},
        deep=True,
    )
    artifact = _reseal(
        artifact,
        evidence_ir=changed_ir,
        evidence_snapshot_hash=changed_ir.content_hash(),
    )

    proof = compile_review_artifact(artifact)

    assert original_ir.content_hash() != changed_ir.content_hash()
    assert original_ir.source_snapshot_hash() == changed_ir.source_snapshot_hash()
    assert proof.decision_for("invoice_calculation_valid").status == "SUPPORTED"
    assert proof.diagnostics == []


def test_changed_source_fingerprint_stales_existing_witness() -> None:
    artifact = _typed_artifact()
    changed_ir = artifact.evidence_ir.model_copy(
        update={"source_fingerprints": {"source.record": "sha256:record-v2"}},
        deep=True,
    )
    artifact = _reseal(
        artifact,
        evidence_ir=changed_ir,
        evidence_snapshot_hash=changed_ir.content_hash(),
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["STALE_WITNESS_EVIDENCE"]


def test_changed_evidence_schema_stales_existing_witness() -> None:
    artifact = _typed_artifact()
    changed_ir = artifact.evidence_ir.model_copy(update={"schema_version": "2"}, deep=True)
    artifact = _reseal(
        artifact,
        evidence_ir=changed_ir,
        evidence_snapshot_hash=changed_ir.content_hash(),
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["STALE_WITNESS_EVIDENCE"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("subject", "record:other"),
        ("predicate", "changed_predicate"),
        ("value", "11.00"),
        ("source_id", "source.other"),
        ("quote", "Changed quote EUR 10.00"),
        ("locator", "page 9"),
        ("confidence", "low"),
        ("attributes", {"currency": "EUR", "unit": "changed"}),
    ],
)
def test_changed_claim_content_fails_existing_witness_replay(
    field: str,
    replacement: object,
) -> None:
    artifact = _typed_artifact()
    changed_claim = artifact.evidence_ir.claims[0].model_copy(
        update={field: replacement},
        deep=True,
    )
    changed_ir = artifact.evidence_ir.model_copy(update={"claims": [changed_claim]}, deep=True)
    artifact = _reseal(
        artifact,
        evidence_ir=changed_ir,
        evidence_snapshot_hash=changed_ir.content_hash(),
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["INVALID_WITNESS_REPLAY"]


@pytest.mark.parametrize(
    "omitted_field",
    ["binding", "witness"],
)
def test_typed_supported_rejects_a_missing_minimum_term(omitted_field: str) -> None:
    artifact = _typed_artifact()
    assessment = artifact.assessments[0]
    if omitted_field == "binding":
        artifact = _replace_typed_assessment(artifact, accepted_binding_ids=[])
    else:
        artifact = _replace_typed_assessment(
            artifact,
            accepted_witness_ids=[
                item
                for item in assessment.accepted_witness_ids
                if not item.startswith("witness.final_total")
            ],
            strong_status_links=[
                item
                for item in assessment.strong_status_links
                if item.witness_id != "witness.final_total.terminal"
            ],
        )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["TYPED_PROOF_INCOMPLETE"]


def test_typed_contradiction_needs_only_one_closed_declared_facet() -> None:
    artifact = _typed_artifact(status="CONTRADICTED")
    artifact = _replace_typed_assessment(
        artifact,
        accepted_binding_ids=[],
        accepted_witness_ids=[
            "witness.line_extensions",
            "witness.line_extensions.terminal",
        ],
        strong_status_links=[
            StrongStatusLink(
                witness_id="witness.line_extensions.terminal",
                true_status="CONTRADICTED",
            )
        ],
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "CONTRADICTED"
    assert proof.diagnostics == []
    result = proof.node_results[0]
    assert result.witness_ids == [
        "witness.line_extensions",
        "witness.line_extensions.terminal",
    ]
    assert "witness.final_total" not in result.witness_ids


def test_decimal_witnesses_without_terminal_status_links_fail_closed() -> None:
    """Regression: the former 0025 SUM + ABS_DIFF shape is not strong proof."""
    artifact = _typed_artifact(status="CONTRADICTED")
    assessment = artifact.assessments[0]
    binding = artifact.binding_proposals[0].model_copy(
        update={
            "term_refs": [
                *artifact.binding_proposals[0].term_refs,
                ProofTermRef(kind="POLICY", ref_id=_TYPED_POLICY),
            ]
        }
    )
    artifact = _replace_typed_assessment(
        _reseal(artifact, binding_proposals=[binding]),
        accepted_witness_ids=[
            item for item in assessment.accepted_witness_ids if not item.endswith(".terminal")
        ],
        strong_status_links=[],
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["TERMINAL_WITNESS_REQUIRED"]


def test_terminal_link_must_name_a_directly_accepted_witness() -> None:
    artifact = _typed_artifact()
    assessment = artifact.assessments[0]
    artifact = _replace_typed_assessment(
        artifact,
        accepted_witness_ids=[
            item
            for item in assessment.accepted_witness_ids
            if item != "witness.final_total.terminal"
        ],
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [
        "TERMINAL_WITNESS_NOT_DIRECTLY_ACCEPTED"
    ]


def test_unsubmitted_terminal_witness_fails_before_status_projection() -> None:
    artifact = _typed_artifact()
    artifact = _reseal(
        artifact,
        submitted_witness_refs={
            "check.typed": [
                item
                for item in artifact.submitted_witness_refs["check.typed"]
                if item != "witness.final_total.terminal"
            ]
        },
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["UNSUBMITTED_WITNESS_REFERENCE"]


def test_tampered_boolean_terminal_witness_fails_replay() -> None:
    artifact = _typed_artifact()
    witnesses = [
        item.model_copy(update={"result": False})
        if item.id == "witness.final_total.terminal"
        else item
        for item in artifact.calculation_witnesses
    ]
    artifact = _reseal(artifact, calculation_witnesses=witnesses)

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["INVALID_WITNESS_REPLAY"]


def test_false_boolean_terminal_maps_to_the_opposite_strong_status() -> None:
    artifact = _typed_artifact()
    claims = {item.id: item for item in artifact.evidence_ir.claims}
    intermediates = {
        item.id: item
        for item in artifact.calculation_witnesses
        if not item.id.endswith(".terminal")
    }
    terminals = [
        compute_witness(
            CalculationRequest(
                id=f"witness.{facet_ref}.terminal",
                check_id="check.typed",
                facet_ref=facet_ref,
                operation="GREATER_THAN",
                operands=[
                    ProofTermRef(kind="POLICY", ref_id=_TYPED_POLICY),
                    ProofTermRef(kind="WITNESS", ref_id=f"witness.{facet_ref}"),
                ],
            ),
            claims=claims,
            witnesses=intermediates,
            policy_values=artifact.resolved_policy_terms,
            evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
            policy_snapshot_hash=artifact.policy_hash,
        )
        for facet_ref in _TYPED_FACETS
    ]
    assert all(item.result is False for item in terminals)
    assessment = artifact.assessments[0].model_copy(
        update={
            "strong_status_links": [
                StrongStatusLink(
                    witness_id=item.id,
                    true_status="CONTRADICTED",
                )
                for item in terminals
            ]
        },
        deep=True,
    )
    artifact = _reseal(
        artifact,
        assessments=[assessment],
        calculation_witnesses=[*intermediates.values(), *terminals],
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "SUPPORTED"
    assert proof.diagnostics == []

    copied_current_status = assessment.model_copy(
        update={
            "reason": "verifier returned CONTRADICTED",
            "status": "CONTRADICTED",
        },
        deep=True,
    )
    mismatched = _reseal(artifact, assessments=[copied_current_status])
    mismatched_proof = compile_review_artifact(mismatched)

    assert mismatched_proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in mismatched_proof.diagnostics] == [
        "TERMINAL_WITNESS_STATUS_MISMATCH"
    ]


def test_true_boolean_with_opposite_status_mapping_fails_closed() -> None:
    artifact = _typed_artifact()
    links = [
        item.model_copy(update={"true_status": "CONTRADICTED"})
        for item in artifact.assessments[0].strong_status_links
    ]
    artifact = _replace_typed_assessment(artifact, strong_status_links=links)

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [
        "TERMINAL_WITNESS_STATUS_MISMATCH"
    ]


def test_terminal_boolean_must_consume_a_declared_artifact_policy() -> None:
    artifact = _typed_artifact()
    claim = artifact.evidence_ir.claims[0]
    intermediate = next(
        item
        for item in artifact.calculation_witnesses
        if item.id == "witness.final_total"
    )
    unrelated = compute_witness(
        CalculationRequest(
            id="witness.final_total.terminal",
            check_id="check.typed",
            facet_ref="final_total",
            operation="GREATER_THAN",
            operands=[
                ProofTermRef(kind="WITNESS", ref_id=intermediate.id),
                ProofTermRef(kind="CLAIM", ref_id=claim.id),
            ],
        ),
        claims={claim.id: claim},
        witnesses={intermediate.id: intermediate},
        policy_values=artifact.resolved_policy_terms,
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
        policy_snapshot_hash=artifact.policy_hash,
    )
    witnesses = [
        unrelated if item.id == unrelated.id else item
        for item in artifact.calculation_witnesses
    ]
    link = StrongStatusLink(
        witness_id=unrelated.id,
        true_status="CONTRADICTED",
    )
    links = [
        link if item.witness_id == link.witness_id else item
        for item in artifact.assessments[0].strong_status_links
    ]
    artifact = _reseal(
        artifact,
        assessments=[artifact.assessments[0].model_copy(update={"strong_status_links": links})],
        calculation_witnesses=witnesses,
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [
        "TERMINAL_WITNESS_POLICY_MISSING"
    ]


def test_claim_only_strong_check_does_not_require_a_terminal_link() -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "SUPPORTED"),
                _assessment("check.currency", "SUPPORTED"),
            ]
        )
    )

    assert proof.decision_for("req.invoice_review").status == "SUPPORTED"
    assert proof.diagnostics == []


@pytest.mark.parametrize(
    ("kind", "diagnostic"),
    [
        ("binding", "UNSUBMITTED_BINDING_REFERENCE"),
        ("witness", "UNSUBMITTED_WITNESS_REFERENCE"),
    ],
)
def test_verifier_cannot_accept_an_unsubmitted_typed_term(
    kind: str,
    diagnostic: str,
) -> None:
    artifact = _typed_artifact()
    if kind == "binding":
        artifact = _reseal(artifact, submitted_binding_refs={"check.typed": []})
    else:
        artifact = _reseal(
            artifact,
            submitted_witness_refs={
                "check.typed": [
                    item
                    for item in artifact.submitted_witness_refs["check.typed"]
                    if item != "witness.final_total"
                ]
            },
        )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [diagnostic]


@pytest.mark.parametrize(
    ("kind", "field_name", "value", "diagnostic"),
    [
        ("binding", "check_id", "check.other", "BINDING_CHECK_MISMATCH"),
        ("binding", "facet_ref", "facet.other", "BINDING_FACET_MISMATCH"),
        ("witness", "check_id", "check.other", "WITNESS_CHECK_MISMATCH"),
        ("witness", "facet_ref", "facet.other", "WITNESS_FACET_MISMATCH"),
    ],
)
def test_typed_terms_cannot_cross_check_or_facet_scope(
    kind: str,
    field_name: str,
    value: str,
    diagnostic: str,
) -> None:
    artifact = _typed_artifact()
    if kind == "binding":
        binding = artifact.binding_proposals[0].model_copy(update={field_name: value})
        artifact = _reseal(artifact, binding_proposals=[binding])
    else:
        witnesses = [
            item.model_copy(update={field_name: value})
            if item.id == "witness.final_total"
            else item
            for item in artifact.calculation_witnesses
        ]
        artifact = _reseal(artifact, calculation_witnesses=witnesses)

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [diagnostic]


@pytest.mark.parametrize(
    ("update", "diagnostic"),
    [
        ({"result": Decimal("999.00")}, "INVALID_WITNESS_REPLAY"),
        ({"lineage_hash": "sha256:forged"}, "INVALID_WITNESS_REPLAY"),
        ({"evidence_snapshot_hash": "sha256:other-evidence"}, "STALE_WITNESS_EVIDENCE"),
        ({"policy_snapshot_hash": "sha256:other-policy"}, "STALE_WITNESS_POLICY"),
    ],
)
def test_tampered_witness_fails_closed_after_model_construction(
    update: dict[str, object],
    diagnostic: str,
) -> None:
    artifact = _typed_artifact()
    witnesses = [
        item.model_copy(update=update)
        if item.id == "witness.final_total"
        else item
        for item in artifact.calculation_witnesses
    ]
    artifact = _reseal(artifact, calculation_witnesses=witnesses)

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [diagnostic]


def _typed_artifact_with_parent_witness() -> ReviewArtifact:
    artifact = _typed_artifact()
    claim = artifact.evidence_ir.claims[0]
    parent = compute_witness(
        CalculationRequest(
            id="witness.parent",
            check_id="check.typed",
            facet_ref="line_extensions",
            operation="SUM",
            operands=[
                ProofTermRef(kind="CLAIM", ref_id=claim.id),
                ProofTermRef(kind="POLICY", ref_id=_TYPED_POLICY),
            ],
        ),
        claims={claim.id: claim},
        witnesses={},
        policy_values=artifact.resolved_policy_terms,
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
        policy_snapshot_hash=artifact.policy_hash,
    )
    child = compute_witness(
        CalculationRequest(
            id="witness.line_extensions",
            check_id="check.typed",
            facet_ref="line_extensions",
            operation="SUM",
            operands=[ProofTermRef(kind="WITNESS", ref_id=parent.id)],
        ),
        claims={claim.id: claim},
        witnesses={parent.id: parent},
        policy_values=artifact.resolved_policy_terms,
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
        policy_snapshot_hash=artifact.policy_hash,
    )
    terminal = compute_witness(
        CalculationRequest(
            id="witness.line_extensions.terminal",
            check_id="check.typed",
            facet_ref="line_extensions",
            operation="GREATER_THAN",
            operands=[
                ProofTermRef(kind="WITNESS", ref_id=child.id),
                ProofTermRef(kind="CLAIM", ref_id=claim.id),
            ],
        ),
        claims={claim.id: claim},
        witnesses={parent.id: parent, child.id: child},
        policy_values=artifact.resolved_policy_terms,
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
        policy_snapshot_hash=artifact.policy_hash,
    )
    witnesses = [
        parent,
        child,
        terminal,
        *[
            item
            for item in artifact.calculation_witnesses
            if not item.id.startswith("witness.line_extensions")
        ],
    ]
    submitted = [item.id for item in witnesses]
    return _reseal(
        artifact,
        calculation_witnesses=witnesses,
        submitted_witness_refs={"check.typed": submitted},
    )


def test_recursive_witness_replay_projects_parent_and_transitive_claim() -> None:
    artifact = _typed_artifact_with_parent_witness()
    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "SUPPORTED"
    assessment = artifact.assessments[0]
    assert "witness.parent" not in assessment.accepted_witness_ids
    terminal = next(
        item
        for item in artifact.calculation_witnesses
        if item.id == "witness.line_extensions.terminal"
    )
    assert all(operand.ref.kind != "POLICY" for operand in terminal.operands)
    result = proof.node_results[0]
    assert "witness.parent" in result.witness_ids
    assert "witness.line_extensions" in result.witness_ids
    assert result.claim_ids == ["claim.amount"]
    assert result.source_ids == ["source.record"]


def _typed_artifact_with_cross_check_parent_witness() -> ReviewArtifact:
    artifact = _typed_artifact()
    claim = artifact.evidence_ir.claims[0]
    parent = next(
        item for item in artifact.calculation_witnesses if item.id == "witness.line_extensions"
    ).model_copy(update={"check_id": "check.lines"})
    # Recompute after changing ownership so the immutable lineage contains the owner CHECK.
    parent = compute_witness(
        CalculationRequest(
            id=parent.id,
            check_id=parent.check_id,
            facet_ref=parent.facet_ref,
            operation=parent.operation,
            operands=[item.ref for item in parent.operands],
        ),
        claims={claim.id: claim},
        witnesses={},
        policy_values=artifact.resolved_policy_terms,
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
        policy_snapshot_hash=artifact.policy_hash,
    )
    line_difference = compute_witness(
        CalculationRequest(
            id="witness.line_extensions.difference",
            check_id="check.lines",
            facet_ref="line_extensions",
            operation="ABS_DIFF",
            operands=[
                ProofTermRef(kind="WITNESS", ref_id=parent.id),
                ProofTermRef(kind="CLAIM", ref_id=claim.id),
            ],
        ),
        claims={claim.id: claim},
        witnesses={parent.id: parent},
        policy_values=artifact.resolved_policy_terms,
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
        policy_snapshot_hash=artifact.policy_hash,
    )
    line_terminal = compute_witness(
        CalculationRequest(
            id="witness.line_extensions.terminal",
            check_id="check.lines",
            facet_ref="line_extensions",
            operation="GREATER_THAN",
            operands=[
                ProofTermRef(kind="WITNESS", ref_id=line_difference.id),
                ProofTermRef(kind="POLICY", ref_id=_TYPED_POLICY),
            ],
        ),
        claims={claim.id: claim},
        witnesses={parent.id: parent, line_difference.id: line_difference},
        policy_values=artifact.resolved_policy_terms,
        evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
        policy_snapshot_hash=artifact.policy_hash,
    )

    def child(
        witness_id: str,
        facet_ref: str,
        *,
        operation: str = "SUM",
        include_policy: bool = False,
    ):
        operands = [ProofTermRef(kind="WITNESS", ref_id=parent.id)]
        if include_policy:
            operands.append(ProofTermRef(kind="POLICY", ref_id=_TYPED_POLICY))
        return compute_witness(
            CalculationRequest(
                id=witness_id,
                check_id="check.typed",
                facet_ref=facet_ref,
                operation=operation,
                operands=operands,
            ),
            claims={claim.id: claim},
            witnesses={parent.id: parent},
            policy_values=artifact.resolved_policy_terms,
            evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
            policy_snapshot_hash=artifact.policy_hash,
        )

    children = [
        child("witness.subtotal_aggregation", "subtotal_aggregation"),
        child("witness.stated_components", "stated_components"),
        child(
            "witness.final_total",
            "final_total",
            operation="ABS_DIFF",
            include_policy=True,
        ),
    ]
    terminals = [
        compute_witness(
            CalculationRequest(
                id=f"{item.id}.terminal",
                check_id="check.typed",
                facet_ref=item.facet_ref,
                operation="GREATER_THAN",
                operands=[
                    ProofTermRef(kind="WITNESS", ref_id=item.id),
                    ProofTermRef(kind="POLICY", ref_id=_TYPED_POLICY),
                ],
            ),
            claims={claim.id: claim},
            witnesses={parent.id: parent, **{child.id: child for child in children}},
            policy_values=artifact.resolved_policy_terms,
            evidence_snapshot_hash=artifact.evidence_ir.source_snapshot_hash(),
            policy_snapshot_hash=artifact.policy_hash,
        )
        for item in children
    ]
    binding = SemanticBindingProposal(
        id="binding.stated_components",
        check_id="check.typed",
        facet_ref="stated_components",
        relation="the submitted stated-component calculation is semantically applicable",
        term_refs=[
            ProofTermRef(kind="WITNESS", ref_id="witness.stated_components")
        ],
        reason="The relationship is proposed from typed proof terms.",
    )
    plan = ProofPlan(
        plan_id="plan.typed.cross-check",
        objective="Compile typed facets that reuse an earlier submitted calculation.",
        active_requirement_ids=["invoice_calculation_valid"],
        policy_refs=[_TYPED_POLICY],
        roots={"invoice_calculation_valid": "root.typed"},
        nodes=[
            ProofNode(
                id="check.lines",
                kind="CHECK",
                statement="Line extensions are recomputed.",
                requirement_refs=["invoice_calculation_valid"],
                policy_refs=[_TYPED_POLICY],
                facet_refs=["line_extensions"],
            ),
            ProofNode(
                id="check.typed",
                kind="CHECK",
                statement="The later totals reuse the submitted line calculation.",
                requirement_refs=["invoice_calculation_valid"],
                policy_refs=[_TYPED_POLICY],
                facet_refs=[
                    "subtotal_aggregation",
                    "stated_components",
                    "final_total",
                ],
                semantic_role_refs=[
                    "COMPONENT_OBSERVATION",
                    "COMPONENT_APPLICABILITY",
                    "COMPONENT_RECONCILIATION",
                ],
            ),
            ProofNode(
                id="root.typed",
                kind="ALL",
                depends_on=["check.lines", "check.typed"],
            ),
        ],
    )
    assessments = [
        CheckAssessment(
            check_id="check.lines",
            claim_ids=[claim.id],
            accepted_witness_ids=[parent.id, line_difference.id, line_terminal.id],
            strong_status_links=[
                StrongStatusLink(
                    witness_id=line_terminal.id,
                    true_status="CONTRADICTED",
                )
            ],
            source_ids=[claim.source_id],
            examined_source_ids=[claim.source_id],
            reason="The line calculation replays.",
            status="SUPPORTED",
        ),
        CheckAssessment(
            check_id="check.typed",
            accepted_binding_ids=[binding.id],
            accepted_witness_ids=[item.id for item in [*children, *terminals]],
            strong_status_links=[
                StrongStatusLink(witness_id=item.id, true_status="SUPPORTED")
                for item in terminals
            ],
            source_ids=[claim.source_id],
            examined_source_ids=[claim.source_id],
            reason="The dependent calculations replay from the submitted parent.",
            status="SUPPORTED",
        ),
    ]
    return _reseal(
        artifact,
        plan=plan,
        plan_hash=plan.content_hash(),
        assessments=assessments,
        binding_proposals=[binding],
        calculation_witnesses=[parent, line_difference, line_terminal, *children, *terminals],
        submitted_claim_refs={"check.lines": [claim.id], "check.typed": []},
        submitted_binding_refs={"check.typed": [binding.id]},
        submitted_witness_refs={
            "check.lines": [parent.id, line_difference.id, line_terminal.id],
            "check.typed": [item.id for item in [*children, *terminals]],
        },
    )


def test_cross_check_parent_witness_is_read_only_dependency_projection() -> None:
    artifact = _typed_artifact_with_cross_check_parent_witness()

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "SUPPORTED"
    assert proof.diagnostics == []
    assert "witness.line_extensions" not in artifact.submitted_witness_refs["check.typed"]
    result = next(item for item in proof.node_results if item.node_id == "check.typed")
    assert result.claim_ids == ["claim.amount"]
    assert result.witness_ids == sorted(
        [
            "witness.line_extensions",
            "witness.subtotal_aggregation",
            "witness.subtotal_aggregation.terminal",
            "witness.stated_components",
            "witness.stated_components.terminal",
            "witness.final_total",
            "witness.final_total.terminal",
        ]
    )


def test_foreign_parent_witness_cannot_be_used_as_a_terminal_status_link() -> None:
    artifact = _typed_artifact_with_cross_check_parent_witness()
    main = next(item for item in artifact.assessments if item.check_id == "check.typed")
    main = main.model_copy(
        update={
            "strong_status_links": [
                *main.strong_status_links,
                StrongStatusLink(
                    witness_id="witness.line_extensions.terminal",
                    true_status="SUPPORTED",
                ),
            ]
        },
        deep=True,
    )
    artifact = _reseal(
        artifact,
        assessments=[
            main if item.check_id == main.check_id else item
            for item in artifact.assessments
        ],
    )

    proof = compile_review_artifact(artifact)

    diagnostic = next(item for item in proof.diagnostics if item.node_id == "check.typed")
    assert diagnostic.code == "TERMINAL_WITNESS_NOT_DIRECTLY_ACCEPTED"


def test_cross_check_parent_must_be_submitted_by_its_owner() -> None:
    artifact = _typed_artifact_with_cross_check_parent_witness()
    artifact = _reseal(
        artifact,
        submitted_witness_refs={
            "check.lines": [],
            "check.typed": artifact.submitted_witness_refs["check.typed"],
        },
    )

    proof = compile_review_artifact(artifact)

    diagnostic = next(
        item for item in proof.diagnostics if item.node_id == "check.typed"
    )
    assert diagnostic.code == "UNSUBMITTED_WITNESS_REFERENCE"


def test_cross_check_parent_uses_only_its_owner_claim_submission() -> None:
    artifact = _typed_artifact_with_cross_check_parent_witness()
    artifact = _reseal(
        artifact,
        submitted_claim_refs={"check.lines": [], "check.typed": ["claim.amount"]},
    )

    proof = compile_review_artifact(artifact)

    diagnostic = next(
        item for item in proof.diagnostics if item.node_id == "check.typed"
    )
    assert diagnostic.code == "UNSUBMITTED_CLAIM_REFERENCE"


@pytest.mark.parametrize(
    ("failure", "diagnostic_code"),
    [
        ("missing", "INVALID_WITNESS_REFERENCE"),
        ("tampered", "INVALID_WITNESS_REPLAY"),
    ],
)
def test_cross_check_parent_must_exist_and_replay(
    failure: str,
    diagnostic_code: str,
) -> None:
    artifact = _typed_artifact_with_cross_check_parent_witness()
    witnesses = list(artifact.calculation_witnesses)
    if failure == "missing":
        witnesses = [item for item in witnesses if item.id != "witness.line_extensions"]
    else:
        witnesses = [
            item.model_copy(update={"result": Decimal("999.00")})
            if item.id == "witness.line_extensions"
            else item
            for item in witnesses
        ]
    artifact = _reseal(artifact, calculation_witnesses=witnesses)

    proof = compile_review_artifact(artifact)

    diagnostic = next(
        item for item in proof.diagnostics if item.node_id == "check.typed"
    )
    assert diagnostic.code == diagnostic_code


def test_direct_foreign_witness_cannot_hide_behind_valid_dependency_reuse() -> None:
    artifact = _typed_artifact_with_cross_check_parent_witness()
    main = next(item for item in artifact.assessments if item.check_id == "check.typed")
    main = main.model_copy(
        update={
            # Put the foreign Witness last: its dependency has already been accepted.
            "accepted_witness_ids": [
                *main.accepted_witness_ids,
                "witness.line_extensions",
            ]
        }
    )
    assessments = [
        main if item.check_id == main.check_id else item for item in artifact.assessments
    ]
    submitted = {
        **artifact.submitted_witness_refs,
        "check.typed": [
            *artifact.submitted_witness_refs["check.typed"],
            "witness.line_extensions",
        ],
    }
    artifact = _reseal(
        artifact,
        assessments=assessments,
        submitted_witness_refs=submitted,
    )

    proof = compile_review_artifact(artifact)

    diagnostic = next(
        item for item in proof.diagnostics if item.node_id == "check.typed"
    )
    assert diagnostic.code == "WITNESS_CHECK_MISMATCH"


def test_foreign_witness_is_not_a_cross_check_binding_shortcut() -> None:
    artifact = _typed_artifact_with_cross_check_parent_witness()
    binding = artifact.binding_proposals[0].model_copy(
        update={
            "term_refs": [
                ProofTermRef(kind="WITNESS", ref_id="witness.line_extensions")
            ]
        }
    )
    artifact = _reseal(artifact, binding_proposals=[binding])

    proof = compile_review_artifact(artifact)

    diagnostic = next(
        item for item in proof.diagnostics if item.node_id == "check.typed"
    )
    assert diagnostic.code == "WITNESS_CHECK_MISMATCH"


@pytest.mark.parametrize("target", ["operand_parent_hash", "parent_lineage_hash"])
def test_tampered_parent_witness_lineage_fails_closed(target: str) -> None:
    artifact = _typed_artifact_with_parent_witness()
    witnesses = list(artifact.calculation_witnesses)
    if target == "operand_parent_hash":
        child = next(item for item in witnesses if item.id == "witness.line_extensions")
        operand = child.operands[0].model_copy(
            update={"parent_lineage_hash": "sha256:forged-parent"}
        )
        changed = child.model_copy(update={"operands": [operand]})
        witnesses = [changed if item.id == changed.id else item for item in witnesses]
    else:
        parent = next(item for item in witnesses if item.id == "witness.parent")
        changed = parent.model_copy(update={"lineage_hash": "sha256:forged-parent"})
        witnesses = [changed if item.id == changed.id else item for item in witnesses]
    artifact = _reseal(artifact, calculation_witnesses=witnesses)

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["INVALID_WITNESS_REPLAY"]


@pytest.mark.parametrize(
    ("accepted_field", "diagnostic"),
    [
        ("accepted_binding_ids", "INVALID_BINDING_REFERENCE"),
        ("accepted_witness_ids", "INVALID_WITNESS_REFERENCE"),
    ],
)
def test_cross_object_malicious_ref_is_schema_valid_but_kernel_rejected(
    accepted_field: str,
    diagnostic: str,
) -> None:
    artifact = _typed_artifact()
    raw = artifact.model_dump(mode="json")
    raw["assessments"][0][accepted_field] = ["ghost.term"]
    raw["artifact_hash"] = ""
    parsed = ReviewArtifact.model_validate(raw)
    parsed = _reseal(parsed)

    proof = compile_review_artifact(parsed)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [diagnostic]


@pytest.mark.parametrize(
    ("artifact_hash", "diagnostic"),
    [
        ("", "MISSING_ARTIFACT_HASH"),
        ("sha256:stale-artifact", "STALE_ARTIFACT"),
    ],
)
def test_artifact_hash_is_a_required_kernel_boundary(
    artifact_hash: str,
    diagnostic: str,
) -> None:
    artifact = _typed_artifact().model_copy(update={"artifact_hash": artifact_hash})

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert diagnostic in [item.code for item in proof.diagnostics]


@pytest.mark.parametrize(
    ("signature_hash", "diagnostic"),
    [
        ("", "MISSING_PROOF_SIGNATURE_HASH"),
        ("sha256:stale-signature", "STALE_PROOF_SIGNATURE"),
    ],
)
def test_proof_signature_hash_is_a_required_kernel_lineage(
    signature_hash: str,
    diagnostic: str,
) -> None:
    artifact = _typed_artifact()
    artifact = _reseal(artifact, proof_signature_hash=signature_hash)

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == [diagnostic]


def test_kernel_rechecks_plan_conformance_even_when_plan_hash_is_current() -> None:
    artifact = _typed_artifact()
    node = artifact.plan.nodes[0].model_copy(
        update={
            "facet_refs": [
                facet_ref
                for facet_ref in artifact.plan.nodes[0].facet_refs
                if facet_ref != "final_total"
            ]
        }
    )
    plan = artifact.plan.model_copy(update={"nodes": [node]}, deep=True)
    artifact = _reseal(artifact, plan=plan, plan_hash=plan.content_hash())

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["PLAN_SIGNATURE_MISMATCH"]


@pytest.mark.parametrize("execution_status", ["COMPLETED", "PARTIAL", "FAILED"])
def test_execution_status_does_not_change_kernel_three_value_logic(
    execution_status: str,
) -> None:
    artifact = _reseal(_typed_artifact(), execution_status=execution_status)

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "SUPPORTED"
    assert proof.diagnostics == []


def test_changed_resolved_policy_term_invalidates_replay() -> None:
    artifact = _typed_artifact()
    artifact = _reseal(
        artifact,
        resolved_policy_terms={
            _TYPED_POLICY: {"value": "9.99", "currency": "EUR", "unit": ""}
        },
    )

    proof = compile_review_artifact(artifact)

    assert proof.decision_for("invoice_calculation_valid").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["INVALID_WITNESS_REPLAY"]


def test_kernel_source_contains_no_invoice_business_rule_vocabulary() -> None:
    source = Path(__file__).parents[1].joinpath(
        "app", "compiler_runtime", "kernel.py"
    ).read_text(encoding="utf-8").lower()

    assert all(
        term not in source
        for term in ("vat", "reverse_charge", "credit_note", "discount_rate")
    )
