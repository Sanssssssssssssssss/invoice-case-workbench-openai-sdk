from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from pydantic import BaseModel

from app.config import get_settings
from app.config import Settings
from app.context import ContextManager, classify_runtime_error
from app.harness import HarnessRuntime
from app.llm import LlmClient
from app.runtime.context_assembler import ContextAssembler
from app.runtime.supervisor_contract import sorted_specialist_tool_specs
from app.runtime.policy_gate import (
    PolicyGate,
    attachment_result_needs_extract,
    guard_retry_feedback,
    requires_attachment_reopen,
    requires_materials_advice,
)
from app.runtime.retry import is_transient_llm_error, is_transient_tool_error
from app.runtime.agents_sdk import FencedJsonOutputSchema, build_run_config
from app.runtime.tool_runtime import ToolRuntime
from app.runtime.turn_runner import TurnRunner, supervisor_task
from app.session_manager import SessionManager
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, ExtractedField, PolicyCheck, SupervisorDecision
from app.tools.catalog import ToolCatalog
from app.tools.file_workspace import FileWorkspace, report_paths_for_run


class FakeLLM:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.calls: list[Any] = []


def test_structured_output_accepts_an_outer_json_code_fence() -> None:
    class Output(BaseModel):
        value: str

    schema = FencedJsonOutputSchema(Output, strict_json_schema=False)

    assert schema.validate_json('```json\n{"value":"ok"}\n```').value == "ok"


def _state(tmp_path, *, message: str = "测试") -> tuple[CaseStore, ContextManager, HarnessRuntime, Any]:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store, max_steps=10)
    sessions = SessionManager(store, FakeLLM())  # type: ignore[arg-type]
    context = ContextManager(store, FakeLLM(), sessions.sessions, sessions.memory)  # type: ignore[arg-type]
    assembler = ContextAssembler(
        store=store,
        llm=FakeLLM(),  # type: ignore[arg-type]
        harness=harness,
        context=context,
        sessions=sessions,
        tool_catalog=ToolCatalog(FileWorkspace(store)),
        planner_prompt="prompt",
    )
    state = assembler.load_context(AgentTurnRequest(case_id="case_policy", message=message), run_id="run_policy")
    return store, context, harness, state


def test_runtime_uses_one_sorted_tool_catalog_for_context_and_sdk_tools(tmp_path) -> None:
    runner = TurnRunner(store=CaseStore(tmp_path / "cases"), llm=LlmClient(Settings(llm_api_key="test")))

    assert runner.context_assembler.tool_catalog is runner.tools

    state = runner.harness.begin_run("case_tool_order", "test", run_id="run_tool_order")
    request = AgentTurnRequest(case_id=state.case_id, message="test", attachments=[])
    sdk_names = [str(getattr(tool, "name", "")) for tool in runner.sdk_tools(state=state, request=request, planner_context={})]
    expected = [name for name, _description, _model in sorted_specialist_tool_specs()]
    expected.extend(item["name"] for item in runner.tools.visible_tools())

    assert sdk_names == expected


def test_supervisor_decision_schema_rejects_old_call_role_shape() -> None:
    with pytest.raises(ValueError):
        SupervisorDecision.model_validate({"action": "call_role", "role": "materials_advisor"})


def test_supervisor_decision_rejects_empty_user_facing_answers() -> None:
    with pytest.raises(ValueError):
        SupervisorDecision.model_validate({"action": "final_answer", "final_answer": ""})
    with pytest.raises(ValueError):
        SupervisorDecision.model_validate({"action": "ask_user", "final_answer": "   "})
    with pytest.raises(ValueError):
        SupervisorDecision.model_validate({"action": "request_approval", "target": "render_pdf"})


def test_extracted_field_accepts_locator_alias_from_reviewer() -> None:
    field = ExtractedField.model_validate(
        {
            "value": "INV-5001",
            "status": "present",
            "source_quote": "Invoice ID: INV-5001",
            "locator": "dossier_text_regex",
            "confidence": "high",
        }
    )

    assert field.source_locator == "dossier_text_regex"
    assert "locator" not in field.model_dump()


