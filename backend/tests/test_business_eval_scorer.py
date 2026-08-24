from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.compiler_runtime.kernel import compile_review_artifact
from app.compiler_runtime.models import (
    Claim,
    EvidenceIR,
    ProofPlan,
    ReviewArtifact,
    StrongStatusLink,
)
from app.compiler_runtime.proof_terms import (
    CalculationRequest,
    CalculationWitness,
    ProofTermRef,
    SemanticBindingProposal,
    compute_witness,
)
from app.compiler_runtime.signatures import proof_signature_hash_for
from app.evals.business.models import (
    BusinessEvalCase,
    BusinessEvalOracle,
    EvalAttachment,
    EvalResult,
    EvalSnapshot,
    FrameworkOracle,
    MeaningOracle,
    RequiredRoleOracle,
    RequiredToolOracle,
    ReportArtifact,
)
from app.evals.business.report import render_eval_report
from app.evals.business.scorer import (
    STAGE_WEIGHTS,
    _RAW_PDF_APPENDIX_HEADING,
    _aggregate_milestone_outcome,
    _boundary_output_assertions,
    _canonical_projection_violations,
    _claim_matches_source_fact,
    _claim_semantics_match_source_fact,
    _equation_witnesses,
    _invalid_equation_claims,
    _claim_is_grounded,
    _locator_supports_quote,
    _meaning_groups_match,
    _match_typed_relation_witnesses,
    _predicate_matches_options,
    _refine_shared_facet_matches,
    _relational_statement_matches,
    _check_outcome,
    _target_outcome,
    _text_has_decimal,
    _typed_witness_matches_relation,
    _upstream_check_closure,
    score_business_eval,
)


def _case() -> BusinessEvalCase:
    return BusinessEvalCase(
        case_id="invoice_arithmetic_conflict_001",
        case_version="1",
        title="发票内部总额存在明确错误",
        suite="atomic_dev",
        business_holes=["invoice_arithmetic"],
        user_message="请帮我审核这张发票，完成申报前检查，并生成一份中文审核报告。",
        policy_version="aurora_ap_lite_v1",
        auto_approve_tools=["write_case_file", "render_pdf"],
        attachments=[
            EvalAttachment(
                id="invoice",
                path="attachments/invoice.pdf",
                sha256="0" * 64,
                content_type="application/pdf",
                source_url="https://example.invalid/invoice.pdf",
                source_revision="fixture",
                license="MIT",
            )
        ],
    )


def _oracle() -> BusinessEvalOracle:
    path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/invoice_arithmetic_conflict_001/oracle.json"
    )
    return BusinessEvalOracle.model_validate_json(path.read_text(encoding="utf-8"))


def _snapshot() -> EvalSnapshot:
    oracle = _oracle()
    source_facts = [fact for fact in oracle.facts if fact.origin == "source"]
    claims = [
        {
            "id": f"claim_{fact.id}",
            "subject": "invoice",
            "predicate": fact.predicate_options[0] if fact.predicate_options else fact.id,
            "value": fact.value,
            "source_id": "source_1",
            "quote": fact.source_quote,
            "locator": f"locator {fact.id}",
            "confidence": "high",
            "attributes": {"currency": fact.currency} if fact.currency else {},
        }
        for fact in source_facts
    ]
    source_content = "\n".join(
        f"locator {fact.id}\n{fact.source_quote}" for fact in source_facts
    )
    line_claim_ids = [f"claim_{fact.id}" for fact in source_facts if fact.id.startswith("line_")]
    source_fact_ids = {fact.id for fact in source_facts}
    milestone_by_id = {item.id: item for item in oracle.milestones}

    def milestone_claim_ids(milestone_id: str) -> list[str]:
        return [
            f"claim_{fact_id}"
            for fact_id in milestone_by_id[milestone_id].fact_ids
            if fact_id in source_fact_ids
        ]

    subtotal_claim_ids = milestone_claim_ids("subtotal_aggregation")
    final_claim_ids = milestone_claim_ids("final_total_reconciliation")
    component_claim_ids = milestone_claim_ids("stated_component_rate_base_validation")
    plan = {
        "plan_id": "plan_invoice_arithmetic",
        "version": "1",
        "objective": "Verify the invoice internal arithmetic and reconcile its printed final total.",
        "active_requirement_ids": ["invoice_calculation_valid"],
        "policy_refs": ["invoice_calculation_rounding_tolerance"],
        "roots": {"invoice_calculation_valid": "root"},
        "nodes": [
            {
                "id": "line_extensions",
                "kind": "CHECK",
                "statement": "For every line item, quantity multiplied by unit price equals its stated line extension.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": ["invoice_calculation_rounding_tolerance"],
                "facet_refs": ["line_extensions"],
            },
            {
                "id": "subtotal_aggregation",
                "kind": "CHECK",
                "statement": "The sum of all line extensions equals and matches the printed subtotal.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": ["invoice_calculation_rounding_tolerance"],
                "facet_refs": ["subtotal_aggregation"],
            },
            {
                "id": "component_discount_rate_base",
                "kind": "CHECK",
                "statement": "Recompute the discount percentage rate times its printed subtotal base.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": ["invoice_calculation_rounding_tolerance"],
                "facet_refs": ["stated_components"],
                "semantic_role_refs": [
                    "COMPONENT_OBSERVATION",
                    "COMPONENT_APPLICABILITY",
                    "COMPONENT_RECONCILIATION",
                ],
            },
            {
                "id": "component_vat_rate_base",
                "kind": "CHECK",
                "statement": "Validate the VAT tax rate times its taxable calculation base.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": ["invoice_calculation_rounding_tolerance"],
                "facet_refs": ["stated_components"],
                "semantic_role_refs": [
                    "COMPONENT_OBSERVATION",
                    "COMPONENT_APPLICABILITY",
                    "COMPONENT_RECONCILIATION",
                ],
            },
            {
                "id": "final_total_reconciliation",
                "kind": "CHECK",
                "statement": "The printed final total equals and reconciles the subtotal plus VAT tax minus discount within tolerance.",
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": ["invoice_calculation_rounding_tolerance"],
                "facet_refs": ["final_total"],
            },
            {
                "id": "root",
                "kind": "ALL",
                "depends_on": [
                    "line_extensions",
                    "subtotal_aggregation",
                    "component_discount_rate_base",
                    "component_vat_rate_base",
                    "final_total_reconciliation",
                ],
            },
        ],
    }
    evidence_ir = {
        "source_ids": ["source_1"],
        "source_fingerprints": {"source_1": "sha256:fixture-source"},
        "claims": claims,
    }
    assessments = [
        {
            "check_id": "line_extensions",
            "claim_ids": line_claim_ids,
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "SUPPORTED",
            "reason": (
                "2 × 23279.90 = 46559.80; 3 × 7734.30 = 23202.90; "
                "1 × 47421.00 = 47421.00; 10 × 457.30 = 4573.00; "
                "1 × 944.00 = 944.00; 100 × 326.50 = 32650.00."
            ),
        },
        {
            "check_id": "subtotal_aggregation",
            "claim_ids": subtotal_claim_ids,
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "SUPPORTED",
            "reason": "46559.80 + 23202.90 + 47421.00 + 4573.00 + 944.00 + 32650.00 = 155350.70.",
        },
        {
            "check_id": "component_discount_rate_base",
            "claim_ids": [
                item
                for item in component_claim_ids
                if item in {
                    "claim_printed_subtotal",
                    "claim_adjustment_1",
                    "claim_adjustment_1_rate_factor",
                }
            ],
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "SUPPORTED",
            "reason": "155350.70 × 0.02 = 3107.0140, which rounds to the printed discount magnitude 3107.01.",
        },
        {
            "check_id": "component_vat_rate_base",
            "claim_ids": [item for item in component_claim_ids if item == "claim_vat_1"],
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "NOT_FOUND",
            "reason": "The VAT tax rate and taxable base are not shown, so rate times base cannot be verified.",
            "missing_fact": "The source does not state the VAT rate or taxable base.",
            "gap_code": "SOURCE_MISSING",
        },
        {
            "check_id": "final_total_reconciliation",
            "claim_ids": final_claim_ids,
            "source_ids": ["source_1"],
            "examined_source_ids": ["source_1"],
            "status": "CONTRADICTED",
            "reason": (
                "155350.70 + 31070.14 - 3107.01 = 183313.83. "
                "188813.24 - 183313.83 = 5499.41, and 5499.41 exceeds 0.01."
            ),
        },
    ]
    plan_model = ProofPlan.model_validate(plan)
    evidence_model = EvidenceIR.model_validate(evidence_ir)
    plan_hash = plan_model.content_hash()
    evidence_hash = evidence_model.content_hash()
    witness_evidence_hash = evidence_model.source_snapshot_hash()
    policy_snapshot_hash = "sha256:policy"
    policy_terms = {
        "invoice_calculation_rounding_tolerance": {
            "value": "0.01",
            "currency": "EUR",
            "unit": "",
        }
    }
    claims_by_id = {claim.id: claim for claim in evidence_model.claims}
    witnesses = {}

    def make_witness(
        witness_id: str,
        check_id: str,
        facet_ref: str,
        operation: str,
        refs: list[ProofTermRef],
    ):
        witness = compute_witness(
            CalculationRequest(
                id=witness_id,
                check_id=check_id,
                facet_ref=facet_ref,
                operation=operation,
                operands=refs,
            ),
            claims=claims_by_id,
            witnesses=witnesses,
            policy_values=policy_terms,
            evidence_snapshot_hash=witness_evidence_hash,
            policy_snapshot_hash=policy_snapshot_hash,
        )
        witnesses[witness.id] = witness
        return witness

    line_witnesses = [
        make_witness(
            f"witness_line_{line_number}",
            "line_extensions",
            "line_extensions",
            "MULTIPLY",
            [
                ProofTermRef(kind="CLAIM", ref_id=f"claim_line_{line_number}_quantity"),
                ProofTermRef(kind="CLAIM", ref_id=f"claim_line_{line_number}_unit_price"),
            ],
        )
        for line_number in range(1, 7)
    ]
    line_difference_witnesses = [
        make_witness(
            f"witness_line_{line_number}_difference",
            "line_extensions",
            "line_extensions",
            "ABS_DIFF",
            [
                ProofTermRef(kind="WITNESS", ref_id=f"witness_line_{line_number}"),
                ProofTermRef(kind="CLAIM", ref_id=f"claim_line_{line_number}_extension"),
            ],
        )
        for line_number in range(1, 7)
    ]
    line_terminal_witnesses = [
        make_witness(
            f"witness_line_{line_number}_tolerance",
            "line_extensions",
            "line_extensions",
            "GREATER_THAN",
            [
                ProofTermRef(
                    kind="WITNESS",
                    ref_id=f"witness_line_{line_number}_difference",
                ),
                ProofTermRef(
                    kind="POLICY",
                    ref_id="invoice_calculation_rounding_tolerance",
                ),
            ],
        )
        for line_number in range(1, 7)
    ]
    subtotal_witness = make_witness(
        "witness_subtotal",
        "subtotal_aggregation",
        "subtotal_aggregation",
        "SUM",
        [
            ProofTermRef(kind="CLAIM", ref_id=f"claim_line_{line_number}_extension")
            for line_number in range(1, 7)
        ],
    )
    subtotal_difference_witness = make_witness(
        "witness_subtotal_difference",
        "subtotal_aggregation",
        "subtotal_aggregation",
        "ABS_DIFF",
        [
            ProofTermRef(kind="WITNESS", ref_id=subtotal_witness.id),
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
        ],
    )
    subtotal_terminal_witness = make_witness(
        "witness_subtotal_tolerance",
        "subtotal_aggregation",
        "subtotal_aggregation",
        "GREATER_THAN",
        [
            ProofTermRef(kind="WITNESS", ref_id=subtotal_difference_witness.id),
            ProofTermRef(
                kind="POLICY",
                ref_id="invoice_calculation_rounding_tolerance",
            ),
        ],
    )
    discount_witness = make_witness(
        "witness_discount",
        "component_discount_rate_base",
        "stated_components",
        "MULTIPLY",
        [
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
            ProofTermRef(kind="CLAIM", ref_id="claim_adjustment_1_rate_factor"),
        ],
    )
    discount_zero_witness = make_witness(
        "witness_discount_zero",
        "component_discount_rate_base",
        "stated_components",
        "SUBTRACT",
        [
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
        ],
    )
    discount_printed_magnitude_witness = make_witness(
        "witness_discount_printed_magnitude",
        "component_discount_rate_base",
        "stated_components",
        "SUBTRACT",
        [
            ProofTermRef(kind="WITNESS", ref_id=discount_zero_witness.id),
            ProofTermRef(kind="CLAIM", ref_id="claim_adjustment_1"),
        ],
    )
    discount_difference_witness = make_witness(
        "witness_discount_difference",
        "component_discount_rate_base",
        "stated_components",
        "ABS_DIFF",
        [
            ProofTermRef(kind="WITNESS", ref_id=discount_witness.id),
            ProofTermRef(
                kind="WITNESS",
                ref_id=discount_printed_magnitude_witness.id,
            ),
        ],
    )
    discount_terminal_witness = make_witness(
        "witness_discount_tolerance",
        "component_discount_rate_base",
        "stated_components",
        "GREATER_THAN",
        [
            ProofTermRef(kind="WITNESS", ref_id=discount_difference_witness.id),
            ProofTermRef(
                kind="POLICY",
                ref_id="invoice_calculation_rounding_tolerance",
            ),
        ],
    )
    final_sum_witness = make_witness(
        "witness_final_sum",
        "final_total_reconciliation",
        "final_total",
        "SUM",
        [
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
            ProofTermRef(kind="CLAIM", ref_id="claim_vat_1"),
            ProofTermRef(kind="CLAIM", ref_id="claim_adjustment_1"),
        ],
    )
    final_difference_witness = make_witness(
        "witness_final_difference",
        "final_total_reconciliation",
        "final_total",
        "ABS_DIFF",
        [
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_total"),
            ProofTermRef(kind="WITNESS", ref_id=final_sum_witness.id),
        ],
    )
    final_tolerance_witness = make_witness(
        "witness_final_tolerance",
        "final_total_reconciliation",
        "final_total",
        "GREATER_THAN",
        [
            ProofTermRef(kind="WITNESS", ref_id=final_difference_witness.id),
            ProofTermRef(
                kind="POLICY",
                ref_id="invoice_calculation_rounding_tolerance",
            ),
        ],
    )
    discount_binding = SemanticBindingProposal(
        id="binding_discount_base",
        check_id="component_discount_rate_base",
        facet_ref="stated_components",
        relation="the stated subtotal is the disclosed base for the discount rate",
        term_refs=[
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
            ProofTermRef(kind="CLAIM", ref_id="claim_adjustment_1_rate_factor"),
            ProofTermRef(kind="CLAIM", ref_id="claim_adjustment_1"),
            ProofTermRef(kind="WITNESS", ref_id=discount_witness.id),
        ],
        reason="The invoice states the percentage adjustment against its subtotal.",
    )
    witness_refs = {
        "line_extensions": [
            item.id
            for item in [
                *line_witnesses,
                *line_difference_witnesses,
                *line_terminal_witnesses,
            ]
        ],
        "subtotal_aggregation": [
            subtotal_witness.id,
            subtotal_difference_witness.id,
            subtotal_terminal_witness.id,
        ],
        "component_discount_rate_base": [
            discount_witness.id,
            discount_zero_witness.id,
            discount_printed_magnitude_witness.id,
            discount_difference_witness.id,
            discount_terminal_witness.id,
        ],
        "component_vat_rate_base": [],
        "final_total_reconciliation": [
            final_sum_witness.id,
            final_difference_witness.id,
            final_tolerance_witness.id,
        ],
    }
    binding_refs = {
        "component_discount_rate_base": [discount_binding.id],
    }
    strong_status_links = {
        "line_extensions": [
            StrongStatusLink(
                witness_id=item.id,
                true_status="CONTRADICTED",
            ).model_dump(mode="json")
            for item in line_terminal_witnesses
        ],
        "subtotal_aggregation": [
            StrongStatusLink(
                witness_id=subtotal_terminal_witness.id,
                true_status="CONTRADICTED",
            ).model_dump(mode="json")
        ],
        "component_discount_rate_base": [
            StrongStatusLink(
                witness_id=discount_terminal_witness.id,
                true_status="CONTRADICTED",
            ).model_dump(mode="json")
        ],
        "final_total_reconciliation": [
            StrongStatusLink(
                witness_id=final_tolerance_witness.id,
                true_status="CONTRADICTED",
            ).model_dump(mode="json")
        ],
    }
    for assessment in assessments:
        assessment["accepted_binding_ids"] = binding_refs.get(assessment["check_id"], [])
        assessment["accepted_witness_ids"] = witness_refs.get(assessment["check_id"], [])
        assessment["strong_status_links"] = strong_status_links.get(
            assessment["check_id"],
            [],
        )
    artifact = {
        "plan": plan,
        "plan_hash": plan_hash,
        "proof_signature_hash": proof_signature_hash_for(
            plan_model.active_requirement_ids
        ),
        "evidence_ir": evidence_ir,
        "evidence_snapshot_hash": evidence_hash,
        "assessments": assessments,
        "binding_proposals": [discount_binding.model_dump(mode="json")],
        "calculation_witnesses": [
            item.model_dump(mode="json") for item in witnesses.values()
        ],
        "submitted_claim_refs": {item["check_id"]: item["claim_ids"] for item in assessments},
        "submitted_binding_refs": binding_refs,
        "submitted_witness_refs": witness_refs,
        "policy_hash": policy_snapshot_hash,
        "resolved_policy_terms": policy_terms,
        "unconfigured_policy_refs": [],
        "execution_status": "COMPLETED",
        "compiler_version": "fixture-vnext",
        "model": "fixture-model",
        "prompt_versions": {},
    }
    artifact_model = ReviewArtifact.model_validate(artifact)
    artifact_model = artifact_model.model_copy(
        update={"artifact_hash": artifact_model.content_hash()}
    )
    compiled_proof = compile_review_artifact(
        artifact_model,
        requirement_requiredness={"invoice_calculation_valid": True},
    )
    state = {
        "requirements": [
            {
                "id": "invoice_calculation_valid",
                "status": "conflict",
                "evidence_ids": ["source_1"],
            }
        ],
        "evidence_items": [{"id": "source_1", "type": "invoice", "content": source_content}],
        "review_artifact": artifact_model.model_dump(mode="json"),
        "compiled_proof": compiled_proof.model_dump(mode="json"),
    }
    return EvalSnapshot(
        case_id="invoice_arithmetic_conflict_001",
        case_version="1",
        run_id="run_good",
        provider="deepseek",
        model="deepseek-v4-flash",
        case_state=state,
        conversation=[
            {"role": "user", "content": _case().user_message},
            {
                "role": "assistant",
                "content": (
                    "审核发现总额错误：票面 188'813.24 EUR，重算 183,313.83 EUR，"
                    "相差 5499.41 EUR。已生成[中文审核报告](reports/audit.pdf)。"
                ),
            },
        ],
        reports=[
            ReportArtifact(
                path="reports/audit.md",
                kind="markdown",
                bytes=320,
                text=(
                    "# 审核报告\n金额不一致：小计 155350.70 EUR，VAT 31070.14 EUR，"
                    "折扣 -3107.01 EUR，票面 188813.24 EUR，"
                    "重算 183'313.83 EUR，差额 5,499.41 EUR。"
                ),
            ),
            ReportArtifact(
                path="reports/audit.pdf",
                kind="pdf",
                bytes=2048,
                text=(
                    "审核报告：金额不一致，小计 155350.70 EUR，VAT 31070.14 EUR，"
                    "折扣 -3107.01 EUR，票面 188813.24 EUR，重算 183313.83 EUR，"
                    "差额 5499.41 EUR。"
                ),
            ),
        ],
        trace={"path": "traces/run_good/events.jsonl", "duration_ms": 1234},
        events=[
            {
                "kind": "provider_call",
                "payload": {"role": "manager", "usage": {"total_tokens": 800}},
            }
        ],
        transcript_path="traces/run_good/deepseek_calls.txt",
        approved_tools=["write_case_file", "render_pdf"],
    )


