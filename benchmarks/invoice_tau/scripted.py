from __future__ import annotations

from typing import Any

from app.agents.capabilities import RoleCapability, role_capability
from app.agents.registry import RoleRegistry
from app.runtime.turn_runner import ManagerRunOutcome, TurnRunner
from app.state.schemas import AgentTurnRequest


class ScriptedManagerRunner:
    """Small tau-bench style policy/user-script runner.

    It drives the existing TurnRunner through the same public tool boundary as the
    real OpenAI manager, but uses scenario-defined decisions. This gives us a cheap,
    deterministic benchmark mode for CI and local regression runs.
    """

    def __init__(self, turns: list[list[dict[str, Any]]] | None = None) -> None:
        self.turns = list(turns or [])
        self.pending: dict[str, dict[str, Any]] = {}
        self.payloads: list[dict[str, Any]] = []

    def run(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state: Any,
        planner_context: dict[str, Any],
        manager_input: dict[str, Any],
    ) -> ManagerRunOutcome:
        self.payloads.append(manager_input)
        decisions = self.turns.pop(0) if self.turns else []
        return self._run_decisions(runner, request, state, planner_context, manager_input, decisions)

    def resume(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state: Any,
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
            target = _decision_target(decision)
            runner.invoke_manager_tool(
                state=state,
                request=request,
                planner_context=planner_context,
                name=target,
                payload=dict(decision.get("input") or {}),
            )
        elif not approved:
            self.pending.pop(state.run_id, None)
        return self._run_decisions(runner, request, state, planner_context, manager_input, [])

    def _run_decisions(
        self,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state: Any,
        planner_context: dict[str, Any],
        manager_input: dict[str, Any],
        decisions: list[dict[str, Any]],
    ) -> ManagerRunOutcome:
        for decision in decisions:
            action = str(decision.get("action") or "final_answer")
            if action in {"call_tool", "delegate_agent", "write_case_patch"}:
                target = _decision_target(decision)
                result = runner.invoke_manager_tool(
                    state=state,
                    request=request,
                    planner_context=planner_context,
                    name=target,
                    payload=dict(decision.get("input") or {}),
                )
                if result.get("status") == "approval_required":
                    self.pending[state.run_id] = decision
                    return ManagerRunOutcome(interruptions=[result], sdk_state="")
                continue
            if action in {"ask_user", "final_answer"}:
                return ManagerRunOutcome(final_output=str(decision.get("final_answer") or decision.get("message") or ""))
        return ManagerRunOutcome(final_output="已完成本轮基准测试脚本。")


def _decision_target(decision: dict[str, Any]) -> str:
    action = str(decision.get("action") or "")
    if action == "write_case_patch":
        return "write_case_patch"
    return str(decision.get("target") or "")


class ScriptedRoleRegistry:
    """RoleRegistry facade that returns scenario-defined structured outputs."""

    def __init__(self, outputs: dict[str, list[dict[str, Any]] | dict[str, Any]] | None = None) -> None:
        self.outputs: dict[str, list[dict[str, Any]]] = {}
        for role, value in (outputs or {}).items():
            if isinstance(value, list):
                self.outputs[role] = [dict(item) for item in value]
            elif isinstance(value, dict):
                self.outputs[role] = [dict(value)]
        self._metadata = RoleRegistry()

    def call(
        self,
        role: str,
        payload: dict[str, Any],
        *,
        on_stream: Any | None = None,
        prompt_partition: dict[str, Any] | None = None,
        hooks: Any | None = None,
    ) -> dict[str, Any]:
        _ = on_stream
        _ = prompt_partition
        _ = hooks
        items = self.outputs.get(role) or []
        if items:
            return dict(items.pop(0))
        return _default_role_output(role, payload)

    def prompt(self, role: str) -> str:
        return self._metadata.prompt(role)

    def prompt_file(self, role: str) -> str:
        return self._metadata.prompt_file(role)

    def prompt_version(self, role: str) -> str:
        return self._metadata.prompt_version(role)

    def capability(self, role: str) -> RoleCapability:
        return role_capability(role)

    def trace_metadata(self, role: str) -> dict[str, Any]:
        metadata = self.capability(role).trace_metadata()
        metadata["runtime"] = "scripted_tau_bench"
        return metadata

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(self.outputs.keys())


def _default_role_output(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    if role == "materials_advisor":
        return {
            "answer": "请补充发票、采购订单、收货或服务验收、供应商主数据以及重复付款检查。",
            "tasks": [
                {
                    "task": "补充核心付款审查材料",
                    "requirement": "invoice,purchase_order,goods_receipt_or_service_acceptance,vendor_identity,duplicate_payment_screen",
                    "current_status": "unknown",
                    "why_insufficient": "当前脚本未提供足够材料。",
                    "owner": "AP reviewer",
                    "format": "source documents or system exports",
                    "required_fields": ["invoice_number", "supplier", "amount"],
                    "impact": "无法形成完整付款审查结论。",
                }
            ],
            "missing_materials": ["invoice", "purchase_order", "goods_receipt_or_service_acceptance", "vendor_identity", "duplicate_payment_screen"],
            "next_questions": ["请上传核心材料。"],
        }
    if role == "evidence_reviewer":
        return {
            "mode": payload.get("mode") or "review",
            "evidence_type": "unknown",
            "credibility": "medium",
            "support_level": "none",
            "should_accept": False,
            "reason": "No scripted evidence reviewer output was supplied.",
            "risk_flags": [],
            "supports": [],
            "conflicts": [],
            "evidence_cards": [],
            "suggested_patch": {"next_questions": ["No scripted reviewer output was supplied."]},
            "reply_to_user": "未提供脚本化证据审查输出。",
        }
    if role == "case_patch_writer":
        return {"patch_type": "no_change", "case_updates": {}, "audit_note": "No scripted patch output was supplied."}
    if role == "report_writer":
        return {"title": "tau_bench_report", "markdown": "# Tau Bench Report\n\nNo scripted report output was supplied.\n"}
    raise KeyError(f"Unknown scripted role: {role}")
