from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.domain.invoice_contracts import build_requirement_contracts
from app.domain.invoice_requirements import (
    AUTO_DERIVED_COMPILER_REQUIREMENTS,
    COMPILER_AUTHORITY_REQUIREMENTS,
    EXPLICIT_COMPILER_REQUIREMENTS,
    PROOF_CONTRACT_DEFINITIONS,
    REQUIREMENT_PACK,
    load_requirement_pack,
)
from app.state.schemas import CaseEvidenceIR, Claim, ClaimSource, ProofProposal, ProposalRef, Requirement, SourceBinding


def test_contract_builder_separates_explicit_and_derived_activation() -> None:
    contracts, _ = build_requirement_contracts(
        [
            Requirement(id="invoice", required=False),
            Requirement(id="purchase_order"),
            Requirement(id="goods_receipt"),
        ]
    )
    by_requirement = {item.requirement_id: item for item in contracts}

    assert "three_way_amount_match" in AUTO_DERIVED_COMPILER_REQUIREMENTS
    assert "vendor_identity_active" in EXPLICIT_COMPILER_REQUIREMENTS
    assert COMPILER_AUTHORITY_REQUIREMENTS == set(PROOF_CONTRACT_DEFINITIONS)
    assert by_requirement["three_way_amount_match"].activation == "derived"
    assert by_requirement["three_way_amount_match"].proof_template == "reconciliation"
    assert by_requirement["three_way_amount_match"].evidence_roles == [
        "invoice",
        "purchase_order",
        "goods_receipt_or_service_acceptance",
    ]
    assert by_requirement["invoice"].proof_template == "evidence_support"
    assert "vendor_identity_active" not in by_requirement


def test_derived_contract_cannot_be_activated_without_its_inputs() -> None:
    contracts, _ = build_requirement_contracts(["three_way_amount_match", "no_active_duplicate"])

    assert contracts == []


def test_contract_builder_returns_only_unresolved_holes() -> None:
    requirements = [
        Requirement(id="approval_matrix"),
        Requirement(id="invoice_approval_record"),
        Requirement(id="approval_authority_satisfied"),
    ]
    empty_ir = CaseEvidenceIR(source_snapshot_hash="snapshot-1")
    contracts, holes = build_requirement_contracts(requirements, evidence_ir=empty_ir)
    conclusion = next(item for item in contracts if item.requirement_id == "approval_authority_satisfied")

    assert {item.kind for item in holes} == {"source", "claim", "relation", "judgment", "policy"}
    assert next(item for item in holes if item.policy_key == "approval_authority_limits").contract_ids == [
        conclusion.contract_id
    ]

    ir = CaseEvidenceIR(
        source_snapshot_hash="snapshot-1",
        claims=[
            Claim(
                id="CLM_APPROVER_IDENTITY",
                subject="approval",
                predicate="approver_identity",
                value_type="string",
                typed_value="A-100",
                evidence_id="ev_approval",
                source_quote="Approval recorded by user A-100 for GBP 1000",
                source_locator="approval export row 1",
                confidence="high",
            ),
            Claim(
                id="CLM_AUTHORITY_LEVEL",
                subject="approval",
                predicate="authority_level",
                value_type="string",
                typed_value="L2",
                evidence_id="ev_matrix",
                source_quote="User A-100 has authority level L2",
                source_locator="matrix row 1",
                confidence="high",
            ),
            Claim(
                id="CLM_APPROVED_AMOUNT",
                subject="approval",
                predicate="approved_amount",
                value_type="decimal",
                typed_value="1000",
                unit="money",
                    currency="GBP",
                    attribute_sources={
                        "currency": ClaimSource(
                            source_quote="Approval recorded by user A-100 for GBP 1000",
                            source_locator="approval export row 1",
                        )
                    },
                evidence_id="ev_approval",
                source_quote="Approval recorded by user A-100 for GBP 1000",
                source_locator="approval export row 1",
                confidence="high",
            ),
        ],
        source_bindings=[
            SourceBinding(
                evidence_id="ev_matrix",
                evidence_type="policy_excerpt",
                source="attachment",
                trusted=True,
                accepted=True,
                supports=["approval_matrix"],
                support_levels={"approval_matrix": "full"},
            ),
            SourceBinding(
                evidence_id="ev_approval",
                evidence_type="process_log",
                source="attachment",
                trusted=True,
                accepted=True,
                supports=["invoice_approval_record"],
                support_levels={"invoice_approval_record": "full"},
            ),
        ],
    )
    refs = [
        ProposalRef(
            claim_id=claim.id,
            evidence_id=claim.evidence_id,
            source_quote=claim.source_quote,
            source_locator=claim.source_locator,
        )
        for claim in ir.claims
    ]
    proposal = ProofProposal(
        id="proposal-1",
        contract_id=conclusion.contract_id,
        contract_hash=conclusion.contract_hash,
        target_predicate=conclusion.target_predicate,
        verdict="SUPPORTED",
        input_refs=refs,
        supporting_refs=refs,
        confidence="high",
        evidence_snapshot_hash="snapshot-1",
    )
    _, resolved_holes = build_requirement_contracts(requirements, evidence_ir=ir, proposals=[proposal])

    assert [(item.kind, item.policy_key) for item in resolved_holes] == [("policy", "approval_authority_limits")]


