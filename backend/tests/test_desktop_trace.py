from __future__ import annotations

from pathlib import Path

import app.desktop.workbench as workbench_module
from app.desktop.workbench import DesktopWorkbench, _reason_from_model_payload


class _Mode:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _Cleaner:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear_session(self, case_id: str) -> None:
        self.cleared.append(case_id)

    def clear_case(self, case_id: str) -> None:
        self.cleared.append(case_id)


class _Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def case_dir(self, case_id: str) -> Path:
        return self.root / case_id


def test_trace_thought_view_uses_raw_model_response() -> None:
    workbench = object.__new__(DesktopWorkbench)
    rows = workbench._trace_thought_rows(
        {
            "kind": "Model",
            "name": "planner",
            "payload": {
                "role": "planner",
                "raw_response": '{"action":"call_role","role":"evidence_reviewer","reason":"review signature area"}',
            },
        }
    )

    text = "".join(value for _tag, value in rows)
    assert "review signature area" in text


def test_model_reason_extracts_visible_reason() -> None:
    assert (
        _reason_from_model_payload(
            {"raw_response": '{"action":"final_answer","reason":"explain blocked attachment"}'}
        )
        == "explain blocked attachment"
    )


def test_default_trace_selection_prefers_visible_thought() -> None:
    workbench = object.__new__(DesktopWorkbench)
    workbench.trace_detail_mode_var = _Mode("Thought")
    events = [
        {"kind": "Tool", "payload": {"tool": "read_attachment", "result_preview": "ok"}},
        {
            "kind": "Planner",
            "payload": {"action": "call_role", "role": "evidence_reviewer", "reason": "review invoice"},
        },
    ]

    assert workbench._default_trace_event_index(events) == 1


def test_visible_thought_line_summarizes_extraction_and_evidence_chain() -> None:
    workbench = object.__new__(DesktopWorkbench)
    extraction_line = workbench._visible_thought_line(
        {
            "kind": "Tool",
            "payload": {
                "tool": "read_attachment",
                "result": {
                    "attachments": [
                        {
                            "name": "invoice.pdf",
                            "extraction_ref": "evidence/extractions/att_001.json",
                            "field_inventory": [{"field": "invoice_number"}],
                        }
                    ]
                },
            },
        }
    )
    assert "dossiers=1" in extraction_line

    reviewer_line = workbench._visible_thought_line(
        {
            "kind": "Role",
            "payload": {
                "role": "evidence_reviewer",
                "result": {
                    "reason": "字段已核对",
                    "extracted_fields": {"invoice_number": {"value": "INV-1"}},
                    "suggested_patch": {
                        "add_evidence": [
                            {"metadata": {"evidence_chain": [{"claim": "invoice number present"}]}}
                        ]
                    },
                },
            },
        }
    )
    assert "fields=1" in reviewer_line
    assert "chain_rows=1" in reviewer_line


def test_delete_current_case_clears_workspace_session_and_memory(tmp_path, monkeypatch) -> None:
    workbench = object.__new__(DesktopWorkbench)
    case_id = "case_delete_cleanup"
    case_dir = tmp_path / case_id
    case_dir.mkdir()
    (case_dir / "case_state.json").write_text("{}", encoding="utf-8")
    sessions = _Cleaner()
    memory = _Cleaner()
    switched_to: list[str] = []
    monkeypatch.setattr(workbench_module, "show_confirm", lambda *args, **kwargs: True)
    workbench.is_sending = False
    workbench.active_case_id = case_id
    workbench.root = object()
    workbench.sessions = sessions
    workbench.memory = memory
    workbench.store = _Store(tmp_path)
    workbench.case_messages = {case_id: [("User", "00:00", "hello")]}
    workbench.case_traces = {case_id: [{"event": "x"}]}
    workbench.case_trace_runs = {case_id: ["run_1"]}
    workbench.case_trace_turns = {case_id: ["turn_1"]}
    workbench.pending_files = ["invoice.pdf"]
    workbench.case_state = object()
    workbench._delete_session_details = lambda _case_id: "details"
    workbench._known_case_ids = lambda include_active=False: ["case_remaining"]
    workbench.switch_case = lambda next_case: switched_to.append(next_case)

    workbench._delete_current_case()

    assert sessions.cleared == [case_id]
    assert memory.cleared == [case_id]
    assert not case_dir.exists()
    assert switched_to == ["case_remaining"]


def test_clear_chat_keeps_memory_and_case_workspace(tmp_path, monkeypatch) -> None:
    workbench = object.__new__(DesktopWorkbench)
    case_id = "case_clear_chat"
    case_dir = tmp_path / case_id
    case_dir.mkdir()
    sessions = _Cleaner()
    memory = _Cleaner()
    refreshed: list[str] = []
    monkeypatch.setattr(workbench_module, "show_confirm", lambda *args, **kwargs: True)
    workbench.is_sending = False
    workbench.active_case_id = case_id
    workbench.root = object()
    workbench.sessions = sessions
    workbench.memory = memory
    workbench.store = _Store(tmp_path)
    workbench.case_messages = {case_id: [("User", "00:00", "hello")]}
    workbench._refresh_messages = lambda: refreshed.append("messages")
    workbench._refresh_case_list = lambda: refreshed.append("cases")

    workbench._clear_current_conversation()

    assert sessions.cleared == [case_id]
    assert memory.cleared == []
    assert case_dir.exists()
    assert workbench.case_messages[case_id] == []
    assert refreshed == ["messages", "cases"]
