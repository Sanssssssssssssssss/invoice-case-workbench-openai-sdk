from __future__ import annotations

from types import SimpleNamespace

from scripts.run_eval_scenarios import _complete_claim_consistency_errors, _reply_claims_complete, _requirement_status


def _state(requirement_statuses: dict[str, str], evidence_types: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        requirements=[
            SimpleNamespace(id=req, status=status)
            for req, status in requirement_statuses.items()
        ],
        evidence_items=[
            SimpleNamespace(type=evidence_type)
            for evidence_type in evidence_types
        ],
    )


def test_complete_reply_requires_complete_case_state() -> None:
    state = _state(
        {
            "invoice": "satisfied",
            "purchase_order": "missing",
            "goods_receipt": "missing",
            "vendor_record": "missing",
            "duplicate_payment_check": "missing",
        },
        ["invoice"],
    )

    assert _reply_claims_complete("五项核心要求均满足，证据链完整。")
    errors = _complete_claim_consistency_errors(state)

    assert any("unsatisfied requirements" in item for item in errors)
    assert any("at least 5 evidence items" in item for item in errors)
    assert any("missing core evidence types" in item for item in errors)


def test_complete_reply_detector_ignores_future_or_improvement_language() -> None:
    assert not _reply_claims_complete("建议补充材料以提高证据链完整性，待材料齐全后再生成报告。")
    assert not _reply_claims_complete("当前证据链不足，需补齐后方可生成最终报告。")
    assert not _reply_claims_complete("补齐这两项后，case 状态可推进至 ready_for_report。")
    assert not _reply_claims_complete("两项材料补齐后，案卷状态将变为 ready_for_report，可生成最终报告。")
    assert not _reply_claims_complete("材料齐全后可生成最终报告。")
    assert not _reply_claims_complete("当所有必需材料齐备且证据链完整后，case 状态将变为 ready_for_report。")


def test_complete_reply_detector_ignores_negative_missing_language() -> None:
    assert not _reply_claims_complete("\u6838\u5fc3\u7f3a\u5931\u6750\u6599\uff08\u5168\u90e8\u672a\u6ee1\u8db3\uff09")
    assert not _reply_claims_complete("\u4e94\u9879\u6838\u5fc3\u8981\u6c42\u5747\u672a\u6ee1\u8db3\uff0c\u8bf7\u7ee7\u7eed\u8865\u5145\u6750\u6599\u3002")


def test_complete_reply_accepts_complete_case_state() -> None:
    requirements = {
        "invoice": "satisfied",
        "purchase_order": "satisfied",
        "goods_receipt": "satisfied",
        "vendor_record": "satisfied",
        "duplicate_payment_check": "satisfied",
    }
    evidence_types = list(requirements)

    assert _complete_claim_consistency_errors(_state(requirements, evidence_types)) == []


def test_complete_reply_accepts_canonical_requirements_with_legacy_evidence_types() -> None:
    requirements = {
        "invoice": "satisfied",
        "purchase_order": "satisfied",
        "goods_receipt_or_service_acceptance": "satisfied",
        "vendor_identity": "satisfied",
        "duplicate_payment_screen": "satisfied",
    }
    evidence_types = ["invoice", "purchase_order", "goods_receipt", "vendor_record", "duplicate_payment_check"]

    assert _complete_claim_consistency_errors(_state(requirements, evidence_types)) == []


def test_requirement_status_expectation_accepts_legacy_alias() -> None:
    state = _state({"duplicate_payment_screen": "conflict"}, ["duplicate_payment_check"])

    assert _requirement_status(state, "duplicate_payment_check") == "conflict"