def _strict_snapshot() -> EvalSnapshot:
    return _snapshot().model_copy(deep=True)


def _framework_oracle(**updates: object) -> BusinessEvalOracle:
    framework = FrameworkOracle(
        required_tools=[
            RequiredToolOracle(name="read_attachment"),
            RequiredToolOracle(name="read_source"),
            RequiredToolOracle(name="bind_claim"),
            RequiredToolOracle(name="compute_witness"),
            RequiredToolOracle(name="submit_check"),
        ],
        forbidden_tools=[],
        max_tool_errors=0,
        max_total_calls=8,
        ordered_milestones=[
            ["read_attachment"],
            ["read_source"],
            ["bind_claim"],
            ["compute_witness"],
            ["submit_check"],
        ],
    ).model_copy(update=updates)
    return _oracle().model_copy(update={"framework": framework})


def _framework_snapshot() -> EvalSnapshot:
    snapshot = _snapshot().model_copy(deep=True)
    # Real shape: events are canonical; compact trace repeats Manager calls.
    snapshot.trace["tool_calls"] = [
        {"tool": "read_attachment", "error": "", "ts": "2026-01-01T00:00:01+00:00"},
    ]
    snapshot.events.extend(
        [
            {"kind": "tool_call", "name": "read_attachment", "payload": {"tool": "read_attachment", "error": ""}},
            {"kind": "tool_started", "name": "executor", "payload": {"tool": "read_source"}},
            {"kind": "tool_finished", "name": "executor", "payload": {"tool": "read_source", "status": "completed"}},
            {"kind": "tool_started", "name": "executor", "payload": {"tool": "bind_claim"}},
            {"kind": "tool_finished", "name": "executor", "payload": {"tool": "bind_claim", "status": "completed"}},
            {"kind": "tool_started", "name": "executor", "payload": {"tool": "list_sources"}},
            {"kind": "tool_finished", "name": "executor", "payload": {"tool": "list_sources", "status": "completed"}},
            {"kind": "tool_started", "name": "executor", "payload": {"tool": "functions.compute_witness"}},
            {"kind": "tool_finished", "name": "executor", "payload": {"tool": "functions.compute_witness", "status": "completed"}},
            {"kind": "tool_started", "name": "executor", "payload": {"tool": "submit_check"}},
            {"kind": "tool_finished", "name": "executor", "payload": {"tool": "submit_check", "status": "completed"}},
        ]
    )
    return snapshot


def _refresh_proof_hashes(snapshot: EvalSnapshot) -> None:
    artifact = snapshot.case_state["review_artifact"]
    artifact["submitted_claim_refs"] = {
        item["check_id"]: item["claim_ids"] for item in artifact["assessments"]
    }
    plan_hash = ProofPlan.model_validate(artifact["plan"]).content_hash()
    evidence_hash = EvidenceIR.model_validate(artifact["evidence_ir"]).content_hash()
    artifact["plan_hash"] = plan_hash
    artifact["proof_signature_hash"] = proof_signature_hash_for(
        artifact["plan"]["active_requirement_ids"]
    )
    artifact["evidence_snapshot_hash"] = evidence_hash
    artifact_model = ReviewArtifact.model_validate(artifact)
    artifact_model = artifact_model.model_copy(
        update={"artifact_hash": artifact_model.content_hash()}
    )
    snapshot.case_state["review_artifact"] = artifact_model.model_dump(mode="json")
    snapshot.case_state["compiled_proof"] = compile_review_artifact(
        artifact_model,
        requirement_requiredness={
            item["id"]: bool(item.get("required", True))
            for item in snapshot.case_state["requirements"]
        },
    ).model_dump(mode="json")


def _rebuild_witness_check_ids(
    artifact: dict,
    witness_check_ids: dict[str, str],
    witness_operands: dict[str, list[ProofTermRef]] | None = None,
) -> None:
    evidence_model = EvidenceIR.model_validate(artifact["evidence_ir"])
    claims = {claim.id: claim for claim in evidence_model.claims}
    rebuilt = {}
    for raw in artifact["calculation_witnesses"]:
        witness = compute_witness(
            CalculationRequest(
                id=raw["id"],
                check_id=witness_check_ids.get(raw["id"], raw["check_id"]),
                facet_ref=raw["facet_ref"],
                operation=raw["operation"],
                operands=(
                    (witness_operands or {}).get(raw["id"])
                    or [
                        ProofTermRef.model_validate(item["ref"])
                        for item in raw["operands"]
                    ]
                ),
            ),
            claims=claims,
            witnesses=rebuilt,
            policy_values=artifact["resolved_policy_terms"],
            evidence_snapshot_hash=evidence_model.source_snapshot_hash(),
            policy_snapshot_hash=artifact["policy_hash"],
        )
        rebuilt[witness.id] = witness
    artifact["calculation_witnesses"] = [
        item.model_dump(mode="json") for item in rebuilt.values()
    ]


def _oracle_with_line_sum(*, prove_equivalence: bool) -> BusinessEvalOracle:
    payload = _oracle().model_dump(mode="json")
    payload["facts"].append(
        {
            "id": "line_sum",
            "origin": "derived",
            "kind": "decimal",
            "value": "155350.70",
            "currency": "EUR",
            "required_in": ["reasoning"],
        }
    )
    if prove_equivalence:
        payload["facts"].append(
            {
                "id": "line_sum_printed_difference",
                "origin": "derived",
                "kind": "decimal",
                "value": "0.00",
                "currency": "EUR",
                "required_in": ["reasoning"],
            }
        )
    for relation in payload["relations"]:
        if relation["id"] == "subtotal_math":
            relation["output_fact_id"] = "line_sum"
        elif relation["id"] == "recomputed_total_math":
            relation["input_fact_ids"] = [
                "line_sum" if item == "printed_subtotal" else item
                for item in relation["input_fact_ids"]
            ]
    if prove_equivalence:
        subtotal_index = next(
            index
            for index, relation in enumerate(payload["relations"])
            if relation["id"] == "subtotal_math"
        )
        payload["relations"].insert(
            subtotal_index + 1,
            {
                "id": "line_sum_matches_printed_subtotal",
                "operation": "absolute_difference",
                "input_fact_ids": ["line_sum", "printed_subtotal"],
                "output_fact_id": "line_sum_printed_difference",
            },
        )
    for milestone in payload["milestones"]:
        if milestone["id"] == "subtotal_aggregation":
            milestone["fact_ids"].append("line_sum")
            if prove_equivalence:
                milestone["fact_ids"].append("line_sum_printed_difference")
                milestone["relation_ids"].append("line_sum_matches_printed_subtotal")
        elif milestone["id"] == "final_total_reconciliation":
            milestone["fact_ids"].append("line_sum")
    return BusinessEvalOracle.model_validate(payload)


def _add_line_sum_equivalence_witness(snapshot: EvalSnapshot) -> None:
    artifact = snapshot.case_state["review_artifact"]
    evidence = EvidenceIR.model_validate(artifact["evidence_ir"])
    claims = {claim.id: claim for claim in evidence.claims}
    existing = {
        item["id"]: CalculationWitness.model_validate(item)
        for item in artifact["calculation_witnesses"]
    }
    witness = compute_witness(
        CalculationRequest(
            id="witness_line_sum_printed_difference",
            check_id="subtotal_aggregation",
            facet_ref="subtotal_aggregation",
            operation="ABS_DIFF",
            operands=[
                ProofTermRef(kind="WITNESS", ref_id="witness_subtotal"),
                ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
            ],
        ),
        claims=claims,
        witnesses=existing,
        policy_values=artifact["resolved_policy_terms"],
        evidence_snapshot_hash=evidence.source_snapshot_hash(),
        policy_snapshot_hash=artifact["policy_hash"],
    )
    artifact["calculation_witnesses"].append(witness.model_dump(mode="json"))
    assessment = next(
        item
        for item in artifact["assessments"]
        if item["check_id"] == "subtotal_aggregation"
    )
    assessment["accepted_witness_ids"].append(witness.id)
    artifact["submitted_witness_refs"]["subtotal_aggregation"].append(witness.id)
    _refresh_proof_hashes(snapshot)