def test_supervisor_plan_progress_preserves_next_and_feedback() -> None:
    decision = SupervisorDecision(
        action="final_answer",
        final_answer="ok",
        plan_progress="intent=ask_missing; next=materials_advisor; feedback=policy; extra=drop",
        reason="done",
    )

    assert decision.plan_progress == "intent=ask_missing; next=materials_advisor; feedback=policy"


def test_policy_gate_blocks_empty_final_answer_even_if_constructed(tmp_path) -> None:
    store, context, _harness, state = _state(tmp_path, message="hello")
    decision = SupervisorDecision.model_construct(action="final_answer", target="", input={}, final_answer="", reason="")
    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="hello"),
        state=state,
        decision=decision,
        planner_context={"attachments": []},
    )

    assert not check.allowed
    assert check.error_type == "empty_final_answer"


def test_policy_gate_blocks_final_answer_when_attachments_unread(tmp_path) -> None:
    store, context, _harness, state = _state(tmp_path, message="看看材料")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="看看材料",
        attachments=[{"name": "invoice.txt", "path": str(tmp_path / "invoice.txt")}],
    )
    gate = PolicyGate(store=store, context=context)
    check = gate.check(
        request=request,
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="已看完。", reason="too early"),
        planner_context={"attachments": [{"name": "invoice.txt"}]},
    )

    assert not check.allowed
    assert check.error_type == "attachment_unread"
    assert "read_attachment" in " ".join(check.recommended_constraints)


def test_policy_gate_feedback_does_not_rewrite_supervisor_decision(tmp_path) -> None:
    store, context, _harness, state = _state(tmp_path, message="看看材料")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="看看材料",
        attachments=[{"name": "invoice.txt", "path": str(tmp_path / "invoice.txt")}],
    )
    decision = SupervisorDecision(action="final_answer", final_answer="直接回答", reason="blocked")
    check = PolicyGate(store=store, context=context).check(
        request=request,
        state=state,
        decision=decision,
        planner_context={"attachments": [{"name": "invoice.txt"}]},
    )

    assert isinstance(check, PolicyCheck)
    assert not check.allowed
    assert decision.action == "final_answer"
    assert decision.final_answer == "直接回答"


def test_policy_gate_surfaces_tool_catalog_approval_without_blocking(tmp_path) -> None:
    store, context, _harness, state = _state(tmp_path, message="list files")
    catalog = ToolCatalog(FileWorkspace(store))
    catalog._specs["list_case_files"] = replace(catalog.get("list_case_files"), approval_mode="always")  # noqa: SLF001

    check = PolicyGate(store=store, context=context, tool_catalog=catalog).check(
        request=AgentTurnRequest(case_id=state.case_id, message="list files"),
        state=state,
        decision=SupervisorDecision(action="call_tool", target="list_case_files", input={}, reason="inspect"),
        planner_context={"attachments": []},
    )

    assert check.allowed is True
    assert check.requires_approval is True
    assert check.risk_level == "read"
    assert check.approval_payload["tool"] == "list_case_files"
    assert check.approval_payload["risk_level"] == "read"
    assert check.approval_payload["input_sha256"]
    assert "input" not in check.approval_payload


def test_policy_gate_blocks_rejected_tool_retry_without_explicit_user_retry(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="list files")
    harness.record_observation(
        state,
        {
            "kind": "approval",
            "name": "rejected",
            "summary": "User rejected list_case_files.",
            "key_facts": ["tool=list_case_files", "risk_level=read"],
            "risks": [],
            "missing_items": [],
            "next_action_hint": "final_answer",
            "must_preserve_refs": [],
        },
    )

    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="list files"),
        state=state,
        decision=SupervisorDecision(action="call_tool", target="list_case_files", input={}, reason="try again"),
        planner_context={"attachments": []},
    )

    assert not check.allowed
    assert check.error_type == "approval_rejected_tool_blocked"


def test_policy_gate_requires_advisor_after_patch_when_user_asks_missing(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="这是材料，还缺什么？")
    harness.record_observation(state, {"kind": "tool", "name": "write_case_patch", "summary": "patch written", "key_facts": [], "risks": [], "missing_items": [], "next_action_hint": "final_answer", "must_preserve_refs": []})
    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="这是材料，还缺什么？"),
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="还缺 PO。", reason="too short"),
        planner_context={"attachments": []},
    )

    assert requires_materials_advice("这是材料，还缺什么？")
    assert not check.allowed
    assert check.error_type == "materials_question_requires_advisor"


