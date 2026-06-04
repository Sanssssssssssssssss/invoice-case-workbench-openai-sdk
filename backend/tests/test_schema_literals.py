from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.state.schemas import CasePatch, EvidenceReviewResult


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
