from __future__ import annotations

import pytest

from app.runtime.policy_gate import requires_evidence_repair, requires_materials_advice
from app.runtime.recovery_policy import (
    InternalRetryInstructionLeakError,
    InvoiceOnlyScopeAnswerError,
    OptionalInvoiceQualityAsRequiredError,
    enforce_no_internal_retry_instruction_leak,
    enforce_optional_invoice_quality_not_required,
    enforce_invoice_only_scope_answer as _enforce_invoice_only_scope_answer,
)
from app.state.schemas import CaseState, Requirement
from app.prompt_loader import load_system_prompt


def test_supervisor_prompt_is_not_a_long_route_table() -> None:
    prompt = load_system_prompt("agents/planner/prompt.md")

    assert "Supervisor Planner Agent" in prompt
    assert "PolicyGate" in prompt
    assert "provider-native tool" in prompt
    assert "delegate_agent" not in prompt
    assert "evidence_reviewer" in prompt
    assert "case_patch_writer" in prompt
    assert "Do not call rag_search" in prompt
    assert "request_approval" not in prompt
    assert "Route Table" not in prompt
    assert "PLANNER_RUNTIME_OVERRIDE" not in prompt
    assert "PlannerAction" not in prompt


def test_supervisor_treats_child_failure_as_an_observation_not_a_relaunch() -> None:
    prompt = load_system_prompt("agents/planner/prompt.md")

    assert "durable child run, not a fire-and-forget call" in prompt
    assert "use the exact child receipt as the next observation" in prompt
    assert "success from a fluent child summary alone" in prompt
    assert "provider, transport, timeout, runtime" in prompt
    assert "establishes no business status" in prompt
    assert "Do not repeat the same child request after the same operational error" in prompt
    assert "status=paused" in prompt
    assert "Inspect that exact `compiler_run_id`" in prompt
    assert "pause_reason=PLAN_READY" in prompt
    assert "continue that exact" in prompt


def test_supervisor_prompt_keeps_human_reply_boundary() -> None:
    prompt = load_system_prompt("agents/planner/prompt.md")

    assert "Greetings, thanks, identity, anxiety" in prompt
    assert "Be warm, natural, concise Chinese" in prompt
    assert "next_expected_action` is `final_answer" in prompt
    assert "only the user-facing result" in prompt
    assert "not general code execution" in prompt
    assert "不能" not in prompt or "payment" in prompt


def test_supervisor_translates_business_goal_without_exposing_internal_profiles() -> None:
    prompt = load_system_prompt("agents/planner/prompt.md")

    assert "Users describe a business goal, not internal Requirements" in prompt
    assert "smallest sufficient subset" in prompt
    assert "Profiles are candidate bundles, not indivisible scopes" in prompt
    assert "asking for a report never widens the review scope" in prompt
    assert "never require the user to name a profile or field checklist" in prompt
    assert "every key is only a grouping label and is never a Requirement id" in prompt
    assert "Never pass profile keys" in prompt
    assert "Every invoice review must include `invoice_calculation_valid`" in prompt
    assert "asked to skip it" in prompt
    assert "does not activate the rest of an invoice profile" in prompt
    assert '`active_requirement_ids` MUST contain EXACTLY `["invoice_calculation_valid"]`' in prompt
    assert "Report generation is output only and MUST NOT add review scope" in prompt
    assert "Runtime expands the Requirement's declared premises" in prompt


def test_supervisor_reports_grounded_arithmetic_conflict_with_figures() -> None:
    prompt = load_system_prompt("agents/planner/prompt.md")

    assert "grounded\nobserved, recomputed, and variance values" in prompt
    assert "instead of reducing the finding to a vague calculation mismatch" in prompt


def test_supervisor_keeps_all_blocking_advisor_gaps_in_missing_material_answer() -> None:
    prompt = load_system_prompt("agents/planner/prompt.md")

    assert "every blocking ProofObligation" in prompt
    assert "every unconfigured policy returned by `materials_advisor`" in prompt
    assert "later task or policy gap merely because the Advisor answer was long" in prompt


def test_materials_and_repair_trigger_terms() -> None:
    assert requires_materials_advice("版式合规性为什么不符合，怎么满足？")
    assert requires_materials_advice("哪里不完整？需要补哪些字段？")
    assert requires_materials_advice("有没有同厂商模板样例？")
    assert requires_materials_advice("这个案例属于什么类型，以后类似案例要保证发票里有什么内容？")
    assert requires_materials_advice("问一下advisor吧，让它rag一下。")
    assert requires_evidence_repair("忽略这个 PNG 弱证据，以 PDF 为准")
    assert requires_evidence_repair("这个金额 OCR 识别错了，重新审核")
    assert requires_materials_advice(
        "只审查供应商主数据，不要扩展 Requirement，不要调用 Advisor，不要生成报告。"
    ) is False
def test_invoice_only_scope_guard_rejects_ap_terms() -> None:
    with pytest.raises(InvoiceOnlyScopeAnswerError):
        _enforce_invoice_only_scope_answer(
            "This should be handled as an AP review with three-way matching.",
            "invoice-only please",
        )
    _enforce_invoice_only_scope_answer(
        "The snapshot maps the approval note to the visible invoice fields.",
        "invoice-only please",
    )


def test_internal_retry_instruction_leak_guard() -> None:
    with pytest.raises(InternalRetryInstructionLeakError):
        enforce_no_internal_retry_instruction_leak(
            "The previous final_answer added a generic boundary template. Rewrite the answer with case_state facts only."
        )
    with pytest.raises(InternalRetryInstructionLeakError):
        enforce_no_internal_retry_instruction_leak(
            "The user asked me to call the reviewer. Let me reconsider the PolicyGate feedback."
        )
    with pytest.raises(InternalRetryInstructionLeakError):
        enforce_no_internal_retry_instruction_leak(
            "I have all the information needed. Let me provide a comprehensive final response."
        )
    with pytest.raises(InternalRetryInstructionLeakError):
        enforce_no_internal_retry_instruction_leak("下面我将为你整理一份最终答复。")
    enforce_no_internal_retry_instruction_leak("已完成材料审查，当前发票字段可见，PDF 报告已生成。")
    enforce_no_internal_retry_instruction_leak(
        "I have reviewed the invoice. The current evidence is insufficient because the purchase order is missing."
    )


def test_invoice_optional_quality_signals_are_not_required_in_final_answer() -> None:
    case_state = CaseState(
        case_id="case_invoice_only",
        case_profile={"type": "invoice_only"},
        requirements=[
            Requirement(id="invoice_number", required=True),
            Requirement(id="signature_or_authorized_signatory", required=False),
            Requirement(id="template_match", required=False),
        ],
    )
    with pytest.raises(OptionalInvoiceQualityAsRequiredError):
        enforce_optional_invoice_quality_not_required(
            "必备核心字段包括发票编号、签名/授权签章和模板匹配。",
            case_state,
        )
    enforce_optional_invoice_quality_not_required(
        "必备核心字段包括发票编号。可选质量信号包括签名/授权签章和模板匹配。",
        case_state,
    )
