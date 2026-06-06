from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app.config import get_settings
from app.harness import HarnessRuntime
from app.runtime import agents_sdk
from app.runtime.streaming import stream_hub
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, AgentTurnResponse, CaseState


def test_streaming_run_api_emits_safe_ordered_final(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    import app.api.agent_runs as agent_runs
    from app.main import app

    stream_hub.clear()
    store = CaseStore(tmp_path / "cases")
    monkeypatch.setattr(agent_runs, "AgentRuntime", lambda: _FakeRuntime(store))
    client = TestClient(app)

    accepted = client.post("/api/agent/runs", json={"case_id": "case_stream", "message": "hello", "attachments": []}).json()
    events = _read_stream_events(client, accepted["stream_url"])

    assert accepted["status"] == "accepted"
    assert [event["event"] for event in events] == ["run_started", "context_loaded", "model_started", "assistant_delta", "tool_started", "tool_finished", "final"]
    final_payload = events[-1]["data"]["payload"]["response"]
    assert final_payload["case_id"] == "case_stream"
    assert final_payload["reply"] == "streamed final"
    assert "SECRET_RAW_ATTACHMENT" not in json.dumps(events, ensure_ascii=False)


def test_streaming_approval_resume_continues_same_run(tmp_path, monkeypatch) -> None:
    _configure(tmp_path, monkeypatch)
    import app.api.agent_runs as agent_runs
    from app.main import app

    stream_hub.clear()
    store = CaseStore(tmp_path / "cases")
    runtime = _FakeRuntime(store, waiting_first=True)
    monkeypatch.setattr(agent_runs, "AgentRuntime", lambda: runtime)
    client = TestClient(app)

    accepted = client.post("/api/agent/runs", json={"case_id": "case_approval", "message": "report", "attachments": []}).json()
    _wait_until(lambda: stream_hub.get(accepted["run_id"]).status == "waiting_approval")
    approval_events = stream_hub.events_after(accepted["run_id"])
    assert [event.kind for event in approval_events if event.kind == "approval_required"]

    resumed = client.post(
        f"/api/agent/runs/{accepted['run_id']}/approval",
        json={"case_id": "case_approval", "approved": True, "reason": "ok"},
    ).json()
    events = _read_stream_events(client, f"{resumed['stream_url']}?after_seq={approval_events[-1].seq}")

    assert resumed["run_id"] == accepted["run_id"]
    assert "approval_decision" in [event["event"] for event in events]
    assert events[-1]["event"] == "final"
    assert events[-1]["data"]["payload"]["response"]["reply"] == "resumed final"


def test_shared_openai_client_pool_reuses_by_timeout(monkeypatch) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    agents_sdk.enable_shared_openai_clients()
    try:
        first = agents_sdk.build_run_config(settings, workflow_name="test", timeout_seconds=12)
        second = agents_sdk.build_run_config(settings, workflow_name="test", timeout_seconds=12)
        third = agents_sdk.build_run_config(settings, workflow_name="test", timeout_seconds=24)

        assert getattr(first, "_invoice_openai_client") is getattr(second, "_invoice_openai_client")
        assert getattr(first, "_invoice_openai_client") is not getattr(third, "_invoice_openai_client")
        assert getattr(first, "_invoice_close_openai_client") is False
    finally:
        asyncio.run(agents_sdk.close_shared_openai_clients())


class _FakeRuntime:
    def __init__(self, store: CaseStore, waiting_first: bool = False) -> None:
        self.store = store
        self.waiting_first = waiting_first
        self.runner = SimpleNamespace(store=store, harness=HarnessRuntime(store))

    async def run_turn_streamed(self, request: AgentTurnRequest, *, run_id: str, event_sink: Any | None = None) -> AgentTurnResponse:
        assert event_sink is not None
        event_sink("context_loaded", {"case_id": request.case_id, "content": "SECRET_RAW_ATTACHMENT"}, summary="context")
        if self.waiting_first:
            interrupts = [
                {
                    "type": "tool_approval",
                    "case_id": request.case_id,
                    "run_id": run_id,
                    "tool": "write_case_file",
                    "risk_level": "external_write",
                    "input_preview": "{}",
                    "input_sha256": "abc",
                    "reason": "approval required",
                }
            ]
            event_sink("approval_required", {"case_id": request.case_id, "run_id": run_id, "interrupts": interrupts}, summary="approval")
            return AgentTurnResponse(
                case_id=request.case_id,
                reply="approval",
                case_state=CaseState(case_id=request.case_id),
                trace={"run_id": run_id, "status": "waiting_approval", "interrupts": interrupts},
            )
        event_sink("model_started", {"role": "planner"}, summary="model")
        event_sink("assistant_delta", {"delta": "streamed final"}, summary="delta")
        event_sink("tool_started", {"tool": "list_case_files", "tool_input": {"secret": "SECRET_RAW_ATTACHMENT"}}, summary="tool")
        event_sink("tool_finished", {"tool": "list_case_files", "status": "success"}, summary="tool done")
        return AgentTurnResponse(
            case_id=request.case_id,
            reply="streamed final",
            case_state=CaseState(case_id=request.case_id),
            trace={"run_id": run_id, "status": "completed"},
        )

    async def resume_approval_streamed(
        self,
        case_id: str,
        run_id: str,
        approved: bool,
        reason: str = "",
        *,
        event_sink: Any | None = None,
    ) -> AgentTurnResponse:
        assert approved is True
        assert event_sink is not None
        event_sink("approval_decision", {"approved": approved, "reason": reason}, summary="decision")
        event_sink("assistant_delta", {"delta": "resumed final"}, summary="delta")
        return AgentTurnResponse(
            case_id=case_id,
            reply="resumed final",
            case_state=CaseState(case_id=case_id),
            trace={"run_id": run_id, "status": "completed"},
        )


def _configure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()


def _read_stream_events(client: TestClient, path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    with client.stream("GET", path) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if not line:
                if current:
                    rows.append(current)
                    if current.get("event") == "final":
                        break
                    current = {}
                continue
            if line.startswith("event: "):
                current["event"] = line.split(": ", 1)[1]
            elif line.startswith("data: "):
                current["data"] = json.loads(line.split(": ", 1)[1])
    return rows


def _wait_until(predicate: Any, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        import time

        time.sleep(0.05)
    raise AssertionError("condition was not met")
