from __future__ import annotations

import json

import pytest

from app.memory_service import MemoryService
from app.session_compactor import SessionCompactPayload, SessionCompactor
from app.state.case_store import CaseStore
from app.state.session_repository import LONG_MESSAGE_THRESHOLD, SessionRepository, SessionVersionConflict


class _FakeSettings:
    session_compact_model = "fake-fast-model"
    context_char_limit = 200000


class _FakeCompactLlm:
    settings = _FakeSettings()

    def __init__(self) -> None:
        self.payloads = []

    def complete_structured(self, *, role, system_prompt, payload, model_type, prompt_version="v1", model=None):
        self.payloads.append(payload)
        return model_type.model_validate(
            {
                "session_summary": "用户偏好：中文回复；旧对话已压缩，业务事实仍以 case_state 为准。",
                "stable_case_facts": ["case_state remains the business truth"],
                "open_questions": ["还缺哪些材料"],
                "recent_decisions": ["保留最近三轮直接可见"],
                "risks": [],
                "artifact_refs_to_keep": [],
                "dropped_or_compacted_turns": [],
                "summary_limitations": ["summary is not case truth"],
                "candidate_case_notes": [],
            }
        )


def test_session_repository_artifacts_long_user_message_without_legacy_files(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    sessions = SessionRepository(store)
    long_message = "Invoice INV-LONG Amount 100 CNY\n" + ("detail " * (LONG_MESSAGE_THRESHOLD // 3))

    turn = sessions.append_user_turn("case_session", long_message, [], "run_001")
    session = sessions.load_session("case_session")

    assert turn["user_message"] == ""
    assert turn["user_message_ref"]
    assert "超长文本材料" in turn["user_message_summary"]
    assert "INV-LONG" not in turn["user_message_summary"]
    assert sessions.read_user_message("case_session", turn) == long_message
    assert session["artifact_refs"] == [turn["user_message_ref"]]
    assert not (store.case_dir("case_session") / "session.json").exists()
    assert not (store.case_dir("case_session") / "conversation.jsonl").exists()


def test_session_repository_context_window_and_rollback_do_not_change_case_state(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    sessions = SessionRepository(store)
    before = store.load("case_rollback").model_dump()
    first = sessions.append_user_turn("case_rollback", "first", [], "run_001")
    sessions.append_assistant_turn("case_rollback", first["turn_id"], "first reply", "run_001")
    second = sessions.append_user_turn("case_rollback", "second", [], "run_002")
    sessions.append_assistant_turn("case_rollback", second["turn_id"], "second reply", "run_002")

    assert [item["turn_id"] for item in sessions.get_context_window("case_rollback", limit=3)] == [
        first["turn_id"],
        second["turn_id"],
    ]

    rolled_back = sessions.rollback_turn("case_rollback", "latest")

    assert rolled_back["turn_id"] == second["turn_id"]
    assert [item["turn_id"] for item in sessions.get_context_window("case_rollback", limit=3)] == [first["turn_id"]]
    assert store.load("case_rollback").model_dump() == before


def test_session_repository_optimistic_version_conflict(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    sessions = SessionRepository(store)
    version = sessions.load_session("case_version")["version"]
    sessions.append_user_turn("case_version", "hello", [], "run_001", expected_version=version)

    with pytest.raises(SessionVersionConflict):
        sessions.append_user_turn("case_version", "stale write", [], "run_002", expected_version=version)


def test_conversation_returns_persistent_safe_attachment_metadata(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    sessions = SessionRepository(store)
    attachment = store.resolve_case_path("case_attachment", "attachments/originals/invoice.pdf")
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(b"%PDF")
    sessions.append_user_turn(
        "case_attachment",
        "review this",
        [
            {
                "name": "invoice.pdf",
                "path": str(attachment),
                "content_type": "application/pdf",
            }
        ],
        "run_001",
    )

    item = sessions.get_conversation_items("case_attachment")[0]

    assert item["attachments"] == [
        {
            "name": "invoice.pdf",
            "path": "attachments/originals/invoice.pdf",
            "content_type": "application/pdf",
        }
    ]
    assert str(tmp_path) not in json.dumps(item)


def test_session_compactor_writes_summary_without_case_patch_or_trace_write(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    sessions = SessionRepository(store)
    llm = _FakeCompactLlm()
    compactor = SessionCompactor(sessions, store, llm)
    for idx in range(7):
        turn = sessions.append_user_turn("case_compact", f"turn {idx}", [], f"run_{idx}")
        sessions.append_assistant_turn("case_compact", turn["turn_id"], f"reply {idx}", f"run_{idx}")
    before = store.load("case_compact").model_dump()

    payload = compactor.compact_before_run("case_compact", force=True, reason="test")
    after = store.load("case_compact").model_dump()
    session = sessions.load_session("case_compact")

    assert payload is not None
    assert session["session_summary"]
    assert session["compact_history"]
    assert before == after
    assert session["recent_turn_ids"] == ["turn_005", "turn_006", "turn_007"]
    assert llm.payloads
    assert "previous_session_summary" in llm.payloads[0]
    assert session["compact_history"][-1]["summary_payload"]["strategy"] == "pre_run_llm"
    event_log = store.resolve_case_path("case_compact", "traces/events.jsonl")
    assert not event_log.exists()


def test_memory_service_requires_source_ref_and_searches_hints(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    memory = MemoryService(store)

    with pytest.raises(ValueError):
        memory.add_memory(case_id="case_memory", memory_type="preference", text="use Chinese", source_ref="")

    memory.add_memory(
        case_id="case_memory",
        memory_type="preference",
        text="用户偏好：报告默认中文，风险速览前置。",
        source_ref="case_memory/run_001/turn_001",
    )
    hits = memory.search(case_id="case_memory", query="报告中文风险前置", limit=3)

    assert hits
    assert hits[0]["source_ref"] == "case_memory/run_001/turn_001"
    assert hits[0]["boundary"] == "memory_hint_only_not_case_truth"
    assert 0 < hits[0]["score"] <= 1
    assert hits[0]["score"] == hits[0]["relevance_score"] == hits[0]["confidence"]
    assert hits[0]["raw_score"] >= 1
    assert hits[0]["score_terms"]
    assert "token_overlap" in hits[0]["score_reason"]


def test_session_compact_payload_coerces_dropped_turn_objects() -> None:
    payload = SessionCompactPayload.model_validate(
        {
            "session_summary": "摘要",
            "stable_case_facts": [],
            "open_questions": [],
            "recent_decisions": [],
            "risks": [],
            "artifact_refs_to_keep": [],
            "dropped_or_compacted_turns": [{"turn_id": "turn_001", "reason": "compacted"}],
            "summary_limitations": [],
            "candidate_case_notes": [],
        }
    )

    assert payload.dropped_or_compacted_turns == ["turn_001"]