def test_policy_gate_requires_advisor_for_case_type_future_material_questions(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="这个案例属于什么类型，以后类似案例要保证发票里有什么内容？")
    harness.record_observation(state, {"kind": "tool", "name": "write_case_patch", "summary": "patch written", "key_facts": [], "risks": [], "missing_items": [], "next_action_hint": "final_answer", "must_preserve_refs": []})
    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="这个案例属于什么类型，以后类似案例要保证发票里有什么内容？"),
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="这是单张发票。", reason="too short"),
        planner_context={"attachments": []},
    )

    assert requires_materials_advice("这个案例属于什么类型，以后类似案例要保证发票里有什么内容？")
    assert not check.allowed
    assert check.error_type == "materials_question_requires_advisor"


def test_requires_materials_advice_for_plain_chinese_material_question() -> None:
    assert requires_materials_advice("你能审核哪些材料，这些材料我需要补充什么呢")
    assert requires_materials_advice("这个案例属于什么类型，以后类似案例要保证发票里有什么内容？")


def test_policy_gate_does_not_loop_after_nonretryable_advisor_failure(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="你能审核哪些材料，这些材料我需要补充什么呢")
    harness.record_role_call(
        state,
        "materials_advisor",
        {},
        {},
        error="BadRequestError: Error code: 400 - {'error': {'message': 'invalid temperature: only 1 is allowed for this model', 'type': 'invalid_request_error'}}",
    )

    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="你能审核哪些材料，这些材料我需要补充什么呢"),
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="材料顾问调用失败，暂时无法生成补料建议，请稍后重试。", reason="fallback"),
        planner_context={"attachments": []},
    )

    assert check.allowed is True


def test_policy_gate_blocks_material_answer_after_advisor_terminal_failure(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="你能审核哪些材料，这些材料我需要补充什么呢")
    harness.record_role_call(
        state,
        "materials_advisor",
        {},
        {},
        error="BadRequestError: invalid_request_error",
    )

    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="你能审核哪些材料，这些材料我需要补充什么呢"),
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="我可以审核发票、PO、GRN、供应商资料。", reason="pretend"),
        planner_context={"attachments": []},
    )

    assert not check.allowed
    assert check.error_type == "advisor_failure_requires_disclosure"


def test_policy_gate_blocks_repeating_nonretryable_failed_role(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="你能审核哪些材料？")
    harness.record_role_call(
        state,
        "materials_advisor",
        {},
        {},
        error="BadRequestError: invalid_request_error",
    )

    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="你能审核哪些材料？"),
        state=state,
        decision=SupervisorDecision(action="delegate_agent", target="materials_advisor", input={}, reason="retry"),
        planner_context={"attachments": []},
    )

    assert not check.allowed
    assert check.error_type == "role_terminal_failure"


def test_policy_gate_reopens_prior_attachment_before_answering(tmp_path) -> None:
    store, context, _harness, state = _state(tmp_path, message="please re-check the previous PDF amount")
    gate = PolicyGate(store=store, context=context)
    planner_context = {
        "attachment_manifest": {
            "attachments": [
                {
                    "attachment_id": "att_pdf_001",
                    "name": "invoice.pdf",
                    "status": "active",
                    "summary": "Stored invoice PDF with visible amount.",
                }
            ]
        }
    }

    assert requires_attachment_reopen("please re-check the previous PDF amount")
    check = gate.check(
        request=AgentTurnRequest(case_id=state.case_id, message="please re-check the previous PDF amount"),
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="The amount is 319.", reason="too early"),
        planner_context=planner_context,
    )

    assert not check.allowed
    assert check.error_type == "prior_attachment_recheck_requires_read"
    assert "att_pdf_001" in " ".join(check.recommended_constraints)

    allowed = gate.check(
        request=AgentTurnRequest(case_id=state.case_id, message="please re-check the previous PDF amount"),
        state=state,
        decision=SupervisorDecision(action="call_tool", target="read_attachment", input={"attachment_id": "att_pdf_001"}, reason="reopen"),
        planner_context=planner_context,
    )
    assert allowed.allowed is True