def test_shared_policy_hole_is_deduplicated_across_contracts() -> None:
    _, holes = build_requirement_contracts(
        [
            Requirement(id="invoice_fields_valid"),
            Requirement(id="tax_and_account_coding_valid"),
        ]
    )
    policy_holes = [item for item in holes if item.policy_key == "jurisdiction_tax_rules"]

    assert len(policy_holes) == 1
    assert policy_holes[0].requirement_ids == ["invoice_fields_valid", "tax_and_account_coding_valid"]


def test_duplicate_search_window_is_a_real_fail_closed_policy_hole() -> None:
    active = ["invoice", "duplicate_payment_screen"]
    _contracts, holes = build_requirement_contracts(active)

    assert [item.policy_key for item in holes if item.policy_key] == ["duplicate_search_window"]

    pack = deepcopy(REQUIREMENT_PACK)
    pack["duplicate_search_window"] = "365 days"
    pack["unconfigured_policy_values"].remove("duplicate_search_window")
    _contracts, configured_holes = build_requirement_contracts(active, pack=pack)
    assert not any(item.policy_key == "duplicate_search_window" for item in configured_holes)

    pack["duplicate_search_window"] = ""
    _contracts, empty_value_holes = build_requirement_contracts(active, pack=pack)
    assert any(item.policy_key == "duplicate_search_window" for item in empty_value_holes)


def test_wrong_claim_type_does_not_resolve_a_typed_hole() -> None:
    ir = CaseEvidenceIR(
        source_snapshot_hash="snapshot",
        claims=[
            Claim(
                id="CLM_VENDOR_STATUS_WRONG_TYPE",
                subject="vendor",
                predicate="status",
                value_type="string",
                typed_value="active",
                evidence_id="ev_vendor",
                source_quote="Vendor status active",
                source_locator="vendor row 1",
                confidence="high",
            )
        ],
        source_bindings=[
            SourceBinding(
                evidence_id="ev_vendor",
                evidence_type="vendor_record",
                source="attachment",
                trusted=True,
                accepted=True,
                supports=["vendor_identity"],
                support_levels={"vendor_identity": "full"},
            )
        ],
    )

    _contracts, holes = build_requirement_contracts(
        ["vendor_identity", "vendor_identity_active"],
        evidence_ir=ir,
    )

    assert any(
        item.semantic_key == "claim:vendor:status:same_entity:vendor"
        for item in holes
    )


def test_requirement_pack_rejects_contract_activation_cycles(tmp_path) -> None:
    pack = deepcopy(REQUIREMENT_PACK)
    pack["proof_contracts"]["three_way_amount_match"]["activation_requirement_groups"] = [
        ["no_active_duplicate"]
    ]
    pack["proof_contracts"]["no_active_duplicate"]["activation_requirement_groups"] = [
        ["three_way_amount_match"]
    ]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(pack), encoding="utf-8")

    with pytest.raises(ValueError, match="Cyclic requirement premises"):
        load_requirement_pack(path)


def test_requirement_pack_rejects_missing_or_conflicting_conclusion_contracts(tmp_path) -> None:
    missing = deepcopy(REQUIREMENT_PACK)
    del missing["proof_contracts"]["vendor_identity_active"]
    missing_path = tmp_path / "missing.json"
    missing_path.write_text(json.dumps(missing), encoding="utf-8")

    with pytest.raises(ValueError, match="Missing proof contracts"):
        load_requirement_pack(missing_path)

    conflicting = deepcopy(REQUIREMENT_PACK)
    conflicting["proof_contracts"]["vendor_bank_account_authorized"]["inputs"][0]["value_type"] = "string"
    conflicting_path = tmp_path / "conflicting.json"
    conflicting_path.write_text(json.dumps(conflicting), encoding="utf-8")

    with pytest.raises(ValueError, match="Conflicting proof input schema"):
        load_requirement_pack(conflicting_path)
