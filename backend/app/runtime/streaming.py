from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


TERMINAL_KINDS = {"final", "error"}


@dataclass
class RunStreamEvent:
    seq: int
    event_id: str
    run_id: str
    case_id: str
    kind: str
    ts: str
    summary: str
    payload: dict[str, Any]


@dataclass
class RunStreamRecord:
    run_id: str
    case_id: str
    status: str = "accepted"
    created_at: str = ""
    updated_at: str = ""
    next_seq: int = 1
    events: list[RunStreamEvent] | None = None
    response: dict[str, Any] | None = None
    error: str = ""


class RunStreamHub:
    def __init__(self, *, max_events_per_run: int = 1000) -> None:
        self.max_events_per_run = max_events_per_run
        self._lock = threading.RLock()
        self._runs: dict[str, RunStreamRecord] = {}

    def create(self, *, run_id: str, case_id: str) -> RunStreamRecord:
        now = _utc_now()
        with self._lock:
            record = RunStreamRecord(
                run_id=run_id,
                case_id=case_id,
                created_at=now,
                updated_at=now,
                events=[],
            )
            self._runs[run_id] = record
        self.emit(run_id, "run_started", {"case_id": case_id, "run_id": run_id}, summary="Run accepted.")
        return self.get(run_id)

    def get(self, run_id: str) -> RunStreamRecord:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            return _copy_record(record)

    def emit(self, run_id: str, kind: str, payload: dict[str, Any] | None = None, *, summary: str = "") -> RunStreamEvent:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            seq = record.next_seq
            record.next_seq += 1
            event = RunStreamEvent(
                seq=seq,
                event_id=f"{run_id}:stream:{seq:06d}",
                run_id=run_id,
                case_id=record.case_id,
                kind=kind,
                ts=_utc_now(),
                summary=summary,
                payload=_safe_payload(payload or {}),
            )
            events = record.events if record.events is not None else []
            events.append(event)
            if len(events) > self.max_events_per_run:
                del events[: len(events) - self.max_events_per_run]
            record.events = events
            record.updated_at = event.ts
            if kind == "approval_required":
                record.status = "waiting_approval"
            elif kind == "final":
                record.status = "completed"
                response = payload.get("response") if isinstance(payload, dict) else None
                record.response = response if isinstance(response, dict) else None
            elif kind == "error":
                record.status = "error"
                record.error = str((payload or {}).get("message") or summary or "streaming run failed")
            elif record.status in {"accepted", "waiting_approval"}:
                record.status = "running"
            return event

    def events_after(self, run_id: str, after_seq: int = 0) -> list[RunStreamEvent]:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            return [event for event in list(record.events or []) if event.seq > after_seq]

    def as_dict(self, record: RunStreamRecord) -> dict[str, Any]:
        data = asdict(record)
        data["events"] = [asdict(event) for event in record.events or []]
        return data

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()


stream_hub = RunStreamHub()


def _copy_record(record: RunStreamRecord) -> RunStreamRecord:
    return RunStreamRecord(
        run_id=record.run_id,
        case_id=record.case_id,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        next_seq=record.next_seq,
        events=list(record.events or []),
        response=dict(record.response or {}) if record.response else None,
        error=record.error,
    )


def _safe_payload(value: Any, *, max_chars: int = 1200) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            lowered = text_key.lower()
            if lowered in {"prompt", "raw_prompt", "raw_input", "raw_args", "full_input", "tool_input"}:
                result[text_key] = "[redacted]"
                continue
            if lowered in {"content", "attachments"} and text_key != "content_type":
                result[text_key] = "[redacted]"
                continue
            result[text_key] = _safe_payload(item, max_chars=max_chars)
        return result
    if isinstance(value, list):
        return [_safe_payload(item, max_chars=max_chars) for item in value[:20]]
    if isinstance(value, str):
        return value[:max_chars] + ("..." if len(value) > max_chars else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:max_chars]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
