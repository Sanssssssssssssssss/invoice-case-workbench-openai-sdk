from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from app.config import get_settings
from app.harness import HarnessRuntime
from app.llm import ModelCallRecord
from app.runtime.turn_runner import AgentRuntime, ManagerRunOutcome, TurnRunner, _runtime_final_answer
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

    class APITimeoutError(Exception):
        pass

    def fail_call(role: str, payload: dict[str, Any]) -> dict[str, Any]:
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

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
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


def test_case_patch_writer_timeout_persists_reviewer_suggested_patch(tmp_path, monkeypatch) -> None:
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

    class APITimeoutError(Exception):
        pass

    def fail_call(role: str, payload: dict[str, Any]) -> dict[str, Any]:
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
    request = AgentTurnRequest(case_id=state.case_id, message="persist review")

    result = runner.invoke_manager_tool(
        state=state,
        request=request,
        planner_context={},
        name="case_patch_writer",
        payload={},
    )

    case_state = runner.store.load(state.case_id)
    assert result["status"] == "success"
    assert any(action.get("action") == "write_case_patch" for action in state.planner_actions)
    assert state.role_calls[-1]["capability"]["runtime_recovery"] == "case_patch_writer_from_reviewer_suggested_patch_after_transient_failure"
    assert runner.llm.calls[-1].error == ""
    assert runner.llm.calls[-1].recovered_by == "case_patch_writer_from_reviewer_suggested_patch_after_transient_failure"
    assert len(case_state.evidence_items) == 1
    assert "duplicate_payment_screen" in case_state.conflict_materials
    assert "PAY-2026-4431" in json.dumps(case_state.model_dump(), ensure_ascii=False)


def test_guard_blocked_manager_final_uses_runtime_final_recovery(tmp_path, monkeypatch) -> None:
    manager = ScriptedManagerRunner(
        [
            {
                "action": "final_answer",
                "final_answer": "材料已审查。本工具不会执行 ERP 付款或审批流程。",
            }
        ]
    )
    runtime = _runtime(tmp_path, monkeypatch, manager)

    response = runtime.run_turn(AgentTurnRequest(case_id="case_final_guard_recovery", message="看看当前材料状态"))

    assert "ERP 付款" not in response.reply
    assert response.trace["step_count"] == 2
    assert [item["action"] for item in response.trace["planner_actions"]] == ["final_answer", "final_answer"]
    assert any(item.get("kind") == "guard" for item in response.trace["observations"])
    events = (
        runtime.runner.store.resolve_case_path("case_final_guard_recovery", f"traces/{response.trace['run_id']}/events.jsonl")
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
