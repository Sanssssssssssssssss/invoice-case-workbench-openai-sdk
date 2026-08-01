from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from app.config import get_settings
from app.harness import HarnessRuntime
from app.llm import ModelCallRecord
from app.runtime.policy_gate import _report_requested as _policy_report_requested
from app.runtime.turn_runner import AgentRuntime, ManagerRunOutcome, SdkManagerRunner, TurnRunner, _report_requested_message, _runtime_final_answer
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, Attachment, CaseState, EvidenceItem, Requirement


class ScriptedManagerRunner:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.payload: dict[str, Any] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    def run(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state,
        planner_context: dict[str, Any],
        manager_input: dict[str, Any],
    ) -> ManagerRunOutcome:
        self.payload = manager_input
        decision = self.decisions.pop(0) if self.decisions else {"action": "final_answer", "final_answer": "已完成。"}
        action = decision.get("action")
        if action in {"call_tool", "delegate_agent", "write_case_patch"}:
            target = "write_case_patch" if action == "write_case_patch" else str(decision.get("target") or "")
            result = runner.invoke_manager_tool(
                state=state,
                request=request,
                planner_context=planner_context,
                name=target,
                payload=dict(decision.get("input") or {}),
            )
            if result.get("status") == "approval_required":
                self.pending[state.run_id] = decision
                return ManagerRunOutcome(
                    interruptions=[{"tool": target, "input": dict(decision.get("input") or {}), "risk_level": "read"}],
                    sdk_state="fake_pending_state",
                )
            return self.run(runner=runner, request=request, state=state, planner_context=planner_context, manager_input=manager_input)
        return ManagerRunOutcome(final_output=str(decision.get("final_answer") or ""))

    def resume(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state,
        sdk_state: str,
        approved: bool,
        reason: str = "",
    ) -> ManagerRunOutcome:
        _ = sdk_state
        _ = reason
        planner_context = runner.context_assembler.build_planner_context(request, state)
        manager_input = runner._manager_input(request, state, planner_context)
        if approved and state.run_id in self.pending:
            decision = self.pending.pop(state.run_id)
            target = str(decision.get("target") or "")
            runner.invoke_manager_tool(
                state=state,
                request=request,
                planner_context=planner_context,
                name=target,
                payload=dict(decision.get("input") or {}),
            )
        elif not approved:
            self.pending.pop(state.run_id, None)
        return self.run(runner=runner, request=request, state=state, planner_context=planner_context, manager_input=manager_input)


def _runtime(tmp_path, monkeypatch, manager: ScriptedManagerRunner) -> AgentRuntime:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    return AgentRuntime(store=CaseStore(tmp_path / "cases"), manager_runner=manager)


def _seed_ready_case(store: CaseStore, case_id: str) -> None:
    store.save(
        CaseState(
            case_id=case_id,
            status="ready_for_report",
            requirements=[
                Requirement(id="invoice", status="satisfied", evidence_ids=["ev_001"], kind="document"),
                Requirement(id="purchase_order", status="satisfied", evidence_ids=["ev_002"], kind="document"),
                Requirement(
                    id="goods_receipt_or_service_acceptance",
                    status="satisfied",
                    evidence_ids=["ev_003"],
                    kind="document",
                ),
                Requirement(id="vendor_identity", status="satisfied", evidence_ids=["ev_004"], kind="document"),
                Requirement(id="duplicate_payment_screen", status="satisfied", evidence_ids=["ev_005"], kind="document"),
            ],
            evidence_items=[
                EvidenceItem(
                    id="ev_001",
                    type="invoice",
                    summary="Invoice INV-5001 amount 12800.00 CNY.",
                    review_result={"should_accept": True},
                    supports=[{"requirement": "invoice", "support_level": "full"}],
                ),
                EvidenceItem(
                    id="ev_002",
                    type="purchase_order",
                    summary="PO-5001 matches supplier and amount.",
                    review_result={"should_accept": True},
                    supports=[{"requirement": "purchase_order", "support_level": "full"}],
                ),
                EvidenceItem(
                    id="ev_003",
                    type="goods_receipt",
                    summary="GRN-5001 received 16 of 16 units.",
                    review_result={"should_accept": True},
                    supports=[{"requirement": "goods_receipt_or_service_acceptance", "support_level": "full"}],
                ),
                EvidenceItem(
                    id="ev_004",
                    type="vendor_record",
                    summary="Vendor is active and bank tail matches.",
                    review_result={"should_accept": True},
                    supports=[{"requirement": "vendor_identity", "support_level": "full"}],
                ),
                EvidenceItem(
                    id="ev_005",
                    type="duplicate_payment_check",
                    summary="No duplicate payment found.",
                    review_result={"should_accept": True},
                    supports=[{"requirement": "duplicate_payment_screen", "support_level": "full"}],
                ),
            ],
        )
    )


