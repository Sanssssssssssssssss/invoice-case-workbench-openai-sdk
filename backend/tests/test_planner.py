from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.runtime.turn_runner import AgentRuntime, ManagerRunOutcome, TurnRunner
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest


class NoopManagerRunner:
    def run(
        self,
        *,
        runner: TurnRunner,
        request: AgentTurnRequest,
        state,
        planner_context: dict[str, Any],
        manager_input: dict[str, Any],
    ) -> ManagerRunOutcome:
        _ = runner, request, state, planner_context, manager_input
        return ManagerRunOutcome(final_output="ok")


def _runtime(tmp_path, monkeypatch) -> AgentRuntime:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    return AgentRuntime(store=CaseStore(tmp_path / "cases"), manager_runner=NoopManagerRunner())


def test_manager_payload_uses_compiled_context_and_capability_cards(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    runner = runtime.runner
    request = AgentTurnRequest(case_id="case_001", message="please inspect attachments")
    state = runner.harness.begin_run(request.case_id, request.message, run_id="run_001")
    planner_context = {
        "case_brief": "case_id=case_001; status=collecting_materials",
        "attachments": [{"name": "01_invoice.pdf", "path": "C:/tmp/01_invoice.pdf"}],
        "recent_observations": [{"summary": "attachment read", "artifact_ref": "traces/artifacts/run_x/art_001.json"}],
    }

    payload = runner._manager_input(request, state, planner_context)  # noqa: SLF001

    assert "context_pack" in payload
    assert "capability_cards" in payload
    assert "decision_schema" not in payload
    assert "observations" not in payload
    assert "case_state" not in payload
    assert payload["capability_cards"]["specialists"]["evidence_reviewer"]
    assert isinstance(payload["capability_cards"]["tools"], list)
    assert payload["capability_cards"]["tools"]
    assert payload["capability_cards"]["skills"]
    assert payload["context_pack"]["attachments"][0]["name"] == "01_invoice.pdf"


def test_manager_sdk_tools_include_specialists_and_workspace_tools(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    runner = runtime.runner
    request = AgentTurnRequest(case_id="case_001", message="list files")
    state = runner.harness.begin_run(request.case_id, request.message, run_id="run_tools")

    tools = runner.sdk_tools(state=state, request=request, planner_context={"attachments": []})
    names = {tool.name for tool in tools}

    assert {"materials_advisor", "evidence_reviewer", "case_patch_writer", "report_writer"}.issubset(names)
    assert "list_case_files" in names
    assert "read_attachment" in names
    assert "write_case_patch" in names
