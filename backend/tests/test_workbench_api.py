from __future__ import annotations

import json
from pathlib import Path
from fastapi.testclient import TestClient

from app.config import get_settings
from app.state.case_store import CaseStore, utc_now
from app.state.schemas import CaseState, EvidenceItem, Requirement
from app.state.session_repository import SessionRepository


def test_workbench_case_reads_and_archive(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_cockpit")
    sessions = SessionRepository(store)
    turn = sessions.append_user_turn("case_cockpit", "Please inspect this package.", [], "run_cockpit")
    sessions.append_assistant_turn("case_cockpit", turn["turn_id"], "Invoice number found.", "run_cockpit")
    (store.ensure_case_dirs("case_cockpit") / "reports" / "summary.md").write_text("# Summary", encoding="utf-8")

    client = TestClient(app)

    cases = client.get("/api/cases").json()
    assert cases[0]["case_id"] == "case_cockpit"
    assert cases[0]["ready_required_count"] == 1

    detail = client.get("/api/cases/case_cockpit").json()
    assert detail["case_id"] == "case_cockpit"
    assert len(client.get("/api/cases/case_cockpit/conversation").json()) == 2
    assert client.get("/api/cases/case_cockpit/requirements").json()[0]["id"] == "invoice_number"
    assert client.get("/api/cases/case_cockpit/evidence").json()[0]["id"] == "ev_001"
    assert client.get("/api/cases/case_cockpit/artifacts").json()[0]["name"] == "summary.md"

    archived = client.delete("/api/cases/case_cockpit")
    assert archived.status_code == 200
    assert not store.case_dir("case_cockpit").exists()


def test_workbench_conversation_keeps_safe_attachment_metadata(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    source = store.resolve_case_path("case_attachment", "attachments/originals/invoice.pdf")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF")
    sessions = SessionRepository(store)
    sessions.append_user_turn(
        "case_attachment",
        "review",
        [{"name": "invoice.pdf", "path": str(source), "content_type": "application/pdf"}],
        "run_attachment",
    )

    item = TestClient(app).get("/api/cases/case_attachment/conversation").json()[0]

    assert item["attachments"] == [
        {"name": "invoice.pdf", "path": "attachments/originals/invoice.pdf", "content_type": "application/pdf"}
    ]
    assert str(tmp_path) not in json.dumps(item)


def test_workbench_runs_events_and_sse_payload(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.api.workbench import sse_payload
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_cockpit")
    _seed_trace(store, "case_cockpit", "run_cockpit")
    client = TestClient(app)

    runs = client.get("/api/cases/case_cockpit/runs").json()
    events = client.get("/api/cases/case_cockpit/runs/run_cockpit/events").json()
    case_events = client.get("/api/cases/case_cockpit/events").json()

    assert runs[0]["run_id"] == "run_cockpit"
    assert runs[0]["event_count"] == 2
    assert [event["kind"] for event in events] == ["planner", "tool"]
    assert case_events[-1]["name"] == "search_files"
    assert "event: trace_event" in sse_payload("trace_event", events[0], event_id=events[0]["event_id"])


def test_workbench_waiting_approval_run_status(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_waiting")
    _seed_waiting_approval_trace(store, "case_waiting", "run_waiting")
    client = TestClient(app)

    runs = client.get("/api/cases/case_waiting/runs").json()

    assert runs[0]["run_id"] == "run_waiting"
    assert runs[0]["status"] == "waiting_approval"
    assert runs[0]["phase"] == "waiting_approval"


def test_blocking_approval_resume_endpoint_is_removed(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app
    client = TestClient(app)

    response = client.post(
        "/api/cases/case_cockpit/runs/run_cockpit/approval",
        json={"approved": True, "reason": "ok"},
    )

    assert response.status_code == 404


def test_generated_case_files_are_listed_and_served_safely(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_cockpit")
    root = store.ensure_case_dirs("case_cockpit")
    (root / "reports" / "final_report.md").write_text("# Final report", encoding="utf-8")
    (root / "reports" / "final_report.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "reports" / "nested").mkdir(parents=True, exist_ok=True)
    artifact_dir = root / "traces" / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "model_output.json").write_text(
        json.dumps({"name": "Model output", "artifact_type": "json", "run_id": "run_cockpit"}),
        encoding="utf-8",
    )
    preview_dir = root / "evidence" / "previews"
    crop_dir = root / "evidence" / "crops"
    preview_dir.mkdir(parents=True, exist_ok=True)
    crop_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "invoice_p001.png").write_bytes(b"\x89PNG\r\n")
    (crop_dir / "amount.png").write_bytes(b"\x89PNG\r\n")

    client = TestClient(app)
    artifacts = client.get("/api/cases/case_cockpit/artifacts").json()
    paths = {item["path"]: item for item in artifacts}

    assert "reports/final_report.md" in paths
    assert "reports/final_report.pdf" in paths
    assert "traces/artifacts/model_output.json" in paths
    assert "evidence/previews/invoice_p001.png" in paths
    assert "evidence/crops/amount.png" in paths
    assert paths["reports/final_report.md"]["generated"] is True
    assert paths["reports/final_report.md"]["bytes"] == len("# Final report")
    assert paths["reports/final_report.md"]["open_url"].endswith("download=0")
    assert paths["reports/final_report.md"]["download_url"].endswith("download=1")

    metadata = client.get(
        "/api/cases/case_cockpit/files/metadata",
        params={"path": "traces/artifacts/model_output.json"},
    ).json()
    assert metadata["name"] == "Model output"
    assert metadata["run_id"] == "run_cockpit"
    assert metadata["absolute_path"].endswith("model_output.json")

    content = client.get(
        "/api/cases/case_cockpit/files/content",
        params={"path": "reports/final_report.md"},
    )
    assert content.status_code == 200
    assert content.text == "# Final report"
    assert content.headers["content-type"].startswith("text/")

    download = client.get(
        "/api/cases/case_cockpit/files/content",
        params={"path": "reports/final_report.md", "download": "1"},
    )
    assert "attachment" in download.headers["content-disposition"]

    assert client.get("/api/cases/case_cockpit/files/metadata", params={"path": "../case_state.json"}).status_code == 400
    assert client.get("/api/cases/case_cockpit/files/metadata", params={"path": "case_state.json"}).status_code == 400
    assert client.get("/api/cases/case_cockpit/files/metadata", params={"path": "reports/missing.md"}).status_code == 404
    assert client.get("/api/cases/case_cockpit/files/metadata", params={"path": "reports/nested"}).status_code == 404


def test_workbench_preserves_each_public_work_event(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_cockpit")
    _seed_streaming_thinking_trace(store, "case_cockpit", "run_stream")
    client = TestClient(app)

    runs = client.get("/api/cases/case_cockpit/runs").json()
    events = client.get("/api/cases/case_cockpit/runs/run_stream/events").json()
    thinking = [event for event in events if event["kind"] == "thinking"]

    assert runs[0]["event_count"] == 5
    assert [event["kind"] for event in events] == ["thinking", "thinking", "model", "planner", "thinking"]
    assert len(thinking) == 3
    assert [event["event_id"] for event in thinking] == [
        "evt_thinking_001",
        "evt_thinking_002",
        "evt_thinking_003",
    ]
    assert thinking[1]["summary"] == "Plan structure validated"
    assert all("reasoning_excerpt" not in event["payload"] for event in thinking)
    assert "SECRET_HIDDEN_COT" not in json.dumps(events, ensure_ascii=False)


def test_workbench_rejects_unsafe_case_paths(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    client = TestClient(app)

    assert client.get("/api/cases/..bad").status_code == 400
    assert client.get("/api/cases/case_x/runs/%2E%2E/events").status_code == 400


def test_live_status_reports_public_work_log_without_hidden_reasoning(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.api.live_status import build_live_status
    from app.api.workbench import sse_payload
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_cockpit")
    _seed_running_thinking_trace(store, "case_cockpit", "run_live")
    client = TestClient(app)

    status = client.get("/api/cases/case_cockpit/live-status").json()
    assert status["runId"] == "run_live"
    assert status["isRunning"] is True
    assert status["thinkingSource"] == "public_work_log"
    assert status["latestThinking"]
    assert status["latestThoughtSummary"]
    assert status["latestThinking"] == "正在核对来源覆盖和字段一致性。"
    assert status["latestThoughtSummary"] == status["latestThinking"]
    assert status["elapsedMs"] >= 0
    assert "secret invoice payload" not in status["latestThinking"]
    assert "event: live_status" in sse_payload("live_status", build_live_status("case_cockpit").model_dump(), event_id=status["latestEventId"])


def test_live_status_does_not_turn_policy_allow_into_thinking(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_policy")
    _seed_policy_only_trace(store, "case_policy", "run_policy")
    client = TestClient(app)

    status = client.get("/api/cases/case_policy/live-status").json()

    assert status["runId"] == "run_policy"
    assert status["isRunning"] is True
    assert status["activeRole"] == "allow"
    assert status["latestSummary"] == "allowed"
    assert status["thinkingSource"] == ""
    assert status["latestThinking"] == ""
    assert status["reasoningChars"] == 0


def test_summarizer_model_call_is_artifact_summary_trace_event(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_summary")
    _seed_summarizer_trace(store, "case_summary", "run_summary")
    client = TestClient(app)

    events = client.get("/api/cases/case_summary/runs/run_summary/events").json()

    assert events[0]["kind"] == "artifact_summary"
    assert events[0]["summary"] == "Attachment artifact summary completed"


def test_live_status_handles_empty_and_completed_runs(tmp_path, monkeypatch) -> None:
    _configure_tmp_settings(tmp_path, monkeypatch)
    from app.main import app

    store = CaseStore()
    _seed_case(store, "case_empty")
    _seed_case(store, "case_done")
    _seed_trace(store, "case_done", "run_done")
    client = TestClient(app)

    empty = client.get("/api/cases/case_empty/live-status").json()
    done = client.get("/api/cases/case_done/live-status").json()

    assert empty["isRunning"] is False
    assert done["runId"] == "run_done"
    assert done["isRunning"] is False
    assert client.get("/api/cases/..bad/live-status").status_code == 400


def _configure_tmp_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INVOICE_AGENT_WORKSPACE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("INVOICE_AGENT_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()


def _seed_case(store: CaseStore, case_id: str) -> None:
    state = CaseState(
        case_id=case_id,
        status="collecting_materials",
        summary="Cockpit seed",
        requirements=[
            Requirement(id="invoice_number", label="Invoice number", status="satisfied", evidence_ids=["ev_001"]),
            Requirement(id="currency_tax", label="Currency and tax", status="weak", evidence_ids=["ev_001"]),
        ],
        evidence_items=[
            EvidenceItem(
                id="ev_001",
                type="invoice",
                credibility="medium",
                source="attachment",
                summary="Invoice INV-001",
                content="Invoice No INV-001",
                created_at=utc_now(),
                review_result={"should_accept": True, "evidence_type": "invoice"},
                supports=[
                    {"requirement": "invoice_number", "support_level": "full", "quoted_text": "INV-001"},
                    {"requirement": "currency_tax", "support_level": "partial", "quoted_text": "USD"},
                ],
            )
        ],
        weak_materials=["currency_tax"],
        satisfied_materials=["invoice_number"],
    )
    store.save(state)


def _seed_trace(store: CaseStore, case_id: str, run_id: str) -> None:
    root = store.ensure_case_dirs(case_id)
    trace = {
        "run_id": run_id,
        "case_id": case_id,
        "started_at": "2026-06-01T10:00:00+00:00",
        "completed_at": "2026-06-01T10:00:01+00:00",
        "phase": "review",
        "tool_calls": [{"tool": "search_files"}],
        "model_calls": [{"role": "planner", "total_tokens": 42}],
        "trace_checkpoints": [{"checkpoint_id": "trace_ckpt_001"}],
        "current_goal": "Review materials",
        "final_answer": "Done",
    }
    (root / "traces" / f"{run_id}.json").write_text(json.dumps(trace), encoding="utf-8")
    run_dir = root / "traces" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "seq": 1,
            "case_seq": 1,
            "event_id": "evt_001",
            "ts": "2026-06-01T10:00:00+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "kind": "planner_action",
            "name": "planner",
            "summary": "Planning",
            "payload": {"input": {"goal": "Review"}},
        },
        {
            "seq": 2,
            "case_seq": 2,
            "event_id": "evt_002",
            "ts": "2026-06-01T10:00:01+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "kind": "tool_call",
            "name": "search_files",
            "summary": "Completed search",
            "payload": {"duration_ms": 120, "output": {"matches": 1}},
        },
    ]
    text = "\n".join(json.dumps(item) for item in events) + "\n"
    (run_dir / "events.jsonl").write_text(text, encoding="utf-8")
    (root / "traces" / "events.jsonl").write_text(text, encoding="utf-8")


def _seed_waiting_approval_trace(store: CaseStore, case_id: str, run_id: str) -> None:
    root = store.ensure_case_dirs(case_id)
    trace = {
        "run_id": run_id,
        "case_id": case_id,
        "started_at": "2026-06-01T10:00:00+00:00",
        "completed_at": "",
        "phase": "waiting_approval",
        "tool_calls": [],
        "model_calls": [{"role": "planner", "total_tokens": 42}],
        "trace_checkpoints": [],
        "current_goal": "Await approval",
        "final_answer": "",
    }
    (root / "traces" / f"{run_id}.json").write_text(json.dumps(trace), encoding="utf-8")
    run_dir = root / "traces" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "seq": 1,
        "case_seq": 1,
        "event_id": "evt_waiting",
        "ts": "2026-06-01T10:00:00+00:00",
        "case_id": case_id,
        "run_id": run_id,
        "kind": "approval_interrupt",
        "name": "list_case_files",
        "summary": "approval required: list_case_files",
        "payload": {"tool": "list_case_files", "risk_level": "read"},
    }
    text = json.dumps(event) + "\n"
    (run_dir / "events.jsonl").write_text(text, encoding="utf-8")
    (root / "traces" / "events.jsonl").write_text(text, encoding="utf-8")


def _seed_running_thinking_trace(store: CaseStore, case_id: str, run_id: str) -> None:
    root = store.ensure_case_dirs(case_id)
    trace = {
        "run_id": run_id,
        "case_id": case_id,
        "started_at": "2026-06-01T10:00:00+00:00",
        "completed_at": "",
        "phase": "review",
        "model_calls": [],
        "current_goal": "Review materials",
    }
    (root / "traces" / f"{run_id}.json").write_text(json.dumps(trace), encoding="utf-8")
    run_dir = root / "traces" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "seq": 1,
            "case_seq": 1,
            "event_id": "evt_thinking",
            "ts": "2026-06-01T10:00:00+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "phase": "review",
            "step_count": 1,
            "kind": "model_thinking",
            "name": "evidence_reviewer",
            "summary": "正在核对证据",
            "payload": {
                "role": "evidence_reviewer",
                "model": "kimi-k2.5",
                "stage": "evidence_reviewer",
                "action": "正在核对证据",
                "public_reason": "正在核对来源覆盖和字段一致性。",
                "status": "running",
                "raw_payload_should_not_leak": "secret invoice payload",
            },
        }
    ]
    text = "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n"
    (run_dir / "events.jsonl").write_text(text, encoding="utf-8")
    (root / "traces" / "events.jsonl").write_text(text, encoding="utf-8")


def _seed_policy_only_trace(store: CaseStore, case_id: str, run_id: str) -> None:
    root = store.ensure_case_dirs(case_id)
    trace = {
        "run_id": run_id,
        "case_id": case_id,
        "started_at": "2026-06-01T10:00:00+00:00",
        "completed_at": "",
        "phase": "attachment_read",
        "model_calls": [],
        "current_goal": "Review materials",
    }
    (root / "traces" / f"{run_id}.json").write_text(json.dumps(trace), encoding="utf-8")
    run_dir = root / "traces" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "seq": 1,
        "case_seq": 1,
        "event_id": "evt_policy",
        "ts": "2026-06-01T10:00:00+00:00",
        "case_id": case_id,
        "run_id": run_id,
        "phase": "attachment_read",
        "step_count": 1,
        "kind": "policy_check",
        "name": "allow",
        "summary": "allowed",
        "payload": {"policy_check": {"allowed": True}},
    }
    text = json.dumps(event, ensure_ascii=False) + "\n"
    (run_dir / "events.jsonl").write_text(text, encoding="utf-8")
    (root / "traces" / "events.jsonl").write_text(text, encoding="utf-8")


def _seed_streaming_thinking_trace(store: CaseStore, case_id: str, run_id: str) -> None:
    root = store.ensure_case_dirs(case_id)
    trace = {
        "run_id": run_id,
        "case_id": case_id,
        "started_at": "2026-06-01T10:00:00+00:00",
        "completed_at": "2026-06-01T10:00:02+00:00",
        "phase": "review",
        "model_calls": [{"role": "planner", "total_tokens": 42}],
        "current_goal": "Answer a simple question",
        "final_answer": "Done",
    }
    (root / "traces" / f"{run_id}.json").write_text(json.dumps(trace), encoding="utf-8")
    run_dir = root / "traces" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "seq": 1,
            "case_seq": 1,
            "event_id": "evt_thinking_001",
            "ts": "2026-06-01T10:00:00+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "step_count": 0,
            "kind": "model_thinking",
            "name": "planner",
            "summary": "Compiling review plan",
            "payload": {
                "role": "task_compiler",
                "stage": "task_compiler",
                "action": "Compiling review plan",
                "public_reason": "Turning requirements into atomic checks.",
                "reasoning_content": "SECRET_HIDDEN_COT",
                "status": "started",
            },
        },
        {
            "seq": 2,
            "case_seq": 2,
            "event_id": "evt_thinking_002",
            "ts": "2026-06-01T10:00:01+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "step_count": 0,
            "kind": "model_thinking",
            "name": "planner",
            "summary": "Plan structure validated",
            "payload": {
                "role": "task_compiler",
                "stage": "task_compiler",
                "action": "Plan structure validated",
                "public_reason": "Requirement roots and policy references are complete.",
                "status": "completed",
            },
        },
        {
            "seq": 3,
            "case_seq": 3,
            "event_id": "evt_model_001",
            "ts": "2026-06-01T10:00:01+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "kind": "model_call",
            "name": "planner",
            "summary": "planner / kimi-k2.5 stop",
            "payload": {"role": "planner", "model": "kimi-k2.5", "total_tokens": 42},
        },
        {
            "seq": 4,
            "case_seq": 4,
            "event_id": "evt_action_001",
            "ts": "2026-06-01T10:00:01+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "kind": "planner_action",
            "name": "final_answer",
            "summary": "step=1; action=final_answer; target=-",
            "payload": {"action": "final_answer"},
        },
        {
            "seq": 5,
            "case_seq": 5,
            "event_id": "evt_thinking_003",
            "parent_event_id": "evt_action_001",
            "ts": "2026-06-01T10:00:02+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "step_count": 1,
            "kind": "model_thinking",
            "name": "planner",
            "summary": "Checking unresolved evidence",
            "payload": {
                "role": "executor",
                "stage": "executor",
                "action": "Checking unresolved evidence",
                "public_reason": "Reviewing only the checks that remain NOT_FOUND.",
                "status": "running",
            },
        },
    ]
    text = "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n"
    (run_dir / "events.jsonl").write_text(text, encoding="utf-8")
    (root / "traces" / "events.jsonl").write_text(text, encoding="utf-8")


def _seed_summarizer_trace(store: CaseStore, case_id: str, run_id: str) -> None:
    root = store.ensure_case_dirs(case_id)
    trace = {
        "run_id": run_id,
        "case_id": case_id,
        "started_at": "2026-06-01T10:00:00+00:00",
        "completed_at": "2026-06-01T10:00:01+00:00",
        "phase": "attachment_read",
        "model_calls": [{"role": "summarizer", "total_tokens": 25}],
        "current_goal": "Read attachment",
        "final_answer": "Done",
    }
    (root / "traces" / f"{run_id}.json").write_text(json.dumps(trace), encoding="utf-8")
    run_dir = root / "traces" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "seq": 1,
            "case_seq": 1,
            "event_id": "evt_summarizer",
            "ts": "2026-06-01T10:00:00+00:00",
            "case_id": case_id,
            "run_id": run_id,
            "kind": "model_call",
            "name": "summarizer",
            "summary": "summarizer / kimi-k2.5 stop",
            "payload": {"role": "summarizer", "model": "kimi-k2.5", "total_tokens": 25},
        }
    ]
    text = "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n"
    (run_dir / "events.jsonl").write_text(text, encoding="utf-8")
    (root / "traces" / "events.jsonl").write_text(text, encoding="utf-8")
