from __future__ import annotations

import re
from typing import Any

from app.guards import (
    CaseStateConsistencyError,
    NoExecutionWordingError,
    enforce_case_state_consistency,
    enforce_no_execution_wording,
)
from app.harness import HarnessRuntime, HarnessRunState
from app.runtime import policy_gate as route_policy
from app.state.case_store import CaseStore
from app.state.schemas import SupervisorDecision


class InvoiceOnlyScopeAnswerError(ValueError):
    pass


class InternalRetryInstructionLeakError(ValueError):
    pass


class OptionalInvoiceQualityAsRequiredError(ValueError):
    pass


class RecoveryPolicy:
    def __init__(self, *, store: CaseStore, harness: HarnessRuntime, context: Any) -> None:
        self.store = store
        self.harness = harness
        self.context = context

    def handle_final_answer(self, state: HarnessRunState, decision: SupervisorDecision) -> None:
        try:
            draft = decision.final_answer or ""
            case_state = self.store.load(state.case_id)
            enforce_no_internal_retry_instruction_leak(draft)
            enforce_optional_invoice_quality_not_required(draft, case_state)
            enforce_invoice_only_scope_answer(draft, state.user_message_for_planner or state.current_goal or "")
            checked = enforce_no_execution_wording(draft)
            state.final_answer = enforce_case_state_consistency(checked, case_state)
        except InternalRetryInstructionLeakError as exc:
            self.harness.record_guard_error(
                state,
                "final_answer_internal_retry_instruction",
                exc,
                runtime_feedback=route_policy.guard_retry_feedback(
                    "final_answer_internal_retry_instruction",
                    "错误类型：内部重试指令泄漏。为什么拦截：最终回复包含给 Supervisor 的修复提示，而不是给用户的中文答复。下一轮必须：只基于 case_state、最近 observations 和已生成文件，用自然中文说明已完成内容、当前限制和下一步。",
                ),
            )
        except OptionalInvoiceQualityAsRequiredError as exc:
            self.harness.record_guard_error(
                state,
                "final_answer_optional_quality_as_required",
                exc,
                runtime_feedback=route_policy.guard_retry_feedback(
                    "final_answer_optional_quality_as_required",
                    "错误类型：invoice-only 可选质量项被写成必填项。为什么拦截：line item、签名/授权签章、template match 在当前案卷不是 required blocker。下一轮必须：保留 advisor 的“必备核心字段/可选质量信号”结构，只把 invoice_number、supplier、buyer、invoice_date、amount_total、currency_tax、source_traceability 写成必备核心字段。",
                ),
            )
        except InvoiceOnlyScopeAnswerError as exc:
            self.harness.record_guard_error(
                state,
                "final_answer_invoice_only_scope",
                exc,
                runtime_feedback=route_policy.guard_retry_feedback(
                    "final_answer_invoice_only_scope",
                    "错误类型：单张发票范围越界。为什么拦截：用户要求 invoice-only 或不做 AP/三单审查，但回复加入了 AP、PO、GRN、供应商主数据、重复付款或付款控制措辞。下一轮必须：只说明发票字段、视觉质量、来源可追溯性和报告/PDF结果。",
                ),
            )
        except NoExecutionWordingError as exc:
            self.harness.record_guard_error(
                state,
                "no_execution_wording",
                exc,
                runtime_feedback=route_policy.guard_retry_feedback(
                    "final_answer_no_execution_wording",
                    "错误类型：执行性措辞违规。为什么拦截：回复包含 ERP、付款、审批、过账、提交或路由等执行声称/暗示。下一轮必须：保留有用审查事实，删除执行性措辞，只描述本地材料审查和报告结果。",
                ),
            )
        except CaseStateConsistencyError as exc:
            self.harness.record_guard_error(
                state,
                "case_state_consistency",
                exc,
                runtime_feedback=route_policy.guard_retry_feedback(
                    "final_answer_case_state_consistency",
                    "错误类型：回复与 case_state 不一致。为什么拦截：最终回复把 missing、weak 或 conflict 的 requirement 说成已满足。下一轮必须：以 case_state 为唯一业务真相，明确当前 satisfied、weak、conflict 和 missing 状态。",
                ),
            )

    def should_offer_final_recovery(self, state: HarnessRunState) -> bool:
        if state.step_count != state.max_steps or state.final_answer:
            return False
        feedback = self.context.last_runtime_feedback(state)
        if not feedback:
            return False
        return str(feedback.get("recommended_action") or "") == "final_answer"

    @staticmethod
    def planner_failure_answer(exc: Exception) -> str:
        return (
            "Planner failed, so this agent run stopped instead of taking deterministic backend recovery actions. "
            f"Error: {type(exc).__name__}: {exc}"
        )


