from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.compiler_runtime.consumer import derive_consumer_packet
from app.domain.invoice_requirements import is_known_requirement
from app.state.attachment_manifest import resolve_manifest_attachment
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, PolicyCheck, SupervisorDecision
from app.tools.approval import ToolApprovalPolicy
from app.tools.catalog import ToolCatalog
from app.tools.file_workspace import FileWorkspace


ROLE_TARGETS = {"materials_advisor", "evidence_reviewer", "case_patch_writer", "report_writer"}


class PolicyGate:
    def __init__(self, *, store: CaseStore, context: Any, tool_catalog: ToolCatalog | None = None) -> None:
        self.store = store
        self.context = context
        self.tool_catalog = tool_catalog or ToolCatalog(FileWorkspace(store))
        self.approval_policy = ToolApprovalPolicy()

    def check(
        self,
        *,
        request: AgentTurnRequest,
        state: Any,
        decision: SupervisorDecision,
        planner_context: dict[str, Any],
    ) -> PolicyCheck:
        if decision.action == "delegate_agent" and decision.target not in ROLE_TARGETS:
            return block("invalid_delegate_target", "delegate_agent must target a known specialist role.")
        if (
            decision.action == "delegate_agent"
            and decision.target == "materials_advisor"
            and forbids_materials_advice(request.message)
        ):
            return block("materials_advisor_forbidden", "The user explicitly forbade materials_advisor for this turn.")
        if decision.action == "call_tool" and not self.tool_catalog.has(decision.target):
            return block("invalid_tool_target", "call_tool must target a known tool.")
        if decision.action == "call_tool" and self.tool_catalog.get(decision.target).internal_only:
            return block("internal_tool_target", f"{decision.target} is an internal-only tool and cannot be called directly.")
        if decision.action == "write_case_patch" and decision.target not in {"", "write_case_patch"}:
            return block("invalid_patch_target", "write_case_patch must not target a normal tool.")
        if decision.action == "delegate_agent" and _role_failed_nonretryable(state, decision.target):
            return block(
                "role_terminal_failure",
                f"{decision.target} already failed with a non-retryable provider/schema error in this turn.",
                constraints=["Do not repeat the same specialist call. Answer from existing case_state/observations or ask the user to retry later."],
            )

        payload_error = _decision_payload_error(decision, self.tool_catalog)
        if payload_error:
            return payload_error

        requirement_scope_error = _reviewer_requirement_scope_error(decision, planner_context)
        if requirement_scope_error:
            return requirement_scope_error

        if decision.action in {"final_answer", "ask_user"} and not str(decision.final_answer or "").strip():
            return block(
                "empty_final_answer",
                "final_answer and ask_user require a non-empty Chinese user-facing reply.",
                constraints=["Choose final_answer or ask_user again with a non-empty final_answer."],
            )

        user_message = getattr(state, "user_message_for_planner", "") or request.message
        if (
            decision.action == "call_tool"
            and _last_approval_rejected_same_tool(state, decision.target)
            and not _user_explicitly_retries_rejected_tool(user_message)
        ):
            return block(
                "approval_rejected_tool_blocked",
                "The user rejected this tool call. Do not request or call the same tool again unless the user explicitly asks.",
                constraints=["Use final_answer explaining the tool was not executed."],
            )

        feedback = _runtime_feedback(planner_context, state, self.context)
        if _is_blocked_by_runtime_feedback(decision, feedback):
            return block(
                str(feedback.get("error_type") or "terminal_runtime_feedback"),
                str(feedback.get("user_message_hint") or "The previous runtime feedback blocks this repeated action."),
                constraints=[f"Do not repeat {feedback.get('blocked_action', '')}. Choose {feedback.get('recommended_action', 'final_answer')}."],
            )

        manifest_feedback = _manifest_attachment_status_block(decision, self.store, state)
        if manifest_feedback:
            return block(
                str(manifest_feedback.get("error_type") or "manifest_attachment_block"),
                str(manifest_feedback.get("user_message_hint") or "Stored attachment status blocks raw re-read."),
                constraints=["Answer from manifest/case_state or ask the user for a new file."],
            )

        duplicate_feedback = _duplicate_current_attachment_read(decision, request, state)
        if duplicate_feedback:
            return block(
                "duplicate_current_attachment_read",
                str(duplicate_feedback.get("user_message_hint") or "read_attachment already ran for this turn."),
                constraints=[f"Continue with {duplicate_feedback.get('recommended_action', 'evidence_reviewer')}."],
            )

        attachments = list(request.attachments or [])
        if attachments and not has_observation(state, kind="tool", name="read_attachment"):
            if not (decision.action == "call_tool" and decision.target == "read_attachment"):
                return block(
                    "attachment_unread",
                    "The current turn includes attachments. Read them before reviewing or answering their content.",
                    constraints=["Choose call_tool target=read_attachment with empty input for current-turn files."],
                )

        if not attachments and requires_attachment_reopen(user_message) and not has_observation(state, kind="tool", name="read_attachment"):
            attachment_id = select_manifest_attachment_id(planner_context, user_message)
            if attachment_id:
                wanted_input = dict(decision.input or {})
                if not (
                    decision.action == "call_tool"
                    and decision.target == "read_attachment"
                    and str(wanted_input.get("attachment_id") or "") == attachment_id
                ):
                    return block(
                        "prior_attachment_recheck_requires_read",
                        "The user asked to re-check a prior attachment. Reopen the stored file before answering.",
                        constraints=[f"Choose call_tool target=read_attachment input.attachment_id={attachment_id}."],
                    )

        if requires_evidence_repair(user_message) and not has_reviewer_mode(state, "review"):
            if not _decision_is_reviewer(decision, "review") and not (
                decision.action == "call_tool" and decision.target == "recheck_compiler_check"
            ):
                return block(
                    "repair_requires_reviewer",
                    "Weak/conflicting evidence must be recompiled from the current source set.",
                    constraints=["Choose delegate_agent target=evidence_reviewer input.mode=review."],
                )

        reviewer_terminal_failure = _role_failed_nonretryable(state, "evidence_reviewer")
        if reviewer_terminal_failure and decision.action not in {"final_answer", "ask_user"}:
            return block(
                "reviewer_failure_requires_final",
                "Evidence review failed terminally in this turn; no CasePatch can be produced.",
                constraints=["Use final_answer. Runtime will disclose that review failed and no evidence was written."],
            )
        if has_observation(state, kind="tool", name="read_attachment") and not _review_finished(state) and not reviewer_terminal_failure:
            expected_mode = _next_reviewer_mode(state)
            if not _decision_is_reviewer(decision, expected_mode):
                return block(
                    "attachment_requires_reviewer",
                    "Attachment content has been read but not yet reviewed by evidence_reviewer.",
                    constraints=[f"Choose delegate_agent target=evidence_reviewer input.mode={expected_mode}."],
                )

        if has_reviewer_mode(state, "review") and not has_observation(state, kind="role", name="case_patch_writer"):
            if not (decision.action == "delegate_agent" and decision.target == "case_patch_writer"):
                return block(
                    "review_requires_patch_draft",
                    "Reviewer output must be reduced into a CasePatch before final answer or report generation.",
                    constraints=["Choose delegate_agent target=case_patch_writer."],
                )

        if has_observation(state, kind="role", name="case_patch_writer") and not has_observation(state, kind="tool", name="write_case_patch"):
            if decision.action != "write_case_patch":
                return block(
                    "patch_draft_requires_write",
                    "A case_patch_writer result exists and must be persisted before answering.",
                    constraints=["Choose write_case_patch."],
                    risk_level="local_write",
                )

        advisor_terminal_failure = _role_failed_nonretryable(state, "materials_advisor")

        if requires_materials_advice(user_message) and has_observation(state, kind="tool", name="write_case_patch"):
            if not has_observation(state, kind="role", name="materials_advisor"):
                if advisor_terminal_failure and decision.action in {"final_answer", "ask_user"} and not _answer_discloses_role_failure(decision.final_answer, "materials_advisor"):
                    return block(
                        "advisor_failure_requires_disclosure",
                        "materials_advisor failed with a non-retryable error. Do not answer as if advisor succeeded.",
                        constraints=["Use final_answer to tell the user the materials advisor call failed and ask them to retry later."],
                    )
                if not advisor_terminal_failure and not (decision.action == "delegate_agent" and decision.target == "materials_advisor"):
                    return block(
                        "materials_question_requires_advisor",
                        "The user asked what is missing or how to fix it after evidence persistence. Use materials_advisor for the detailed task answer.",
                        constraints=["Choose delegate_agent target=materials_advisor."],
                    )

        if not attachments and requires_materials_advice(user_message) and not has_observation(state, kind="role", name="materials_advisor"):
            if advisor_terminal_failure and decision.action in {"final_answer", "ask_user"} and not _answer_discloses_role_failure(decision.final_answer, "materials_advisor"):
                return block(
                    "advisor_failure_requires_disclosure",
                    "materials_advisor failed with a non-retryable error. Do not answer as if advisor succeeded.",
                    constraints=["Use final_answer to tell the user the materials advisor call failed and ask them to retry later."],
                )
            if not advisor_terminal_failure and not _small_start_question(user_message) and not (decision.action == "delegate_agent" and decision.target == "materials_advisor"):
                return block(
                    "materials_question_requires_advisor",
                    "Material-gap, rule, template, and supplement questions belong to materials_advisor unless this is only a short getting-started chat.",
                    constraints=["Choose delegate_agent target=materials_advisor or answer briefly only if no case-specific task is needed."],
                )

        report_pipeline_started = has_observation(state, kind="role", name="report_writer") or has_observation(
            state, kind="tool", name="write_case_file"
        )
        approved_report_tool = decision.action == "call_tool" and decision.target in {"write_case_file", "render_pdf"} and _last_approval_approved_same_tool(
            state, decision.target
        )
        report_requested = _report_requested(user_message)
        evidence_pipeline_pending = _evidence_pipeline_pending(request, state)
        case_state = self.store.load(request.case_id) if not evidence_pipeline_pending else None
        consumer_packet = derive_consumer_packet(case_state) if case_state is not None else None
        reportability = consumer_packet.reportability if consumer_packet is not None else "NONE"
        reportable = reportability in {"FULL", "PARTIAL"}
        report_action = (
            decision.action == "delegate_agent" and decision.target == "report_writer"
        ) or (
            decision.action == "call_tool" and decision.target in {"write_case_file", "render_pdf"}
        )
        if report_requested and not evidence_pipeline_pending and not reportable and report_action:
            blocking = [
                item.missing_fact
                for item in list(getattr(getattr(case_state, "compiled_proof", None), "obligations", []) or [])
                if item.blocking
            ]
            return block(
                "report_blocked_by_proof",
                "A report requires at least one integrity-accepted canonical leaf finding.",
                constraints=[
                    "Use final_answer to explain that no reportable Kernel leaf is available.",
                    *[f"Blocking: {item}" for item in blocking[:5]],
                ],
            )

        if (
            reportability == "PARTIAL"
            and decision.action in {"final_answer", "ask_user"}
            and _claims_formal_approval(str(decision.final_answer or ""))
        ):
            return block(
                "partial_proof_cannot_approve",
                "A PARTIAL proof may report accepted leaf findings but cannot authorize payment or final approval.",
                constraints=[
                    "State that the review is partial and name the unresolved obligations.",
                    "Do not claim whole-case support, payment readiness, posting readiness, or final approval.",
                ],
            )

        if not report_requested and not report_pipeline_started and not approved_report_tool:
            if decision.action == "delegate_agent" and decision.target == "report_writer":
                return block(
                    "report_not_requested",
                    "The user did not ask to generate a report. Do not draft report content yet.",
                    constraints=["Answer from case_state/observations or call materials_advisor if the user asked about missing materials."],
                )
            if decision.action == "call_tool" and decision.target in {"write_case_file", "render_pdf"}:
                return block(
                    "report_file_not_requested",
                    "The user did not ask to write or render a report file.",
                    constraints=["Use final_answer or the relevant specialist instead of report file tools."],
                    risk_level="local_write",
                )

        if report_requested and not evidence_pipeline_pending and reportable:
            if not has_observation(state, kind="role", name="report_writer"):
                if not (decision.action == "delegate_agent" and decision.target == "report_writer"):
                    return block("report_requires_writer", "Report generation must start with report_writer.", constraints=["Choose delegate_agent target=report_writer."])
            elif not has_observation(state, kind="tool", name="write_case_file"):
                if not (decision.action == "call_tool" and decision.target == "write_case_file"):
                    return block("report_requires_file_write", "Persist report_writer Markdown before rendering or answering paths.", constraints=["Choose call_tool target=write_case_file with content_ref=last_role:report_writer.markdown."], risk_level="local_write")
            elif not _markdown_only(user_message) and not has_observation(state, kind="tool", name="render_pdf"):
                if not (decision.action == "call_tool" and decision.target == "render_pdf"):
                    return block("report_requires_pdf", "Default report output is PDF. Render PDF after Markdown unless the user asked Markdown only.", constraints=["Choose call_tool target=render_pdf."], risk_level="local_write")

        if reportable and has_observation(state, kind="role", name="report_writer") and not has_observation(state, kind="tool", name="write_case_file"):
            if decision.action == "final_answer":
                return block("report_writer_requires_file_write", "report_writer produced Markdown; write it before final answer.", constraints=["Choose call_tool target=write_case_file."], risk_level="local_write")

        if reportable and has_observation(state, kind="tool", name="write_case_file") and not _markdown_only(user_message) and not has_observation(state, kind="tool", name="render_pdf"):
            if decision.action == "final_answer":
                return block("report_file_requires_pdf", "A report Markdown file was written. Render the default PDF before final answer.", constraints=["Choose call_tool target=render_pdf."], risk_level="local_write")

        approval = self._approval_check(decision)
        if approval.requires_approval:
            return PolicyCheck(
                allowed=True,
                risk_level=approval.risk_level,
                requires_approval=True,
                approval_reason=approval.reason,
                approval_payload=approval.payload,
            )
        return PolicyCheck(allowed=True, risk_level=approval.risk_level)

    def _approval_check(self, decision: SupervisorDecision):
        if decision.action == "call_tool":
            spec = self.tool_catalog.get(decision.target)
            return self.approval_policy.evaluate(spec=spec, tool_input=decision.input or {})
        if decision.action == "write_case_patch":
            spec = self.tool_catalog.get("write_case_patch")
            payload = decision.case_patch if decision.case_patch is not None else decision.input or {}
            return self.approval_policy.evaluate(spec=spec, tool_input=payload, internal=True)
        return self.approval_policy.evaluate_no_tool()


