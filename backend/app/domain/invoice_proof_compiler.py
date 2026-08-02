from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel

from app.domain.invoice_requirements import POLICY_PATH
from app.domain.semantic_graph import (
    AttributeBinding,
    ClaimBinding,
    ClaimSelector,
    NodeSpec,
    SemanticGraphSpec,
    compile_proof_graph,
)
from app.state.schemas import CompiledProof, EvidenceItem, VerificationRecord


class InvoiceProofPolicy(BaseModel):
    policy_version: str
    compiler_version: str
    amount_tolerance_percent: Decimal
    allowed_amount_basis_by_subject: dict[str, list[str]]
    comparable_tax_bases: list[str]
    comparable_coverages: list[str]
    amount_match_requirement: str
    semantic_judgment_min_confidence: str = "high"


AMOUNT_SUBJECTS = ("invoice", "purchase_order", "goods_receipt")


def load_invoice_proof_policy(path: Path = POLICY_PATH) -> InvoiceProofPolicy:
    return InvoiceProofPolicy.model_validate_json(path.read_text(encoding="utf-8"))


def amount_proof_program(policy: InvoiceProofPolicy) -> SemanticGraphSpec:
    attributes = (
        AttributeBinding("currency", ("currency", "currency_tax"), "currency", fallback_from_primary_quote=True),
        AttributeBinding("basis", ("basis", "amount_basis"), allowed_values=("invoice_total", "line_total", "order_total", "received_value", "cumulative_received_value", "unknown")),
        AttributeBinding("tax_basis", ("tax_basis",), allowed_values=("gross", "net", "unknown")),
        AttributeBinding("coverage", ("coverage", "amount_coverage"), allowed_values=("full", "partial", "cumulative", "unknown")),
    )
    bindings = (
        ClaimBinding("invoice", "amount", "extracted_field", ("invoice",), "invoice", "decimal", ("amount_total", "invoice_total", "amount", "total"), attributes=attributes, quote_must_contain_value=True),
        ClaimBinding("purchase_order", "amount", "extracted_field", ("purchase_order",), "purchase_order", "decimal", ("po_amount", "approved_amount", "order_total", "amount_total", "amount", "total"), attributes=attributes, quote_must_contain_value=True),
        ClaimBinding("goods_receipt", "amount", "extracted_field", ("goods_receipt",), "goods_receipt_or_service_acceptance", "decimal", ("received_value", "grn_amount", "amount_total", "amount", "total"), attributes=attributes, quote_must_contain_value=True),
        ClaimBinding("invoice", "order_scope_identity", "semantic_ir", ("invoice",), "invoice", "string", quote_must_contain_value=True),
        ClaimBinding("purchase_order", "order_scope_identity", "semantic_ir", ("purchase_order",), "purchase_order", "string", quote_must_contain_value=True),
        ClaimBinding("goods_receipt", "order_scope_identity", "semantic_ir", ("goods_receipt",), "goods_receipt_or_service_acceptance", "string", quote_must_contain_value=True),
    )
    selectors = tuple(ClaimSelector(subject, "amount") for subject in AMOUNT_SUBJECTS)
    scope_selectors = tuple(ClaimSelector(subject, "order_scope_identity") for subject in AMOUNT_SUBJECTS)
    presence = ("CHK_INVOICE_AMOUNT_PRESENT", "CHK_PO_AMOUNT_PRESENT", "CHK_GRN_VALUE_PRESENT")
    nodes = (
        NodeSpec(presence[0], policy.amount_match_requirement, "present", (selectors[0],), missing_premise="source-traceable invoice amount", obligation_id="OBL_INVOICE_AMOUNT_PRESENT"),
        NodeSpec(presence[1], policy.amount_match_requirement, "present", (selectors[1],), missing_premise="source-traceable purchase order amount", obligation_id="OBL_PO_AMOUNT_PRESENT"),
        NodeSpec(presence[2], policy.amount_match_requirement, "present", (selectors[2],), missing_premise="source-traceable goods receipt amount", obligation_id="OBL_GRN_VALUE_PRESENT"),
        NodeSpec("CHK_DOCUMENT_SCOPE_IDENTITY", policy.amount_match_requirement, "same", scope_selectors, missing_premise="one explicit source-linked order scope identity shared by invoice, purchase order, and goods receipt", obligation_id="OBL_DOCUMENT_SCOPE_IDENTITY"),
        NodeSpec("CHK_CURRENCY_COMPATIBLE", policy.amount_match_requirement, "same", selectors, presence, "currency", missing_premise="explicit source-linked currency for every amount", obligation_id="OBL_CURRENCY_COMPATIBLE"),
        NodeSpec("CHK_AMOUNT_BASIS_ALLOWED", policy.amount_match_requirement, "allowed_by_subject", selectors, presence, "basis", {"allowed_by_subject": policy.allowed_amount_basis_by_subject, "on_disallowed": "INCOMPLETE"}, missing_premise="source-linked amount basis for every document", obligation_id="OBL_AMOUNT_BASIS"),
        NodeSpec("CHK_TAX_BASIS_ALLOWED", policy.amount_match_requirement, "allowed", selectors, presence, "tax_basis", {"allowed": policy.comparable_tax_bases, "on_disallowed": "INCOMPLETE"}, missing_premise="source-linked tax basis for every document", obligation_id="OBL_TAX_BASIS"),
        NodeSpec("CHK_TAX_BASIS_EQUAL", policy.amount_match_requirement, "same", selectors, presence, "tax_basis", {"on_mismatch": "INCOMPLETE"}, missing_premise="reconciliation showing one comparable tax basis across all three documents", obligation_id="OBL_TAX_BASIS_EQUAL"),
        NodeSpec("CHK_COVERAGE_COMPATIBLE", policy.amount_match_requirement, "allowed", selectors, presence, "coverage", {"allowed": policy.comparable_coverages, "on_disallowed": "INCOMPLETE"}, missing_premise="source-linked full and comparable document coverage", obligation_id="OBL_DOCUMENT_COVERAGE"),
        NodeSpec("CHK_AMOUNT_BASIS_COMPATIBLE", policy.amount_match_requirement, "all", depends_on=("CHK_AMOUNT_BASIS_ALLOWED", "CHK_TAX_BASIS_ALLOWED", "CHK_TAX_BASIS_EQUAL", "CHK_COVERAGE_COMPATIBLE")),
        NodeSpec(
            "JDG_AMOUNT_SCOPE_COMPARABLE",
            policy.amount_match_requirement,
            "llm_judgment",
            selectors,
            (*presence, "CHK_DOCUMENT_SCOPE_IDENTITY", "CHK_CURRENCY_COMPATIBLE", "CHK_AMOUNT_BASIS_COMPATIBLE"),
            params={"min_confidence": policy.semantic_judgment_min_confidence},
            executor="llm",
            judgment_id="JDG_AMOUNT_SCOPE_COMPARABLE",
            required_attributes=("basis", "tax_basis", "coverage"),
            missing_premise="source-grounded semantic judgment that the three amounts describe the same economic scope",
            obligation_id="OBL_AMOUNT_SCOPE_COMPARABLE",
        ),
        NodeSpec(
            "CHK_AMOUNT_WITHIN_TOLERANCE",
            policy.amount_match_requirement,
            "within_percent",
            selectors,
            ("CHK_DOCUMENT_SCOPE_IDENTITY", "CHK_CURRENCY_COMPATIBLE", "CHK_AMOUNT_BASIS_COMPATIBLE", "JDG_AMOUNT_SCOPE_COMPARABLE"),
            params={"threshold_percent": policy.amount_tolerance_percent, "pairs": ((0, 1), (0, 2))},
        ),
        NodeSpec(
            "REQ_THREE_WAY_AMOUNT_MATCH",
            policy.amount_match_requirement,
            "all",
            depends_on=(*presence, "CHK_DOCUMENT_SCOPE_IDENTITY", "CHK_CURRENCY_COMPATIBLE", "CHK_AMOUNT_BASIS_COMPATIBLE", "JDG_AMOUNT_SCOPE_COMPARABLE", "CHK_AMOUNT_WITHIN_TOLERANCE"),
        ),
    )
    return SemanticGraphSpec(
        id="three_way_amount_match",
        version="3",
        requirement_id=policy.amount_match_requirement,
        claim_bindings=bindings,
        nodes=nodes,
        root_id="REQ_THREE_WAY_AMOUNT_MATCH",
        activation_requirements=("invoice", "purchase_order", "goods_receipt_or_service_acceptance"),
    )