def _fake_report_writer(role: str, role_input: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    assert role == "report_writer"
    assert role_input["case_state"]["status"] == "ready_for_report"
    return {"title": "INV-5001 审核报告", "markdown": "# INV-5001 审核报告\n\n五项核心材料均已满足。\n"}


def test_report_intent_respects_negative_generation_request() -> None:
    assert not _report_requested_message("请审查这一批材料，判断证据链是否完整，不要生成报告。")
    assert not _report_requested_message("review the evidence without report generation")
    assert not _report_requested_message("review materials and tell me whether the case is ready_for_report")
    assert _report_requested_message("请生成报告并渲染 PDF")
    assert _report_requested_message("final report and render PDF")
    chinese_report_pdf = "\u7ee7\u7eed\u5904\u7406\uff0c\u751f\u6210\u4e2d\u6587\u5ba1\u6838\u62a5\u544a\uff0c\u5e76\u6e32\u67d3 PDF\u3002"
    assert _report_requested_message(chinese_report_pdf)
    assert _policy_report_requested(chinese_report_pdf)


def test_sdk_manager_resume_passes_run_state_as_runner_input(monkeypatch) -> None:
    class FakeRunState:
        def __init__(self) -> None:
            self.approved = False

        def get_interruptions(self) -> list[str]:
            return ["approval"]

        def approve(self, _item: str) -> None:
            self.approved = True

    class FakeResult:
        final_output = "resumed"
        interruptions: list[Any] = []

    run_state = FakeRunState()
    captured: dict[str, Any] = {}

    monkeypatch.setattr("app.runtime.turn_runner.RunState.from_string", lambda *_args, **_kwargs: run_state)

    def fake_run_agent_sync(*args: Any, **kwargs: Any) -> FakeResult:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr("app.runtime.turn_runner.run_agent_sync", fake_run_agent_sync)
    runner = SimpleNamespace(
        context_assembler=SimpleNamespace(build_planner_context=lambda *_args, **_kwargs: {}),
        sdk_tools=lambda **_kwargs: [],
        manager_factory=SimpleNamespace(build=lambda *_args, **_kwargs: "manager"),
        run_config=lambda _state: "run_config",
        record_manager_model_call=lambda *_args, **_kwargs: None,
    )
    state = SimpleNamespace(case_id="case_resume", run_id="run_resume", max_steps=10, step_count=1)

    outcome = SdkManagerRunner().resume(
        runner=runner,
        request=AgentTurnRequest(case_id="case_resume", message="approve"),
        state=state,
        sdk_state="serialized",
        approved=True,
        reason="ok",
    )

    assert outcome.final_output == "resumed"
    assert run_state.approved is True
    assert captured["args"][:2] == ("manager", run_state)
    assert captured["kwargs"]["run_config"] == "run_config"


def test_agent_runtime_uses_manager_policy_loop(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {
                "action": "final_answer",
                "final_answer": "你好，我是这个工作台里的发票材料审查助手，可以帮你整理材料、审查证据并生成本地报告/PDF。",
            }
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_live_test", message="你好，你是谁？你能做什么？"))

    assert "发票材料审查助手" in response.reply
    assert response.trace["step_count"] == 1
    assert response.trace["planner_actions"][-1]["action"] == "final_answer"
    assert response.trace["tool_calls"] == []
    assert response.trace["role_calls"] == []
    assert isinstance(manager.payload["capability_cards"]["tools"], list)
    assert manager.payload["capability_cards"]["tools"][0]["risk_level"]
    assert manager.payload["capability_cards"]["skills"]


def test_manager_tool_output_keeps_raw_attachment_in_artifact(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_manager_tool_output", "review", run_id="run_manager_tool_output")
    source = tmp_path / "invoice.md"
    marker = "DO_NOT_ECHO_RAW_TOOL_CONTENT"
    source.write_text(f"# Invoice\n\n{marker}\n\nTotal: 12.00 CNY", encoding="utf-8")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review attachment",
        attachments=[Attachment(name=source.name, path=str(source))],
    )

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="read_attachment",
        payload={},
    )

    rendered = json.dumps(result, ensure_ascii=False, default=str)
    assert result["status"] == "success"
    assert result["tool"] == "read_attachment"
    assert "result" not in result
    assert "result_preview" not in result
    assert result["artifact_ref"]
    assert marker not in rendered
    artifact = runner.context.artifacts.read(state.case_id, result["artifact_ref"])
    assert marker in json.dumps(artifact, ensure_ascii=False, default=str)


def test_current_turn_attachment_read_normalizes_manager_path_to_batch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_manager_batch_read", "review", run_id="run_manager_batch_read")
    first = tmp_path / "invoice.md"
    second = tmp_path / "po.md"
    first.write_text("# Invoice\n\nINV-BATCH-001", encoding="utf-8")
    second.write_text("# Purchase order\n\nPO-BATCH-001", encoding="utf-8")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review attachments",
        attachments=[Attachment(name=first.name, path=str(first)), Attachment(name=second.name, path=str(second))],
    )

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="read_attachment",
        payload={"name": first.name, "path": str(first), "max_chars": 12000},
    )

    artifact = runner.context.artifacts.read(state.case_id, result["artifact_ref"])
    assert result["status"] == "success"
    assert len(artifact["attachments"]) == 2
    assert {item["name"] for item in artifact["attachments"]} == {"invoice.md", "po.md"}