def _replace_subtotal_with_pairwise_sum(snapshot: EvalSnapshot) -> None:
    artifact = snapshot.case_state["review_artifact"]
    evidence = EvidenceIR.model_validate(artifact["evidence_ir"])
    claims = {claim.id: claim for claim in evidence.claims}
    witnesses = {
        item["id"]: CalculationWitness.model_validate(item)
        for item in artifact["calculation_witnesses"]
        if item["check_id"] != "subtotal_aggregation"
    }
    chain: list[CalculationWitness] = []
    previous_id = ""
    for line_number in range(2, 7):
        operands = (
            [
                ProofTermRef(kind="CLAIM", ref_id="claim_line_1_extension"),
                ProofTermRef(kind="CLAIM", ref_id="claim_line_2_extension"),
            ]
            if line_number == 2
            else [
                ProofTermRef(kind="WITNESS", ref_id=previous_id),
                ProofTermRef(
                    kind="CLAIM",
                    ref_id=f"claim_line_{line_number}_extension",
                ),
            ]
        )
        witness = compute_witness(
            CalculationRequest(
                id=f"witness_subtotal_pair_{line_number}",
                check_id="subtotal_aggregation",
                facet_ref="subtotal_aggregation",
                operation="SUM",
                operands=operands,
            ),
            claims=claims,
            witnesses=witnesses,
            policy_values=artifact["resolved_policy_terms"],
            evidence_snapshot_hash=evidence.source_snapshot_hash(),
            policy_snapshot_hash=artifact["policy_hash"],
        )
        witnesses[witness.id] = witness
        chain.append(witness)
        previous_id = witness.id
    difference = compute_witness(
        CalculationRequest(
            id="witness_subtotal_difference",
            check_id="subtotal_aggregation",
            facet_ref="subtotal_aggregation",
            operation="ABS_DIFF",
            operands=[
                ProofTermRef(kind="WITNESS", ref_id=previous_id),
                ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
            ],
        ),
        claims=claims,
        witnesses=witnesses,
        policy_values=artifact["resolved_policy_terms"],
        evidence_snapshot_hash=evidence.source_snapshot_hash(),
        policy_snapshot_hash=artifact["policy_hash"],
    )
    witnesses[difference.id] = difference
    terminal = compute_witness(
        CalculationRequest(
            id="witness_subtotal_tolerance",
            check_id="subtotal_aggregation",
            facet_ref="subtotal_aggregation",
            operation="GREATER_THAN",
            operands=[
                ProofTermRef(kind="WITNESS", ref_id=difference.id),
                ProofTermRef(
                    kind="POLICY",
                    ref_id="invoice_calculation_rounding_tolerance",
                ),
            ],
        ),
        claims=claims,
        witnesses=witnesses,
        policy_values=artifact["resolved_policy_terms"],
        evidence_snapshot_hash=evidence.source_snapshot_hash(),
        policy_snapshot_hash=artifact["policy_hash"],
    )
    artifact["calculation_witnesses"] = [
        item
        for item in artifact["calculation_witnesses"]
        if item["check_id"] != "subtotal_aggregation"
    ] + [item.model_dump(mode="json") for item in [*chain, difference, terminal]]
    assessment = next(
        item
        for item in artifact["assessments"]
        if item["check_id"] == "subtotal_aggregation"
    )
    assessment["accepted_witness_ids"] = [
        item.id for item in [*chain, difference, terminal]
    ]
    artifact["submitted_witness_refs"]["subtotal_aggregation"] = [
        item.id for item in [*chain, difference, terminal]
    ]
    _refresh_proof_hashes(snapshot)


