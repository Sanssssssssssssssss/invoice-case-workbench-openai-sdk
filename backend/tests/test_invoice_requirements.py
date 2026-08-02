from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from app.domain.invoice_requirements import (
    AP_LITE_REQUIREMENTS,
    COMPILER_DERIVED_REQUIREMENTS,
    INVOICE_OPTIONAL_FIELD_REQUIREMENTS,
    INVOICE_REQUIRED_FIELD_REQUIREMENTS,
    POLICY_PATH,
    REQUIREMENT_PACK,
    REVIEWER_DERIVED_REQUIREMENTS,
    load_requirement_pack,
    profile_requirements,
    requirement_premises,
)
from app.state.case_store import CaseStore
from app.state.attachment_manifest import save_attachment_manifest


def _save_sources(store: CaseStore, case_id: str, sources: dict[str, str]) -> None:
    rows = []
    for evidence_id, text in sources.items():
        original_ref = f"attachments/originals/{evidence_id}.md"
        path = store.resolve_case_path(case_id, original_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        rows.append(
            {
                "attachment_id": f"att_{evidence_id}",
                "name": f"{evidence_id}.md",
                "original_ref": original_ref,
                "status": "active",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "evidence_ids": [evidence_id],
            }
        )
    save_attachment_manifest(store, case_id, {"attachments": rows})


def test_requirement_profiles_are_loaded_from_versioned_policy_pack() -> None:
    assert POLICY_PATH.exists()
    assert REQUIREMENT_PACK["requirement_pack_version"] == "aurora_requirement_pack_v1"
    assert AP_LITE_REQUIREMENTS == (
        "invoice",
        "purchase_order",
        "goods_receipt_or_service_acceptance",
        "vendor_identity",
        "duplicate_payment_screen",
    )
    assert "currency_tax" in INVOICE_REQUIRED_FIELD_REQUIREMENTS
    assert "template_match" in INVOICE_OPTIONAL_FIELD_REQUIREMENTS
    assert COMPILER_DERIVED_REQUIREMENTS == {"three_way_amount_match", "no_active_duplicate"}
    assert "vendor_bank_account_authorized" in REVIEWER_DERIVED_REQUIREMENTS
    assert "approval_authority_limits" in REQUIREMENT_PACK["unconfigured_policy_values"]


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
        "发票原件",
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
    assert inferred_requirements["vendor_bank_account_authorized"].required is False
    assert inferred_requirements["vendor_bank_account_authorized"].status == "weak"
    assert inferred_requirements["vendor_identity"].required is False
    assert inferred_requirements["vendor_bank_change_record"].required is False
    assert "vendor_bank_account_authorized" not in inferred.satisfied_materials

    reloaded_requirements = {item.id: item for item in store.load("case_reviewer_owned_conclusion").requirements}
    assert reloaded_requirements["vendor_bank_account_authorized"].required is False
    refreshed = store.apply_patch(
        "case_reviewer_owned_conclusion",
        {"patch_type": "update_case", "case_updates": {"reply_brief": "unrelated refresh"}},
    )
    assert all(
        not item.required
        for item in refreshed.requirements
        if item.id in {"vendor_bank_account_authorized", "vendor_identity", "vendor_bank_change_record"}
    )

    activated = store.apply_patch(
        "case_reviewer_owned_conclusion",
        {
            "patch_type": "update_case",
            "case_updates": {"requirements": [{"id": "vendor_bank_account_authorized"}]},
        },
    )
    activated_requirements = {item.id: item for item in activated.requirements}
    assert activated_requirements["vendor_bank_account_authorized"].required is True
    assert activated_requirements["vendor_bank_account_authorized"].status == "weak"
    assert activated_requirements["vendor_identity"].required is True
    assert activated_requirements["vendor_bank_change_record"].required is True


@pytest.mark.parametrize(
    ("verdict", "evidence_ids", "open_questions", "expected_status"),
    [
        ("SUPPORTED", ["ev_vendor", "ev_bank"], [], "satisfied"),
        ("REFUTED", ["ev_vendor", "ev_bank"], [], "conflict"),
        ("SUPPORTED", "ev_vendor", [], "weak"),
        ("SUPPORTED", ["ev_vendor", "ev_bank"], {}, "weak"),
    ],
)
def test_trusted_reviewer_verdict_projects_semantic_conclusion(
    tmp_path, verdict, evidence_ids, open_questions, expected_status
) -> None:
    store = CaseStore(tmp_path)
    case_id = f"case_reviewer_verdict_{verdict.lower()}"
    _save_sources(
        store,
        case_id,
        {
            "ev_vendor": "Vendor V-100 is active with current bank ending 3344.",
            "ev_bank": "Bank change workflow for V-100 was independently verified.",
        },
    )
    updated = store.apply_patch(
        case_id,
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "requirements": [{"id": "vendor_bank_account_authorized"}],
                "add_evidence": [
                    {
                        "id": "ev_vendor",
                        "type": "vendor_record",
                        "source": "attachment",
                        "credibility": "high",
                        "review_result": {"should_accept": True, "evidence_type": "vendor_record"},
                        "supports": [{"requirement": "vendor_identity", "support_level": "full"}],
                        "metadata": {
                            "classification": "business_evidence",
                            "original_ref": "attachments/originals/ev_vendor.md",
                        },
                    },
                    {
                        "id": "ev_bank",
                        "type": "process_log",
                        "source": "attachment",
                        "credibility": "high",
                        "review_result": {"should_accept": True, "evidence_type": "vendor_record"},
                        "supports": [{"requirement": "vendor_bank_change_record", "support_level": "full"}],
                        "metadata": {
                            "classification": "business_evidence",
                            "original_ref": "attachments/originals/ev_bank.md",
                            "requirement_verdicts": [
                                {
                                    "requirement_id": "vendor_bank_account_authorized",
                                    "verdict": verdict,
                                    "evidence_ids": evidence_ids,
                                    "confidence": "high",
                                    "open_questions": open_questions,
                                    "reason": "Both current master data and the bank-change workflow were considered.",
                                }
                            ],
                        },
                    },
                ],
            },
        },
    )

    requirement = next(item for item in updated.requirements if item.id == "vendor_bank_account_authorized")
    assert requirement.status == expected_status
    assert requirement.evidence_ids == (["ev_vendor", "ev_bank"] if expected_status != "weak" else [])
    assert updated.status == ("ready_for_report" if expected_status != "weak" else "collecting_materials")