def test_policy_blocked_manager_tool_does_not_consume_step_budget(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_policy_block_budget", "review", run_id="run_policy_block_budget")
    source = tmp_path / "invoice.md"
    source.write_text("# Invoice\n\nTotal: 12.00 CNY", encoding="utf-8")
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review attachment",
        attachments=[Attachment(name=source.name, path=str(source))],
    )

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="evidence_reviewer",
        payload={"mode": "review"},
    )

    assert result["status"] == "blocked"
    assert result["policy_check"]["error_type"] == "attachment_unread"
    assert state.step_count == 0
    assert state.role_calls == []
    assert state.planner_actions == []
    assert any(observation.get("kind") == "policy" and observation.get("name") == "attachment_unread" for observation in state.observations)


def test_evidence_reviewer_timeout_recovers_text_direct_batch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_reviewer_timeout_recovery", "review", run_id="run_reviewer_timeout_recovery")
    readme = tmp_path / "README.md"
    invoice = tmp_path / "01_invoice_INV-9201.md"
    duplicate = tmp_path / "05_duplicate_payment_check_INV-9201.md"
    readme.write_text(
        "# Sample case\n\nSuggested desktop test: upload the evidence files. Expected high-level result: review succeeds.",
        encoding="utf-8",
    )
    invoice.write_text(
        "# Invoice INV-9201\n\n- Evidence type: invoice\n- Supplier legal name: Apex Motors Ltd.\n- Invoice total amount: 9900.00 CNY\n",
        encoding="utf-8",
    )
    duplicate.write_text(
        "# Duplicate Payment Check INV-9201\n\n"
        "- Evidence type: duplicate_payment_check\n"
        "- Duplicate invoice found: Yes\n"
        "- Prior payment document: PAY-2026-4431\n"
        "- Clearing document: CLR-2026-4431\n",
        encoding="utf-8",
    )
    request = AgentTurnRequest(
        case_id=state.case_id,
        message="review duplicate payment",
        attachments=[
            Attachment(name=readme.name, path=str(readme)),
            Attachment(name=invoice.name, path=str(invoice)),
            Attachment(name=duplicate.name, path=str(duplicate)),
        ],
    )
    runner.invoke_manager_tool(state=state, request=request, planner_context={}, name="read_attachment", payload={})
    extract_result = {
        "mode": "extract",
        "source_doc_id": "attachment_batch",
        "evidence_type": "invoice",
        "credibility": "high",
        "extracted_fields": {},
        "evidence_cards": [{"title": "extracted invoice"}],
    }
    runner.harness.record_role_call(state, "evidence_reviewer", {"mode": "extract"}, extract_result)
    runner.harness.record_observation(state, runner.context.record_result(state, kind="role", name="evidence_reviewer", result=extract_result))
    runner._update_phase_after_role(state, "evidence_reviewer", extract_result)  # noqa: SLF001
    state.observations = [item for item in state.observations if item.get("kind") != "tool" or item.get("name") != "read_attachment"]

    class APITimeoutError(Exception):
        pass

    def fail_call(role: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        raise APITimeoutError("Request timed out.")

    monkeypatch.setattr(runner.roles, "call", fail_call)
    runner.llm.calls.append(
        ModelCallRecord(
            role="evidence_reviewer",
            model="test",
            prompt_version="test",
            input_preview="{}",
            output_preview="",
            error="APITimeoutError: Request timed out.",
        )
    )

    review_request = AgentTurnRequest(case_id=state.case_id, message="continue review")
    result = runner.invoke_manager_tool(
        state=state,
        request=review_request,
        planner_context={},
        name="evidence_reviewer",
        payload={"mode": "review"},
    )

    role_result = runner.context.last_evidence_reviewer_result(state, mode="review")
    assert result["status"] == "success"
    assert result["role"] == "evidence_reviewer"
    assert result["next_action_hint"] == "delegate_agent:case_patch_writer"
    assert role_result["suggested_patch"]["add_evidence"]
    assert len(role_result["suggested_patch"]["add_evidence"]) == 2
    assert "PAY-2026-4431" in json.dumps(role_result, ensure_ascii=False)
    assert state.role_calls[-1]["capability"]["runtime_recovery"] == "text_direct_review_after_specialist_timeout"
    assert runner.llm.calls[-1].error == ""
    assert runner.llm.calls[-1].recovered_by == "text_direct_review_after_specialist_timeout"
    trace_events = (runner.store.resolve_case_path(state.case_id, f"traces/{state.run_id}/events.jsonl")).read_text(encoding="utf-8")
    assert "runtime_recovery" in trace_events


def test_evidence_reviewer_timeout_recovers_pdf_text_batch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_reviewer_pdf_text_recovery", "review", run_id="run_reviewer_pdf_text_recovery")
    batch = {
        "attachments": [
            {
                "status": "success",
                "content_kind": "pdf",
                "extraction_method": "pdf_text",
                "name": "invoice_extract.pdf",
                "attachment_id": "att_pdf_text",
                "body_markdown": "# Invoice INV-PDF-1\n\n- Evidence type: invoice\n- Supplier legal name: PDF Vendor Ltd.\n- Invoice total amount: 319.00 INR\n",
            }
        ]
    }
    ref = runner.context.artifacts.save(state.case_id, state.run_id, "attachment_batch", "read_attachment", batch)
    state.observability["latest_attachment_batch_ref"] = ref

    class APITimeoutError(Exception):
        pass

    def fail_call(role: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        runner.llm.calls.append(
            ModelCallRecord(
                role=role,
                model="test",
                prompt_version="test",
                input_preview="{}",
                output_preview="",
                error="APITimeoutError: Request timed out.",
            )
        )
        raise APITimeoutError("Request timed out.")

    monkeypatch.setattr(runner.roles, "call", fail_call)
    request = AgentTurnRequest(case_id=state.case_id, message="review pdf text")

    review_request = AgentTurnRequest(case_id=state.case_id, message="continue review")
    result = runner.invoke_manager_tool(
        state=state,
        request=review_request,
        planner_context={},
        name="evidence_reviewer",
        payload={"mode": "review"},
    )

    role_result = runner.context.last_evidence_reviewer_result(state, mode="review")
    assert result["status"] == "success"
    assert role_result["suggested_patch"]["add_evidence"][0]["type"] == "invoice"
    assert "PDF Vendor Ltd" in json.dumps(role_result, ensure_ascii=False)


def test_case_patch_writer_reduces_review_without_llm_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_patch_timeout_recovery", "review", run_id="run_patch_timeout_recovery")
    reviewer_result = {
        "mode": "review",
        "suggested_patch": {
            "requirements": [
                {"id": "duplicate_payment_screen", "label": "Duplicate payment screen", "status": "conflict", "kind": "risk_check"}
            ],
            "add_evidence": [
                {
                    "id": "ev_dup",
                    "type": "duplicate_payment_check",
                    "credibility": "medium",
                    "summary": "Prior payment PAY-2026-4431 and clearing CLR-2026-4431 require duplicate review.",
                    "source": "attachment",
                    "review_result": {"should_accept": True},
                    "supports": [{"requirement": "duplicate_payment_screen", "support_level": "partial"}],
                    "conflicts": ["duplicate_payment_screen unresolved duplicate payment PAY-2026-4431 / CLR-2026-4431"],
                    "quoted_text": ["Prior payment document: PAY-2026-4431; Clearing document: CLR-2026-4431"],
                }
            ],
            "risk_flags": ["duplicate_payment_hit_unresolved PAY-2026-4431 CLR-2026-4431"],
            "reply_brief": "Potential duplicate payment remains unresolved.",
        },
        "evidence_cards": [{"title": "Duplicate payment check", "doc_type": "duplicate_payment_check"}],
    }
    runner.harness.record_role_call(state, "evidence_reviewer", {}, reviewer_result)
    runner.harness.record_observation(state, runner.context.record_result(state, kind="role", name="evidence_reviewer", result=reviewer_result))
    runner._update_phase_after_role(state, "evidence_reviewer", reviewer_result)  # noqa: SLF001

    def fail_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("deterministic patch reduction must not call the LLM")

    monkeypatch.setattr(runner.roles, "call", fail_call)
    request = AgentTurnRequest(case_id=state.case_id, message="persist review")

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="case_patch_writer",
        payload={},
    )
    write_result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="write_case_patch",
        payload={},
    )

    case_state = runner.store.load(state.case_id)
    assert result["status"] == "success"
    assert write_result["status"] == "success"
    assert any(action.get("action") == "write_case_patch" for action in state.planner_actions)
    assert state.role_calls[-1]["capability"]["runtime"] == "deterministic_reducer"
    assert not any(call.role == "case_patch_writer" for call in runner.llm.calls)
    assert len(case_state.evidence_items) == 1
    assert "duplicate_payment_screen" in case_state.conflict_materials
    assert "PAY-2026-4431" in json.dumps(case_state.model_dump(), ensure_ascii=False)


