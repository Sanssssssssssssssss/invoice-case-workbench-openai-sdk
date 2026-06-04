from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_debug_events(case_root: Path, trace: dict[str, Any]) -> list[dict[str, Any]]:
    root = case_root.resolve()
    run_id = str(trace.get("run_id") or "")
    refs: list[str] = []
    if trace.get("debug_events_ref"):
        refs.append(str(trace.get("debug_events_ref")))
    if run_id:
        refs.append(f"traces/{run_id}/events.jsonl")
    for ref in refs:
        path = (root / ref).resolve()
        if root not in path.parents and path != root:
            continue
        if not path.exists():
            continue
        events = _read_debug_events(path, run_id)
        if events:
            return events[-500:]
    return []


def build_debug_timeline_events(debug_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_event_from_debug_event(event) for event in debug_events]


def _read_debug_events(path: Path, run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            item = json.loads(raw_line)
            if not isinstance(item, dict):
                continue
            if run_id and str(item.get("run_id") or "") != run_id:
                continue
            events.append(item)
    except Exception:
        return []
    return events


def _event_from_debug_event(event: dict[str, Any]) -> dict[str, Any]:
    raw_kind = str(event.get("kind") or "")
    payload = _flatten_debug_payload(event)
    mapped_kind = {
        "planner_action": "Planner",
        "model_call": "Model",
        "role_call": "Role",
        "tool_call": "Tool",
        "observation": "Observation",
        "checkpoint": "Checkpoint",
        "final_answer": "Artifact",
    }.get(raw_kind, "Observation")
    status = _debug_event_status(raw_kind, payload)
    return {
        "kind": mapped_kind,
        "name": str(event.get("name") or raw_kind or "event"),
        "detail": str(event.get("summary") or event.get("payload_preview") or ""),
        "status": status,
        "payload": payload,
        "tag": "error" if status == "error" else _trace_tag_for_kind(mapped_kind),
    }


def _flatten_debug_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    inner = event.get("payload")
    if isinstance(inner, dict):
        for key, value in inner.items():
            payload.setdefault(key, value)
    payload["raw_event"] = event
    return payload


def _debug_event_status(raw_kind: str, payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if error or payload.get("schema_validation_error"):
        return "error"
    if isinstance(error, dict) and error:
        return "error"
    if raw_kind == "checkpoint":
        return "saved"
    return "ok"


def _trace_tag_for_kind(kind: str) -> str:
    return {
        "Planner": "planner",
        "Tool": "tool",
        "Model": "model",
        "Checkpoint": "normal",
        "Artifact": "tool",
    }.get(kind, "normal")