def test_reviewer_verdict_requires_trusted_full_support_for_each_premise(tmp_path) -> None:
    store = CaseStore(tmp_path)
    case_id = "case_reviewer_verdict_premise_trust"
    _save_sources(store, case_id, {"ev_bank": "The bank-change workflow was verified."})
    updated = store.apply_patch(
        case_id,
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "requirements": [{"id": "vendor_bank_account_authorized"}],
                "add_evidence": [
                    {
                        "id": "ev_vendor",
                        "type": "vendor_record",
                        "source": "user_message",
                        "credibility": "high",
                        "review_result": {"should_accept": True},
                        "supports": [{"requirement": "vendor_identity", "support_level": "full"}],
                    },
                    {
                        "id": "ev_bank",
                        "type": "process_log",
                        "source": "attachment",
                        "credibility": "high",
                        "review_result": {"should_accept": True},
                        "supports": [
                            {"requirement": "vendor_identity", "support_level": "partial"},
                            {"requirement": "vendor_bank_change_record", "support_level": "full"},
                        ],
                        "metadata": {
                            "classification": "business_evidence",
                            "original_ref": "attachments/originals/ev_bank.md",
                            "requirement_verdicts": [
                                {
                                    "requirement_id": "vendor_bank_account_authorized",
                                    "verdict": "SUPPORTED",
                                    "evidence_ids": ["ev_bank"],
                                    "confidence": "high",
                                    "open_questions": [],
                                    "reason": "The current bank-change record was reviewed.",
                                }
                            ],
                        },
                    },
                ],
            },
        },
    )

    requirement = next(item for item in updated.requirements if item.id == "vendor_bank_account_authorized")
    assert requirement.status == "weak"
    assert updated.status == "collecting_materials"


def test_unconfigured_policy_value_keeps_reviewer_verdict_incomplete(tmp_path) -> None:
    store = CaseStore(tmp_path)
    case_id = "case_unconfigured_approval_limit"
    _save_sources(store, case_id, {"ev_matrix": "Approval matrix", "ev_approval": "Approval record"})
    updated = store.apply_patch(
        case_id,
        {
            "patch_type": "add_evidence",
            "case_updates": {
                "requirements": [{"id": "approval_authority_satisfied"}],
                "add_evidence": [
                    {
                        "id": "ev_matrix",
                        "type": "policy_excerpt",
                        "source": "attachment",
                        "credibility": "high",
                        "review_result": {"should_accept": True},
                        "supports": [{"requirement": "approval_matrix", "support_level": "full"}],
                        "metadata": {"classification": "business_evidence", "original_ref": "attachments/originals/ev_matrix.md"},
                    },
                    {
                        "id": "ev_approval",
                        "type": "process_log",
                        "source": "attachment",
                        "credibility": "high",
                        "review_result": {"should_accept": True},
                        "supports": [{"requirement": "invoice_approval_record", "support_level": "full"}],
                        "metadata": {
                            "classification": "business_evidence",
                            "original_ref": "attachments/originals/ev_approval.md",
                            "requirement_verdicts": [
                                {
                                    "requirement_id": "approval_authority_satisfied",
                                    "verdict": "SUPPORTED",
                                    "evidence_ids": ["ev_matrix", "ev_approval"],
                                    "confidence": "high",
                                    "open_questions": [],
                                    "reason": "The approval record names an approver in the matrix.",
                                }
                            ],
                        },
                    },
                ],
            },
        },
    )

    requirement = next(item for item in updated.requirements if item.id == "approval_authority_satisfied")
    assert requirement.status == "weak"
    assert "approval_authority_satisfied" in updated.weak_materials


def test_legacy_requirement_ids_still_activate_compiler_programs(tmp_path) -> None:
    store = CaseStore(tmp_path)
    root = store.ensure_case_dirs("case_legacy_compiler_activation")
    (root / "case_state.json").write_text(
        json.dumps(
            {
                "case_id": "case_legacy_compiler_activation",
                "requirements": [
                    {"id": "invoice"},
                    {"id": "purchase_order"},
                    {"id": "goods_receipt"},
                    {"id": "vendor_record"},
                    {"id": "duplicate_payment_check"},
                ],
            }
        ),
        encoding="utf-8",
    )

    state = store.load("case_legacy_compiler_activation")

    assert state.compiled_proof is not None
    assert state.compiled_proof.decision_for("three_way_amount_match") is not None
    assert state.compiled_proof.decision_for("no_active_duplicate") is not None