def _not_found_output_fixture() -> tuple[BusinessEvalOracle, EvalSnapshot]:
    nf_path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/tax_inclusive_arithmetic_supported_0053/oracle.json"
    )
    nf_oracle = BusinessEvalOracle.model_validate_json(
        nf_path.read_text(encoding="utf-8")
    )
    payload = _oracle().model_dump(mode="json")
    payload["requirement"] = {
        "requirement_id": "invoice_calculation_valid",
        "decision_status": "NOT_FOUND",
        "projected_status": "weak",
        "blocking_obligations": True,
    }
    payload["communication"] = nf_oracle.communication.model_dump(mode="json")
    nf_component = next(
        item
        for item in nf_oracle.milestones
        if item.id == "stated_component_rate_base_validation"
    )
    for milestone in payload["milestones"]:
        if milestone["id"] == "stated_component_rate_base_validation":
            milestone["missing_meaning"] = nf_component.missing_meaning.model_dump(
                mode="json"
            )
        elif milestone["id"] == "final_total_reconciliation":
            milestone["expected_status"] = "SUPPORTED"
    oracle = BusinessEvalOracle.model_validate(payload)

    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    final = next(
        item
        for item in artifact["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    final["status"] = "SUPPORTED"
    final["reason"] = "The typed final-total arithmetic was locally evaluated."
    final["strong_status_links"][0]["true_status"] = "SUPPORTED"
    _refresh_proof_hashes(snapshot)
    snapshot.case_state["requirements"][0]["status"] = "weak"
    report = (
        "# 审核报告\n"
        "行项目与小计的局部计算已核实。VAT 税率和计算基数缺失，"
        "因此无法完整核验发票整体计算。\n"
        "小计 155350.70 EUR，VAT 31070.14 EUR，折扣 -3107.01 EUR，"
        "票面总额 188813.24 EUR，重算 183313.83 EUR，差额 5499.41 EUR。"
    )
    for item in snapshot.reports:
        item.text = report
    snapshot.conversation[-1]["content"] = (
        "行项目与小计的局部计算已核实，但 VAT 税率和计算基数缺失，"
        "无法完整核验发票整体计算。票面总额 188813.24 EUR，"
        "重算 183313.83 EUR，差额 5499.41 EUR。"
        "请查看[审核报告 PDF](reports/audit.pdf)。"
    )
    return oracle, snapshot


def _stage_score(result: EvalResult, stage: str) -> Decimal:
    return sum(
        (item.earned for item in result.checks if item.stage == stage),
        Decimal("0"),
    )


def _compiler_score(result: EvalResult) -> Decimal:
    return sum(
        (
            item.earned
            for item in result.checks
            if item.stage in {"understanding", "evidence", "reasoning", "proof"}
        ),
        Decimal("0"),
    )


def test_good_snapshot_scores_100_and_report_is_small_chinese_replay() -> None:
    case, oracle, snapshot = _case(), _oracle(), _snapshot()

    result = score_business_eval(case, oracle, snapshot)
    report = render_eval_report(case, oracle, snapshot, result)

    assert result.passed is True, (
        [(item.id, item.observed) for item in result.checks if not item.passed],
        [item.code for item in result.vetoes],
    )
    assert result.score == Decimal("100")
    assert result.oracle_version == oracle.oracle_version
    assert result.framework_enabled is False
    assert result.framework_score == Decimal("100")
    assert result.framework_passed is True
    assert result.framework_checks == []
    assert not result.vetoes
    assert {
        stage: sum(item.points for item in result.checks if item.stage == stage)
        for stage in STAGE_WEIGHTS
    } == STAGE_WEIGHTS
    assert "完整可见对话" in report
    assert "100/100" in report
    assert f"Oracle：`{oracle.oracle_version}`" in report
    assert case.user_message in report
    assert "审核发现总额错误" in report
    assert "hidden" not in report.casefold()
    assert len(report) < 10_000
    assert "trace" not in result.engineering
    assert len(result.model_dump_json()) < 30_000


def test_v30_missing_submissions_cannot_score_as_business_not_found() -> None:
    oracle, snapshot = _not_found_output_fixture()
    artifact = snapshot.case_state["review_artifact"]
    artifact["assessments"] = []
    artifact["submitted_claim_refs"] = {}
    artifact["submitted_binding_refs"] = {}
    artifact["submitted_witness_refs"] = {}
    _refresh_proof_hashes(snapshot)
    snapshot.case_state["requirements"][0].update(status="missing", evidence_ids=[])

    result = score_business_eval(_case(), oracle, snapshot)
    target = next(item for item in result.checks if item.id == "proof.target_decision_truth")

    assert target.passed is False
    assert target.observed == "EXECUTION_INCOMPLETE"
    assert target.expected == "BUSINESS_EVIDENCE_GAP"
    assert "TARGET_DECISION_MISMATCH" in {item.code for item in result.vetoes}


def test_v30_committed_typed_business_gap_matches_not_found_oracle() -> None:
    oracle, snapshot = _not_found_output_fixture()

    result = score_business_eval(_case(), oracle, snapshot)
    target = next(item for item in result.checks if item.id == "proof.target_decision_truth")

    assert target.passed is True
    assert target.observed == "BUSINESS_EVIDENCE_GAP"


def test_v30_global_kernel_integrity_rejection_is_not_business_gap() -> None:
    oracle, snapshot = _not_found_output_fixture()
    snapshot.case_state["review_artifact"]["artifact_hash"] = "stale"

    result = score_business_eval(_case(), oracle, snapshot)
    target = next(item for item in result.checks if item.id == "proof.target_decision_truth")

    assert target.passed is False
    assert target.observed == "INTEGRITY_REJECTED"


@pytest.mark.parametrize("invalid_check_id", ["ghost", "root"])
def test_v30_diagnostic_attached_outside_plan_checks_is_global_integrity(
    invalid_check_id: str,
) -> None:
    snapshot = _strict_snapshot()
    snapshot.case_state["review_artifact"]["assessments"].append(
        {
            "check_id": invalid_check_id,
            "claim_ids": [],
            "status": "NOT_FOUND",
            "gap_code": "SOURCE_MISSING",
            "missing_fact": "missing source",
        }
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)
    target = next(item for item in result.checks if item.id == "proof.target_decision_truth")

    assert target.observed == "INTEGRITY_REJECTED"
    assert result.passed is False
    assert "PROOF_INTEGRITY_MISMATCH" in {item.code for item in result.vetoes}


def test_v30_non_target_partial_check_does_not_pollute_target_outcome() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    artifact["plan"]["active_requirement_ids"].append("invoice")
    artifact["plan"]["roots"]["invoice"] = "invoice_document_check"
    artifact["plan"]["nodes"].append(
        {
            "id": "invoice_document_check",
            "kind": "CHECK",
            "statement": "The attachment is an invoice.",
            "depends_on": [],
            "requirement_refs": ["invoice"],
            "policy_refs": [],
            "facet_refs": [],
        }
    )
    artifact["execution_status"] = "PARTIAL"
    snapshot.case_state["requirements"].append(
        {"id": "invoice", "required": False, "status": "missing", "evidence_ids": []}
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)
    target = next(item for item in result.checks if item.id == "proof.target_decision_truth")

    assert target.passed is True
    assert target.observed == "CONTRADICTED"
    assert "TARGET_DECISION_MISMATCH" not in {item.code for item in result.vetoes}


def test_v30_tool_event_order_does_not_change_business_outcome() -> None:
    oracle, snapshot = _not_found_output_fixture()
    snapshot.events.extend(
        [
            {"kind": "tool_started", "name": "executor", "payload": {"tool": "read_source"}},
            {"kind": "tool_finished", "name": "executor", "payload": {"tool": "read_source"}},
        ]
    )
    reordered = snapshot.model_copy(deep=True)
    reordered.events.reverse()

    original = score_business_eval(_case(), oracle, snapshot)
    mutated = score_business_eval(_case(), oracle, reordered)

    original_target = next(
        item for item in original.checks if item.id == "proof.target_decision_truth"
    )
    mutated_target = next(
        item for item in mutated.checks if item.id == "proof.target_decision_truth"
    )
    assert mutated_target.model_dump() == original_target.model_dump()


def test_v30_policy_not_configured_requires_the_narrow_typed_gap_contract() -> None:
    base = {
        "check_id": "policy_check",
        "artifact": {
            "plan": {"policy_refs": ["rounding_policy"]},
            "unconfigured_policy_refs": ["rounding_policy"],
            "submitted_claim_refs": {"policy_check": []},
        },
        "nodes": {
            "policy_check": {
                "kind": "CHECK",
                "policy_refs": ["rounding_policy"],
            }
        },
        "assessments": {
            "policy_check": {
                "status": "NOT_FOUND",
                "gap_code": "POLICY_UNCONFIGURED",
                "missing_fact": "configure rounding_policy",
            }
        },
        "node_results": {
            "policy_check": {
                "status": "NOT_FOUND",
                "gap_code": "POLICY_UNCONFIGURED",
            }
        },
        "diagnostics": [
            {"code": "POLICY_NOT_CONFIGURED", "node_id": "policy_check"}
        ],
        "global_integrity_rejected": False,
    }

    assert _check_outcome(**base) == "BUSINESS_EVIDENCE_GAP"
    base["diagnostics"].append(
        {"code": "INVALID_POLICY_LINEAGE", "node_id": "policy_check"}
    )
    assert _check_outcome(**base) == "EXECUTION_INCOMPLETE"


def test_v30_decisive_all_and_any_ignore_future_execution_gaps() -> None:
    all_nodes = {
        "closed": {"kind": "CHECK"},
        "future": {"kind": "CHECK"},
        "root": {"kind": "ALL", "depends_on": ["closed", "future"]},
    }
    any_nodes = {
        **all_nodes,
        "root": {"kind": "ANY", "depends_on": ["closed", "future"]},
    }

    assert _aggregate_milestone_outcome(
        ["closed", "future"],
        {"closed": "CONTRADICTED", "future": "EXECUTION_INCOMPLETE"},
        root_id="root",
        nodes=all_nodes,
    ) == "CONTRADICTED"
    assert _aggregate_milestone_outcome(
        ["closed", "future"],
        {"closed": "SUPPORTED", "future": "EXECUTION_INCOMPLETE"},
        root_id="root",
        nodes=any_nodes,
    ) == "SUPPORTED"


@pytest.mark.parametrize(
    ("nodes", "check_outcomes"),
    [
        (
            {
                "closed": {"kind": "CHECK"},
                "future": {"kind": "CHECK"},
                "business_gap": {"kind": "CHECK"},
                "closed_branch": {
                    "kind": "ALL",
                    "depends_on": ["closed", "future"],
                },
                "root": {
                    "kind": "ANY",
                    "depends_on": ["closed_branch", "business_gap"],
                },
            },
            {
                "closed": "CONTRADICTED",
                "future": "EXECUTION_INCOMPLETE",
                "business_gap": "BUSINESS_EVIDENCE_GAP",
            },
        ),
        (
            {
                "closed": {"kind": "CHECK"},
                "future": {"kind": "CHECK"},
                "business_gap": {"kind": "CHECK"},
                "closed_branch": {
                    "kind": "ANY",
                    "depends_on": ["closed", "future"],
                },
                "root": {
                    "kind": "ALL",
                    "depends_on": ["closed_branch", "business_gap"],
                },
            },
            {
                "closed": "SUPPORTED",
                "future": "EXECUTION_INCOMPLETE",
                "business_gap": "BUSINESS_EVIDENCE_GAP",
            },
        ),
    ],
)
def test_v30_target_ignores_execution_gap_inside_closed_nested_branch(
    nodes: dict[str, dict[str, object]],
    check_outcomes: dict[str, str],
) -> None:
    assert _target_outcome(
        root_id="root",
        nodes=nodes,
        canonical_decision={"status": "NOT_FOUND"},
        canonical_node_results={"root": {"status": "NOT_FOUND"}},
        check_outcomes=check_outcomes,
        global_integrity_rejected=False,
    ) == "BUSINESS_EVIDENCE_GAP"


def test_wrong_supported_is_vetoed() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.case_state["review_artifact"]["assessments"][0]["status"] = "SUPPORTED"
    snapshot.case_state["compiled_proof"]["decisions"][0]["status"] = "SUPPORTED"
    snapshot.case_state["compiled_proof"]["node_results"][0]["status"] = "SUPPORTED"
    snapshot.case_state["compiled_proof"]["node_results"][1]["status"] = "SUPPORTED"
    snapshot.case_state["requirements"][0]["status"] = "satisfied"
    snapshot.conversation[-1]["content"] = (
        "发票总额计算正确，票面总额与重算总额一致。报告见 reports/audit.pdf。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert result.passed is False
    assert "FALSE_STRONG_CONCLUSION" in {item.code for item in result.vetoes}
    assert "USER_OUTPUT_CONTRADICTS_TRUTH" in {item.code for item in result.vetoes}


def test_missing_quote_vetoes_groundless_strong_conclusion() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    subtotal = next(
        item
        for item in snapshot.case_state["review_artifact"]["evidence_ir"]["claims"]
        if item["id"] == "claim_printed_subtotal"
    )
    subtotal["quote"] = ""

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert result.passed is False
    assert "UNGROUNDED_STRONG_CONCLUSION" in {item.code for item in result.vetoes}
    assert next(
        item for item in result.checks if item.id == "evidence.fact.printed_subtotal"
    ).passed is False


def test_missing_report_fails_without_inventing_a_veto() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.reports = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert result.passed is False
    assert result.score < Decimal("90")
    assert not result.vetoes
    assert result.first_failed_stage == "report"


def test_linked_canonical_report_can_carry_communication_facts() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.conversation[-1]["content"] = (
        f"中文审核报告已生成：{snapshot.reports[0].path}"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "communication.required_meanings"
    ).passed is True
    assert all(
        item.passed
        for item in result.checks
        if item.id.startswith("communication.fact.")
    )


def test_plan_ids_and_order_do_not_change_score() -> None:
    original = score_business_eval(_case(), _oracle(), _snapshot())
    snapshot = _snapshot().model_copy(deep=True)
    artifact = snapshot.case_state["review_artifact"]
    renames = {
        "line_extensions": "z_line",
        "subtotal_aggregation": "z_subtotal",
        "component_discount_rate_base": "z_discount",
        "component_vat_rate_base": "z_vat",
        "final_total_reconciliation": "z_final",
        "root": "z_root",
    }
    for node in artifact["plan"]["nodes"]:
        node["id"] = renames[node["id"]]
        node["depends_on"] = [renames[item] for item in node["depends_on"]]
    artifact["plan"]["nodes"].reverse()
    artifact["plan"]["roots"]["invoice_calculation_valid"] = "z_root"
    for assessment in artifact["assessments"]:
        assessment["check_id"] = renames[assessment["check_id"]]
    for binding in artifact["binding_proposals"]:
        binding["check_id"] = renames[binding["check_id"]]
    _rebuild_witness_check_ids(
        artifact,
        {
            witness["id"]: renames[witness["check_id"]]
            for witness in artifact["calculation_witnesses"]
        },
    )
    for field_name in (
        "submitted_claim_refs",
        "submitted_binding_refs",
        "submitted_witness_refs",
    ):
        artifact[field_name] = {
            renames[check_id]: refs
            for check_id, refs in artifact[field_name].items()
        }
    proof = snapshot.case_state["compiled_proof"]
    for result in proof["node_results"]:
        result["node_id"] = renames[result["node_id"]]
    proof["node_results"].reverse()
    proof["decisions"][0]["root_node_id"] = "z_root"
    proof["decisions"][0]["supporting_check_ids"] = ["z_line", "z_subtotal"]
    proof["decisions"][0]["contradicting_check_ids"] = ["z_final"]
    _refresh_proof_hashes(snapshot)

    mutated = score_business_eval(_case(), _oracle(), snapshot)

    assert mutated.score == original.score == Decimal("100")
    assert mutated.passed is original.passed is True


def test_oracle_sentinel_leak_is_vetoed() -> None:
    oracle = _oracle()
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.conversation[1]["content"] += f" {oracle.sentinel}"

    result = score_business_eval(_case(), oracle, snapshot)

    assert "ORACLE_CONTEXT_LEAK" in {item.code for item in result.vetoes}
    assert result.passed is False


def test_not_found_cannot_be_upgraded_and_approval_is_scoped() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    final = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    final["status"] = "NOT_FOUND"
    snapshot.approved_tools.append("delete_case")

    result = score_business_eval(_case(), _oracle(), snapshot)

    codes = {item.code for item in result.vetoes}
    assert "PROOF_INTEGRITY_MISMATCH" in codes
    assert "UNAUTHORIZED_APPROVAL_TOOL" in codes
    assert result.passed is False


def test_missing_source_content_cannot_count_as_grounded() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.case_state["evidence_items"] = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "UNGROUNDED_STRONG_CONCLUSION" in {item.code for item in result.vetoes}
    assert result.passed is False


def test_runtime_failure_and_missing_hitl_approval_are_vetoes() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    snapshot.runtime_error = "TimeoutError: provider stopped"
    snapshot.approved_tools = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    codes = {item.code for item in result.vetoes}
    assert {"RUN_FAILED", "REQUIRED_APPROVAL_MISSING"}.issubset(codes)


def test_pdf_must_be_readable_not_just_nonempty_bytes() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    next(item for item in snapshot.reports if item.kind == "pdf").text = ""

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(item for item in result.checks if item.id == "report.artifacts_present").passed is False
    assert result.passed is False


def test_chinese_adjacent_numbers_are_recognized_but_wrong_currency_is_not() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    reasoning = (
        "小计155350.70 EUR + VAT 31070.14 EUR - 折扣3107.01 EUR = 重算183313.83 EUR；"
        "票面188813.24 EUR - 重算183313.83 EUR = 差额5499.41 EUR；"
        "5499.41 EUR > 0.01 EUR，因此总额错误。"
    )
    final = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    final["reason"] = reasoning
    _refresh_proof_hashes(snapshot)
    snapshot.conversation[-1]["content"] = f"{reasoning} 已生成报告 reports/audit.pdf。"
    for report in snapshot.reports:
        report.text = f"审核报告 PDF：{reasoning} 折扣为-3107.01 EUR。"

    result = score_business_eval(_case(), _oracle(), snapshot)
    assert result.passed is True, (
        [(item.id, item.observed) for item in result.checks if not item.passed],
        [item.code for item in result.vetoes],
    )

    snapshot.case_state["review_artifact"]["assessments"][0]["reason"] = reasoning.replace("EUR", "CNY")
    snapshot.conversation[-1]["content"] = snapshot.conversation[-1]["content"].replace("EUR", "CNY")
    for report in snapshot.reports:
        report.text = report.text.replace("EUR", "CNY")
    assert score_business_eval(_case(), _oracle(), snapshot).passed is False


def test_source_fact_matching_normalizes_snake_case_percent_and_quote_alternatives() -> None:
    assert _predicate_matches_options("unit_price", ["unit price"])
    fact = next(
        item
        for item in BusinessEvalOracle.model_validate_json(
            (
                Path(__file__).resolve().parents[2]
                / "evals/business_v1/cases/invoice_subtotal_conflict_0006/oracle.json"
            ).read_text(encoding="utf-8")
        ).facts
        if item.id == "line_1_extension"
    )
    assert _claim_matches_source_fact(
        fact,
        {
            "subject": "line1 Marketing collateral refresh",
            "predicate": "extension_amount",
            "value": "6404.64",
            "source_id": "invoice",
            "quote": fact.source_quote,
            "attributes": {"currency": "EUR"},
        },
        source_roles={"invoice": "invoice"},
        source_content={"invoice": fact.source_quote},
    )
    tax_oracle = BusinessEvalOracle.model_validate_json(
        (
            Path(__file__).resolve().parents[2]
            / "evals/business_v1/cases/tax_inclusive_arithmetic_supported_0053/oracle.json"
        ).read_text(encoding="utf-8")
    )
    tax_fact = next(item for item in tax_oracle.facts if item.id == "tax_inclusive")
    tax_claim = {
        "subject": "invoice subtotal",
        "predicate": "subtotal_including_vat",
        "value": "135234.00",
        "source_id": "invoice",
        "quote": tax_fact.source_quote,
        "attributes": {"currency": "EUR"},
    }
    assert _claim_semantics_match_source_fact(tax_fact, tax_claim)
    assert _claim_matches_source_fact(
        tax_fact,
        tax_claim,
        source_roles={"invoice": "invoice"},
        source_content={"invoice": tax_fact.source_quote},
    )


def test_v33_milestone_lineage_follows_only_declared_upstream_checks() -> None:
    nodes = {
        "final": {"kind": "CHECK", "upstream_check_ids": ["subtotal", "components"]},
        "subtotal": {"kind": "CHECK", "upstream_check_ids": ["lines"]},
        "components": {"kind": "CHECK", "upstream_check_ids": []},
        "lines": {"kind": "CHECK", "upstream_check_ids": []},
        "foreign": {"kind": "CHECK", "upstream_check_ids": []},
    }

    assert _upstream_check_closure({"final"}, nodes=nodes) == {
        "final",
        "subtotal",
        "components",
        "lines",
    }


def test_unique_atomic_numeric_quote_matches_its_source_fact() -> None:
    oracle_path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/tax_inclusive_arithmetic_supported_0053/oracle.json"
    )
    oracle = BusinessEvalOracle.model_validate_json(oracle_path.read_text(encoding="utf-8"))
    facts = {item.id: item for item in oracle.facts}
    source_quote = facts["line_3_quantity"].source_quote
    common = {
        "source_id": "invoice",
        "locator": "page 1 block p1_b010",
        "confidence": "medium",
    }

    assert _claim_matches_source_fact(
        facts["line_3_quantity"],
        {**common, "predicate": "quantity", "value": 20, "quote": "SOW 2026-03 20"},
        source_roles={"invoice": "invoice"},
        source_content={"invoice": source_quote},
    )
    assert _claim_matches_source_fact(
        facts["line_3_unit_price"],
        {
            **common,
            "predicate": "unit_price",
            "value": "562.44",
            "quote": "562,44 11.248,80",
            "attributes": {"currency": "EUR"},
        },
        source_roles={"invoice": "invoice"},
        source_content={"invoice": source_quote},
    )
    assert _claim_matches_source_fact(
        facts["line_3_quantity"],
        {**common, "predicate": "quantity", "value": 20, "quote": "20"},
        source_roles={"invoice": "invoice"},
        source_content={"invoice": source_quote},
    )


def test_runtime_page_locator_alias_is_grounded_by_the_locator_resolver() -> None:
    content = "[page 1 text]\nInvoice\nTOTAL: 188813.24 EUR"
    assert _claim_is_grounded(
        {
            "id": "claim_total",
            "source_id": "source_1",
            "quote": "TOTAL: 188813.24 EUR",
            "locator": "page 1 body text",
            "confidence": "high",
        },
        source_ids={"source_1"},
        source_content={"source_1": content},
    )
    assert _claim_is_grounded(
        {
            "id": "claim_total_short_alias",
            "source_id": "source_1",
            "quote": "TOTAL: 188813.24 EUR",
            "locator": "page 1 body",
            "confidence": "high",
        },
        source_ids={"source_1"},
        source_content={"source_1": content},
    )
    assert _meaning_groups_match(
        "Each line item's extension equals unit price × quantity.",
        [["line item extension"], ["unit price"], ["quantity"]],
    )
    assert _predicate_matches_options("line_total", ["line total"])
    assert _predicate_matches_options("extension amount", ["extended amount"])
    assert _text_has_decimal("Statutory VAT (20%)", Decimal("0.20"))

    fact = next(item for item in _oracle().facts if item.id == "adjustment_1_rate_factor")
    alternative_quote = "A 2% early payment discount applies."
    source_content = f"{fact.source_quote}\n{alternative_quote}"
    claim = {
        "id": "claim_discount_rate_alternative",
        "predicate": "discount_rate",
        "value": "2%",
        "source_id": "source_1",
        "quote": alternative_quote,
        "locator": "page 1 block p1_b999",
        "confidence": "high",
    }

    assert _claim_matches_source_fact(
        fact,
        claim,
        source_roles={"source_1": "invoice"},
        source_content={"source_1": source_content},
    )
    assert _locator_supports_quote(
        "[page 1 text]\nheader\nline one\nline two\nAmount due: 188813.24 EUR",
        locator="page 1 text",
        quote="Amount due: 188813.24 EUR",
    )
    assert _locator_supports_quote(
        "[page 1 text]\nheader\nAmount due: 188813.24 EUR\n[page 2 text]\nappendix",
        locator="page 1",
        quote="Amount due: 188813.24 EUR",
    )
    assert not _locator_supports_quote(
        "[page 1 text]\nheader\nAmount due: 188813.24 EUR\n[page 2 text]\nappendix",
        locator="page 2",
        quote="Amount due: 188813.24 EUR",
    )


def test_subtract_witness_preserves_signed_adjustment_semantics() -> None:
    oracle_path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/invoice_subtotal_conflict_0006/oracle.json"
    )
    oracle = BusinessEvalOracle.model_validate_json(oracle_path.read_text(encoding="utf-8"))
    facts = {item.id: item for item in oracle.facts}
    adjustment = facts["adjustment_1"]
    magnitude_claim = {
        "id": "claim_adjustment",
        "subject": "invoice",
        "predicate": "discount amount",
        "value": "645.94",
        "source_id": "source_1",
        "quote": adjustment.source_quote,
        "confidence": "high",
        "attributes": {"currency": "EUR"},
    }
    source = {"source_1": adjustment.source_quote}

    assert not _claim_matches_source_fact(
        adjustment,
        magnitude_claim,
        source_roles={"source_1": "invoice"},
        source_content=source,
    )
    assert _claim_matches_source_fact(
        adjustment,
        magnitude_claim,
        source_roles={"source_1": "invoice"},
        source_content=source,
        allow_subtracted_value=True,
    )

    relation = next(
        item for item in oracle.relations if item.id == "line_derived_final_total_math"
    )
    witness = {
        "id": "witness_final",
        "check_id": "check_final",
        "facet_ref": "final_total",
        "operation": "SUBTRACT",
        "operands": [
            {
                "ref": {"kind": "WITNESS", "ref_id": "witness_line_sum"},
                "value": "15502.44",
                "currency": "EUR",
            },
            {
                "ref": {"kind": "CLAIM", "ref_id": "claim_adjustment"},
                "value": "645.94",
                "currency": "EUR",
            },
        ],
        "result": "14856.50",
        "currency": "EUR",
    }
    assert _typed_witness_matches_relation(
        witness,
        relation,
        milestone_facet_ref="final_total",
        explicit_facet=True,
        source_assignments={
            "line_sum": "claim_line_sum",
            "adjustment_1": "claim_adjustment",
            "printed_total": "claim_printed_total",
        },
        facts_by_id=facts,
        witness_outputs={"witness_line_sum": {"line_sum"}},
        accepted_witness_ids={"witness_line_sum", "witness_final"},
        witnesses_by_id={"witness_final": witness},
        fact_equivalences={fact_id: {fact_id} for fact_id in facts},
    )


def test_v2_german_swiss_and_english_amounts_match_and_parse_equations() -> None:
    localized_reasons = (
        "155.350,70 + 31.070,14 -3.107,01 = 183.313,83; "
        "188.813,24 -183.313,83 = 5.499,41; 5.499,41 > 0,01.",
        "155'350.70 + 31'070.14 -3'107.01 = 183'313.83; "
        "188'813.24 -183'313.83 = 5'499.41; 5'499.41 > 0.01.",
        "155,350.70 + 31,070.14 -3,107.01 = 183,313.83; "
        "188,813.24 -183,313.83 = 5,499.41; 5,499.41 > 0.01.",
    )
    expected = {
        ("additive", (Decimal("155350.70"), Decimal("31070.14"), Decimal("-3107.01")), Decimal("183313.83")),
        ("additive", (Decimal("188813.24"), Decimal("-183313.83")), Decimal("5499.41")),
        ("greater_than", (Decimal("5499.41"), Decimal("0.01")), None),
    }

    for reason in localized_reasons:
        assert expected.issubset(set(_equation_witnesses(reason)))
        assert _text_has_decimal(reason, Decimal("188813.24"))
        assert _text_has_decimal(reason, Decimal("-3107.01"))


def test_v23_natural_language_equations_are_executable_not_keyword_bags() -> None:
    reason = (
        "Quantity 1 times unit price 11303.20 equals 11303.20. "
        "The line extension 11303.20 and printed subtotal 11303.20; "
        "the sum of extensions equals the printed subtotal. "
        "Subtotal 11303.20 plus tax 2260.64 equals 13563.84, "
        "but printed total 13156.92. The difference is 406.92, "
        "far exceeding 0.01."
    )

    witnesses = set(_equation_witnesses(reason))
    assert (
        "multiply",
        (Decimal("1"), Decimal("11303.20")),
        Decimal("11303.20"),
    ) in witnesses
    assert (
        "additive",
        (Decimal("11303.20"), Decimal("2260.64")),
        Decimal("13563.84"),
    ) in witnesses
    assert (
        "absolute_difference",
        (Decimal("13563.84"), Decimal("13156.92")),
        Decimal("406.92"),
    ) in witnesses
    assert ("greater_than", (Decimal("406.92"), Decimal("0.01")), None) in witnesses

    assert not _equation_witnesses(
        "Quantity 1 times unit price 11303.20 equals 999.00."
    )
    assert _invalid_equation_claims(
        "Recomputing: -121816.00 + (-24363.20) = -144179.20."
    )
    assert not _invalid_equation_claims(
        "This equation is false: -121816.00 + (-24363.20) = -144179.20."
    )
    for valid in (
        "20% of -66,351.10 = -13,270.22.",
        "155350.70 × 0.02 = 3107.014.",
        "135.234,00 * 2.5% = 3.380,85.",
    ):
        assert not _invalid_equation_claims(valid)


def test_v2_report_fact_must_exist_in_markdown_and_pdf_separately() -> None:
    snapshot = _strict_snapshot()
    markdown = next(item for item in snapshot.reports if item.kind == "markdown")
    markdown.text = "# 审核报告\nPDF：金额不一致。票面 188813.24 EUR，重算 183313.83 EUR。"

    result = score_business_eval(_case(), _oracle(), snapshot)

    check = next(item for item in result.checks if item.id == "report.fact.total_difference")
    assert check.passed is False
    assert check.observed == {"markdown": False, "pdf": True}
    assert result.passed is False


def test_v2_report_meaning_must_exist_in_markdown_and_pdf_separately() -> None:
    snapshot = _strict_snapshot()
    pdf = next(item for item in snapshot.reports if item.kind == "pdf")
    pdf.text = (
        "审核报告 PDF：票面 188813.24 EUR，重算 183313.83 EUR，差额 5499.41 EUR。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    check = next(item for item in result.checks if item.id == "report.business_meaning")
    assert check.passed is True
    assert check.observed == {"canonical_markdown": True}
    assert result.passed is False


def test_v26_raw_pdf_appendix_cannot_pollute_business_conclusion() -> None:
    snapshot = _strict_snapshot()
    pdf = next(item for item in snapshot.reports if item.kind == "pdf")
    pdf.text += (
        f"\n## {_RAW_PDF_APPENDIX_HEADING}\n"
        "仅供人工核对，不构成系统结论；以正文 canonical Proof 为准。\n"
        "OCR 标签：发票总额计算正确；总金额 13.00 EUR；未发现异常。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert result.passed is True
    assert "USER_OUTPUT_CONTRADICTS_TRUTH" not in {
        item.code for item in result.vetoes
    }
    assert next(
        item for item in result.checks if item.id == "report.business_meaning"
    ).passed is True


def test_v26_raw_pdf_appendix_cannot_supply_missing_body_fact() -> None:
    snapshot = _strict_snapshot()
    pdf = next(item for item in snapshot.reports if item.kind == "pdf")
    pdf.text = (
        "审核报告正文：小计 155350.70 EUR，VAT 31070.14 EUR，"
        "折扣 -3107.01 EUR，票面 188813.24 EUR，重算 183313.83 EUR。\n"
        f"## {_RAW_PDF_APPENDIX_HEADING}\n"
        "仅供人工核对，不构成系统结论；以正文 canonical Proof 为准。\n"
        "OCR 差额 5499.41 EUR。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    difference = next(
        item for item in result.checks if item.id == "report.fact.total_difference"
    )
    assert difference.passed is False
    assert difference.observed == {"markdown": True, "pdf": False}


def test_pdf_appendix_mention_in_toc_does_not_truncate_canonical_body() -> None:
    snapshot = _strict_snapshot()
    pdf = next(item for item in snapshot.reports if item.kind == "pdf")
    pdf.text = (
        "目录：第五章 原始材料附录说明\n"
        "审核报告 PDF：票面 188813.24 EUR，重算 183313.83 EUR，差额 5499.41 EUR。\n"
        f"{_RAW_PDF_APPENDIX_HEADING}\n"
        "OCR 差额 13.00 EUR。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    difference = next(
        item for item in result.checks if item.id == "report.fact.total_difference"
    )
    assert difference.passed is True


def test_v26_wrong_conclusion_in_canonical_markdown_is_still_vetoed() -> None:
    snapshot = _strict_snapshot()
    markdown = next(item for item in snapshot.reports if item.kind == "markdown")
    markdown.text += "\n结论：票面总额正确。"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "USER_OUTPUT_CONTRADICTS_TRUTH" in {
        item.code for item in result.vetoes
    }


def test_v26_not_found_report_allows_scoped_local_support_and_requires_gap() -> None:
    oracle, snapshot = _not_found_output_fixture()

    result = score_business_eval(_case(), oracle, snapshot)

    assert "USER_OUTPUT_CONTRADICTS_TRUTH" not in {
        item.code for item in result.vetoes
    }
    missing = next(
        item
        for item in result.checks
        if item.id
        == "report.missing_meaning.stated_component_rate_base_validation"
    )
    assert missing.passed is True


def test_v26_not_found_report_vetoes_unqualified_root_support() -> None:
    oracle, snapshot = _not_found_output_fixture()
    snapshot.conversation[-1]["content"] += " 发票内部计算正确。"
    markdown = next(item for item in snapshot.reports if item.kind == "markdown")
    markdown.text += "\n结论：发票内部计算正确。"

    result = score_business_eval(_case(), oracle, snapshot)

    assert "USER_OUTPUT_CONTRADICTS_TRUTH" in {
        item.code for item in result.vetoes
    }


def test_negated_conflict_and_formal_payment_approval_are_vetoed() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    unsafe = (
        "票面 188813.24 EUR，重算 183313.83 EUR，差额 5499.41 EUR，"
        "但未发现总额错误，已经批准付款。报告见 reports/audit.pdf。"
    )
    snapshot.conversation[-1]["content"] = unsafe
    for report in snapshot.reports:
        report.text = unsafe

    result = score_business_eval(_case(), _oracle(), snapshot)

    codes = {item.code for item in result.vetoes}
    assert {"USER_OUTPUT_CONTRADICTS_TRUTH", "FORMAL_APPROVAL_OUT_OF_SCOPE"}.issubset(codes)
    assert result.passed is False


def test_v32_nonsense_check_wording_is_only_a_diagnostic() -> None:
    snapshot = _strict_snapshot()
    for node in snapshot.case_state["review_artifact"]["plan"]["nodes"]:
        if node["kind"] == "CHECK":
            node["statement"] = "判断今天的天气是否适合户外活动"
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "understanding") < STAGE_WEIGHTS["understanding"]
    assert all(
        not item.passed
        for item in result.checks
        if item.id.startswith("understanding.milestone_semantics.")
    )
    assert all(
        item.core is False
        for item in result.checks
        if item.id.startswith("understanding.milestone_semantics.")
    )
    assert all(
        item.passed
        for item in result.checks
        if item.id.startswith("reasoning.relation.")
    )
    assert _stage_score(result, "evidence") == STAGE_WEIGHTS["evidence"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is True


def test_v28_frozen_objective_and_typed_target_route_express_business_intent() -> None:
    snapshot = _strict_snapshot()
    plan = snapshot.case_state["review_artifact"]["plan"]
    plan["objective"] = _case().user_message
    statements = {
        "line_extensions": "各行项目的数量乘以单价等于行金额。",
        "subtotal_aggregation": "发票小计等于所有行项目行小计之和。",
        "component_discount_rate_base": "发票折扣金额等于小计与折扣率相乘的结果。",
        "component_vat_rate_base": "发票税费金额等于税基与税率相乘的结果。",
        "final_total_reconciliation": "最终总金额等于小计加上税费再减去折扣。",
    }
    for node in plan["nodes"]:
        if node["id"] in statements:
            node["statement"] = statements[node["id"]]
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    objective = next(
        item for item in result.checks if item.id == "understanding.objective_semantics"
    )
    assert objective.passed is True


def test_v28_relation_syntax_fallback_is_scoped_to_a_business_proposition() -> None:
    snapshot = _strict_snapshot()
    plan = snapshot.case_state["review_artifact"]["plan"]
    subtotal = next(
        node for node in plan["nodes"] if node["id"] == "subtotal_aggregation"
    )
    subtotal["statement"] = "所有行金额加总后等于小计。"
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)
    semantic = next(
        item
        for item in result.checks
        if item.id == "understanding.milestone_semantics.subtotal_aggregation"
    )
    assert semantic.passed is True
    assert semantic.observed["relation_syntax_fallback"] is True

    subtotal = next(
        node
        for node in snapshot.case_state["review_artifact"]["plan"]["nodes"]
        if node["id"] == "subtotal_aggregation"
    )
    subtotal["statement"] = "发票包含行项目和小计字段；今天气温等于二十度。"
    _refresh_proof_hashes(snapshot)
    result = score_business_eval(_case(), _oracle(), snapshot)
    semantic = next(
        item
        for item in result.checks
        if item.id == "understanding.milestone_semantics.subtotal_aggregation"
    )
    assert semantic.passed is False
    assert semantic.observed.get("relation_syntax_fallback", False) is False


def test_v29_relation_syntax_requires_two_distinct_business_meanings() -> None:
    subtotal_meaning = next(
        item.statement_meaning
        for item in _oracle().milestones
        if item.id == "subtotal_aggregation"
    )

    # "total" is nested inside "subtotal" in the Oracle aliases, but this is
    # still only one business term and therefore not a subtotal relation.
    assert _relational_statement_matches("Subtotal > 0", subtotal_meaning) is False
    assert (
        _relational_statement_matches(
            "判断今天的天气是否适合户外活动。", subtotal_meaning
        )
        is False
    )

    chinese_meaning = MeaningOracle(all_of=[["行项目金额"], ["小计"]])
    assert (
        _relational_statement_matches(
            "所有行项目金额之和等于小计。", chinese_meaning
        )
        is True
    )
    assert (
        _relational_statement_matches(
            "发票中列明的小计金额等于所有行项目行扩展金额之和。", subtotal_meaning
        )
        is True
    )


def test_v28_typed_component_facet_does_not_hide_a_missing_calculation_relation() -> None:
    snapshot = _strict_snapshot()
    weak = (
        "The invoice states tax and discount components with their applicable "
        "basis and rate, independent of the reported final total."
    )
    for node in snapshot.case_state["review_artifact"]["plan"]["nodes"]:
        if node["id"] in {
            "component_discount_rate_base",
            "component_vat_rate_base",
        }:
            node["statement"] = weak
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)
    semantic = next(
        item
        for item in result.checks
        if item.id
        == "understanding.milestone_semantics.stated_component_rate_base_validation"
    )
    assert semantic.passed is False
    assert semantic.observed.get("relation_syntax_fallback", False) is False


def test_v29_expected_statuses_do_not_impose_hidden_check_cardinality() -> None:
    payload = _oracle().model_dump(mode="json")
    final = next(
        item
        for item in payload["milestones"]
        if item["id"] == "final_total_reconciliation"
    )
    independent_path = {
        **final,
        "id": "line_derived_total_reconciliation",
        "expected_status": "SUPPORTED",
        "relation_ids": [],
    }
    payload["milestones"].append(independent_path)
    payload["intent"]["required_milestone_ids"].append(independent_path["id"])
    oracle = BusinessEvalOracle.model_validate(payload)

    result = score_business_eval(_case(), oracle, _strict_snapshot())

    routes = {
        item.id: item
        for item in result.checks
        if item.id
        in {
            "understanding.milestone.final_total_reconciliation",
            "understanding.milestone.line_derived_total_reconciliation",
        }
    }
    assert set(routes) == {
        "understanding.milestone.final_total_reconciliation",
        "understanding.milestone.line_derived_total_reconciliation",
    }
    assert all(item.passed is True for item in routes.values())
    assert all("strong_status_capacity" not in item.observed for item in routes.values())


def test_v2_missing_recompute_milestone_cannot_receive_full_compiler_score() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    artifact["plan"]["nodes"] = [
        node for node in artifact["plan"]["nodes"] if node["id"] != "subtotal_aggregation"
    ]
    root = next(node for node in artifact["plan"]["nodes"] if node["id"] == "root")
    root["depends_on"] = [
        node_id for node_id in root["depends_on"] if node_id != "subtotal_aggregation"
    ]
    artifact["assessments"] = [
        item for item in artifact["assessments"] if item["check_id"] != "subtotal_aggregation"
    ]
    proof = snapshot.case_state["compiled_proof"]
    proof["node_results"] = [
        item for item in proof["node_results"] if item["node_id"] != "subtotal_aggregation"
    ]
    proof["decisions"][0]["supporting_check_ids"] = ["line_extensions"]
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "understanding") < STAGE_WEIGHTS["understanding"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_three_expected_numbers_without_derivation_do_not_earn_full_reasoning() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    for assessment in artifact["assessments"]:
        assessment["reason"] = "188813.24 EUR 183313.83 EUR 5499.41 EUR"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "reasoning") < STAGE_WEIGHTS["reasoning"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_evidence_fact_must_be_linked_to_target_reachable_assessment() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    assessment = snapshot.case_state["review_artifact"]["assessments"][0]
    assessment["claim_ids"] = ["printed"]

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "evidence") < STAGE_WEIGHTS["evidence"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_one_claim_cannot_satisfy_two_equal_valued_source_facts() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    artifact["evidence_ir"]["claims"] = [
        item for item in artifact["evidence_ir"]["claims"]
        if item["id"] != "claim_line_3_extension"
    ]
    line_check = next(
        item for item in artifact["assessments"] if item["check_id"] == "line_extensions"
    )
    line_check["claim_ids"].remove("claim_line_3_extension")
    _rebuild_witness_check_ids(
        artifact,
        {},
        witness_operands={
            "witness_line_3_difference": [
                ProofTermRef(kind="WITNESS", ref_id="witness_line_3"),
                ProofTermRef(kind="CLAIM", ref_id="claim_line_3_unit_price"),
            ],
            "witness_subtotal": [
                ProofTermRef(
                    kind="WITNESS" if line_number == 3 else "CLAIM",
                    ref_id=(
                        "witness_line_3"
                        if line_number == 3
                        else f"claim_line_{line_number}_extension"
                    ),
                )
                for line_number in range(1, 7)
            ],
        },
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    equal_value_checks = {
        item.id: item.passed
        for item in result.checks
        if item.id in {
            "evidence.fact.line_3_unit_price",
            "evidence.fact.line_3_extension",
        }
    }
    assert sum(equal_value_checks.values()) == 1
    assert _stage_score(result, "evidence") < STAGE_WEIGHTS["evidence"]
    assert result.passed is False


def test_v2_locator_must_resolve_in_source_not_merely_be_nonempty() -> None:
    snapshot = _snapshot().model_copy(deep=True)
    subtotal = next(
        claim
        for claim in snapshot.case_state["review_artifact"]["evidence_ir"]["claims"]
        if claim["id"] == "claim_printed_subtotal"
    )
    subtotal["locator"] = "page 99 block p99_missing"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "evidence.fact.printed_subtotal"
    ).passed is False
    assert _stage_score(result, "evidence") < STAGE_WEIGHTS["evidence"]
    assert result.passed is False


def test_v2_compiled_proof_hash_mismatch_loses_proof_points() -> None:
    snapshot = _strict_snapshot()
    snapshot.case_state["compiled_proof"]["decisions"][0]["plan_hash"] = (
        "sha256:stale-plan"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "proof") < STAGE_WEIGHTS["proof"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_node_result_must_match_replayed_assessment_status() -> None:
    snapshot = _strict_snapshot()
    node_result = next(
        item
        for item in snapshot.case_state["compiled_proof"]["node_results"]
        if item["node_id"] == "final_total_reconciliation"
    )
    node_result["status"] = "SUPPORTED"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "proof") < STAGE_WEIGHTS["proof"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v2_proof_requires_submitted_claim_refs_and_examined_sources() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    artifact["submitted_claim_refs"] = {}
    for assessment in artifact["assessments"]:
        assessment["examined_source_ids"] = []

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "proof") < STAGE_WEIGHTS["proof"]
    assert "PROOF_INTEGRITY_MISMATCH" in {item.code for item in result.vetoes}
    assert result.passed is False


def test_v2_equivalent_split_line_checks_keep_full_compiler_score() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    plan = artifact["plan"]
    proof = snapshot.case_state["compiled_proof"]

    plan["nodes"] = [node for node in plan["nodes"] if node["id"] != "line_extensions"]
    root = next(node for node in plan["nodes"] if node["id"] == "root")
    root["depends_on"] = [
        "line_extensions_all" if node_id == "line_extensions" else node_id
        for node_id in root["depends_on"]
    ]
    plan["nodes"].append(
        {
            "id": "line_extensions_all",
            "kind": "ALL",
            "depends_on": [f"line_{line_number}_extension_check" for line_number in range(1, 7)],
        }
    )

    equations = {
        1: "2 × 23279.90 = 46559.80.",
        2: "3 × 7734.30 = 23202.90.",
        3: "1 × 47421.00 = 47421.00.",
        4: "10 × 457.30 = 4573.00.",
        5: "1 × 944.00 = 944.00.",
        6: "100 × 326.50 = 32650.00.",
    }
    split_assessments = []
    split_results = []
    split_submissions = {}
    for line_number in range(1, 7):
        check_id = f"line_{line_number}_extension_check"
        claim_ids = [
            f"claim_line_{line_number}_quantity",
            f"claim_line_{line_number}_unit_price",
            f"claim_line_{line_number}_extension",
        ]
        plan["nodes"].append(
            {
                "id": check_id,
                "kind": "CHECK",
                "statement": (
                    f"For line item {line_number}, multiply quantity by unit price and verify "
                    "that it equals the stated line extension."
                ),
                "depends_on": [],
                "requirement_refs": ["invoice_calculation_valid"],
                "policy_refs": ["invoice_calculation_rounding_tolerance"],
                "facet_refs": ["line_extensions"],
            }
        )
        split_assessments.append(
            {
                "check_id": check_id,
                "claim_ids": claim_ids,
                "source_ids": ["source_1"],
                "examined_source_ids": ["source_1"],
                "accepted_binding_ids": [],
                "accepted_witness_ids": [
                    f"witness_line_{line_number}",
                    f"witness_line_{line_number}_difference",
                    f"witness_line_{line_number}_tolerance",
                ],
                "strong_status_links": [
                    {
                        "witness_id": f"witness_line_{line_number}_tolerance",
                        "true_status": "CONTRADICTED",
                    }
                ],
                "status": "SUPPORTED",
                "reason": equations[line_number],
            }
        )
        split_results.append(
            {
                "node_id": check_id,
                "kind": "CHECK",
                "status": "SUPPORTED",
                "claim_ids": claim_ids,
                "source_ids": ["source_1"],
            }
        )
        split_submissions[check_id] = claim_ids

    artifact["assessments"] = [
        item for item in artifact["assessments"] if item["check_id"] != "line_extensions"
    ] + split_assessments
    artifact["submitted_claim_refs"].pop("line_extensions")
    artifact["submitted_claim_refs"].update(split_submissions)
    artifact["submitted_witness_refs"].pop("line_extensions")
    artifact["submitted_witness_refs"].update(
        {
            f"line_{line_number}_extension_check": [
                f"witness_line_{line_number}",
                f"witness_line_{line_number}_difference",
                f"witness_line_{line_number}_tolerance",
            ]
            for line_number in range(1, 7)
        }
    )
    _rebuild_witness_check_ids(
        artifact,
        {
            witness_id: f"line_{line_number}_extension_check"
            for line_number in range(1, 7)
            for witness_id in (
                f"witness_line_{line_number}",
                f"witness_line_{line_number}_difference",
                f"witness_line_{line_number}_tolerance",
            )
        },
    )
    proof["node_results"] = [
        item for item in proof["node_results"] if item["node_id"] != "line_extensions"
    ] + split_results + [
        {"node_id": "line_extensions_all", "kind": "ALL", "status": "SUPPORTED"}
    ]
    decision = proof["decisions"][0]
    decision["supporting_check_ids"] = [
        "subtotal_aggregation",
        *(f"line_{line_number}_extension_check" for line_number in range(1, 7)),
    ]
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _compiler_score(result) == Decimal("75"), [
        (item.id, item.observed) for item in result.checks if not item.passed
    ]
    assert "PROOF_INTEGRITY_MISMATCH" not in {item.code for item in result.vetoes}


def test_v2_unrelated_equations_do_not_prove_arithmetic_relations() -> None:
    snapshot = _strict_snapshot()
    assessments = snapshot.case_state["review_artifact"]["assessments"]
    next(item for item in assessments if item["check_id"] == "line_extensions")["reason"] = (
        "Observed numbers: 2, 23279.90, 46559.80, 3, 7734.30, 23202.90, "
        "1, 47421.00, 47421.00, 10, 457.30, 4573.00, 1, 944.00, "
        "944.00, 100, 326.50, 32650.00. Unrelated equation: 0 * 0 = 0."
    )
    next(item for item in assessments if item["check_id"] == "subtotal_aggregation")["reason"] = (
        "Observed numbers: 46559.80, 23202.90, 47421.00, 4573.00, 944.00, "
        "32650.00, 155350.70. Unrelated equation: 0 + 0 = 0."
    )
    next(item for item in assessments if item["check_id"] == "final_total_reconciliation")["reason"] = (
        "Observed numbers: 155350.70, 31070.14, -3107.01, 183313.83, "
        "188813.24, 5499.41, 0.01. Unrelated equations: 0 + 0 = 0; "
        "0 - 0 = 0; 0 > 0."
    )

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert _stage_score(result, "reasoning") < STAGE_WEIGHTS["reasoning"]
    assert _compiler_score(result) < Decimal("75")
    assert result.passed is False


def test_v26_kernel_admitted_pairwise_sum_dag_matches_nary_oracle() -> None:
    snapshot = _strict_snapshot()
    _replace_subtotal_with_pairwise_sum(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "reasoning.relation.subtotal_math"
    ).passed is True
    assert result.passed is True


def test_v32_relation_preserves_an_admitted_derived_operand() -> None:
    oracle_path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/invoice_subtotal_conflict_0006/oracle.json"
    )
    oracle = BusinessEvalOracle.model_validate_json(oracle_path.read_text(encoding="utf-8"))
    facts = {item.id: item for item in oracle.facts}
    relation = next(
        item for item in oracle.relations if item.id == "line_derived_final_total_math"
    )
    line_sum = {
        "id": "witness_line_sum",
        "operation": "SUM",
        "operands": [
            {"ref": {"kind": "CLAIM", "ref_id": "claim_line_1"}, "value": "6404.64"},
            {"ref": {"kind": "CLAIM", "ref_id": "claim_line_2"}, "value": "9097.80"},
        ],
    }
    final = {
        "id": "witness_final",
        "check_id": "check_final",
        "facet_ref": "final_total",
        "operation": "SUM",
        "result": "14856.50",
        "currency": "EUR",
        "operands": [
            {
                "ref": {"kind": "WITNESS", "ref_id": "witness_line_sum"},
                "value": "15502.44",
                "currency": "EUR",
            },
            {
                "ref": {"kind": "CLAIM", "ref_id": "claim_adjustment"},
                "value": "-645.94",
                "currency": "EUR",
            },
        ],
    }

    assert _typed_witness_matches_relation(
        final,
        relation,
        milestone_facet_ref="final_total",
        explicit_facet=True,
        source_assignments={
            "adjustment_1": "claim_adjustment",
            "printed_total": "claim_total",
        },
        facts_by_id=facts,
        witness_outputs={"witness_line_sum": {"line_sum"}},
        accepted_witness_ids={"witness_line_sum", "witness_final"},
        witnesses_by_id={"witness_line_sum": line_sum, "witness_final": final},
        fact_equivalences={fact_id: {fact_id} for fact_id in facts},
    )


