from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.domain.invoice_requirements import (
    AP_LITE_REQUIREMENTS,
    COMPILER_DERIVED_REQUIREMENTS,
    INVOICE_OPTIONAL_FIELD_REQUIREMENTS,
    INVOICE_REQUIRED_FIELD_REQUIREMENTS,
    POLICY_PATH,
    REQUIREMENT_CATALOG_VERSION,
    REQUIREMENT_PACK,
    REQUIREMENT_PLANNING_HINTS,
    REVIEWER_DERIVED_REQUIREMENTS,
    load_requirement_pack,
    profile_requirements,
    requirement_premises,
)
from app.state.case_store import CaseStore


def test_requirement_profiles_are_loaded_from_versioned_policy_pack() -> None:
    assert POLICY_PATH.exists()
    assert REQUIREMENT_PACK["requirement_pack_version"] == "aurora_requirement_pack_v1"
    assert REQUIREMENT_CATALOG_VERSION == "aurora_requirement_pack_v1"
    assert "proof_contracts" not in REQUIREMENT_PACK
    assert "contract_version" not in REQUIREMENT_PACK
    assert set(REQUIREMENT_PLANNING_HINTS["three_way_amount_match"]) == {
        "activation",
        "activation_requirement_groups",
        "capability",
        "target_predicate",
    }
    assert AP_LITE_REQUIREMENTS == (
        "invoice",
        "purchase_order",
        "goods_receipt_or_service_acceptance",
        "vendor_identity",
        "duplicate_payment_screen",
    )
    assert "currency_tax" in INVOICE_REQUIRED_FIELD_REQUIREMENTS
    assert "template_match" in INVOICE_OPTIONAL_FIELD_REQUIREMENTS
    assert "invoice" not in INVOICE_REQUIRED_FIELD_REQUIREMENTS
    assert "invoice_calculation_valid" not in INVOICE_REQUIRED_FIELD_REQUIREMENTS
    assert profile_requirements("invoice_only")[:2] == ("invoice", "invoice_number")
    assert "invoice_calculation_valid" in profile_requirements("invoice_only")
    for profile_id, rows in REQUIREMENT_PACK["profiles"].items():
        profile_ids = {str(row["id"]) for row in rows}
        if "invoice" in profile_ids:
            assert "invoice_calculation_valid" in profile_ids, profile_id
    assert COMPILER_DERIVED_REQUIREMENTS == {"three_way_amount_match", "no_active_duplicate"}
    assert "vendor_bank_account_authorized" in REVIEWER_DERIVED_REQUIREMENTS
    assert "approval_authority_limits" in REQUIREMENT_PACK["unconfigured_policy_values"]
    assert "invoice_calculation_rounding_tolerance" not in REQUIREMENT_PACK["unconfigured_policy_values"]
    assert REQUIREMENT_PACK["invoice_calculation_rounding_tolerance"] == {
        "amount": "0.01",
        "unit": "document_currency",
        "scope": "invoice_internal_arithmetic_rounding",
        "note": "Absolute rounding allowance for one invoice's internal arithmetic; this is not the three-way matching percentage tolerance.",
    }


def test_enterprise_profiles_keep_materials_and_semantic_conclusions_separate() -> None:
    assert profile_requirements("bank_change_control") == (
        "vendor_identity",
        "vendor_bank_change_record",
        "vendor_bank_account_authorized",
    )
    assert profile_requirements("approval_control") == (
        "approval_matrix",
        "invoice_approval_record",
        "approval_authority_satisfied",
    )
    assert profile_requirements("exception_hold_control") == (
        "exception_hold_record",
        "no_unresolved_payment_hold",
    )
    assert profile_requirements("audit_control") == ("audit_trail_record", "audit_chain_complete")
    assert requirement_premises("approval_authority_satisfied") == (
        "approval_matrix",
        "invoice_approval_record",
    )


def test_requirement_pack_rejects_unknown_policy_values_and_nested_reviewer_rules(tmp_path) -> None:
    path = tmp_path / "policy.json"
    pack = deepcopy(REQUIREMENT_PACK)
    pack["requirements"]["vendor_identity_active"]["required_policy_values"] = ["misspelled_policy_key"]
    path.write_text(json.dumps(pack), encoding="utf-8")
    with pytest.raises(ValueError, match="policy values"):
        load_requirement_pack(path)

    pack = deepcopy(REQUIREMENT_PACK)
    pack["requirements"]["vendor_identity_active"]["premise_requirements"] = ["audit_chain_complete"]
    path.write_text(json.dumps(pack), encoding="utf-8")
    with pytest.raises(ValueError, match="evidence-owned"):
        load_requirement_pack(path)


