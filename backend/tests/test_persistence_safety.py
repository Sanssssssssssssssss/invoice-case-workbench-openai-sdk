from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.harness import HarnessRuntime
from app.state.case_store import CaseStore
from app.state.persistence import atomic_write_text
from app.state.schemas import Attachment
from app.tools import file_workspace


def test_http_attachment_path_must_stay_inside_case_workspace(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="case workspace"):
        store.validate_attachment_path("case_boundary", str(outside))

    inside = store.resolve_case_path("case_boundary", "attachments/invoice.txt")
    assert store.validate_attachment_path("case_boundary", str(inside)) == inside


def test_same_name_images_keep_distinct_immutable_previews(tmp_path, monkeypatch) -> None:
    from PIL import Image

    store = CaseStore(tmp_path / "cases")
    first = store.resolve_case_path("case_previews", "attachments/first.png")
    second = store.resolve_case_path("case_previews", "attachments/second.png")
    Image.new("RGB", (10, 10), "red").save(first)
    Image.new("RGB", (10, 10), "blue").save(second)
    monkeypatch.setattr(file_workspace, "_run_ocr", lambda _path: "invoice")
    first_result = file_workspace._read_attachment_item(
        store,
        "case_previews",
        Attachment(name="invoice.png", path=str(first), content_type="image/png"),
        max_chars=12000,
    )
    first_preview = first_result["preview_paths"][0]
    first_bytes = store.resolve_case_path("case_previews", first_preview).read_bytes()
    second_result = file_workspace._read_attachment_item(
        store,
        "case_previews",
        Attachment(name="invoice.png", path=str(second), content_type="image/png"),
        max_chars=12000,
    )
    second_preview = second_result["preview_paths"][0]

    assert first_preview != second_preview
    assert store.resolve_case_path("case_previews", first_preview).read_bytes() == first_bytes


def test_atomic_write_keeps_previous_file_if_replace_fails(tmp_path, monkeypatch) -> None:
    import app.state.persistence as persistence

    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(persistence.os, "replace", lambda _source, _target: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_case_patch_read_modify_write_is_serialized_across_store_instances(tmp_path) -> None:
    root = tmp_path / "cases"
    CaseStore(root).load("case_concurrent")

    def add_flag(index: int) -> None:
        CaseStore(root).apply_patch(
            "case_concurrent",
            {
                "patch_type": "update_case",
                "case_updates": {"risk_flags": [f"risk_{index:02d}"]},
                "audit_note": f"risk {index}",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_flag, range(20)))

    assert set(CaseStore(root).load("case_concurrent").risk_flags) == {f"risk_{index:02d}" for index in range(20)}


def test_trace_case_sequences_are_unique_under_concurrent_append(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")

    def append(index: int) -> None:
        state = SimpleNamespace(
            debug_event_seq=0,
            case_id="case_trace",
            run_id=f"run_{index:02d}",
            session_id="case_trace:main",
            turn_id=f"turn_{index:02d}",
            step_count=0,
            phase="running",
            last_debug_event_id="",
        )
        HarnessRuntime(CaseStore(store.workspace_root)).append_debug_event(
            state,
            kind="tool",
            name="test",
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(20)))

    events_path = store.resolve_case_path("case_trace", "traces/events.jsonl")
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    sequences = [row["case_seq"] for row in rows]
    assert sequences == list(range(1, 21))