def test_v26_proven_zero_difference_allows_transitive_fact_substitution() -> None:
    snapshot = _strict_snapshot()
    _add_line_sum_equivalence_witness(snapshot)

    result = score_business_eval(
        _case(),
        _oracle_with_line_sum(prove_equivalence=True),
        snapshot,
    )

    assert next(
        item
        for item in result.checks
        if item.id == "reasoning.relation.line_sum_matches_printed_subtotal"
    ).passed is True
    assert next(
        item
        for item in result.checks
        if item.id == "reasoning.relation.recomputed_total_math"
    ).passed is True
    assert result.passed is True


def test_v26_same_numeric_value_without_typed_equality_cannot_substitute() -> None:
    result = score_business_eval(
        _case(),
        _oracle_with_line_sum(prove_equivalence=False),
        _strict_snapshot(),
    )

    assert next(
        item
        for item in result.checks
        if item.id == "reasoning.relation.recomputed_total_math"
    ).passed is False
    assert result.passed is False


def test_v26_shared_final_facet_is_split_by_typed_path_then_uses_plan_logic() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/credit_note_total_conflict_0016/oracle.json"
    )
    oracle = BusinessEvalOracle.model_validate_json(path.read_text(encoding="utf-8"))
    milestones = {
        item.id: item
        for item in oracle.milestones
        if item.id in {"final_total_reconciliation", "credit_note_sign_semantics"}
    }
    checks = {
        "total_path": {
            "kind": "CHECK",
            "statement": "The recomputed final total does not match the printed total.",
        },
        "sign_path": {
            "kind": "CHECK",
            "statement": "The credit note negative-sign calculation is treated consistently.",
        },
    }
    initial = {item.id: set(checks) for item in milestones.values()}
    final_milestone = milestones["final_total_reconciliation"]
    sign_milestone = milestones["credit_note_sign_semantics"]
    relation_matches = {
        relation_id: ("total_path", f"witness_{relation_id}")
        for relation_id in final_milestone.relation_ids
    }
    refined = _refine_shared_facet_matches(
        list(milestones.values()),
        initial,
        relation_matches=relation_matches,
        checks=checks,
    )
    nodes = {
        **checks,
        "root": {"kind": "ALL", "depends_on": ["total_path", "sign_path"]},
    }
    assessments = {
        "total_path": {"status": "CONTRADICTED"},
        "sign_path": {"status": "SUPPORTED"},
    }

    assert refined[final_milestone.id] == {"total_path"}
    assert refined[sign_milestone.id] == {"sign_path"}
    assert _aggregate_milestone_outcome(
        sorted(refined[final_milestone.id]),
        {"total_path": "CONTRADICTED", "sign_path": "SUPPORTED"},
        root_id="root",
        nodes=nodes,
    ) == "CONTRADICTED"
    assert _aggregate_milestone_outcome(
        sorted(refined[sign_milestone.id]),
        {"total_path": "CONTRADICTED", "sign_path": "SUPPORTED"},
        root_id="root",
        nodes=nodes,
    ) == "SUPPORTED"