def block(error_type: str, feedback: str, *, constraints: list[str] | None = None, risk_level: str = "read") -> PolicyCheck:
    return PolicyCheck(
        allowed=False,
        error_type=error_type,
        feedback_to_supervisor=feedback,
        recommended_constraints=constraints or [],
        risk_level=risk_level,  # type: ignore[arg-type]
    )


def _decision_payload_error(decision: SupervisorDecision, tool_catalog: ToolCatalog) -> PolicyCheck | None:
    input_chars = len(str(decision.input or {}))
    if input_chars > 4000:
        return block(
            "supervisor_input_too_large",
            "The proposed next-step input is too large. Only pass concise parameters, refs, ids, mode, or content_ref; do not paste evidence text or prior context into the decision input.",
            constraints=["Retry with a small input object. Use artifact/manifest refs already present in context."],
        )
    if decision.action != "call_tool" or not tool_catalog.has(decision.target):
        return None
    capability = tool_catalog.get(decision.target).capability
    try:
        capability.input_model.model_validate(decision.input or {})
    except Exception as exc:
        allowed = sorted(set(getattr(capability.input_model, "model_fields", {}).keys()))
        return block(
            "tool_payload_invalid",
            f"Tool input for {decision.target} does not match its schema: {type(exc).__name__}: {exc}",
            constraints=[
                f"Allowed keys for {decision.target}: {', '.join(allowed) if allowed else '(none)'}",
                "For current-turn read_attachment, use input={} unless reopening by attachment_id/original_ref.",
            ],
        )
    return None