def test_requirement_pack_rejects_old_contract_fields_in_planning_hints(tmp_path) -> None:
    path = tmp_path / "policy.json"
    pack = deepcopy(REQUIREMENT_PACK)
    pack["planning_hints"]["vendor_identity_active"]["proof_template"] = "semantic_gate"
    path.write_text(json.dumps(pack), encoding="utf-8")

    with pytest.raises(ValueError, match="planning hint"):
        load_requirement_pack(path)


def test_requirement_pack_rejects_invoice_profile_without_calculation(tmp_path) -> None:
    path = tmp_path / "policy.json"
    pack = deepcopy(REQUIREMENT_PACK)
    pack["profiles"]["invoice_only"] = [
        row
        for row in pack["profiles"]["invoice_only"]
        if row["id"] != "invoice_calculation_valid"
    ]
    path.write_text(json.dumps(pack), encoding="utf-8")

    with pytest.raises(ValueError, match="must include invoice_calculation_valid"):
        load_requirement_pack(path)


def test_case_store_uses_pack_owned_requirement_metadata(tmp_path) -> None:
    store = CaseStore(tmp_path)
    updated = store.apply_patch(
        "case_pack_owned_requirements",
        {
            "patch_type": "update_case",
            "case_updates": {
                "requirements": [
                    {"id": "invoice", "label": "agent override", "kind": "risk_check", "required": False},
                    {"id": "vendor_bank_change_record", "kind": "field"},
                    {"id": "vendor_bank_account_authorized", "kind": "document"},
                ]
            },
        },
    )

    requirements = {item.id: item for item in updated.requirements}
    assert (requirements["invoice"].label, requirements["invoice"].kind, requirements["invoice"].required) == (
        "发票文档",
        "document",
        True,
    )
    assert requirements["vendor_bank_change_record"].kind == "document"
    assert requirements["vendor_bank_account_authorized"].kind == "cross_check"

    with pytest.raises(ValueError, match="Unknown requirement id"):
        store.apply_patch(
            "case_pack_owned_requirements",
            {"patch_type": "update_case", "case_updates": {"requirements": [{"id": "agent_invented_rule"}]}},
        )


def test_reviewer_conclusion_cannot_be_satisfied_by_plain_evidence_support(tmp_path) -> None:
    store = CaseStore(tmp_path)
    inferred = store.apply_patch(
        "case_reviewer_owned_conclusion",
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "add_evidence": [
                    {
                        "id": "ev_bank_claim",
                        "type": "vendor_record",
                        "credibility": "high",
                        "review_result": {"should_accept": True, "evidence_type": "vendor_record"},
                        "supports": [
                            {
                                "requirement": "vendor_bank_account_authorized",
                                "support_level": "full",
                                "quoted_text": "Bank account authorized",
                            }
                        ],
                    }
                ]
            },
        },
    )

    inferred_requirements = {item.id: item for item in inferred.requirements}
    assert "vendor_bank_account_authorized" not in inferred_requirements
    assert inferred_requirements == {}
    assert "vendor_bank_account_authorized" not in inferred.satisfied_materials

    reloaded_requirements = {item.id: item for item in store.load("case_reviewer_owned_conclusion").requirements}
    assert "vendor_bank_account_authorized" not in reloaded_requirements

    activated = store.apply_patch(
        "case_reviewer_owned_conclusion",
        {
            "patch_type": "update_case",
            "case_updates": {"requirements": [{"id": "vendor_bank_account_authorized"}]},
        },
    )
    activated_requirements = {item.id: item for item in activated.requirements}
    assert activated_requirements["vendor_bank_account_authorized"].required is True
    assert activated_requirements["vendor_bank_account_authorized"].status == "missing"
    assert activated_requirements["vendor_identity"].required is True
    assert activated_requirements["vendor_bank_change_record"].required is True


def test_legacy_case_load_canonicalizes_requirement_and_support_aliases(tmp_path) -> None:
    store = CaseStore(tmp_path)
    root = store.ensure_case_dirs("case_legacy_aliases")
    (root / "case_state.json").write_text(
        json.dumps({
            "case_id": "case_legacy_aliases",
            "requirements": [
                {"id": "goods_receipt"},
                {"id": "vendor_record"},
                {"id": "duplicate_payment_check"},
            ],
            "evidence_items": [{
                "id": "ev_legacy",
                "type": "goods_receipt",
                "source": "user_message",
                "review_result": {"should_accept": True},
                "supports": [{"requirement": "goods_receipt", "support_level": "full"}],
            }],
        }),
        encoding="utf-8",
    )

    state = store.load("case_legacy_aliases")

    assert {item.id for item in state.requirements} == {
        "goods_receipt_or_service_acceptance",
        "vendor_identity",
        "duplicate_payment_screen",
    }
    assert state.evidence_items[0].supports[0].requirement == "goods_receipt_or_service_acceptance"