def test_v26_0006_difference_above_tolerance_blocks_invalid_substitution() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "evals/business_v1/cases/invoice_subtotal_conflict_0006/oracle.json"
    )
    oracle = BusinessEvalOracle.model_validate_json(path.read_text(encoding="utf-8"))
    facts_by_id = {item.id: item for item in oracle.facts}
    claims = {
        item.id: item
        for item in (
            Claim(
                id="claim_line_1_extension",
                subject="line 1",
                predicate="line extension",
                value="6404.64",
                source_id="source",
                quote="6404.64",
                locator="line 1",
                attributes={"currency": "EUR"},
            ),
            Claim(
                id="claim_line_2_extension",
                subject="line 2",
                predicate="line extension",
                value="9097.80",
                source_id="source",
                quote="9097.80",
                locator="line 2",
                attributes={"currency": "EUR"},
            ),
            Claim(
                id="claim_printed_subtotal",
                subject="invoice",
                predicate="printed subtotal",
                value="15507.44",
                source_id="source",
                quote="15507.44",
                locator="subtotal",
                attributes={"currency": "EUR"},
            ),
            Claim(
                id="claim_adjustment_1",
                subject="invoice",
                predicate="adjustment",
                value="-645.94",
                source_id="source",
                quote="-645.94",
                locator="adjustment",
                attributes={"currency": "EUR"},
            ),
        )
    }
    witnesses: dict[str, CalculationWitness] = {}

    def make(
        witness_id: str,
        check_id: str,
        facet_ref: str,
        operation: str,
        operands: list[ProofTermRef],
    ) -> CalculationWitness:
        witness = compute_witness(
            CalculationRequest(
                id=witness_id,
                check_id=check_id,
                facet_ref=facet_ref,
                operation=operation,
                operands=operands,
            ),
            claims=claims,
            witnesses=witnesses,
            policy_values={
                "invoice_calculation_rounding_tolerance": {
                    "value": "0.01",
                    "currency": "EUR",
                }
            },
            evidence_snapshot_hash="sha256:evidence",
            policy_snapshot_hash="sha256:policy",
        )
        witnesses[witness.id] = witness
        return witness

    line_sum = make(
        "witness_line_sum",
        "subtotal",
        "subtotal_aggregation",
        "SUM",
        [
            ProofTermRef(kind="CLAIM", ref_id="claim_line_1_extension"),
            ProofTermRef(kind="CLAIM", ref_id="claim_line_2_extension"),
        ],
    )
    difference = make(
        "witness_subtotal_difference",
        "subtotal",
        "subtotal_aggregation",
        "ABS_DIFF",
        [
            ProofTermRef(kind="WITNESS", ref_id=line_sum.id),
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
        ],
    )
    exceeds = make(
        "witness_subtotal_exceeds",
        "subtotal",
        "subtotal_aggregation",
        "GREATER_THAN",
        [
            ProofTermRef(kind="WITNESS", ref_id=difference.id),
            ProofTermRef(
                kind="POLICY",
                ref_id="invoice_calculation_rounding_tolerance",
            ),
        ],
    )
    wrong_final = make(
        "witness_wrong_line_path",
        "line_path",
        "final_total",
        "SUM",
        [
            ProofTermRef(kind="CLAIM", ref_id="claim_printed_subtotal"),
            ProofTermRef(kind="CLAIM", ref_id="claim_adjustment_1"),
        ],
    )
    milestone_by_id = {item.id: item for item in oracle.milestones}
    subtotal = milestone_by_id["subtotal_aggregation"]
    line_path = milestone_by_id["line_derived_total_reconciliation"]
    relation_ids = {
        "line_sum_math",
        "subtotal_difference_math",
        "subtotal_difference_exceeds_tolerance",
        "line_derived_final_total_math",
    }
    relations = [item for item in oracle.relations if item.id in relation_ids]
    relation_owner = {
        relation.id: (
            line_path
            if relation.id == "line_derived_final_total_math"
            else subtotal
        )
        for relation in relations
    }
    matched, _ = _match_typed_relation_witnesses(
        relations,
        relation_owner=relation_owner,
        milestone_check_ids={
            subtotal.id: {"subtotal"},
            line_path.id: {"line_path"},
        },
        canonical_node_results={
            "subtotal": {
                "claim_ids": [
                    "claim_line_1_extension",
                    "claim_line_2_extension",
                    "claim_printed_subtotal",
                ],
                "witness_ids": [line_sum.id, difference.id, exceeds.id],
            },
            "line_path": {
                "claim_ids": ["claim_printed_subtotal", "claim_adjustment_1"],
                "witness_ids": [wrong_final.id],
            },
        },
        calculation_witnesses=[
            item.model_dump(mode="json") for item in witnesses.values()
        ],
        facts_by_id=facts_by_id,
        fact_claim_ids={
            "line_1_extension": {"claim_line_1_extension"},
            "line_2_extension": {"claim_line_2_extension"},
            "printed_subtotal": {"claim_printed_subtotal"},
            "adjustment_1": {"claim_adjustment_1"},
        },
    )

    assert difference.result == Decimal("5.00")
    assert exceeds.result is True
    assert {
        "line_sum_math",
        "subtotal_difference_math",
        "subtotal_difference_exceeds_tolerance",
    }.issubset(matched)
    assert "line_derived_final_total_math" not in matched