def _reviewer_requirement_scope_error(
    decision: SupervisorDecision,
    planner_context: dict[str, Any],
) -> PolicyCheck | None:
    if decision.action != "delegate_agent" or decision.target != "evidence_reviewer":
        return None
    raw_ids = (decision.input or {}).get("active_requirement_ids") or []
    requested = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in raw_ids
            if str(item or "").strip()
        )
    )
    profiles = ((planner_context.get("requirement_catalog") or {}).get("profiles") or {})
    catalog_ids = {
        str(item or "").strip()
        for rows in profiles.values()
        for item in (rows if isinstance(rows, list) else [])
        if str(item or "").strip()
    }
    unknown = [
        item
        for item in requested
        if item not in catalog_ids and (catalog_ids or not is_known_requirement(item))
    ]
    if unknown:
        profile_keys = [item for item in unknown if item in profiles]
        feedback = f"active_requirement_ids contains unknown Requirement ids: {', '.join(unknown)}."
        if profile_keys:
            feedback += " The matching values are profile grouping keys, not Requirement ids."
        return block(
            "unknown_active_requirement_ids",
            feedback,
            constraints=[
                "Retry evidence_reviewer with only actual Requirement ids found inside context_pack.requirement_catalog.profiles value lists.",
                "Do not pass a profile key and do not rely on PolicyGate to expand or choose a profile.",
            ],
        )

    calculation_requirement = "invoice_calculation_valid"
    invoice_scope_ids = {
        str(item or "").strip()
        for rows in profiles.values()
        if isinstance(rows, list) and calculation_requirement in rows
        for item in rows
        if str(item or "").strip() != calculation_requirement
    }
    if set(requested).intersection(invoice_scope_ids) and calculation_requirement not in requested:
        return block(
            "invoice_calculation_required",
            "Invoice review scope omitted the mandatory internal-calculation check.",
            constraints=[
                "Retry evidence_reviewer with invoice_calculation_valid included in active_requirement_ids.",
                "Internal invoice arithmetic is mandatory for invoice review and cannot be omitted based on user wording.",
            ],
        )
    return None