def test_supervisor_task_carries_decision_brief_and_policy_feedback(tmp_path) -> None:
    _store, _context, harness, state = _state(tmp_path, message="审核这份发票")
    harness.record_observation(
        state,
        {
            "kind": "policy",
            "name": "attachment_requires_reviewer",
            "summary": "must review",
            "key_facts": [],
            "risks": [],
            "missing_items": [],
            "next_action_hint": "delegate_agent:evidence_reviewer_extract",
            "must_preserve_refs": [],
            "policy_feedback": {"error_type": "attachment_requires_reviewer"},
        },
    )
    decision = SupervisorDecision(
        action="delegate_agent",
        target="evidence_reviewer",
        input={"mode": "extract"},
        reason="用户上传了发票，需要先抽取字段。",
        short_plan=["抽取字段", "再审查证据"],
        plan_progress="准备调用 reviewer extract",
    )

    task = supervisor_task(decision, state)

    assert task["mode"] == "extract"
    assert task["reason"] == "用户上传了发票，需要先抽取字段。"
    assert task["short_plan"] == ["抽取字段", "再审查证据"]
    assert task["policy_feedback"]["error_type"] == "attachment_requires_reviewer"


def test_text_attachment_extraction_ref_does_not_force_extract_mode() -> None:
    result = {
        "attachments": [
            {
                "name": "invoice.md",
                "content_kind": "text",
                "extraction_method": "text_direct",
                "extraction_ref": "evidence/extractions/att_text.json",
                "preview_paths": [],
            }
        ]
    }

    assert attachment_result_needs_extract(result) is False


def test_multiple_text_attachments_do_not_force_extract_mode() -> None:
    result = {
        "attachments": [
            {"name": "invoice.md", "content_kind": "text", "extraction_method": "text_direct", "preview_paths": []},
            {"name": "po.md", "content_kind": "text", "extraction_method": "text_direct", "preview_paths": []},
            {"name": "duplicate_check.md", "content_kind": "text", "extraction_method": "text_direct", "preview_paths": []},
        ]
    }

    assert attachment_result_needs_extract(result) is False


def test_multiple_pdf_attachments_still_use_extract_mode() -> None:
    result = {
        "attachments": [
            {"name": "invoice.pdf", "content_kind": "pdf", "extraction_method": "pymupdf_text", "preview_paths": []},
            {"name": "po.md", "content_kind": "text", "extraction_method": "text_direct", "preview_paths": []},
        ]
    }

    assert attachment_result_needs_extract(result) is True


def test_policy_gate_blocks_extract_when_text_attachment_expects_review(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="这是发票材料，还缺什么？")
    harness.record_observation(
        state,
        {
            "kind": "tool",
            "name": "read_attachment",
            "summary": "text attachment read",
            "key_facts": [],
            "risks": [],
            "missing_items": [],
            "next_action_hint": "delegate_agent:evidence_reviewer_review",
            "must_preserve_refs": [],
        },
    )
    check = PolicyGate(store=store, context=context).check(
        request=AgentTurnRequest(case_id=state.case_id, message="这是发票材料，还缺什么？"),
        state=state,
        decision=SupervisorDecision(action="delegate_agent", target="evidence_reviewer", input={"mode": "extract"}, reason="wrong mode"),
        planner_context={"attachments": []},
    )

    assert not check.allowed
    assert check.error_type == "attachment_requires_reviewer"
    assert "mode=review" in " ".join(check.recommended_constraints)