def test_manager_final_with_boundary_note_is_not_rewritten(tmp_path, monkeypatch) -> None:
    final_answer = "材料已审查。本工具不会执行 ERP 付款或审批流程。"
    manager = ScriptedManagerRunner(
        [
            {
                "action": "final_answer",
                "final_answer": final_answer,
            }
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_final_guard_recovery", message="看看当前材料状态"))

    assert response.reply == final_answer
    assert response.trace["step_count"] == 1
    assert [item["action"] for item in response.trace["planner_actions"]] == ["final_answer"]
    assert not any(item.get("kind") == "guard" for item in response.trace["observations"])
    events = (
        runtime.runner.store.resolve_case_path("case_final_guard_recovery", f"traces/{response.trace['run_id']}/events.jsonl")
    ).read_text(encoding="utf-8")
    assert "final_answer_guard_rewrite" not in events


def test_manager_missing_duplicate_history_claim_uses_case_state_recovery(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [{"action": "final_answer", "final_answer": "重复付款筛查证据缺失：未提供历史付款记录比对。"}]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)
    runtime.runner.store.save(
        CaseState(
            case_id="case_duplicate_history_guard",
            status="collecting_materials",
            requirements=[Requirement(id="duplicate_payment_screen", status="conflict")],
            evidence_items=[
                EvidenceItem(
                    id="ev_history",
                    type="duplicate_payment_check",
                    summary="Prior payment document: PAY-543-HIST; Clearing document: CLR-543-HIST.",
                    review_result={"should_accept": True},
                    supports=[{"requirement": "duplicate_payment_screen", "support_level": "partial"}],
                    conflicts=[
                        {
                            "conflict_type": "duplicate_payment_hit",
                            "requirement": "duplicate_payment_screen",
                            "description": "Prior payment and clearing documents match the invoice.",
                        }
                    ],
                )
            ],
            risk_flags=["historical_payment_document_found", "clearing_document_found"],
        )
    )

    response = runtime.run_turn(AgentTurnRequest(case_id="case_duplicate_history_guard", message="总结重复付款审查结果"))

    assert "重复付款检查命中风险" in response.reply
    assert "历史付款记录 PAY-543-HIST" in response.reply
    assert "未提供历史付款记录" not in response.reply
    events = runtime.runner.store.resolve_case_path(
        "case_duplicate_history_guard", f"traces/{response.trace['run_id']}/events.jsonl"
    ).read_text(encoding="utf-8")
    assert "final_answer_guard_rewrite" in events


def test_runtime_final_answer_does_not_infer_duplicate_hit_from_screen_name(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    state = HarnessRuntime(store).begin_run("case_runtime_final_clean", "review", run_id="run_runtime_final_clean")
    case_state = CaseState(
        case_id=state.case_id,
        status="collecting_materials",
        requirements=[
            Requirement(id="invoice", status="satisfied"),
            Requirement(id="purchase_order", status="satisfied"),
            Requirement(id="goods_receipt_or_service_acceptance", status="satisfied"),
            Requirement(id="vendor_identity", status="satisfied"),
            Requirement(id="duplicate_payment_screen", status="weak"),
        ],
        evidence_items=[
            EvidenceItem(
                id="ev_001",
                type="invoice",
                summary=(
                    "Invoice ID: INV-5001 - Supplier legal name: Orion Parts Co., Ltd, "
                    "Amount: 12800.00 CNY, Date: 2026-04-18, Result: No duplicates found."
                ),
                supports=[{"requirement": "invoice", "support_level": "full"}],
            ),
            EvidenceItem(
                id="ev_005",
                type="duplicate_payment_check",
                summary="Duplicate invoice search result: No duplicate invoice found; Payment history search result: No prior payment found.",
                supports=[{"requirement": "duplicate_payment_screen", "support_level": "partial"}],
            ),
        ],
    )

    answer = _runtime_final_answer(case_state, state)

    assert "当前材料未显示重复付款命中" in answer
    assert "重复付款检查命中风险" not in answer
    assert "Orion Parts Co., Ltd." in answer
    assert "Supplier ID" not in answer
    assert "Result: No duplicates" not in answer


def test_runtime_final_answer_explains_bank_change_conflict(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    state = HarnessRuntime(store).begin_run("case_bank_change", "review", run_id="run_bank_change")
    case_state = CaseState(
        case_id=state.case_id,
        status="collecting_materials",
        requirements=[Requirement(id="vendor_identity", status="conflict")],
        risk_flags=["bank_change_request_without_vendor_master_approval"],
    )

    answer = _runtime_final_answer(case_state, state)

    assert "供应商名称等身份字段已匹配" in answer
    assert "银行账户变更缺少供应商主数据审批依据" in answer
    case_state.risk_flags = ["bank_account_mismatch_invoice_vs_vendor"]
    assert "银行账户变更缺少供应商主数据审批依据" not in _runtime_final_answer(case_state, state)


def test_evidence_reviewer_mode_mismatch_becomes_policy_feedback(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_mode_mismatch", "review", run_id="run_mode_mismatch")

    mismatch = runner._record_reviewer_mode_mismatch(
        state,
        "evidence_reviewer",
        {"mode": "extract"},
        {"mode": "review", "reason": "model ignored requested mode"},
        {"prompt_version": "test"},
    )

    assert mismatch is True
    assert state.observations[-1]["kind"] == "policy"
    assert state.observations[-1]["name"] == "role_mode_mismatch"
    assert state.observations[-1]["next_action_hint"] == "delegate_agent:evidence_reviewer_extract"
    assert state.role_calls[-1]["error"]


def test_evidence_reviewer_extract_shape_repairs_wrong_mode(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    runner = runtime.runner
    state = HarnessRuntime(runner.store).begin_run("case_mode_repair", "review", run_id="run_mode_repair")
    result = {
        "mode": "review",
        "extracted_fields": {"invoice_number": {"value": "INV-1"}},
        "extraction_result": {"source_docs": [{"doc_id": "att_1"}]},
        "suggested_patch": {},
    }

    mismatch = runner._record_reviewer_mode_mismatch(
        state,
        "evidence_reviewer",
        {"mode": "extract"},
        result,
        {"prompt_version": "test"},
    )

    assert mismatch is False
    assert result["mode"] == "extract"


def test_agent_runtime_interrupts_before_approved_tool_execution(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "inspect files"},
            {"action": "final_answer", "final_answer": "已列出本地 case 文件。"},
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)
    runner = runtime.runner
    runner.tools._specs["list_case_files"] = replace(runner.tools.get("list_case_files"), approval_mode="always")  # noqa: SLF001

    response = runtime.run_turn(AgentTurnRequest(case_id="case_approval_ok", message="列一下文件"))

    assert response.trace["status"] == "waiting_approval"
    assert response.trace["tool_calls"] == []
    assert response.trace["interrupts"][0]["tool"] == "list_case_files"
    assert response.trace["interrupts"][0]["risk_level"] == "read"
    assert response.trace["interrupts"][0]["input_sha256"]
    assert "input" not in response.trace["interrupts"][0]

    resumed = runtime.resume_approval("case_approval_ok", response.trace["run_id"], approved=True, reason="ok")

    assert resumed.trace["tool_calls"][0]["tool"] == "list_case_files"
    assert resumed.trace["tool_calls"][0]["capability"]["approval_mode"] == "always"
    assert resumed.reply == "已列出本地 case 文件。"


def test_agent_runtime_rejected_approval_does_not_execute_tool(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "inspect files"},
            {"action": "final_answer", "final_answer": "已取消查看文件，未执行该工具。"},
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)
    runtime.runner.tools._specs["list_case_files"] = replace(runtime.runner.tools.get("list_case_files"), approval_mode="always")  # noqa: SLF001

    response = runtime.run_turn(AgentTurnRequest(case_id="case_approval_no", message="列一下文件"))
    resumed = runtime.resume_approval("case_approval_no", response.trace["run_id"], approved=False, reason="no")

    assert resumed.trace["tool_calls"] == []
    assert resumed.reply == "已取消查看文件，未执行该工具。"
    assert any(observation["kind"] == "approval" and observation["name"] == "rejected" for observation in resumed.trace["observations"])


def test_report_request_runs_file_and_pdf_approval_pipeline(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    _seed_ready_case(runtime.runner.store, "case_report_approval")
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_report_approval", message="final report and render PDF"))

    assert response.trace["status"] == "waiting_approval"
    assert response.trace["interrupts"][0]["tool"] == "write_case_file"
    assert response.trace["role_calls"][0]["role"] == "report_writer"
    assert not list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.md"))
    waiting_trace = json.loads((tmp_path / "cases" / "case_report_approval" / "traces" / f"{response.trace['run_id']}.json").read_text(encoding="utf-8"))
    assert waiting_trace["interrupts"][0]["tool"] == "write_case_file"

    resumed = runtime.resume_approval("case_report_approval", response.trace["run_id"], approved=True, reason="ok")

    assert resumed.trace["status"] == "waiting_approval"
    assert resumed.trace["interrupts"][0]["tool"] == "render_pdf"
    assert list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.md"))
    assert not list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.pdf"))

    final = runtime.resume_approval("case_report_approval", resumed.trace["run_id"], approved=True, reason="ok")

    assert final.trace["phase"] == "finalized"
    assert "PDF reports/final_report_" in final.reply
    assert {call["tool"] for call in final.trace["tool_calls"]} >= {"write_case_file", "render_pdf"}
    assert list((tmp_path / "cases" / "case_report_approval" / "reports").glob("*.pdf"))
    assert any(event["kind"] == "approval" and event["name"] == "approved" for event in final.trace["observations"])
    final_trace = json.loads((tmp_path / "cases" / "case_report_approval" / "traces" / f"{response.trace['run_id']}.json").read_text(encoding="utf-8"))
    assert final_trace["interrupts"] == []


def test_report_completion_wins_over_step_limit_after_pdf_render(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_MAX_STEPS", "5")
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    _seed_ready_case(runtime.runner.store, "case_report_step_limit")
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_report_step_limit", message="final report and render PDF"))
    assert response.trace["status"] == "waiting_approval"
    response = runtime.resume_approval("case_report_step_limit", response.trace["run_id"], approved=True, reason="ok")
    assert response.trace["status"] == "waiting_approval"

    final = runtime.resume_approval("case_report_step_limit", response.trace["run_id"], approved=True, reason="ok")

    assert final.trace["phase"] == "finalized"
    assert final.trace["step_count"] >= final.trace["max_steps"]
    assert "报告已生成" in final.reply
    assert "当前运行已到本轮步数上限" not in final.reply
    assert {call["tool"] for call in final.trace["tool_calls"]} >= {"write_case_file", "render_pdf"}
    assert list((tmp_path / "cases" / "case_report_step_limit" / "reports").glob("*.pdf"))


def test_report_request_rejected_approval_does_not_write_file(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner([]))
    _seed_ready_case(runtime.runner.store, "case_report_rejected")
    monkeypatch.setattr(runtime.runner.roles, "call", _fake_report_writer)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_report_rejected", message="final report and render PDF"))
    final = runtime.resume_approval("case_report_rejected", response.trace["run_id"], approved=False, reason="no")

    assert final.trace["phase"] == "finalized"
    assert "已取消执行 write_case_file" in final.reply
    assert not list((tmp_path / "cases" / "case_report_rejected" / "reports").glob("*.md"))
    assert "write_case_file" not in {call["tool"] for call in final.trace["tool_calls"]}
    assert any(event["kind"] == "approval" and event["name"] == "rejected" for event in final.trace["observations"])