def next_action_hint(kind: str, name: str, result: Any | None = None) -> str:
    if kind == "tool" and name == "read_attachment":
        return "delegate_agent:evidence_reviewer_review"
    if kind == "tool" and name == "recheck_compiler_check":
        return "delegate_agent:evidence_reviewer_review"
    if kind == "role" and name == "evidence_reviewer":
        return "delegate_agent:case_patch_writer"
    if kind == "role" and name == "case_patch_writer":
        return "write_case_patch"
    if kind == "tool" and name == "write_case_patch":
        return "final_answer"
    if kind == "role" and name == "report_writer":
        return "call_tool:write_case_file"
    if kind == "tool" and name == "write_case_file":
        return "call_tool:render_pdf"
    if kind in {"tool", "role"}:
        return "final_answer"
    return ""


def reviewer_hint_after_attachment(result: Any | None) -> str:
    return "delegate_agent:evidence_reviewer_review"


def has_observation(state: Any, *, kind: str, name: str) -> bool:
    return any(
        observation.get("kind") == kind and observation.get("name") == name and not observation.get("error")
        for observation in getattr(state, "observations", []) or []
        if isinstance(observation, dict)
    )


def has_reviewer_mode(state: Any, mode: str) -> bool:
    for observation in getattr(state, "observations", []) or []:
        if not isinstance(observation, dict):
            continue
        if observation.get("kind") != "role" or observation.get("name") != "evidence_reviewer" or observation.get("error"):
            continue
        observed_mode = str(observation.get("reviewer_mode") or "").strip()
        if not observed_mode and mode == "review":
            return True
        if observed_mode == mode:
            return True
    return False


