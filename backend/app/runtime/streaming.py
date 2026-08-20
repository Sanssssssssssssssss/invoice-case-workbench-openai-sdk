from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator


TERMINAL_KINDS = {"final", "error"}
TERMINAL_STATUSES = {"completed", "error"}
ACTIVE_STATUSES = {"accepted", "running", "waiting_approval"}


class ActiveCaseRunError(RuntimeError):
    """Raised when a case already has a live run."""


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
    pending_approval_event_id: str = ""


@dataclass
class _RunSubscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[RunStreamEvent]
    active: bool = True

    def push(self, event: RunStreamEvent) -> None:
        def enqueue() -> None:
            if not self.active:
                return
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self.queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        try:
            self.loop.call_soon_threadsafe(enqueue)
        except RuntimeError:
            self.active = False


class RunStreamHub:
    def __init__(self, *, max_events_per_run: int = 1000, max_terminal_runs: int = 100) -> None:
        self.max_events_per_run = max_events_per_run
        self.max_terminal_runs = max_terminal_runs
        self._lock = threading.RLock()
        self._runs: dict[str, RunStreamRecord] = {}
        self._subscribers: dict[str, list[_RunSubscriber]] = {}

    def create(self, *, run_id: str, case_id: str) -> RunStreamRecord:
        now = _utc_now()
        with self._lock:
            active = next(
                (
                    record
                    for record in self._runs.values()
                    if record.case_id == case_id and record.status in ACTIVE_STATUSES
                ),
                None,
            )
            if active is not None:
                raise ActiveCaseRunError(
                    f"case {case_id!r} already has active run {active.run_id!r}"
                )
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
        subscribers: list[_RunSubscriber] = []
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            if record.status in TERMINAL_STATUSES:
                raise RuntimeError(f"run {run_id!r} is already terminal")
            if kind == "approval_required" and record.status == "waiting_approval":
                raise RuntimeError(f"run {run_id!r} already has a pending approval")
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
                record.pending_approval_event_id = event.event_id
            elif kind == "final":
                record.status = "completed"
                record.pending_approval_event_id = ""
                response = payload.get("response") if isinstance(payload, dict) else None
                record.response = response if isinstance(response, dict) else None
            elif kind == "error":
                record.status = "error"
                record.pending_approval_event_id = ""
                record.error = str((payload or {}).get("message") or summary or "streaming run failed")
            elif record.status == "accepted":
                record.status = "running"
            subscribers = [subscriber for subscriber in self._subscribers.get(run_id, []) if subscriber.active]
            self._subscribers[run_id] = subscribers
            if kind in TERMINAL_KINDS:
                self._evict_terminal_runs_locked()
        for subscriber in subscribers:
            subscriber.push(event)
        return event

    def events_after(self, run_id: str, after_seq: int = 0) -> list[RunStreamEvent]:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            return [event for event in list(record.events or []) if event.seq > after_seq]

    def claim_approval(self, run_id: str, case_id: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            if record.case_id != case_id:
                raise ValueError("approval case does not match run case")
            if record.status != "waiting_approval":
                return False
            record.status = "running"
            record.pending_approval_event_id = ""
            record.updated_at = _utc_now()
            return True

    def run_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    async def subscribe(self, run_id: str, after_seq: int = 0) -> AsyncIterator[RunStreamEvent]:
        loop = asyncio.get_running_loop()
        subscriber = _RunSubscriber(loop=loop, queue=asyncio.Queue(maxsize=self.max_events_per_run))
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            replay = [event for event in list(record.events or []) if event.seq > after_seq]
            caught_up_terminal = record.status in TERMINAL_STATUSES and not replay
            if not caught_up_terminal:
                self._subscribers.setdefault(run_id, []).append(subscriber)
        last_seq = after_seq
        try:
            for event in replay:
                last_seq = max(last_seq, event.seq)
                yield event
                if event.kind in TERMINAL_KINDS:
                    return
            if caught_up_terminal:
                return
            while True:
                event = await subscriber.queue.get()
                if event.seq <= last_seq:
                    continue
                last_seq = max(last_seq, event.seq)
                yield event
                if event.kind in TERMINAL_KINDS:
                    return
        finally:
            subscriber.active = False
            with self._lock:
                subscribers = self._subscribers.get(run_id)
                if subscribers is not None:
                    self._subscribers[run_id] = [item for item in subscribers if item is not subscriber and item.active]
                    if not self._subscribers[run_id]:
                        self._subscribers.pop(run_id, None)

    def as_dict(self, record: RunStreamRecord) -> dict[str, Any]:
        data = asdict(record)
        data["events"] = [asdict(event) for event in record.events or []]
        return data

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            for subscribers in self._subscribers.values():
                for subscriber in subscribers:
                    subscriber.active = False
            self._subscribers.clear()

    def _evict_terminal_runs_locked(self) -> None:
        terminal = sorted(
            (record for record in self._runs.values() if record.status in TERMINAL_STATUSES),
            key=lambda record: record.updated_at,
        )
        for record in terminal[: max(0, len(terminal) - self.max_terminal_runs)]:
            self._runs.pop(record.run_id, None)
            for subscriber in self._subscribers.pop(record.run_id, []):
                subscriber.active = False


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
        pending_approval_event_id=record.pending_approval_event_id,
    )


def _safe_payload(value: Any, *, max_chars: int = 1200) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            lowered = text_key.lower()
            if lowered in {"reasoning_excerpt", "reasoning_delta", "reasoning_content", "chain_of_thought"}:
                continue
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
