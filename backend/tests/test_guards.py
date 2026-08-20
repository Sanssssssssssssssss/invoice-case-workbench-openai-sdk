from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.context import _sanitize_report_markdown_for_guards
from app.guards import CaseStateConsistencyError, NoExecutionWordingError, enforce_case_state_consistency, enforce_no_execution_wording
from app.state.schemas import SupervisorDecision, new_case_state


def _supported_runtime_proof(requirement_id: str, *, source_id: str, claim_id: str) -> SimpleNamespace:
    root_id = f"check.{requirement_id}"
    artifact = SimpleNamespace(
        evidence_ir=SimpleNamespace(
            claims=[
                SimpleNamespace(
                    id=claim_id,
                    source_id=source_id,
                    quote="grounded source quote",
                    locator="line 1",
                )
            ]
        )
    )
    proof = SimpleNamespace(
        node_results=[
            SimpleNamespace(
                node_id=root_id,
                status="SUPPORTED",
                claim_ids=[claim_id],
                source_ids=[source_id],
            )
        ],
        decisions=[
            SimpleNamespace(
                requirement_id=requirement_id,
                root_node_id=root_id,
                status="SUPPORTED",
            )
        ],
    )
    return SimpleNamespace(artifact=artifact, proof=proof)


def test_no_execution_wording_blocks_direct_execution_claims() -> None:
    for text in ("approved and paid", "I have paid the invoice.", "invoice has been paid.", "I can submit to ERP."):
        with pytest.raises(NoExecutionWordingError):
            enforce_no_execution_wording(text)


def test_no_execution_wording_allows_material_and_negative_history_context() -> None:
    assert enforce_no_execution_wording("Please provide the approved purchase order as review evidence.")
    assert enforce_no_execution_wording("Duplicate-payment check shows no paid record in the submitted history export.")
    assert enforce_no_execution_wording("本案件已提交完整AP五件套材料，可生成本地审查报告。")


def test_report_sanitizer_neutralizes_unsupported_execution_status_cells() -> None:
    markdown = "| unsupported status |\n|---|\n| invoice has been paid |"
    sanitized = _sanitize_report_markdown_for_guards(markdown, new_case_state("case_guard"))

    with pytest.raises(NoExecutionWordingError):
        enforce_no_execution_wording(sanitized)


def test_supervisor_decision_rejects_old_role_tool_fields() -> None:
    with pytest.raises(ValueError):
        SupervisorDecision.model_validate({"action": "call_tool", "tool": "list_case_files", "input": {}})


def test_supervisor_decision_keeps_trace_fields_compact() -> None:
    decision = SupervisorDecision(
        action="call_tool",
        target="read_attachment",
        short_plan=["read an extremely long attachment explanation that should not enter trace in full", "review", "write", "extra"],
        plan_progress="intent=submit_evidence; route=attachment_evidence; step=read_attachment; commentary=too long",
        reason="This is a deliberately long reason. " * 20,
    )

    assert len(decision.short_plan) == 3
    assert all(len(item) <= 36 for item in decision.short_plan)
    assert decision.plan_progress == "intent=submit_evidence; route=attachment_evidence; step=read_attachment"
    assert len(decision.reason) <= 160


def test_supervisor_decision_rejects_write_patch_as_call_tool() -> None:
    with pytest.raises(ValueError):
        SupervisorDecision.model_validate({"action": "call_tool", "target": "write_case_patch", "input": {}})


def test_case_state_consistency_blocks_complete_claim_when_requirements_missing() -> None:
    state = SimpleNamespace(
        requirements=[
            SimpleNamespace(id="invoice", status="satisfied", required=True),
            SimpleNamespace(id="purchase_order", status="missing", required=True),
        ],
        evidence_items=[SimpleNamespace(type="invoice", supports=[SimpleNamespace(requirement="invoice")])],
    )

    with pytest.raises(CaseStateConsistencyError):
        enforce_case_state_consistency("status = ready_for_report", state)