def enforce_invoice_only_scope_answer(final_answer: str, user_message: str) -> None:
    if not user_declares_invoice_only(user_message):
        return
    text = str(final_answer or "").lower()
    forbidden_terms = (
        "采购订单",
        "收货",
        "验收单",
        "三单",
        "供应商主数据",
        "重复付款",
        "查重",
        "付款控制",
        "付款审查",
    )
    forbidden_patterns = (
        r"\bap\b",
        r"\bpo\b",
        r"\bgrn\b",
        r"\bpurchase\s+orders?\b",
        r"\bgoods\s+receipts?\b",
        r"\bvendor\s+(master|identity)\b",
        r"\bduplicate[-\s]?payment\b",
        r"\bpayment\s+(control|review)s?\b",
        r"\bthree[-\s]?way\b",
    )
    if any(term in text for term in forbidden_terms) or any(re.search(pattern, text) for pattern in forbidden_patterns):
        raise InvoiceOnlyScopeAnswerError("Invoice-only final_answer included AP/payment-control scope terms")


def enforce_no_internal_retry_instruction_leak(final_answer: str) -> None:
    text = str(final_answer or "")
    if not text.strip():
        return
    leaked_patterns = (
        r"The previous final_answer",
        r"Rewrite the answer",
        r"Rewrite the final_answer",
        r"previous final answer",
        r"runtime feedback",
        r"policy_feedback",
        r"guard retry",
        r"case_state facts only",
    )
    if any(re.search(pattern, text, flags=re.I) for pattern in leaked_patterns):
        raise InternalRetryInstructionLeakError("Final answer leaked internal retry or guard instruction")


def enforce_optional_invoice_quality_not_required(final_answer: str, case_state: Any) -> None:
    if not _case_is_invoice_only_with_optional_quality(case_state):
        return
    text = str(final_answer or "")
    required_section = re.split(r"(?:可选质量|可选项|额外建议|补充说明|quality signal)", text, maxsplit=1, flags=re.I)[0]
    optional_terms = r"(商品/服务明细|商品明细|行项目|line item|line_items_product_title|签名|授权签章|授权签署|signature|template match|template_match|模板匹配)"
    required_terms = r"(必备|必须|必要|核心字段|required|blocker)"
    suspicious_patterns = (
        rf"{required_terms}[\s\S]{{0,700}}{optional_terms}",
        rf"{optional_terms}[\s\S]{{0,80}}{required_terms}",
        r"全部\s*9\s*项核心",
        r"全部九项核心",
    )
    if any(re.search(pattern, required_section, flags=re.I) for pattern in suspicious_patterns):
        raise OptionalInvoiceQualityAsRequiredError("Invoice-only final_answer made optional quality signals required")


def _case_is_invoice_only_with_optional_quality(case_state: Any) -> bool:
    profile = getattr(case_state, "case_profile", {}) or {}
    if isinstance(profile, dict) and str(profile.get("type") or profile.get("profile") or "").lower() in {"invoice_only", "invoice-only"}:
        return True
    optional_ids = {"line_items_product_title", "signature_or_authorized_signatory", "template_match"}
    requirements = list(getattr(case_state, "requirements", []) or [])
    return any(getattr(item, "id", "") in optional_ids and getattr(item, "required", True) is False for item in requirements)


def user_declares_invoice_only(message: str) -> bool:
    text = str(message or "").lower()
    markers = (
        "单张发票审核",
        "只做单张发票",
        "仅按单张发票",
        "invoice-only",
        "invoice only",
        "single invoice",
        "不做ap",
        "不做 ap",
        "不要ap",
        "不要 ap",
        "不做三单",
        "不要三单",
        "不做付款审查",
        "不做付款控制",
        "no ap",
        "without ap",
        "no three-way",
        "without three-way",
        "no payment review",
        "without payment review",
    )
    return any(marker in text for marker in markers)
