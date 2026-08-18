from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.compiler_runtime import (
    CheckAssessment,
    Claim,
    EvidenceIR,
    ProofNode,
    ProofPlan,
    ReviewArtifact,
    compile_review_artifact,
)


def _plan(*, root_kind: str = "ALL", reverse: bool = False) -> ProofPlan:
    nodes = [
        ProofNode(
            id="check.invoice_total",
            kind="CHECK",
            statement="The invoice total is present and grounded in the invoice.",
            requirement_refs=["req.invoice_review"],
            policy_refs=["policy.invoice_fields"],
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
        policy_refs=["policy.invoice_fields"],
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
    return ReviewArtifact(
        plan=plan,
        plan_hash=plan_hash or plan.content_hash(),
        evidence_ir=evidence_ir,
        evidence_snapshot_hash=evidence_snapshot_hash or evidence_ir.content_hash(),
        assessments=assessments,
        submitted_claim_refs=submitted_claim_refs,
        policy_hash="policy-sha256",
        unconfigured_policy_refs=unconfigured_policy_refs or [],
        compiler_version="test-1",
        model="fixture",
        prompt_versions={"task_compiler": "1", "verifier": "1"},
    )


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


def test_ir_hash_tracks_source_content_fingerprints() -> None:
    changed = _ir().model_copy(
        update={"source_fingerprints": {"source.invoice": "sha256:invoice-v2"}}
    )

    assert _ir().content_hash() != changed.content_hash()


def test_ir_rejects_partial_source_fingerprint_coverage() -> None:
    with pytest.raises(ValidationError, match="must cover every source"):
        EvidenceIR(
            source_ids=["source.one", "source.two"],
            source_fingerprints={"source.one": "sha256:one"},
        )


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


@pytest.mark.parametrize(
    ("child_status", "expected"),
    [
        ("SUPPORTED", "CONTRADICTED"),
        ("CONTRADICTED", "SUPPORTED"),
        ("NOT_FOUND", "NOT_FOUND"),
    ],
)
def test_not_node_preserves_three_value_semantics(child_status: str, expected: str) -> None:
    plan = ProofPlan(
        plan_id="not-plan",
        objective="Establish the negation of one grounded check.",
        active_requirement_ids=["req.not"],
        roots={"req.not": "root.not"},
        nodes=[
            ProofNode(
                id="check.fact",
                kind="CHECK",
                statement="The prohibited fact exists.",
                requirement_refs=["req.not"],
            ),
            ProofNode(id="root.not", kind="NOT", depends_on=["check.fact"]),
        ],
    )
    assessment = CheckAssessment(
        check_id="check.fact",
        status=child_status,
        claim_ids=["claim.total"] if child_status != "NOT_FOUND" else [],
        source_ids=["source.invoice"] if child_status != "NOT_FOUND" else [],
        examined_source_ids=["source.invoice"],
        missing_fact="whether the prohibited fact exists" if child_status == "NOT_FOUND" else "",
    )

    proof = compile_review_artifact(_artifact([assessment], plan=plan))

    assert proof.decision_for("req.not").status == expected


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


def test_unconfigured_policy_forces_its_check_to_not_found() -> None:
    proof = compile_review_artifact(
        _artifact(
            [
                _assessment("check.invoice_total", "SUPPORTED"),
                _assessment("check.currency", "SUPPORTED"),
            ],
            unconfigured_policy_refs=["policy.invoice_fields"],
        )
    )

    assert proof.decision_for("req.invoice_review").status == "NOT_FOUND"
    assert [item.code for item in proof.diagnostics] == ["POLICY_NOT_CONFIGURED"]
    assert [item.check_id for item in proof.obligations] == ["check.invoice_total"]


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
