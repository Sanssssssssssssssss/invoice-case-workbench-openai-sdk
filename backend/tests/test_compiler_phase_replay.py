from __future__ import annotations

import json

import pytest

from app.compiler_runtime.runtime import _planning_source_documents
from scripts.run_compiler_phase_replay import load_model_call


def test_load_model_call_selects_complete_phase_payload(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    rows = [
        {"kind": "model_call", "name": "planner", "payload": {"payload": {"skip": True}}},
        {
            "kind": "model_call",
            "name": "task_compiler",
            "run_id": "run_1",
            "payload": {
                "prompt_version": "compiler_v1",
                "system_prompt": "plan",
                "payload": {"required_output": {"active_requirement_ids": ["invoice"]}},
                "raw_response": "{}",
            },
        },
    ]
    events.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    call = load_model_call(events, role="task_compiler")

    assert call["run_id"] == "run_1"
    assert call["payload"]["required_output"]["active_requirement_ids"] == ["invoice"]


def test_load_model_call_rejects_missing_index(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")

    with pytest.raises(IndexError, match="found 0"):
        load_model_call(events, role="fine_verifier")


def test_snapshot_evidence_items_lower_to_identity_free_compiler_documents() -> None:
    assert _planning_source_documents(
        [{"id": "ev-secret", "type": "invoice", "content": "INVOICE\nTOTAL 10 EUR"}]
    ) == [{"document_index": 1, "kind": "invoice", "content": "INVOICE\nTOTAL 10 EUR"}]