def test_policy_gate_enforces_report_writer_file_and_pdf_sequence(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="生成最终报告")
    gate = PolicyGate(store=store, context=context)
    request = AgentTurnRequest(case_id=state.case_id, message="生成最终报告")

    check = gate.check(
        request=request,
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="报告好了。", reason="too early"),
        planner_context={"attachments": []},
    )
    assert not check.allowed
    assert check.error_type == "report_requires_writer"

    harness.record_observation(state, {"kind": "role", "name": "report_writer", "summary": "draft", "key_facts": [], "risks": [], "missing_items": [], "next_action_hint": "call_tool:write_case_file", "must_preserve_refs": []})
    check = gate.check(
        request=request,
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="报告好了。", reason="too early"),
        planner_context={"attachments": []},
    )
    assert not check.allowed
    assert check.error_type in {"report_requires_file_write", "report_writer_requires_file_write"}

    harness.record_observation(state, {"kind": "tool", "name": "write_case_file", "summary": "written", "key_facts": [], "risks": [], "missing_items": [], "next_action_hint": "call_tool:render_pdf", "must_preserve_refs": []})
    check = gate.check(
        request=request,
        state=state,
        decision=SupervisorDecision(action="final_answer", final_answer="报告好了。", reason="too early"),
        planner_context={"attachments": []},
    )
    assert not check.allowed
    assert check.error_type in {"report_requires_pdf", "report_file_requires_pdf"}


def test_policy_gate_does_not_reblock_approved_report_file_tool(tmp_path) -> None:
    store, context, harness, state = _state(tmp_path, message="???????? PDF?")
    gate = PolicyGate(store=store, context=context)
    harness.record_observation(
        state,
        {
            "kind": "approval",
            "name": "approved",
            "summary": "User approved write_case_file.",
            "key_facts": ["tool=write_case_file", "risk_level=local_write"],
            "risks": [],
            "missing_items": [],
            "next_action_hint": "dispatch",
            "must_preserve_refs": [],
        },
    )

    check = gate.check(
        request=AgentTurnRequest(case_id=state.case_id, message="???????? PDF?"),
        state=state,
        decision=SupervisorDecision(
            action="call_tool",
            target="write_case_file",
            input={"relative_path": "reports/final_report.md", "content": "# report"},
            reason="approved report write",
        ),
        planner_context={"attachments": []},
    )

    assert check.allowed
    assert check.error_type != "report_file_not_requested"


def test_tool_runtime_preserves_capability_metadata(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store)
    context = ContextManager(store, FakeLLM())  # type: ignore[arg-type]
    tools = ToolCatalog(FileWorkspace(store), content_resolver=context.resolve_content_ref)
    runtime = ToolRuntime(tools, harness, context)
    state = harness.begin_run("case_tool", "list files", run_id="run_tool")

    result = runtime.call(state, "list_case_files", {})

    assert result["case_id"] == "case_tool"
    assert state.tool_calls[0]["capability"]["side_effect"] == "case_read"


def test_report_paths_are_timestamped() -> None:
    markdown, pdf = report_paths_for_run("2026-05-30T14:40:19+00:00")
    assert markdown == "reports/final_report_20260530_144019.md"
    assert pdf == "reports/final_report_20260530_144019.pdf"


def test_runtime_error_classification_blocks_terminal_read_attachment() -> None:
    feedback = classify_runtime_error(kind="tool", name="read_attachment", error={"message": "unsupported attachment type: exe"})
    assert feedback["retry_allowed"] is False
    assert feedback["blocked_action"] == "call_tool:read_attachment"


def test_retry_predicates_only_allow_transient_failures() -> None:
    assert is_transient_llm_error(TimeoutError("provider timeout"))
    assert is_transient_llm_error(RuntimeError("503 service unavailable"))
    assert is_transient_tool_error(OSError("temporary OCR subprocess timeout"))
    assert not is_transient_llm_error(ValueError("schema validation failed"))
    assert not is_transient_tool_error(ValueError("policy block"))


def test_kimi_structured_output_uses_agents_sdk_model_settings() -> None:
    disabled = LlmClient(Settings(llm_model="kimi-k2.5", llm_temperature=0.6, llm_thinking_type="disabled"))
    enabled = LlmClient(Settings(llm_model="kimi-k2.5", llm_temperature=0.6, llm_thinking_type="enabled"))

    assert disabled._temperature("kimi-k2.5") == 0.6  # noqa: SLF001
    assert enabled._temperature("kimi-k2.5") == 1.0  # noqa: SLF001


