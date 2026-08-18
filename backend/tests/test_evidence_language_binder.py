from __future__ import annotations

import hashlib

import pytest

from app.domain.invoice_contracts import build_requirement_contracts
from app.domain.invoice_proof_compiler import compile_evidence_proof
from app.state.schemas import EvidenceItem


def _vendor_evidence(*, duplicate_status: bool = False) -> tuple[EvidenceItem, dict[str, dict[str, object]]]:
    active = {"vendor_identity", "vendor_identity_active"}
    _contracts, holes = build_requirement_contracts(active)
    by_predicate = {hole.predicate: hole.id for hole in holes}
    quote = "Vendor V-42 status active effective 2026-01-01"
    claims = [
        {
            "handle": "vendor-id",
            "hole_id": by_predicate["identity"],
            "typed_value": "V-42",
            "source_quote": quote,
            "source_locator": "vendor row 1",
            "confidence": "high",
        },
        {
            "handle": "vendor-status",
            "hole_id": by_predicate["status"],
            "typed_value": "active",
            "source_quote": quote,
            "source_locator": "vendor row 1",
            "confidence": "high",
        },
        {
            "handle": "vendor-date",
            "hole_id": by_predicate["status_effective_date"],
            "typed_value": "2026-01-01",
            "source_quote": quote,
            "source_locator": "vendor row 1",
            "confidence": "high",
        },
    ]
    if duplicate_status:
        duplicate_quote = f"{quote}; vendor V-42 status suspended"
        claims.append({
            **claims[1],
            "handle": "vendor-status-2",
            "typed_value": "suspended",
            "source_quote": duplicate_quote,
        })
        quote = duplicate_quote
    claim_handles = [claim["handle"] for claim in claims]
    evidence = EvidenceItem(
        id="ev_vendor",
        type="vendor_record",
        source="attachment",
        credibility="high",
        content=quote,
        review_result={"should_accept": True},
        supports=[{"requirement": "vendor_identity", "support_level": "full", "quoted_text": quote}],
        metadata={"classification": "business_evidence"},
        semantic_claims=claims,
        semantic_proposals=[{
            "handle": "vendor-active",
            "hole_id": by_predicate["vendor.identity_active"],
            "verdict": "SUPPORTED",
            "input_handles": claim_handles,
            "supporting_handles": claim_handles,
            "opposing_handles": [],
            "confidence": "high",
            "reason": "The vendor record states an active current status.",
        }],
    )
    trusted = {
        evidence.id: {
            "texts": [quote],
            "sha256": hashlib.sha256(quote.encode()).hexdigest(),
            "original_ref": "vendor.md",
        }
    }
    return evidence, trusted


def _invoice_evidence_with_duplicate_supplier() -> tuple[EvidenceItem, dict[str, dict[str, object]]]:
    active = {"invoice", "invoice_fields_valid"}
    _contracts, holes = build_requirement_contracts(active)
    by_predicate = {hole.predicate: hole.id for hole in holes}
    fields = [
        ("invoice-id", "identifier", "INV-100", "Invoice identifier INV-100"),
        ("supplier-a", "supplier_identity", "Supplier A", "Supplier identity Supplier A"),
        ("supplier-b", "supplier_identity", "Supplier B", "Supplier identity Supplier B"),
        ("buyer", "buyer_identity", "Aurora Ltd", "Buyer identity Aurora Ltd"),
        ("date", "issue_date", "2026-01-02", "Issue date 2026-01-02"),
        ("currency", "currency", "GBP", "Currency GBP"),
        ("tax", "tax_treatment", "standard", "Tax treatment standard"),
    ]
    claims = [
        {
            "handle": handle,
            "hole_id": by_predicate[predicate],
            "typed_value": value,
            "source_quote": quote,
            "source_locator": f"invoice field {index}",
            "confidence": "high",
        }
        for index, (handle, predicate, value, quote) in enumerate(fields, start=1)
    ]
    quotes = [quote for _handle, _predicate, _value, quote in fields]
    content = "\n".join(quotes)
    evidence = EvidenceItem(
        id="ev_invoice",
        type="invoice",
        source="attachment",
        credibility="high",
        content=content,
        review_result={"should_accept": True},
        supports=[{"requirement": "invoice", "support_level": "full", "quoted_text": quotes[0]}],
        metadata={"classification": "business_evidence"},
        semantic_claims=claims,
        semantic_proposals=[{
            "handle": "invoice-fields-valid",
            "hole_id": by_predicate["invoice.fields_valid"],
            "verdict": "SUPPORTED",
            "input_handles": [claim["handle"] for claim in claims],
            "supporting_handles": [claim["handle"] for claim in claims],
            "opposing_handles": [],
            "confidence": "high",
            "reason": "All required invoice fields are semantically valid.",
        }],
    )
    trusted = {
        evidence.id: {
            "texts": quotes,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "original_ref": "invoice.md",
        }
    }
    return evidence, trusted


def test_same_entity_contract_binds_one_source_record_without_repeated_entity_keys() -> None:
    evidence, trusted = _vendor_evidence()

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )

    assert proof.decision_for("vendor_identity_active").proof_status == "PROVED"
    assert len(proof.evidence_ir.claims) == 3
    assert len({claim.entity_key for claim in proof.evidence_ir.claims}) == 1
    assert not [item for item in proof.diagnostics if item.category == "binding"]


def test_same_entity_contract_fails_closed_when_one_slot_has_two_values() -> None:
    evidence, trusted = _vendor_evidence(duplicate_status=True)

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )

    assert proof.decision_for("vendor_identity_active").proof_status == "INCOMPLETE"
    assert {item.code for item in proof.diagnostics if item.category == "binding"} == {"SCOPE_CONFLICT"}


@pytest.mark.parametrize(
    ("input_handles", "supporting_handles", "expected_error"),
    [
        ([], [], "strong Proposal requires explicit input_handles"),
        (["missing"], ["missing"], "dangling input handle missing"),
        (["vendor-id", "vendor-id"], ["vendor-id"], "duplicate input handle vendor-id"),
        (["vendor-id", "vendor-status", "vendor-date"], [], "SUPPORTED requires supporting refs and no opposing refs"),
    ],
)
def test_strong_proposal_fails_closed_without_resolved_polarized_handles(
    input_handles: list[str],
    supporting_handles: list[str],
    expected_error: str,
) -> None:
    evidence, trusted = _vendor_evidence()
    proposal = evidence.semantic_proposals[0]
    proposal.input_handles = input_handles
    proposal.supporting_handles = supporting_handles

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"vendor_identity", "vendor_identity_active"},
        trusted_sources=trusted,
    )

    assert proof.decision_for("vendor_identity_active").proof_status == "INCOMPLETE"
    assert len(proof.proposals) == 1
    assert proof.proposals[0].valid is False
    assert expected_error in proof.proposals[0].validation_errors


def test_singleton_by_role_conflict_cannot_enter_a_strong_proof_view() -> None:
    evidence, trusted = _invoice_evidence_with_duplicate_supplier()

    proof = compile_evidence_proof(
        [evidence],
        active_requirement_ids={"invoice", "invoice_fields_valid"},
        trusted_sources=trusted,
    )

    assert proof.decision_for("invoice_fields_valid").proof_status == "INCOMPLETE"
    assert not proof.proposals
    binding = [item for item in proof.diagnostics if item.category == "binding"]
    assert {item.code for item in binding} == {"SCOPE_CONFLICT"}
    assert {item.details.get("slot_id") for item in binding} == {
        "invoice_fields_valid:claim:invoice:supplier_identity"
    }