def no_active_duplicate_program(policy: InvoiceProofPolicy) -> SemanticGraphSpec:
    bindings = (
        ClaimBinding("invoice", "payable_identity", "semantic_ir", ("invoice",), "invoice", "string", quote_must_contain_value=True),
        ClaimBinding("duplicate_search", "payable_identity", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "string", quote_must_contain_value=True),
        ClaimBinding("duplicate_search", "coverage", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "enum", allowed_values=("complete", "partial", "unknown")),
        ClaimBinding("duplicate_search", "result", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "enum", allowed_values=("no_candidate", "candidate_found", "unknown")),
        ClaimBinding("duplicate_search", "candidate_identity", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "string", quote_must_contain_value=True),
        ClaimBinding("payment", "identity", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "string", quote_must_contain_value=True),
        ClaimBinding("payment", "relationship_to_payable", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "enum", allowed_values=("same_obligation", "different_obligation", "unknown")),
        ClaimBinding("payment", "economic_effect", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "enum", allowed_values=("neutralized_by_reversal", "active_settled", "unknown")),
        ClaimBinding("reversal", "reverses", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "string", quote_must_contain_value=True),
        ClaimBinding("reversal", "posting_status", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "enum", allowed_values=("posted", "draft", "voided", "unknown")),
        ClaimBinding("reversal", "scope", "semantic_ir", ("duplicate_payment_check",), "duplicate_payment_screen", "enum", allowed_values=("full", "partial", "unknown")),
    )
    selectors = (
        ClaimSelector("invoice", "payable_identity"),
        ClaimSelector("duplicate_search", "payable_identity"),
        ClaimSelector("duplicate_search", "coverage"),
        ClaimSelector("duplicate_search", "result"),
        ClaimSelector("duplicate_search", "candidate_identity", required=False),
        ClaimSelector("payment", "identity", required=False),
        ClaimSelector("payment", "relationship_to_payable", required=False),
        ClaimSelector("payment", "economic_effect", required=False),
        ClaimSelector("reversal", "reverses", required=False),
        ClaimSelector("reversal", "posting_status", required=False),
        ClaimSelector("reversal", "scope", required=False),
    )
    nodes = (
        NodeSpec(
            "CHK_DUPLICATE_SEARCH_SCOPE",
            "no_active_duplicate",
            "same",
            selectors[:2],
            params={"on_mismatch": "INCOMPLETE"},
            missing_premise="duplicate search explicitly scoped to the current payable identity",
            obligation_id="OBL_DUPLICATE_SEARCH_SCOPE",
        ),
        NodeSpec(
            "CHK_DUPLICATE_SEARCH_COVERAGE",
            "no_active_duplicate",
            "allowed",
            (selectors[2],),
            ("CHK_DUPLICATE_SEARCH_SCOPE",),
            params={"allowed": ("complete",), "on_disallowed": "INCOMPLETE"},
            missing_premise="complete authoritative duplicate-payment search coverage",
            obligation_id="OBL_DUPLICATE_SEARCH_COVERAGE",
        ),
        NodeSpec(
            "CHK_DUPLICATE_SEARCH_RESULT",
            "no_active_duplicate",
            "allowed",
            (selectors[3],),
            ("CHK_DUPLICATE_SEARCH_SCOPE",),
            params={"allowed": ("no_candidate", "candidate_found"), "on_disallowed": "INCOMPLETE"},
            missing_premise="resolved duplicate-payment search result",
            obligation_id="OBL_DUPLICATE_SEARCH_RESULT",
        ),
        NodeSpec(
            "JDG_NO_ACTIVE_DUPLICATE",
            "no_active_duplicate",
            "llm_judgment",
            selectors,
            ("CHK_DUPLICATE_SEARCH_COVERAGE", "CHK_DUPLICATE_SEARCH_RESULT"),
            params={
                "min_confidence": policy.semantic_judgment_min_confidence,
                "entity_judgments": {
                    "branch_selector": ("duplicate_search", "result"),
                    "empty_value": "no_candidate",
                    "grouped_value": "candidate_found",
                    "empty_verdict": "SUPPORTED",
                    "group_selectors": tuple(
                        (selector.subject, selector.predicate)
                        for selector in selectors[4:]
                    ),
                    "anchor_selector": ("duplicate_search", "candidate_identity"),
                    "verdict_value_options": {
                        "SUPPORTED": (
                            (
                                ("payment", "relationship_to_payable", ("same_obligation",)),
                                ("payment", "economic_effect", ("neutralized_by_reversal",)),
                                ("reversal", "posting_status", ("posted",)),
                                ("reversal", "scope", ("full",)),
                            ),
                        ),
                        "REFUTED": (
                            (
                                ("payment", "relationship_to_payable", ("same_obligation",)),
                                ("payment", "economic_effect", ("active_settled",)),
                            ),
                        ),
                    },
                    "verdict_equalities": {
                        "SUPPORTED": (
                            (
                                ("duplicate_search", "candidate_identity"),
                                ("payment", "identity"),
                            ),
                            (
                                ("payment", "identity"),
                                ("reversal", "reverses"),
                            ),
                        ),
                        "REFUTED": (
                            (
                                ("duplicate_search", "candidate_identity"),
                                ("payment", "identity"),
                            ),
                        ),
                    },
                },
            },
            executor="llm",
            judgment_id="JDG_NO_ACTIVE_DUPLICATE",
            missing_premise="authoritative lifecycle disposition for every candidate historical payment",
            obligation_id="OBL_DUPLICATE_DISPOSITION",
        ),
        NodeSpec("FINAL_NO_ACTIVE_DUPLICATE", "no_active_duplicate", "all", depends_on=("JDG_NO_ACTIVE_DUPLICATE",)),
    )
    return SemanticGraphSpec(
        id="no_active_duplicate",
        version="3",
        requirement_id="no_active_duplicate",
        claim_bindings=bindings,
        nodes=nodes,
        root_id="FINAL_NO_ACTIVE_DUPLICATE",
        activation_requirements=("duplicate_payment_screen",),
    )


