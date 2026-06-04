from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from app.runtime.turn_runner import AgentRuntime
from app.state.schemas import AgentTurnRequest
from tests.test_graph import ScriptedManagerRunner, _runtime


def _runtime_with_approval(tmp_path, monkeypatch, decisions: list[dict[str, Any]]) -> AgentRuntime:
    runtime = _runtime(tmp_path, monkeypatch, ScriptedManagerRunner(decisions))
    runtime.runner.tools._specs["list_case_files"] = replace(runtime.runner.tools.get("list_case_files"), approval_mode="always")  # noqa: SLF001
    return runtime


def test_approval_rejection_blocks_same_tool_retry(tmp_path, monkeypatch) -> None:
    runtime = _runtime_with_approval(
        tmp_path,
        monkeypatch,
        [
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "inspect"},
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "try rejected tool again"},
            {"action": "final_answer", "final_answer": "tool was not executed"},
        ],
    )

    response = runtime.run_turn(AgentTurnRequest(case_id="case_approval_retry_block", message="list files"))
    resumed = runtime.resume_approval("case_approval_retry_block", response.trace["run_id"], approved=False, reason="no")

    assert resumed.trace["tool_calls"] == []
    assert resumed.reply == "tool was not executed"
    assert any(
        observation["kind"] == "policy" and observation["name"] == "approval_rejected_tool_blocked"
        for observation in resumed.trace["observations"]
    )


def test_approval_resume_targets_the_requested_run_when_two_runs_wait(tmp_path, monkeypatch) -> None:
    runtime = _runtime_with_approval(
        tmp_path,
        monkeypatch,
        [
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "first"},
            {"action": "call_tool", "target": "list_case_files", "input": {}, "reason": "second"},
            {"action": "final_answer", "final_answer": "first approval resumed"},
        ],
    )

    first = runtime.run_turn(AgentTurnRequest(case_id="case_two_waiting", message="first list files"))
    second = runtime.run_turn(AgentTurnRequest(case_id="case_two_waiting", message="second list files"))

    assert first.trace["status"] == "waiting_approval"
    assert second.trace["status"] == "waiting_approval"
    assert first.trace["run_id"] != second.trace["run_id"]

    resumed = runtime.resume_approval("case_two_waiting", first.trace["run_id"], approved=True, reason="ok")

    assert resumed.trace["run_id"] == first.trace["run_id"]
    assert resumed.trace["tool_calls"][0]["tool"] == "list_case_files"
    second_trace_path = runtime.runner.store.resolve_case_path("case_two_waiting", f"traces/{second.trace['run_id']}.json")
    second_trace = json.loads(second_trace_path.read_text(encoding="utf-8"))
    assert second_trace["phase"] == "waiting_approval"
    assert second_trace["tool_calls"] == []
