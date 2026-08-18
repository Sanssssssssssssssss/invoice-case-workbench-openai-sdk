from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.risk_rules import resolved_conflict_note
from app.state.schemas import CasePatch, EvidenceReviewerOutput, EvidenceReviewResult


def test_sparse_reviewer_rejects_bare_semantic_handles() -> None:
    with pytest.raises(ValidationError):
        EvidenceReviewerOutput.model_validate(
            {
                "sources": [
                    {
                        "local_source_handle": "s1",
                        "attachment_id": "att-1",
                        "type": "invoice",
                        "classification": "business_evidence",
                        "semantic_claims": ["c1"],
                        "semantic_proposals": ["p1"],
                    }
                ]
            }
        )


def test_evidence_review_rejects_unknown_enum_values() -> None:
    with pytest.raises(ValidationError):
        EvidenceReviewResult.model_validate(
            {
                "evidence_type": "invoice_bundle",
                "credibility": "strong",
                "should_accept": True,
                "supports": [],
                "conflicts": [],
                "suggested_patch": {},
                "reply_to_user": "",
            }
        )


@pytest.mark.parametrize("evidence_type", ["mixed_document_batch", "multi_document_review", "multi_document_packet"])
def test_evidence_review_normalizes_common_multi_document_shapes(evidence_type: str) -> None:
    result = EvidenceReviewResult.model_validate(
        {
            "evidence_type": evidence_type,
            "conflicts": [
                {
                    "conflict_type": "bank_account_mismatch",
                    "source_values": [
                        {"source": "vendor_record", "value": "9012"},
                        {"source": "email_request", "value": "7788"},
                    ],
                }
            ],
        }
    )

    assert result.evidence_type == "unknown"
    assert result.conflicts[0].source_values == {"vendor_record": "9012", "email_request": "7788"}


def test_evidence_review_normalizes_declared_conflict_value_shapes() -> None:
    result = EvidenceReviewResult.model_validate(
        {
            "conflicts": [{
                "details": {"invoice": "10500", "purchase_order": "10000"},
                "quoted_text": ["Invoice total 10500", "PO total 10000"],
                "conflict_with": ["purchase_order", "goods_receipt"],
            }]
        }
    )

    conflict = result.conflicts[0]
    assert conflict.details == '{"invoice":"10500","purchase_order":"10000"}'
    assert conflict.quoted_text == "Invoice total 10500\nPO total 10000"
    assert conflict.conflict_with == "purchase_order, goods_receipt"


def test_evidence_review_accepts_nested_conflict_resolution_status() -> None:
    result = EvidenceReviewResult.model_validate(
        {
            "suggested_patch": {
                "add_evidence": [
                    {
                        "type": "invoice",
                        "conflicts": [
                            {
                                "conflict_type": "amount_extraction_error",
                                "resolution_status": "pending_correction",
                            }
                        ],
                    }
                ]
            }
        }
    )

    assert result.suggested_patch.add_evidence[0].conflicts[0].resolution_status == "pending_correction"


def test_conflict_resolution_requires_structured_status() -> None:
    assert not resolved_conflict_note("Conflict clarified; use the PDF original.")
    assert resolved_conflict_note({"resolution_status": "resolved", "description": "OCR corrected"})


def test_case_patch_rejects_unknown_patch_type() -> None:
    with pytest.raises(ValidationError):
        CasePatch.model_validate({"patch_type": "approve_payment", "case_updates": {}, "audit_note": ""})


def test_case_patch_rejects_status_and_missing_materials_updates() -> None:
    with pytest.raises(ValidationError):
        CasePatch.model_validate(
            {
                "patch_type": "add_evidence",
                "case_updates": {"status": "ready_for_report", "missing_materials": []},
                "audit_note": "",
            }
        )


def test_evidence_review_rejects_unknown_extracted_field_status() -> None:
    with pytest.raises(ValidationError):
        EvidenceReviewResult.model_validate(
            {
                "extracted_fields": {"amount": {"value": "100", "status": "verified", "source_quote": "Amount 100"}},
            }
        )


def test_evidence_review_derives_cards_when_review_adds_evidence() -> None:
    derived = EvidenceReviewResult.model_validate(
        {
            "mode": "review",
            "evidence_type": "invoice",
            "suggested_patch": {
                "add_evidence": [
                    {
                        "type": "invoice",
                        "summary": "Invoice INV-001 captured.",
                        "supports": [{"requirement": "invoice_number", "support_level": "full"}],
                    }
                ]
            },
        }
    )

    assert derived.evidence_cards[0]["doc_type"] == "invoice"
    assert derived.suggested_patch.evidence_cards

    result = EvidenceReviewResult.model_validate(
        {
            "mode": "review",
            "evidence_type": "invoice",
            "evidence_cards": [{"title": "发票 INV-001", "doc_type": "invoice"}],
            "suggested_patch": {
                "add_evidence": [
                    {
                        "type": "invoice",
                        "supports": [{"requirement": "invoice_number", "support_level": "full"}],
                    }
                ]
            },
        }
    )
    assert result.evidence_cards[0]["title"] == "发票 INV-001"