def test_v26_milestone_status_respects_any_instead_of_forcing_all() -> None:
    nodes = {
        "left": {"kind": "CHECK"},
        "right": {"kind": "CHECK"},
        "root": {"kind": "ANY", "depends_on": ["left", "right"]},
    }
    assessments = {
        "left": {"status": "CONTRADICTED"},
        "right": {"status": "SUPPORTED"},
    }

    assert _aggregate_milestone_outcome(
        ["left", "right"],
        {"left": "CONTRADICTED", "right": "SUPPORTED"},
        root_id="root",
        nodes=nodes,
    ) == "SUPPORTED"


def test_v25_unaccepted_foreign_witness_cannot_satisfy_relation() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    original = next(
        item
        for item in artifact["calculation_witnesses"]
        if item["id"] == "witness_subtotal"
    )
    evidence = EvidenceIR.model_validate(artifact["evidence_ir"])
    claims = {claim.id: claim for claim in evidence.claims}
    accepted = {
        item["id"]: CalculationWitness.model_validate(item)
        for item in artifact["calculation_witnesses"]
    }
    foreign = compute_witness(
        CalculationRequest(
            id="witness_foreign_subtotal",
            check_id=original["check_id"],
            facet_ref=original["facet_ref"],
            operation=original["operation"],
            operands=[
                ProofTermRef.model_validate(item["ref"])
                for item in original["operands"]
            ],
        ),
        claims=claims,
        witnesses=accepted,
        policy_values=artifact["resolved_policy_terms"],
        evidence_snapshot_hash=evidence.source_snapshot_hash(),
        policy_snapshot_hash=artifact["policy_hash"],
    )
    artifact["calculation_witnesses"].append(foreign.model_dump(mode="json"))
    subtotal = next(
        item for item in artifact["assessments"] if item["check_id"] == "subtotal_aggregation"
    )
    subtotal["accepted_witness_ids"] = []
    artifact["submitted_witness_refs"].pop("subtotal_aggregation")
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    relation = next(
        item for item in result.checks if item.id == "reasoning.relation.subtotal_math"
    )
    assert relation.passed is False
    assert "witness_foreign_subtotal" not in str(relation.observed)


def test_v25_wrong_parent_operands_make_terminal_status_fail_closed() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    _rebuild_witness_check_ids(
        artifact,
        {},
        witness_operands={
            "witness_line_1": [
                ProofTermRef(kind="CLAIM", ref_id="claim_line_1_quantity"),
                ProofTermRef(kind="CLAIM", ref_id="claim_line_1_extension"),
            ]
        },
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "reasoning.milestone_status.line_extensions"
    ).passed is False
    assert next(
        item for item in result.checks if item.id == "reasoning.relation.line_1_extension_math"
    ).passed is False
    canonical_line = next(
        item
        for item in snapshot.case_state["compiled_proof"]["node_results"]
        if item["node_id"] == "line_extensions"
    )
    assert canonical_line["status"] == "NOT_FOUND"


def test_v25_tampered_witness_is_excluded_by_kernel_graph() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    witness = next(
        item
        for item in artifact["calculation_witnesses"]
        if item["id"] == "witness_line_1"
    )
    witness["result"] = "999999.99"
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "reasoning.relation.line_1_extension_math"
    ).passed is False
    canonical_line = next(
        item
        for item in snapshot.case_state["compiled_proof"]["node_results"]
        if item["node_id"] == "line_extensions"
    )
    assert canonical_line["status"] == "NOT_FOUND"


def test_v25_false_reason_equation_is_vetoed_but_not_used_as_canonical_witness() -> None:
    snapshot = _strict_snapshot()
    assessment = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    assessment["reason"] = (
        "155350.70 + 31070.14 - 3107.01 = 183313.83. "
        "188813.24 + 183313.83 = 5499.41. 5499.41 > 0.01."
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item for item in result.checks if item.id == "reasoning.relation.total_difference_math"
    ).passed is True
    assert next(
        item for item in result.checks if item.id == "reasoning.explicit_equations_valid"
    ).passed is False
    assert "INVALID_ARITHMETIC_WITNESS" in {item.code for item in result.vetoes}


