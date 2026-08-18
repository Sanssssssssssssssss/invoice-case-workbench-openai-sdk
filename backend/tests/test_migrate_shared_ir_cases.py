from __future__ import annotations

import copy

from app.domain.invoice_contracts import build_requirement_contracts, contract_hole_id
from scripts.migrate_shared_ir_cases import _backup_files, migrate_document, restore_backup


def _case(claims: list[dict], *, judgments: list[dict] | None = None) -> dict:
    return {
        "case_id": "case_history",
        "requirements": [{"id": "vendor_identity"}, {"id": "vendor_identity_active"}],
        "compiled_proof": {"claims": [], "decision": {"proof_status": "PROVED"}},
        "evidence_items": [{
            "id": "ev_vendor",
            "type": "vendor_record",
            "source": "attachment",
            "credibility": "high",
            "review_result": {"should_accept": True},
            "supports": [{"requirement": "vendor_identity", "support_level": "full"}],
            "metadata": {
                "classification": "business_evidence",
                "claim_to_source_refs": claims,
                "semantic_judgments": judgments or [],
            },
        }],
    }


def _claim(identifier: str, predicate: str, value_type: str, value: str, quote: str) -> dict:
    return {
        "id": identifier,
        "subject": "vendor",
        "predicate": predicate,
        "value_type": value_type,
        "typed_value": value,
        "source_quote": quote,
        "source_locator": "vendor master row V-42",
        "confidence": "high",
    }


def test_complete_legacy_claims_and_proposal_use_current_holes() -> None:
    claims = [
        _claim("OLD_ID", "identity", "string", "V-42", "Vendor ID V-42"),
        _claim("OLD_STATUS", "status", "enum", "Active", "Status Active"),
        _claim("OLD_DATE", "status_effective_date", "date", "2025-11-12", "Effective 2025-11-12"),
    ]
    refs = [
        {"evidence_id": "ev_vendor", "claim_id": row["id"]}
        for row in claims
    ]
    document = _case(claims, judgments=[{
        "requirement_id": "vendor_identity_active",
        "verdict": "SUPPORTED",
        "input_refs": refs,
        "supporting_refs": refs,
        "opposing_refs": [],
        "open_questions": [],
        "confidence": "high",
    }])
    texts = {"ev_vendor": ("Vendor ID V-42\nStatus Active\nEffective 2025-11-12",)}

    migrated, stats, skipped = migrate_document(document, trusted_texts=texts)

    contracts, _ = build_requirement_contracts(["vendor_identity", "vendor_identity_active"])
    contract = next(item for item in contracts if item.requirement_id == "vendor_identity_active")
    expected_claim_holes = {
        contract_hole_id(item, contract)
        for item in contract.inputs
        if item.hole_kind in {"claim", "relation"}
    }
    expected_judgment = next(item for item in contract.inputs if item.hole_kind == "judgment")
    evidence = migrated["evidence_items"][0]
    assert {item["hole_id"] for item in evidence["semantic_claims"]} == expected_claim_holes
    assert evidence["semantic_proposals"][0]["hole_id"] == contract_hole_id(expected_judgment, contract)
    assert "claim_to_source_refs" not in evidence["metadata"]
    assert "semantic_judgments" not in evidence["metadata"]
    assert migrated["compiled_proof"] is None
    assert stats["claim_rows_migrated"] == 3
    assert stats["proposal_rows_migrated"] == 1
    assert skipped == {}


def test_incomplete_narrative_claim_and_ungrounded_strong_verdict_are_dropped() -> None:
    document = _case(
        [{"claim": "供应商有效", "quote": "Status Active", "confidence": "high"}],
        judgments=[{
            "requirement_id": "vendor_identity_active",
            "verdict": "SUPPORTED",
            "evidence_ids": ["ev_vendor"],
            "confidence": "high",
        }],
    )

    migrated, stats, skipped = migrate_document(
        document,
        trusted_texts={"ev_vendor": ("Status Active",)},
    )

    evidence = migrated["evidence_items"][0]
    assert evidence.get("semantic_claims", []) == []
    assert evidence.get("semantic_proposals", []) == []
    assert stats["claim_rows_skipped"] == 1
    assert stats["proposal_rows_skipped"] == 1
    assert stats["strong_conclusions_downgraded"] == 1
    assert skipped == {
        "incomplete_claim_shape": 1,
        "proposal_refs_unresolved": 1,
    }


def test_migration_is_idempotent_after_legacy_fields_are_removed() -> None:
    document = _case([
        _claim("OLD_STATUS", "status", "enum", "Active", "Status Active"),
    ])
    texts = {"ev_vendor": ("Status Active",)}
    first, first_stats, _ = migrate_document(document, trusted_texts=texts)
    second, second_stats, skipped = migrate_document(copy.deepcopy(first), trusted_texts=texts)

    assert first_stats["cases_planned"] == 1
    assert second == first
    assert second_stats["cases_planned"] == 0
    assert skipped == {}


def test_backup_manifest_restores_original_bytes(tmp_path) -> None:
    workspace = tmp_path / "cases"
    target = workspace / "case_history" / "case_state.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original\n")
    backup = tmp_path / "backup"

    _backup_files(workspace, [target], backup)
    target.write_bytes(b"migrated\n")
    result = restore_backup(backup)

    assert result["restored_files"] == 1
    assert target.read_bytes() == b"original\n"