def guard_retry_feedback(error_type: str, hint: str) -> dict[str, Any]:
    return {
        "status": "retryable",
        "error_type": error_type,
        "retry_allowed": True,
        "recommended_action": "final_answer",
        "blocked_action": "",
        "user_message_hint": hint,
    }


def requires_materials_advice(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    if forbids_materials_advice(message):
        return False
    chinese_terms = (
        "审核哪些材料",
        "能审核哪些材料",
        "哪些材料",
        "材料清单",
        "补充什么",
        "需要补充",
        "还缺",
        "缺什么",
        "补什么",
        "怎么满足",
        "哪里不完整",
        "为什么不符合",
        "为什么不合规",
        "材料类型",
        "案例类型",
        "发票类型",
        "保证发票",
        "发票里有什么",
        "发票里面有什么",
        "问一下advisor",
        "材料顾问",
    )
    if any(term in text for term in chinese_terms):
        return True
    if any(term in text for term in ("以后", "未来", "类似", "下次", "下一次")) and any(
        term in text for term in ("准备", "保证", "内容", "字段", "材料", "发票", "规则", "模板", "案例")
    ):
        return True
    explicit_advisor_terms = (
        "advisor", "materials_advisor", "材料顾问", "问一下顾问", "问一下advisor", "rag一下", "rag 一下",
        "缺口", "补料",
        "缺什么", "还缺", "补什么", "需要什么", "需要准备", "准备什么", "怎么满足", "哪里不完整",
        "为什么不符合", "为什么不合规", "保证发票", "发票里有什么", "发票里面有什么",
    )
    future_terms = ("以后", "未来", "类似", "下次", "下一次")
    future_question_terms = ("准备", "保证", "内容", "字段", "材料", "发票", "规则", "模板", "案例")
    classification_subjects = ("这个案例", "本案", "这个材料", "这份材料", "这张发票", "这个文件")
    classification_terms = ("属于什么类型", "是什么类型", "材料类型", "案例类型", "怎么归类", "如何归类", "分类")
    if any(term in text for term in explicit_advisor_terms):
        return True
    if any(term in text for term in future_terms) and any(term in text for term in future_question_terms):
        return True
    if any(subject in text for subject in classification_subjects) and any(term in text for term in classification_terms):
        return True
    terms = (
        "补什么", "补哪些", "缺什么", "缺哪些", "还缺", "补料", "需要补",
        "任务清单", "材料清单", "找谁拿", "什么格式", "必填字段", "哪些字段",
        "审核字段", "审核规则", "为什么不符合", "为什么不合规", "不符合的原因",
        "哪里不完整", "怎么满足", "如何满足", "下一步建议", "处理建议",
        "版式合规", "传统版式", "标准版式", "长什么样", "有案例", "有样例",
        "模板", "规则是什么", "visual quality", "review fields", "requirement",
        "missing material", "materials task", "why not compliant", "what is incomplete",
        "how to satisfy", "required fields", "standard layout", "example", "sample", "template",
    )
    return any(term in text for term in terms)


def requires_evidence_repair(message: str) -> bool:
    text = str(message or "").lower()
    cleanup = ("忽略", "不用管", "别管", "清理", "删掉", "删除", "覆盖", "翻案", "修复", "以pdf为准", "以 pdf 为准", "ignore", "clear", "remove", "override", "supersede", "repair")
    evidence = ("弱证据", "图片", "png", "ocr", "pdf", "证据", "conflict", "冲突", "误识", "错", "金额", "来源", "traceability", "evidence")
    recheck = ("重新审核", "重新审查", "重新复核", "re-review")
    return (any(item in text for item in cleanup) and any(item in text for item in evidence)) or any(item in text for item in recheck)


def requires_attachment_reopen(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    prior_terms = (
        "之前", "上次", "刚才", "前面", "旧", "原来", "先前",
        "previous", "prior", "last", "earlier", "old",
    )
    file_terms = (
        "附件", "文件", "pdf", "发票", "截图", "图片", "金额", "供应商", "日期", "签名",
        "attachment", "file", "invoice", "amount", "supplier", "signature",
    )
    action_terms = (
        "重新看", "再看", "复查", "重查", "重新审", "再审", "核对", "检查", "确认",
        "re-check", "recheck", "review again", "check again", "look again",
    )
    return any(term in text for term in prior_terms) and any(term in text for term in file_terms) and any(term in text for term in action_terms)


def select_manifest_attachment_id(planner_context: dict[str, Any], message: str) -> str:
    manifest = planner_context.get("attachment_manifest")
    rows = manifest.get("attachments") if isinstance(manifest, dict) else []
    if not isinstance(rows, list):
        return ""
    text = str(message or "").lower()
    candidates = [item for item in rows if isinstance(item, dict)]
    usable = [item for item in candidates if str(item.get("status") or "").lower() not in {"excluded", "quarantined", "error"}]
    for item in usable:
        name = str(item.get("name") or "").lower()
        summary = str(item.get("summary") or "").lower()
        stem = Path(name).stem.lower()
        if (name and name in text) or (stem and stem in text) or ("flipkart" in text and "flipkart" in f"{name} {summary}"):
            return str(item.get("attachment_id") or "")
    if ("pdf" in text or "invoice" in text or "发票" in text) and len(usable) == 1:
        return str(usable[0].get("attachment_id") or "")
    return ""


def _runtime_feedback(planner_context: dict[str, Any], state: Any, context: Any) -> dict[str, Any]:
    feedback = planner_context.get("runtime_feedback") if isinstance(planner_context.get("runtime_feedback"), dict) else {}
    return feedback or context.last_runtime_feedback(state)


def _is_blocked_by_runtime_feedback(decision: SupervisorDecision, feedback: dict[str, Any]) -> bool:
    if not feedback or feedback.get("retry_allowed") is not False:
        return False
    blocked = str(feedback.get("blocked_action") or "")
    if decision.action == "call_tool" and decision.target:
        return blocked == f"call_tool:{decision.target}"
    if decision.action == "delegate_agent" and decision.target:
        return blocked == f"delegate_agent:{decision.target}"
    if decision.action == "write_case_patch":
        return blocked == "write_case_patch"
    return False


def forbids_materials_advice(message: str) -> bool:
    compact = re.sub(r"\s+", "", str(message or "").lower())
    return any(
        term in compact
        for term in (
            "不要advisor",
            "不要调用advisor",
            "别调用advisor",
            "不调用advisor",
            "无需advisor",
            "不用advisor",
            "noadvisor",
            "withoutadvisor",
        )
    )


def _manifest_attachment_status_block(decision: SupervisorDecision, store: CaseStore, state: Any) -> dict[str, Any]:
    if decision.action != "call_tool" or decision.target != "read_attachment":
        return {}
    attachment_id = str((decision.input or {}).get("attachment_id") or "")
    original_ref = str((decision.input or {}).get("original_ref") or "")
    if not attachment_id and not original_ref:
        return {}
    try:
        entry = resolve_manifest_attachment(store, state.case_id, attachment_id=attachment_id, original_ref=original_ref)
    except Exception:
        return {}
    status = str(entry.get("manifest_status") or "").lower()
    if status not in {"quarantined", "excluded", "error"}:
        return {}
    return {
        "error_type": f"manifest_attachment_{status}",
        "user_message_hint": f"The stored attachment is marked {status}. Do not re-read raw content for evidence support.",
    }


def _duplicate_current_attachment_read(decision: SupervisorDecision, request: AgentTurnRequest, state: Any) -> dict[str, Any]:
    if decision.action != "call_tool" or decision.target != "read_attachment":
        return {}
    if not list(request.attachments or []):
        return {}
    if not has_observation(state, kind="tool", name="read_attachment"):
        return {}
    return {"recommended_action": "delegate_agent:evidence_reviewer", "user_message_hint": "The current turn attachments have already been read."}


def _last_approval_rejected_same_tool(state: Any, tool: str) -> bool:
    for observation in reversed(getattr(state, "observations", []) or []):
        if not isinstance(observation, dict) or observation.get("kind") != "approval":
            continue
        facts = " ".join(str(item) for item in observation.get("key_facts") or [])
        if f"tool={tool}" not in facts:
            continue
        return str(observation.get("name") or "").lower() == "rejected"
    return False


def _last_approval_approved_same_tool(state: Any, tool: str) -> bool:
    for observation in reversed(getattr(state, "observations", []) or []):
        if not isinstance(observation, dict) or observation.get("kind") != "approval":
            continue
        facts = " ".join(str(item) for item in observation.get("key_facts") or [])
        if f"tool={tool}" not in facts:
            continue
        return str(observation.get("name") or "").lower() == "approved"
    return False


def _user_explicitly_retries_rejected_tool(message: str) -> bool:
    text = str(message or "").lower()
    return any(
        term in text
        for term in (
            "允许",
            "批准",
            "同意",
            "继续执行",
            "重新执行",
            "再执行",
            "可以执行",
            "approve",
            "approved",
            "allow",
            "retry",
            "run it",
            "execute",
        )
    )


def _role_failed_nonretryable(state: Any, role: str) -> bool:
    for call in reversed(getattr(state, "role_calls", []) or []):
        if str(call.get("role") or "") != role:
            continue
        error = str(call.get("error") or "")
        if not error:
            return False
        lowered = error.lower()
        return any(
            token in lowered
            for token in (
                "badrequesterror",
                "invalid_request_error",
                "invalid temperature",
                "structured_output_parse_error",
                "schema validation",
                "modelbehaviorerror",
                "invalid json",
                "timeout",
                "timed out",
                "apitimeouterror",
                "apiconnectionerror",
                "connection error",
            )
        )
    return False


def _answer_discloses_role_failure(answer: str, role: str) -> bool:
    text = str(answer or "").lower()
    if role == "materials_advisor":
        role_terms = ("materials_advisor", "materials advisor", "advisor", "材料顾问")
    else:
        role_terms = (role.lower(),)
    failure_terms = ("失败", "调用失败", "暂时无法", "技术问题", "报错", "failed", "error", "temporarily unavailable")
    return any(term in text for term in role_terms) and any(term in text for term in failure_terms)


def _review_finished(state: Any) -> bool:
    return has_reviewer_mode(state, "review")


def _next_reviewer_mode(state: Any) -> str:
    _ = state
    return "review"


def _decision_is_reviewer(decision: SupervisorDecision, mode: str) -> bool:
    return decision.action == "delegate_agent" and decision.target == "evidence_reviewer" and str((decision.input or {}).get("mode") or mode) == mode


def _small_start_question(message: str) -> bool:
    text = str(message or "").strip().lower()
    return text in {"我不知道怎么开始", "怎么开始", "从哪里开始", "怎么用", "how to start", "where do i start"}


def _report_requested(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    if any(
        term in text
        for term in (
            "不要生成报告",
            "不要报告",
            "不生成报告",
            "不需要报告",
            "无需生成报告",
            "无需报告",
            "不用生成报告",
            "不用报告",
            "别生成报告",
            "no report",
            "without report",
            "do not generate report",
            "don't generate report",
        )
    ) or any(term in compact for term in ("不要生成报告", "不要生成pdf", "不生成报告", "不需要报告", "无需报告", "不用生成报告", "不用报告", "别生成报告")):
        return False
    text = text.replace("ready_for_report", "")
    compact = compact.replace("ready_for_report", "")
    if re.search(r"(生成|撰写|写入|写|输出|导出|渲染|出具|制作).{0,16}(报告|pdf)", text, flags=re.I):
        return True
    if re.search(r"(我要|要|需要|给我|来一份).{0,8}(一份)?(报告|pdf)", text, flags=re.I):
        return True
    return any(
        term in text
        for term in (
            "生成报告",
            "最终报告",
            "导出报告",
            "渲染pdf",
            "生成 pdf",
            "pdf report",
            "final report",
            "generate report",
            "write report",
            "render report",
            "export report",
            "i need a report",
            "give me a report",
        )
    ) or any(term in compact for term in ("生成报告", "最终报告", "导出报告", "出具报告", "制作报告", "渲染pdf", "生成pdf", "导出pdf"))


def _claims_formal_approval(message: str) -> bool:
    text = str(message or "")
    return bool(
        re.search(
            r"(?:可|可以|建议|应当)\s*(?:直接)?\s*(?:付款|支付|审批|批准|过账|提交)"
            r"|(?:最终审批|付款审批)\s*(?:通过|完成|已完成)"
            r"|\b(?:ready|safe|approved)\s+to\s+(?:pay|post|approve|submit)\b",
            text,
            flags=re.I,
        )
    )


def _evidence_pipeline_pending(request: AgentTurnRequest, state: HarnessRunState) -> bool:
    attachments = list(request.attachments or [])
    if attachments and not has_observation(state, kind="tool", name="read_attachment"):
        return True
    if has_observation(state, kind="tool", name="read_attachment") and not _review_finished(state):
        return True
    if has_reviewer_mode(state, "review") and not has_observation(
        state, kind="role", name="case_patch_writer"
    ):
        return True
    if has_observation(state, kind="role", name="case_patch_writer") and not has_observation(state, kind="tool", name="write_case_patch"):
        return True
    return False


def _markdown_only(message: str) -> bool:
    text = str(message or "").lower()
    return any(term in text for term in ("只要 markdown", "只要md", "不要 pdf", "不用 pdf", "markdown only", "md only"))


def _latest_next_action_hint(observations: list[dict[str, Any]]) -> str:
    for observation in reversed(observations):
        if isinstance(observation, dict) and observation.get("next_action_hint"):
            return str(observation.get("next_action_hint") or "")
    return ""