def test_v25_negated_reason_does_not_override_kernel_accepted_greater_than_witness() -> None:
    snapshot = _strict_snapshot()
    assessment = next(
        item
        for item in snapshot.case_state["review_artifact"]["assessments"]
        if item["check_id"] == "final_total_reconciliation"
    )
    assessment["reason"] = (
        "155350.70 + 31070.14 - 3107.01 = 183313.83. "
        "188813.24 - 183313.83 = 5499.41. "
        "5499.41 does not exceed 0.01."
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert next(
        item
        for item in result.checks
        if item.id == "reasoning.relation.total_difference_exceeds_tolerance"
    ).passed is True


def test_v25_prefix_negation_does_not_replace_typed_greater_than_result() -> None:
    for denial in (
        "It is false that 5499.41 exceeds 0.01.",
        "并不是 5499.41 超过 0.01。",
    ):
        snapshot = _strict_snapshot()
        assessment = next(
            item
            for item in snapshot.case_state["review_artifact"]["assessments"]
            if item["check_id"] == "final_total_reconciliation"
        )
        assessment["reason"] = (
            "155350.70 + 31070.14 - 3107.01 = 183313.83. "
            "188813.24 - 183313.83 = 5499.41. "
            f"{denial}"
        )
        _refresh_proof_hashes(snapshot)

        result = score_business_eval(_case(), _oracle(), snapshot)

        assert next(
            item
            for item in result.checks
            if item.id == "reasoning.relation.total_difference_exceeds_tolerance"
        ).passed is True


def test_v23_canonical_target_status_must_equal_oracle_and_is_score_capped() -> None:
    snapshot = _strict_snapshot()
    for assessment in snapshot.case_state["review_artifact"]["assessments"]:
        assessment["status"] = "SUPPORTED"
    _refresh_proof_hashes(snapshot)
    snapshot.case_state["requirements"][0]["status"] = "satisfied"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "TARGET_DECISION_MISMATCH" in {item.code for item in result.vetoes}
    assert result.raw_score is not None and result.raw_score > result.score
    assert result.score == result.score_cap == Decimal("59")


def test_v23_canonical_decision_and_case_state_projection_cannot_diverge() -> None:
    snapshot = _strict_snapshot()
    snapshot.case_state["requirements"][0]["status"] = "satisfied"

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "CASESTATE_PROJECTION_MISMATCH" in {item.code for item in result.vetoes}
    projection = next(
        item for item in result.checks if item.id == "proof.all_requirement_projections"
    )
    assert projection.passed is False
    assert result.score_cap == Decimal("59")


def test_v23_projection_check_covers_non_target_canonical_decisions() -> None:
    violations = _canonical_projection_violations(
        {
            "decisions": [
                {
                    "requirement_id": "invoice_calculation_valid",
                    "status": "CONTRADICTED",
                    "contradicting_check_ids": ["calc"],
                },
                {
                    "requirement_id": "invoice",
                    "status": "SUPPORTED",
                    "supporting_check_ids": ["document"],
                },
            ],
            "node_results": [
                {"node_id": "calc", "source_ids": ["source_1"]},
                {"node_id": "document", "source_ids": ["source_1"]},
            ],
        },
        requirement_rows=[
            {
                "id": "invoice_calculation_valid",
                "status": "conflict",
                "evidence_ids": ["source_1"],
            },
            {"id": "invoice", "status": "missing", "evidence_ids": []},
        ],
    )

    assert [item["requirement_id"] for item in violations] == ["invoice"]


def test_v30_projection_does_not_treat_missing_decision_status_as_not_found() -> None:
    violations = _canonical_projection_violations(
        {
            "decisions": [{"requirement_id": "invoice_calculation_valid"}],
            "node_results": [],
        },
        requirement_rows=[
            {
                "id": "invoice_calculation_valid",
                "status": "missing",
                "evidence_ids": [],
            }
        ],
    )

    assert violations == [
        {
            "requirement_id": "invoice_calculation_valid",
            "error": "missing_decision_status",
            "observed_status": "缺失",
        }
    ]


def test_v23_milestone_wording_failure_is_not_mislabeled_as_ungrounded() -> None:
    snapshot = _strict_snapshot()
    for node in snapshot.case_state["review_artifact"]["plan"]["nodes"]:
        if node["kind"] == "CHECK":
            node["statement"] = f"Independent business question {node['id']}"
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    assert "UNGROUNDED_STRONG_CONCLUSION" not in {item.code for item in result.vetoes}
    assert any(
        not item.passed and item.id.startswith("understanding.milestone_semantics.")
        for item in result.checks
    )


def test_v23_wrong_currency_predicate_short_quote_and_distant_locator_do_not_ground() -> None:
    mutations = (
        ("value", "155350.70 CNY"),
        ("predicate", "printed total"),
        ("predicate", "weather_temperature"),
        ("quote", "Subtotal"),
        ("locator", "locator line_1_quantity"),
    )
    for field, value in mutations:
        snapshot = _strict_snapshot()
        claim = next(
            item
            for item in snapshot.case_state["review_artifact"]["evidence_ir"]["claims"]
            if item["id"] == "claim_printed_subtotal"
        )
        claim[field] = value
        result = score_business_eval(_case(), _oracle(), snapshot)
        fact_check = next(
            item for item in result.checks if item.id == "evidence.fact.printed_subtotal"
        )
        assert fact_check.passed is False, (field, result)
        assert result.passed is False
        if field == "locator":
            assert "UNGROUNDED_STRONG_CONCLUSION" in {item.code for item in result.vetoes}


def test_v23_boundary_scans_assessment_reason_and_user_outputs() -> None:
    for location in ("reason", "output"):
        snapshot = _strict_snapshot()
        overclaim = "VAT rate and taxable base were validated and calculated correctly."
        if location == "reason":
            snapshot.case_state["review_artifact"]["assessments"][0]["reason"] += overclaim
            _refresh_proof_hashes(snapshot)
        else:
            snapshot.conversation[-1]["content"] += overclaim
            for report in snapshot.reports:
                report.text += overclaim
        result = score_business_eval(_case(), _oracle(), snapshot)
        assert "EPISTEMIC_BOUNDARY_VIOLATION" in {item.code for item in result.vetoes}
        assert result.score_cap == Decimal("84")


def test_boundary_check_does_not_join_subtotal_statement_with_vat_reason() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    subtotal = next(
        node for node in artifact["plan"]["nodes"] if node["id"] == "subtotal_aggregation"
    )
    subtotal["statement"] = "The printed subtotal was calculated correctly."
    assessment = next(
        item
        for item in artifact["assessments"]
        if item["check_id"] == "subtotal_aggregation"
    )
    assessment["reason"] = "The source separately labels the invoice incl. VAT."
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    boundary = next(
        item
        for item in result.checks
        if item.id == "reasoning.epistemic_boundary.component_rate_or_base_not_proven"
    )
    assert "subtotal_aggregation" not in boundary.observed["matched_check_ids"]
    assert boundary.passed is True


def test_boundary_check_matches_rate_base_correctness_in_one_proposition() -> None:
    snapshot = _strict_snapshot()
    artifact = snapshot.case_state["review_artifact"]
    assessment = next(
        item
        for item in artifact["assessments"]
        if item["check_id"] == "component_vat_rate_base"
    )
    assessment["status"] = "SUPPORTED"
    assessment["reason"] = (
        "The stated VAT rate and taxable base were validated and calculated correctly."
    )
    _refresh_proof_hashes(snapshot)

    result = score_business_eval(_case(), _oracle(), snapshot)

    boundary = next(
        item
        for item in result.checks
        if item.id == "reasoning.epistemic_boundary.component_rate_or_base_not_proven"
    )
    assert "component_vat_rate_base" in boundary.observed["matched_check_ids"]
    assert "component_vat_rate_base" in boundary.observed["check_violations"]
    assert "EPISTEMIC_BOUNDARY_VIOLATION" in {item.code for item in result.vetoes}


def test_chinese_unresolved_configuration_output_is_not_supported() -> None:
    meaning = MeaningOracle(all_of=[["VAT"], ["正确"]])

    for state in ("未决", "未配置", "需配置", "需要配置"):
        assertions = _boundary_output_assertions(f"VAT 正确性{state}。", meaning)
        assert assertions and assertions[0][0] == "NOT_FOUND", state

    assert _boundary_output_assertions("VAT 正确有效。", meaning)[0][0] == "SUPPORTED"


def test_blocking_obligations_requires_canonical_target_decision() -> None:
    snapshot = _strict_snapshot()
    snapshot.case_state["review_artifact"]["plan"]["roots"] = {}

    result = score_business_eval(_case(), _oracle(), snapshot)

    check = next(item for item in result.checks if item.id == "proof.blocking_obligations")
    assert check.passed is False


def test_v23_structured_opposite_conclusion_catches_keyword_preserving_reversal() -> None:
    oracle = _oracle().model_copy(deep=True)
    oracle.communication.opposite_conclusions = [
        MeaningOracle(all_of=[["总额", "金额"], ["正确", "一致", "通过"]])
    ]
    snapshot = _strict_snapshot()
    reversal = "虽然前文保留金额不一致字样，但最终总额实际上正确并通过审核。"
    snapshot.conversation[-1]["content"] += reversal
    for report in snapshot.reports:
        report.text += reversal

    result = score_business_eval(_case(), oracle, snapshot)

    assert "USER_OUTPUT_CONTRADICTS_TRUTH" in {item.code for item in result.vetoes}
    assert result.score_cap == Decimal("59")


def test_v23_core_failure_caps_raw_99_and_report_explains_cap() -> None:
    snapshot = _strict_snapshot()
    snapshot.conversation[-1]["content"] = (
        "审核发现总额错误：票面 188813.24 EUR，重算 183313.83 EUR，"
        "相差 5499.41 EUR。中文审核报告已经生成。"
    )

    result = score_business_eval(_case(), _oracle(), snapshot)
    report = render_eval_report(_case(), _oracle(), snapshot, result)

    assert not result.vetoes
    assert result.raw_score == Decimal("99")
    assert result.score == result.score_cap == Decimal("89")
    assert "原始得分" in report and "失败封顶" in report and "封顶原因" in report


def test_framework_protocol_passes_ordered_tools_with_harmless_interleaving() -> None:
    result = score_business_eval(_case(), _framework_oracle(), _framework_snapshot())

    assert result.business_passed is True
    assert result.score == Decimal("100")
    assert result.framework_enabled is True
    assert result.framework_score == Decimal("100")
    assert result.framework_passed is True
    assert result.passed is True
    assert all(item.passed for item in result.framework_checks)
    assert sum((item.points for item in result.framework_checks), Decimal("0")) == Decimal("100")
    assert result.engineering["tool_names"] == [
        "read_attachment",
        "read_source",
        "bind_claim",
        "list_sources",
        "compute_witness",
        "submit_check",
    ]


def test_framework_protocol_can_gate_report_role_tools_and_approvals() -> None:
    snapshot = _framework_snapshot()
    snapshot.trace["role_calls"] = [
        {"role": "report_writer", "error": "", "ts": "2026-01-01T00:00:10+00:00"},
    ]
    snapshot.trace["tool_calls"].extend(
        [
            {"tool": "write_case_file", "error": "", "ts": "2026-01-01T00:00:11+00:00"},
            {"tool": "render_pdf", "error": "", "ts": "2026-01-01T00:00:12+00:00"},
        ]
    )
    snapshot.events.extend(
        [
            {"kind": "role_call", "name": "report_writer", "payload": {"role": "report_writer", "error": ""}},
            {"kind": "tool_call", "name": "write_case_file", "payload": {"tool": "write_case_file", "error": ""}},
            {"kind": "tool_call", "name": "render_pdf", "payload": {"tool": "render_pdf", "error": ""}},
        ]
    )
    base = _framework_oracle().framework
    assert base is not None
    framework = base.model_copy(
        update={
            "required_roles": [RequiredRoleOracle(name="report_writer")],
            "required_approved_tools": ["write_case_file", "render_pdf"],
            "max_total_calls": 10,
            "ordered_milestones": [
                *base.ordered_milestones,
                ["role:report_writer"],
                ["tool:write_case_file"],
                ["tool:render_pdf"],
            ],
        }
    )
    oracle = _oracle().model_copy(update={"framework": framework})

    result = score_business_eval(_case(), oracle, snapshot)

    assert result.framework_passed is True
    assert result.passed is True
    assert any(item.id == "framework.required_role.report_writer" for item in result.framework_checks)
    assert {
        item.id for item in result.framework_checks if item.id.startswith("framework.required_approval")
    } == {
        "framework.required_approval.write_case_file",
        "framework.required_approval.render_pdf",
    }


def test_framework_protocol_with_attachment_requires_read_attachment_by_oracle() -> None:
    snapshot = _framework_snapshot()
    snapshot.events = [
        item
        for item in snapshot.events
        if str((item.get("payload") or {}).get("tool") or "") != "read_attachment"
    ]

    result = score_business_eval(_case(), _framework_oracle(), snapshot)

    check = next(
        item for item in result.framework_checks if item.id == "framework.required_tool.read_attachment"
    )
    assert check.passed is False
    assert result.score == Decimal("100")  # Business score is deliberately untouched.
    assert result.business_passed is True
    assert result.framework_passed is False
    assert result.passed is False


def test_framework_protocol_requires_compute_witness_by_configuration_not_domain_code() -> None:
    snapshot = _framework_snapshot()
    snapshot.events = [
        item
        for item in snapshot.events
        if "compute_witness" not in str(item.get("payload") or {})
    ]

    result = score_business_eval(_case(), _framework_oracle(), snapshot)

    check = next(
        item for item in result.framework_checks if item.id == "framework.required_tool.compute_witness"
    )
    assert check.passed is False
    assert result.business_passed is True
    assert result.framework_passed is False
    assert result.passed is False


def test_framework_protocol_events_are_canonical_and_trace_duplicate_is_ignored() -> None:
    snapshot = _framework_snapshot()

    result = score_business_eval(_case(), _framework_oracle(), snapshot)

    assert result.framework_passed is True
    assert result.engineering["tool_names"].count("read_attachment") == 1
    assert result.engineering["tool_calls"] == 6


def test_framework_protocol_fails_closed_when_fallback_sources_have_no_shared_order() -> None:
    snapshot = _framework_snapshot()
    snapshot.events = []
    snapshot.trace["tool_calls"] = [{"tool": "read_attachment", "error": ""}]
    snapshot.trace["provider_tool_calls"] = [
        {"tool": "read_source", "error": ""},
        {"tool": "bind_claim", "error": ""},
        {"tool": "compute_witness", "error": ""},
        {"tool": "submit_check", "error": ""},
    ]

    result = score_business_eval(_case(), _framework_oracle(), snapshot)

    ordered = next(
        item for item in result.framework_checks if item.id == "framework.ordered_milestones"
    )
    assert ordered.passed is False
    assert ordered.observed["orderable"] is False
    assert all(
        item.passed
        for item in result.framework_checks
        if item.id.startswith("framework.required_tool")
    )


def test_framework_protocol_forbidden_write_and_report_tools_fail() -> None:
    snapshot = _framework_snapshot()
    snapshot.events.extend(
        [
            {"kind": "tool_call", "ts": "2026-01-01T00:00:10+00:00", "payload": {"tool": "write_case_file"}},
            {"kind": "tool_call", "ts": "2026-01-01T00:00:11+00:00", "payload": {"tool": "render_pdf"}},
        ]
    )
    oracle = _framework_oracle(forbidden_tools=["write_case_file", "render_pdf"])

    result = score_business_eval(_case(), oracle, snapshot)

    assert result.business_passed is True
    assert result.framework_passed is False
    assert result.passed is False
    assert {
        item.id for item in result.framework_checks if not item.passed
    } == {
        "framework.forbidden_tool.write_case_file",
        "framework.forbidden_tool.render_pdf",
    }


def test_framework_protocol_tool_errors_and_total_calls_have_independent_budgets() -> None:
    snapshot = _framework_snapshot()
    snapshot.events.extend(
        [
            {
                "kind": "tool_finished",
                "ts": "2026-01-01T00:00:10+00:00",
                "payload": {"tool": "read_source", "status": "rejected", "hook_code": "BAD_REF"},
            },
            {
                "kind": "tool_call",
                "ts": "2026-01-01T00:00:11+00:00",
                "payload": {"tool": "list_case_files", "error": "temporary failure"},
            },
        ]
    )
    oracle = _framework_oracle(max_tool_errors=1, max_total_calls=6)

    result = score_business_eval(_case(), oracle, snapshot)

    failed = {item.id for item in result.framework_checks if not item.passed}
    assert failed == {"framework.max_tool_errors", "framework.max_total_calls"}
    assert result.engineering["tool_error_calls"] == 2
    assert result.framework_passed is False
    assert result.score == Decimal("100")


def test_framework_protocol_report_has_small_separate_summary() -> None:
    oracle = _framework_oracle()
    snapshot = _framework_snapshot()
    result = score_business_eval(_case(), oracle, snapshot)

    report = render_eval_report(_case(), oracle, snapshot, result)

    assert "## 框架协议" in report
    assert "不计入业务 100 分" in report
    assert "framework.ordered_milestones" in report
    assert len(report) < 12_000


def test_v23_false_or_unverified_equations_are_not_witnesses() -> None:
    for reason in (
        "This equation is false: 2 × 3 = 6.",
        "2 × 3 = 6 is unverified.",
        "该等式未经验证：2 × 3 = 6。",
        "2 × 3 = 6 不成立。",
    ):
        assert _equation_witnesses(reason) == []
