from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.invoice_proof_compiler import compile_invoice_proof as _compile_invoice_proof, load_invoice_proof_policy
from app.state.schemas import EvidenceItem, VerificationRecord


def _evidence(
    evidence_id: str,
    evidence_type: str,
    amount: str | None,
    *,
    currency: str = "GBP",
    basis: str | None = None,
    tax_basis: str = "gross",
    coverage: str = "full",
    order_scope_identity: str | None = "PO-SCOPE-1",
) -> EvidenceItem:
    basis = basis or {"invoice": "invoice_total", "purchase_order": "order_total", "goods_receipt": "received_value"}[evidence_type]
    field = ({
        "value": amount,
        "source_quote": f"Total {currency} {amount}",
        "source_locator": f"{evidence_id}:total",
        "confidence": "high",
    } if amount is not None else None)
    key = {"invoice": "amount_total", "purchase_order": "po_amount", "goods_receipt": "received_value"}[evidence_type]
    fields = {
        key: field,
        "currency": {"value": currency, "source_quote": f"Currency {currency}", "source_locator": f"{evidence_id}:currency", "confidence": "high"},
        "basis": {"value": basis, "source_quote": f"Amount basis {basis}", "source_locator": f"{evidence_id}:basis", "confidence": "high"},
        "tax_basis": {"value": tax_basis, "source_quote": f"Tax basis {tax_basis}", "source_locator": f"{evidence_id}:tax_basis", "confidence": "high"},
        "coverage": {"value": coverage, "source_quote": f"Coverage {coverage}", "source_locator": f"{evidence_id}:coverage", "confidence": "high"},
    } if field else {}
    requirement = {"invoice": "invoice", "purchase_order": "purchase_order", "goods_receipt": "goods_receipt_or_service_acceptance"}[evidence_type]
    scope_claims = ([{
        "id": f"CLM_{evidence_id.upper()}_ORDER_SCOPE_IDENTITY",
        "subject": evidence_type,
        "predicate": "order_scope_identity",
        "value_type": "string",
        "typed_value": order_scope_identity,
        "source_quote": f"Order reference {order_scope_identity}",
        "source_locator": f"{evidence_id}:order_reference",
        "confidence": "high",
    }] if order_scope_identity else [])
    return EvidenceItem(
        id=evidence_id,
        type=evidence_type,
        credibility="high",
        source="attachment",
        review_result={"should_accept": True},
        supports=[{"requirement": requirement, "support_level": "full"}],
        metadata={
            "classification": "business_evidence",
            "source_traceability": "original_document",
            "extracted_fields": fields,
            "claim_to_source_refs": scope_claims,
        },
    )


def compile_invoice_proof(
    items: list[EvidenceItem],
    verification_records: list[VerificationRecord] | None = None,
    policy: object | None = None,
    *,
    judgment_verdict: str = "SUPPORTED",
):
    refs = [
        {"evidence_id": item.id, "subject": str(item.type), "predicate": "amount"}
        for item in items
        if item.type in {"invoice", "purchase_order", "goods_receipt"}
    ]
    carriers = sorted(
        (
            item
            for item in items
            if item.source == "attachment" and item.review_result.get("should_accept") is True
        ),
        key=lambda item: item.id,
    )
    carrier = carriers[0] if carriers else items[0]
    carrier.metadata["semantic_judgments"] = [{
        "id": "JDG_AMOUNT_SCOPE_COMPARABLE",
        "verdict": judgment_verdict,
        "input_refs": refs,
        "supporting_refs": refs if judgment_verdict == "SUPPORTED" else [],
        "opposing_refs": refs if judgment_verdict == "REFUTED" else [],
        "open_questions": [],
        "confidence": "high",
        "reason": "The source-linked totals describe the same economic scope.",
    }]
    return _compile_invoice_proof(
        items,
        verification_records,
        policy,
        trusted_sources=_trusted_sources(items),
    )


