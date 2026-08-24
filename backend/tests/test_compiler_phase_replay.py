from __future__ import annotations

import json

import pytest

from app.compiler_runtime.runtime import _planning_source_documents
from scripts.run_compiler_phase_replay import _frozen_source_items, load_model_call


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


def test_load_model_call_selects_executor_check(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    rows = [
        {
            "kind": "model_call",
            "name": "executor",
            "payload": {"payload": {"focus_check_ids": [check_id]}},
        }
        for check_id in ("check_a", "check_b")
    ]
    events.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    call = load_model_call(events, role="executor", check_id="check_b")

    assert call["payload"]["focus_check_ids"] == ["check_b"]


def test_frozen_source_items_restore_persisted_source_identity() -> None:
    items = _frozen_source_items(
        {
            "evidence_items": [
                {
                    "type": "invoice",
                    "title": "invoice.pdf",
                    "content": "TOTAL 10 EUR",
                    "metadata": {
                        "source_doc_id": "src_1",
                        "source_fingerprint": "sha256:1",
                    },
                }
            ]
        }
    )

    assert items == [
        {
            "source_doc_id": "src_1",
            "source_fingerprint": "sha256:1",
            "already_persisted": True,
            "source_id": "src_1",
            "source_content": "TOTAL 10 EUR",
            "name": "invoice.pdf",
            "type": "invoice",
        }
    ]


def test_snapshot_evidence_items_lower_to_identity_free_compiler_documents() -> None:
    assert _planning_source_documents(
        [{"id": "ev-secret", "type": "invoice", "content": "INVOICE\nTOTAL 10 EUR"}]
    ) == [{"document_index": 1, "kind": "invoice", "content": "INVOICE\nTOTAL 10 EUR"}]