def compile_evidence_proof(
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None = None,
    policy: InvoiceProofPolicy | None = None,
    *,
    active_requirement_ids: set[str],
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> CompiledProof:
    policy = policy or load_invoice_proof_policy()
    programs = [
        program
        for program in (amount_proof_program(policy), no_active_duplicate_program(policy))
        if set(program.activation_requirements) <= active_requirement_ids
    ]
    return _compile_programs(
        programs,
        evidence_items,
        verification_records,
        policy,
        trusted_sources=trusted_sources,
    )


def _compile_programs(
    programs: list[SemanticGraphSpec],
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None,
    policy: InvoiceProofPolicy,
    *,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> CompiledProof:
    results = [
        compile_proof_graph(
            program,
            evidence_items,
            policy_version=policy.policy_version,
            compiler_version=policy.compiler_version,
            verification_records=verification_records,
            trusted_sources=trusted_sources,
        )
        for program in programs
    ]
    claims = _unique_models([item for result in results for item in result.claims])
    judgments = _unique_models([item for result in results for item in result.judgments])
    checks = [item for result in results for item in result.checks]
    obligations = [item for result in results for item in result.obligations]
    decisions = [result.decision for result in results]
    primary = next((item for item in decisions if item.requirement_id == policy.amount_match_requirement), decisions[0] if decisions else None)
    return CompiledProof(claims=claims, judgments=judgments, checks=checks, obligations=obligations, decisions=decisions, decision=primary)


def compile_invoice_proof(
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None = None,
    policy: InvoiceProofPolicy | None = None,
    *,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> CompiledProof:
    policy = policy or load_invoice_proof_policy()
    return _compile_programs(
        [amount_proof_program(policy)],
        evidence_items,
        verification_records,
        policy,
        trusted_sources=trusted_sources,
    )


def compile_no_active_duplicate_proof(
    evidence_items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None = None,
    policy: InvoiceProofPolicy | None = None,
    *,
    trusted_sources: Mapping[str, Mapping[str, Any]] | None = None,
) -> CompiledProof:
    policy = policy or load_invoice_proof_policy()
    return _compile_programs(
        [no_active_duplicate_program(policy)],
        evidence_items,
        verification_records,
        policy,
        trusted_sources=trusted_sources,
    )


def _unique_models(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = value.model_dump_json(exclude_none=True)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


__all__ = [
    "InvoiceProofPolicy",
    "amount_proof_program",
    "compile_evidence_proof",
    "compile_invoice_proof",
    "compile_no_active_duplicate_proof",
    "load_invoice_proof_policy",
    "no_active_duplicate_program",
]