def _trusted_sources(items: list[EvidenceItem]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in items:
        fields = item.metadata.get("extracted_fields") or {}
        quotes = [
            str(field.get("source_quote") or "")
            for field in fields.values()
            if isinstance(field, dict) and field.get("source_quote")
        ]
        quotes.extend(item.quoted_text)
        quotes.extend(
            str(row.get("source_quote") or row.get("quote") or "")
            for row in item.metadata.get("claim_to_source_refs") or []
            if isinstance(row, dict)
        )
        text = "\n".join(quotes)
        result[item.id] = {
            "texts": (text,),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "original_ref": f"test/{item.id}",
        }
    return result


def test_compiler_distinguishes_proved_disproved_and_incomplete() -> None:
    clean = [_evidence("inv", "invoice", "10000"), _evidence("po", "purchase_order", "10000"), _evidence("grn", "goods_receipt", "10000")]
    proved = compile_invoice_proof(clean)
    assert proved.decision.proof_status == "PROVED"
    assert proved.decision.outcome == "EVIDENCE_SUFFICIENT_FOR_REPORT"

    mismatch = compile_invoice_proof([_evidence("inv", "invoice", "10500"), *clean[1:]])
    assert mismatch.decision.proof_status == "DISPROVED"
    assert mismatch.decision.outcome == "EVIDENCE_SUFFICIENT_FOR_REPORT"
    assert mismatch.obligations == []

    incomplete = compile_invoice_proof([clean[0], _evidence("po", "purchase_order", None), clean[2]])
    assert incomplete.decision.proof_status == "INCOMPLETE"
    assert incomplete.decision.outcome == "HOLD_FOR_EVIDENCE"
    assert [item.id for item in incomplete.obligations] == ["OBL_PO_AMOUNT_PRESENT"]


def test_equal_amounts_from_unrelated_order_scopes_cannot_prove_match() -> None:
    proof = compile_invoice_proof([
        _evidence("inv", "invoice", "10000"),
        _evidence("po", "purchase_order", "10000"),
        _evidence("grn", "goods_receipt", "10000", order_scope_identity="PO-200"),
    ])

    assert proof.decision.proof_status == "DISPROVED"
    assert "CHK_DOCUMENT_SCOPE_IDENTITY" in proof.decision.failing_check_ids


def test_missing_order_scope_identity_is_incomplete() -> None:
    proof = compile_invoice_proof([
        _evidence("inv", "invoice", "10000"),
        _evidence("po", "purchase_order", "10000"),
        _evidence("grn", "goods_receipt", "10000", order_scope_identity=None),
    ])

    assert proof.decision.proof_status == "INCOMPLETE"
    assert "OBL_DOCUMENT_SCOPE_IDENTITY" in proof.decision.obligation_ids


def test_order_scope_identity_must_occur_in_its_source_quote() -> None:
    grn = _evidence("grn", "goods_receipt", "10000", order_scope_identity="PO-OTHER")
    grn.metadata["claim_to_source_refs"][0]["typed_value"] = "PO-SCOPE-1"
    proof = compile_invoice_proof([
        _evidence("inv", "invoice", "10000"),
        _evidence("po", "purchase_order", "10000"),
        grn,
    ])

    assert proof.decision.proof_status == "INCOMPLETE"


def test_generic_order_word_cannot_be_used_as_scope_identity() -> None:
    items = [
        _evidence("inv", "invoice", "10000", order_scope_identity="PO-100"),
        _evidence("po", "purchase_order", "10000", order_scope_identity="PO-200"),
        _evidence("grn", "goods_receipt", "10000", order_scope_identity="PO-300"),
    ]
    for item in items:
        item.metadata["claim_to_source_refs"][0]["typed_value"] = "Order"

    proof = compile_invoice_proof(items)

    assert proof.decision.proof_status == "INCOMPLETE"


def test_amount_values_do_not_bypass_the_required_semantic_judgment() -> None:
    items = [
        _evidence("inv", "invoice", "10000"),
        _evidence("po", "purchase_order", "10000"),
        _evidence("grn", "goods_receipt", "10000"),
    ]
    proof = _compile_invoice_proof(items, trusted_sources=_trusted_sources(items))

    assert proof.decision.proof_status == "INCOMPLETE"
    assert "OBL_AMOUNT_SCOPE_COMPARABLE" in {item.id for item in proof.obligations}


def test_reviewer_field_cannot_self_certify_against_a_different_runtime_source() -> None:
    items = [
        _evidence("inv", "invoice", "10000"),
        _evidence("po", "purchase_order", "10000"),
        _evidence("grn", "goods_receipt", "10000"),
    ]
    trusted = _trusted_sources(items)
    trusted["inv"]["texts"] = ("Original invoice total GBP 9000",)

    proof = _compile_invoice_proof(items, trusted_sources=trusted)

    assert proof.decision.proof_status == "INCOMPLETE"
    assert all(claim.evidence_id != "inv" for claim in proof.claims)


def test_compiler_provenance_hash_and_exhaustion_are_deterministic() -> None:
    items = [_evidence("inv", "invoice", "10000"), _evidence("po", "purchase_order", None), _evidence("grn", "goods_receipt", "10000")]
    first = compile_invoice_proof(items)
    second = compile_invoice_proof(list(reversed(items)))
    assert first.decision.evidence_snapshot_hash == second.decision.evidence_snapshot_hash
    changed_policy = load_invoice_proof_policy().model_copy(update={"amount_tolerance_percent": Decimal("3")})
    assert compile_invoice_proof(items, policy=changed_policy).decision.evidence_snapshot_hash != first.decision.evidence_snapshot_hash
    changed_compiler = load_invoice_proof_policy().model_copy(update={"compiler_version": "next"})
    assert compile_invoice_proof(items, policy=changed_compiler).decision.evidence_snapshot_hash != first.decision.evidence_snapshot_hash
    assert first.claims[0].source_quote and first.claims[0].source_locator
    assert set(first.claims[0].attribute_sources) == {"currency", "basis", "tax_basis", "coverage"}
    exhausted = compile_invoice_proof(items, [VerificationRecord(
        obligation_id="OBL_PO_AMOUNT_PRESENT",
        action_id="ACT_PO_AMOUNT_PRESENT",
        proof_hash_before=first.decision.evidence_snapshot_hash,
        retry_allowed=False,
    )])
    assert exhausted.decision.outcome == "ABSTAIN_OR_ESCALATE"


def test_agent_patch_cannot_write_derived_proof() -> None:
    from app.state.schemas import CasePatch

    with pytest.raises(ValidationError):
        CasePatch.model_validate({"case_updates": {"compiled_proof": {}}})


def test_currency_can_be_normalized_from_the_amount_quote() -> None:
    item = _evidence("grn", "goods_receipt", "10000")
    item.metadata["extracted_fields"].pop("currency")
    proof = compile_invoice_proof([_evidence("inv", "invoice", "10000"), _evidence("po", "purchase_order", "10000"), item])
    assert proof.decision.proof_status == "PROVED"


def test_item_excerpt_cannot_replace_a_field_source_quote() -> None:
    item = _evidence("inv", "invoice", "10500")
    field = item.metadata["extracted_fields"]["amount_total"]
    item.quoted_text = [field.pop("source_quote")]
    proof = compile_invoice_proof([item, _evidence("po", "purchase_order", "10000"), _evidence("grn", "goods_receipt", "10000")])
    assert proof.decision.proof_status == "INCOMPLETE"
    assert all(not (claim.subject == "invoice" and claim.predicate == "amount") for claim in proof.claims)


def test_compiler_rejects_inadmissible_and_ambiguous_claims() -> None:
    rejected = _evidence("inv_rejected", "invoice", "10000")
    rejected.review_result = {"should_accept": False}
    ambiguous = compile_invoice_proof([
        rejected,
        _evidence("inv_a", "invoice", "10000"),
        _evidence("inv_b", "invoice", "11000"),
        _evidence("po", "purchase_order", "10000"),
        _evidence("grn", "goods_receipt", "10000"),
    ])
    assert ambiguous.decision.proof_status == "INCOMPLETE"
    assert "ambiguous" in next(check for check in ambiguous.checks if check.id == "CHK_INVOICE_AMOUNT_PRESENT").reason
    assert [item.id for item in ambiguous.obligations] == ["OBL_INVOICE_AMOUNT_PRESENT"]


def test_claim_dimensions_do_not_leak_into_amount_presence() -> None:
    duplicate_a = _evidence("inv_a", "invoice", "100.0", currency="GBP")
    duplicate_b = _evidence("inv_b", "invoice", "100.00", currency="EUR")
    proof = compile_invoice_proof([
        duplicate_a,
        duplicate_b,
        _evidence("po", "purchase_order", "100"),
        _evidence("grn", "goods_receipt", "100"),
    ])

    assert next(item for item in proof.checks if item.id == "CHK_INVOICE_AMOUNT_PRESENT").status == "PROVED"
    assert next(item for item in proof.checks if item.id == "CHK_CURRENCY_COMPATIBLE").status == "INCOMPLETE"


def test_stale_verification_record_cannot_exhaust_current_proof() -> None:
    items = [_evidence("inv", "invoice", "10000"), _evidence("po", "purchase_order", None), _evidence("grn", "goods_receipt", "10000")]
    current = compile_invoice_proof(items)
    stale = VerificationRecord(obligation_id="OBL_PO_AMOUNT_PRESENT", action_id="ACT_PO_AMOUNT_PRESENT", proof_hash_before="old", retry_allowed=False)
    assert compile_invoice_proof(items, [stale]).decision.outcome == "HOLD_FOR_EVIDENCE"
    stale.proof_hash_before = current.decision.evidence_snapshot_hash
    assert compile_invoice_proof(items, [stale]).decision.outcome == "ABSTAIN_OR_ESCALATE"


def test_tolerance_is_inclusive_and_european_amounts_are_typed() -> None:
    within = compile_invoice_proof([_evidence("inv", "invoice", "10.200,00"), _evidence("po", "purchase_order", "10.000,00"), _evidence("grn", "goods_receipt", "10.000,00")])
    outside = compile_invoice_proof([_evidence("inv", "invoice", "10.201,00"), _evidence("po", "purchase_order", "10.000,00"), _evidence("grn", "goods_receipt", "10.000,00")])
    assert within.decision.proof_status == "PROVED"
    assert outside.decision.proof_status == "DISPROVED"


def test_partial_coverage_is_incomplete_not_disproved() -> None:
    proof = compile_invoice_proof([
        _evidence("inv", "invoice", "10000"),
        _evidence("po", "purchase_order", "10000"),
        _evidence("grn", "goods_receipt", "10000", coverage="partial"),
    ])

    assert proof.decision.proof_status == "INCOMPLETE"
    assert "OBL_DOCUMENT_COVERAGE" in proof.decision.obligation_ids


def test_comparability_gate_ignores_refuted_judgment_when_coverage_is_partial() -> None:
    proof = compile_invoice_proof(
        [
            _evidence("inv", "invoice", "10000"),
            _evidence("po", "purchase_order", "10000"),
            _evidence("grn", "goods_receipt", "10000", coverage="partial"),
        ],
        judgment_verdict="REFUTED",
    )

    assert proof.decision.proof_status == "INCOMPLETE"
    assert next(item for item in proof.checks if item.id == "JDG_AMOUNT_SCOPE_COMPARABLE").status == "INCOMPLETE"


def test_mixed_gross_and_net_basis_is_incomplete_not_disproved() -> None:
    proof = compile_invoice_proof([
        _evidence("inv", "invoice", "10000", tax_basis="gross"),
        _evidence("po", "purchase_order", "10000", tax_basis="net"),
        _evidence("grn", "goods_receipt", "10000", tax_basis="gross"),
    ])

    assert proof.decision.proof_status == "INCOMPLETE"
    assert "OBL_TAX_BASIS_EQUAL" in proof.decision.obligation_ids


def test_disproved_check_stops_blocking_work_without_hiding_other_uncertainty() -> None:
    proof = compile_invoice_proof([
        _evidence("inv", "invoice", "10000", currency="GBP"),
        _evidence("po", "purchase_order", "10000", currency="EUR"),
        _evidence("grn", "goods_receipt", "10000", coverage="unknown"),
    ])

    assert proof.decision.proof_status == "DISPROVED"
    assert proof.decision.outcome == "EVIDENCE_SUFFICIENT_FOR_REPORT"
    assert proof.decision.obligation_ids == []
    assert proof.obligations and all(not item.blocking for item in proof.obligations)
    assert next(item for item in proof.checks if item.id == "CHK_AMOUNT_WITHIN_TOLERANCE").status == "NOT_APPLICABLE"


def test_amount_claim_without_a_real_locator_is_not_admissible() -> None:
    invoice = _evidence("inv", "invoice", "10000")
    invoice.metadata["extracted_fields"]["amount_total"].pop("source_locator")

    proof = compile_invoice_proof([invoice, _evidence("po", "purchase_order", "10000"), _evidence("grn", "goods_receipt", "10000")])

    assert proof.decision.proof_status == "INCOMPLETE"
    assert all(not (claim.evidence_id == "inv" and claim.predicate == "amount") for claim in proof.claims)


def test_cross_case_evidence_cannot_enter_the_proof() -> None:
    invoice = _evidence("inv", "invoice", "10000")
    invoice.metadata["classification"] = "mixed_case_document"

    proof = compile_invoice_proof([invoice, _evidence("po", "purchase_order", "10000"), _evidence("grn", "goods_receipt", "10000")])

    assert proof.decision.proof_status == "INCOMPLETE"
    assert all(claim.evidence_id != "inv" for claim in proof.claims)


def test_explicitly_unclear_or_low_confidence_fields_cannot_prove_a_check() -> None:
    invoice = _evidence("inv", "invoice", "10000")
    invoice.metadata["extracted_fields"]["basis"]["status"] = "unclear"
    po = _evidence("po", "purchase_order", "10000")
    po.metadata["extracted_fields"]["tax_basis"]["confidence"] = "low"

    proof = compile_invoice_proof([invoice, po, _evidence("grn", "goods_receipt", "10000")])

    assert proof.decision.proof_status == "INCOMPLETE"
    assert next(item for item in proof.checks if item.id == "CHK_AMOUNT_BASIS_COMPATIBLE").status == "INCOMPLETE"

    invoice = _evidence("inv", "invoice", "10000")
    invoice.metadata["extracted_fields"]["basis"].pop("confidence")
    proof = compile_invoice_proof([invoice, _evidence("po", "purchase_order", "10000"), _evidence("grn", "goods_receipt", "10000")])
    assert proof.decision.proof_status == "INCOMPLETE"


def test_malformed_reviewer_values_fail_closed_to_incomplete() -> None:
    invoice = _evidence("inv", "invoice", "10000")
    invoice.metadata["extracted_fields"]["basis"]["value"] = "invoice total"
    proof = compile_invoice_proof([invoice, _evidence("po", "purchase_order", "10000"), _evidence("grn", "goods_receipt", "10000")])
    assert proof.decision.proof_status == "INCOMPLETE"

    invoice = _evidence("inv", "invoice", "10000")
    invoice.metadata["extracted_fields"]["amount_total"]["confidence"] = "0.9"
    proof = compile_invoice_proof([invoice, _evidence("po", "purchase_order", "10000"), _evidence("grn", "goods_receipt", "10000")])
    assert proof.decision.proof_status == "INCOMPLETE"


def test_common_amount_field_is_supported_but_user_or_advisory_claims_are_not() -> None:
    invoice = _evidence("inv", "invoice", "10000")
    invoice.metadata["extracted_fields"]["amount"] = invoice.metadata["extracted_fields"].pop("amount_total")
    po = _evidence("po", "purchase_order", "10000")
    po.metadata["extracted_fields"]["approved_amount"] = po.metadata["extracted_fields"].pop("po_amount")
    assert compile_invoice_proof([invoice, po, _evidence("grn", "goods_receipt", "10000")]).decision.proof_status == "PROVED"

    invoice.source = "user_message"
    advisory = _evidence("po_advisory", "purchase_order", "10000")
    advisory.metadata["truth_status"] = "advisory"
    proof = compile_invoice_proof([invoice, advisory, _evidence("grn", "goods_receipt", "10000")])
    assert proof.decision.proof_status == "INCOMPLETE"
    assert {claim.evidence_id for claim in proof.claims} == {"grn"}


def test_conflicting_amount_aliases_fail_closed() -> None:
    po = _evidence("po", "purchase_order", "10000")
    po.metadata["extracted_fields"]["approved_amount"] = {
        "value": "11000",
        "source_quote": "Approved amount GBP 11000",
        "source_locator": "po:approved_amount",
        "confidence": "high",
    }
    proof = compile_invoice_proof([_evidence("inv", "invoice", "10000"), po, _evidence("grn", "goods_receipt", "10000")])
    assert proof.decision.proof_status == "INCOMPLETE"
    assert next(check for check in proof.checks if check.id == "CHK_PO_AMOUNT_PRESENT").status == "INCOMPLETE"