def test_evidence_reviewer_has_long_role_timeout_by_default(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "17")
    monkeypatch.delenv("INVOICE_AGENT_EVIDENCE_REVIEWER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("EVIDENCE_REVIEWER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_EVIDENCE_REVIEWER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("INVOICE_AGENT_CASE_PATCH_WRITER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CASE_PATCH_WRITER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_CASE_PATCH_WRITER_TIMEOUT_SECONDS", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.timeout_for_role("planner") == 17
    assert settings.timeout_for_role("materials_advisor") == 17
    assert settings.timeout_for_role("evidence_reviewer") == 600
    assert settings.timeout_for_role("case_patch_writer") == 300

    get_settings.cache_clear()


def test_evidence_reviewer_timeout_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_EVIDENCE_REVIEWER_TIMEOUT_SECONDS", "420")
    monkeypatch.setenv("INVOICE_AGENT_CASE_PATCH_WRITER_TIMEOUT_SECONDS", "240")
    get_settings.cache_clear()

    assert get_settings().timeout_for_role("evidence_reviewer") == 420
    assert get_settings().timeout_for_role("case_patch_writer") == 240

    get_settings.cache_clear()


def test_agents_sdk_run_config_uses_explicit_timeout() -> None:
    config = build_run_config(
        Settings(llm_api_key="test", llm_base_url="https://api.openai.com/v1", llm_timeout_seconds=11),
        workflow_name="invoice_agent.test",
        timeout_seconds=123,
    )
    client = getattr(config, "_invoice_openai_client")

    try:
        assert client.timeout == 123
    finally:
        asyncio.run(client.close())


def test_attachment_batch_summary_is_deterministic_without_llm(tmp_path) -> None:
    class BombLLM:
        settings = Settings(llm_api_key="test")
        calls: list[Any] = []

        def complete_structured(self, **kwargs: Any) -> Any:
            raise AssertionError("attachment summaries must not call the LLM")

    store = CaseStore(tmp_path / "cases")
    context = ContextManager(store, BombLLM())  # type: ignore[arg-type]
    state = HarnessRuntime(store).begin_run("case_attachment_summary", "review", run_id="run_attachment_summary")
    result = {
        "attachment_count": 1,
        "attachments": [
            {
                "attachment_id": "att_001",
                "name": "invoice.md",
                "status": "success",
                "original_ref": "attachments/originals/invoice.md",
                "content": "x" * 9000,
            }
        ],
    }

    observation = context.record_result(state, kind="tool", name="read_attachment", result=result)

    assert observation["summary"] == "读取了 1 个附件：invoice.md"
    assert observation["next_action_hint"]


def test_case_patch_normalizes_grn_support_alias(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    state = store.apply_patch(
        "case_grn_alias",
        {
            "patch_type": "update_case",
            "case_updates": {
                "requirements": [
                    {"id": "goods_receipt_or_service_acceptance", "label": "Goods receipt", "kind": "document"}
                ],
                "add_evidence": [
                    {
                        "type": "goods_receipt",
                        "credibility": "medium",
                        "summary": "GRN supplier matches the PO and invoice.",
                        "review_result": {"should_accept": True},
                        "supports": [{"requirement": "grn_supplier", "support_level": "full"}],
                    }
                ],
            },
        },
    )

    assert state.evidence_items[0].supports[0].requirement == "goods_receipt_or_service_acceptance"
    assert state.requirements[0].status == "satisfied"


def test_step_limit_answer_does_not_leak_guard_retry_instruction(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    harness = HarnessRuntime(store, max_steps=10)
    state = harness.begin_run("case_step_limit", "guard retry leak", run_id="run_limit")
    state.step_count = state.max_steps
    harness.record_guard_error(
        state,
        "final_answer_no_execution_wording",
        Exception("The previous final_answer added a generic template. Rewrite the answer."),
        runtime_feedback=guard_retry_feedback(
            "final_answer_no_execution_wording",
            "The previous final_answer added a generic template. Rewrite the answer.",
        ),
    )

    answer = harness.step_limit_answer(state)

    assert "The previous final_answer" not in answer
    assert "Rewrite the answer" not in answer
    assert "当前运行已到本轮步数上限" in answer
    assert "最终回复包含执行性措辞" in answer
