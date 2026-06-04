from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.context import _sanitize_report_markdown_for_guards
from app.guards import CaseStateConsistencyError, NoExecutionWordingError, enforce_case_state_consistency, enforce_no_execution_wording
from app.state.schemas import SupervisorDecision, new_case_state


def test_no_execution_wording_blocks_direct_execution_claims() -> None:
    for text in ("approved and paid", "I have paid the invoice.", "invoice has been paid.", "I can submit to ERP."):
        with pytest.raises(NoExecutionWordingError):
            enforce_no_execution_wording(text)


def test_no_execution_wording_allows_material_and_negative_history_context() -> None:
    assert enforce_no_execution_wording("Please provide the approved purchase order as review evidence.")
    assert enforce_no_execution_wording("Duplicate-payment check shows no paid record in the submitted history export.")


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


def test_case_state_consistency_ignores_optional_weak_but_blocks_optional_conflict() -> None:
    text = "status = ready_for_report"
    ok_state = SimpleNamespace(
        requirements=[
            SimpleNamespace(id="invoice_number", status="satisfied", required=True),
            SimpleNamespace(id="signature_or_authorized_signatory", status="weak", required=False),
        ],
        evidence_items=[SimpleNamespace(supports=[SimpleNamespace(requirement="invoice_number")])],
    )
    assert enforce_case_state_consistency(text, ok_state) == text

    conflict_state = SimpleNamespace(
        requirements=[
            SimpleNamespace(id="invoice_number", status="satisfied", required=True),
            SimpleNamespace(id="template_match", status="conflict", required=False),
        ],
        evidence_items=[SimpleNamespace(supports=[SimpleNamespace(requirement="invoice_number"), SimpleNamespace(requirement="template_match")])],
    )
    with pytest.raises(CaseStateConsistencyError, match="optional requirement conflicts"):
        enforce_case_state_consistency(text, conflict_state)