def test_case_state_consistency_allows_capability_introduction_without_case_truth() -> None:
    state = SimpleNamespace(requirements=[], evidence_items=[])
    text = "你好，我是发票证据审查工作台助手。我能帮你收集材料、审查证据并生成本地报告。"

    assert enforce_case_state_consistency(text, state) == text

    with pytest.raises(CaseStateConsistencyError):
        enforce_case_state_consistency("五项核心材料均已满足，可以进入报告阶段。", state)


def test_case_state_consistency_rejects_explicit_stale_requirement_status() -> None:
    state = SimpleNamespace(
        requirements=[
            SimpleNamespace(id="invoice", label="发票文档", status="accepted", required=True),
            SimpleNamespace(id="no_active_duplicate", label="无仍具经济效力的重复付款", status="weak", required=True),
        ],
    )

    with pytest.raises(CaseStateConsistencyError, match="requirement invoice is accepted"):
        enforce_case_state_consistency("- **invoice**：weak（证明未成立）", state)

    with pytest.raises(CaseStateConsistencyError, match="requirement invoice is accepted"):
        enforce_case_state_consistency("按要求未生成报告。\n\n- **invoice**：weak（证明未成立）", state)

    assert (
        enforce_case_state_consistency(
            "invoice=accepted；no_active_duplicate=NOT_FOUND",
            state,
        )
        == "invoice=accepted；no_active_duplicate=NOT_FOUND"
    )


def test_case_state_consistency_accepts_compiler_derived_requirement_support() -> None:
    compiled = _supported_runtime_proof(
        "three_way_amount_match",
        source_id="ev_001",
        claim_id="claim_001",
    )
    state = SimpleNamespace(
        requirements=[SimpleNamespace(id="three_way_amount_match", status="satisfied", required=True)],
        evidence_items=[SimpleNamespace(id="ev_001", supports=[])],
        review_artifact=compiled.artifact,
        compiled_proof=compiled.proof,
    )

    assert enforce_case_state_consistency("status = ready_for_report", state) == "status = ready_for_report"


def test_case_state_consistency_ignores_optional_weak_but_blocks_optional_conflict() -> None:
    text = "status = ready_for_report"
    compiled = _supported_runtime_proof(
        "invoice_number",
        source_id="ev_invoice",
        claim_id="claim_invoice_number",
    )
    ok_state = SimpleNamespace(
        requirements=[
            SimpleNamespace(id="invoice_number", status="satisfied", required=True),
            SimpleNamespace(id="signature_or_authorized_signatory", status="weak", required=False),
        ],
        evidence_items=[SimpleNamespace(id="ev_invoice", supports=[SimpleNamespace(requirement="invoice_number")])],
        review_artifact=compiled.artifact,
        compiled_proof=compiled.proof,
    )
    assert enforce_case_state_consistency(text, ok_state) == text

    conflict_state = SimpleNamespace(
        requirements=[
            SimpleNamespace(id="invoice_number", status="satisfied", required=True),
            SimpleNamespace(id="template_match", status="conflict", required=False),
        ],
        evidence_items=[SimpleNamespace(id="ev_invoice", supports=[SimpleNamespace(requirement="invoice_number"), SimpleNamespace(requirement="template_match")])],
        review_artifact=ok_state.review_artifact,
        compiled_proof=ok_state.compiled_proof,
    )
    with pytest.raises(CaseStateConsistencyError, match="optional requirement conflicts"):
        enforce_case_state_consistency(text, conflict_state)


def test_case_state_consistency_blocks_missing_claim_when_duplicate_history_exists() -> None:
    state = SimpleNamespace(
        requirements=[SimpleNamespace(id="duplicate_payment_screen", status="conflict")],
        risk_flags=["historical_payment_document_found", "clearing_document_found"],
    )

    with pytest.raises(CaseStateConsistencyError, match="recorded payment history"):
        enforce_case_state_consistency("重复付款筛查证据缺失：未提供历史付款记录比对。", state)
