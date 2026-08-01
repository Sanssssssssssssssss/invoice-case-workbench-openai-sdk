from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.observability.model_metrics import trace_duration_ms, trace_token_count
from app.state.case_store import CaseStore
from app.state.schemas import CaseState, Requirement
from app.state.session_repository import SessionRepository


READY_STATUSES = {"accepted", "satisfied"}
SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
GENERATED_FILE_PREFIXES = (
    "reports/",
    "traces/artifacts/",
    "evidence/previews/",
    "evidence/crops/",
)
GENERATED_FILE_ORDER = {
    "reports": 0,
    "traces/artifacts": 1,
    "evidence/previews": 2,
    "evidence/crops": 3,
}
REQUIREMENT_WEIGHTS = {
    "satisfied": 1.0,
    "accepted": 1.0,
    "submitted": 0.6,
    "weak": 0.35,
    "conflict": 0.25,
    "rejected": 0.0,
    "missing": 0.0,
}


@dataclass(frozen=True)
class RequirementProgress:
    percent: int
    ready: int
    total: int


@dataclass(frozen=True)
class CaseSummary:
    case_id: str
    status: str
    summary: str
    updated_at: str
    requirement_count: int
    required_count: int
    ready_required_count: int
    evidence_count: int
    missing_count: int
    weak_count: int
    conflict_count: int
    satisfied_count: int


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    status: str
    started_at: str
    completed_at: str
    updated_at: str
    duration_ms: int | None
    phase: str
    tool_count: int
    role_count: int
    model_count: int
    checkpoint_count: int
    error_count: int
    event_count: int
    current_goal: str
    final_answer: str


@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    run_id: str
    case_id: str
    seq: int
    case_seq: int
    ts: str
    kind: str
    raw_kind: str
    name: str
    status: str
    summary: str
    parent_event_id: str
    caused_by_event_id: str
    duration_ms: int | None
    token_count: int | None
    input_preview: str
    output_preview: str
    payload: dict[str, Any]
    raw: dict[str, Any]


class WorkbenchReadService:
    def __init__(self, store: CaseStore | None = None, sessions: SessionRepository | None = None) -> None:
        self.store = store or CaseStore()
        self.sessions = sessions or SessionRepository(self.store)

    def list_cases(self) -> list[CaseSummary]:
        root = self.store.workspace_root
        if not root.exists():
            return []
        rows: list[CaseSummary] = []
        for path in root.iterdir():
            if not path.is_dir():
                continue
            state_path = path / "case_state.json"
            if not state_path.exists():
                continue
            try:
                case_id = self.store.validate_case_id(path.name)
            except ValueError:
                continue
            data = _read_json(state_path)
            if isinstance(data, dict):
                rows.append(case_summary_from_data(case_id, data, _mtime_iso(state_path)))
        return sorted(rows, key=lambda item: item.updated_at, reverse=True)

    def load_case(self, case_id: str) -> CaseState:
        return self.store.load(case_id)

    def conversation(self, case_id: str, limit: int = 150) -> list[dict[str, Any]]:
        return self.sessions.get_conversation_items(case_id, limit=limit)

    def runs(self, case_id: str) -> list[RunSummary]:
        root = self.store.ensure_case_dirs(case_id)
        trace_root = root / "traces"
        if not trace_root.exists():
            return []
        rows = [_run_summary(path) for path in trace_root.glob("run_*.json") if path.is_file()]
        return sorted([row for row in rows if row], key=lambda item: item.started_at or item.updated_at, reverse=True)

    def run(self, case_id: str, run_id: str) -> RunSummary:
        run_id = validate_trace_id(run_id)
        path = self.store.resolve_case_path(case_id, f"traces/{run_id}.json")
        summary = _run_summary(path)
        if not summary:
            raise FileNotFoundError(run_id)
        return summary

    def run_events(self, case_id: str, run_id: str, limit: int = 1000, after_seq: int = 0) -> list[TraceEvent]:
        run_id = validate_trace_id(run_id)
        path = self.store.resolve_case_path(case_id, f"traces/{run_id}/events.jsonl")
        rows = [row for row in _read_jsonl(path) if _int_value(row.get("seq", row.get("run_seq", 0))) > after_seq]
        return normalize_trace_events(rows[-limit:])

    def case_events(self, case_id: str, limit: int = 1000, after_case_seq: int = 0) -> list[TraceEvent]:
        path = self.store.resolve_case_path(case_id, "traces/events.jsonl")
        rows = [row for row in _read_jsonl(path) if _int_value(row.get("case_seq", 0)) > after_case_seq]
        return normalize_trace_events(rows[-limit:])

    def artifacts(self, case_id: str) -> list[dict[str, Any]]:
        root = self.store.ensure_case_dirs(case_id)
        paths: list[Path] = []
        for relative_root in ("reports", "traces/artifacts", "evidence/previews", "evidence/crops"):
            folder = root / relative_root
            if folder.exists():
                paths.extend(path for path in folder.rglob("*") if path.is_file())
        sorted_paths = sorted(paths, key=lambda item: (_artifact_sort_key(item.relative_to(root).as_posix()), -item.stat().st_mtime))
        return [self.case_file_metadata(case_id, path.relative_to(root).as_posix(), include_absolute=False) for path in sorted_paths]

    def case_file_metadata(self, case_id: str, relative_path: str, *, include_absolute: bool = True) -> dict[str, Any]:
        case_id = self.store.validate_case_id(case_id)
        normalized = normalize_generated_file_path(relative_path)
        path = self.store.resolve_case_path(case_id, normalized)
        if not path.exists():
            raise FileNotFoundError(normalized)
        if not path.is_file():
            raise IsADirectoryError(normalized)
        stat = path.stat()
        metadata = _generated_file_metadata(normalized, path, case_id=case_id)
        metadata.update(
            {
                "case_id": case_id,
                "path": normalized,
                "updated_at": _mtime_iso(path),
                "bytes": stat.st_size,
                "content_type": _content_type(path),
                "open_url": _case_file_url(case_id, normalized, download=False),
                "download_url": _case_file_url(case_id, normalized, download=True),
                "generated": True,
            }
        )
        if include_absolute:
            metadata["absolute_path"] = str(path)
        return metadata


def case_summary_from_state(store: CaseStore, state: CaseState) -> CaseSummary:
    path = store.resolve_case_path(state.case_id, "case_state.json")
    return case_summary_from_data(state.case_id, state.model_dump(mode="json"), _mtime_iso(path))


def validate_trace_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not value or not SAFE_TRACE_ID.match(value) or ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"Unsafe run_id: {run_id}")
    return value


def normalize_generated_file_path(relative_path: str) -> str:
    value = str(relative_path or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise ValueError("path is required")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value) or "\x00" in value:
        raise ValueError("absolute paths are not allowed")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("unsafe path segment")
    if not is_generated_file_path(value):
        raise ValueError("path is not an allowed generated file")
    return value


def is_generated_file_path(relative_path: str) -> bool:
    value = str(relative_path or "").replace("\\", "/")
    return any(value.startswith(prefix) for prefix in GENERATED_FILE_PREFIXES)


def case_summary_from_data(case_id: str, data: dict[str, Any], updated_at: str) -> CaseSummary:
    requirements = [item for item in data.get("requirements") or [] if isinstance(item, dict)]
    evidence_items = [item for item in data.get("evidence_items") or [] if isinstance(item, dict)]
    required = [item for item in requirements if item.get("required", True)]
    return CaseSummary(
        case_id=case_id,
        status=str(data.get("status") or "new"),
        summary=str(data.get("summary") or ""),
        updated_at=updated_at,
        requirement_count=len(requirements),
        required_count=len(required),
        ready_required_count=sum(1 for item in required if item.get("status") in READY_STATUSES),
        evidence_count=len(evidence_items),
        missing_count=len(data.get("missing_materials") or []),
        weak_count=len(data.get("weak_materials") or []),
        conflict_count=len(data.get("conflict_materials") or []),
        satisfied_count=len(data.get("satisfied_materials") or []),
    )


def compute_requirement_progress(requirements: list[Requirement]) -> RequirementProgress:
    required = [item for item in requirements if item.required is not False]
    total = len(required)
    if not total:
        return RequirementProgress(percent=0, ready=0, total=0)
    weighted = sum(REQUIREMENT_WEIGHTS.get(str(item.status), 0.0) for item in required)
    ready = sum(1 for item in required if item.status in READY_STATUSES)
    return RequirementProgress(percent=round((weighted / total) * 100), ready=ready, total=total)


def normalize_trace_events(rows: list[dict[str, Any]]) -> list[TraceEvent]:
    by_id: dict[str, TraceEvent] = {}
    for row in rows:
        event = normalize_trace_event(row)
        current = by_id.get(event.event_id)
        if current is None or (event.case_seq, event.seq) >= (current.case_seq, current.seq):
            by_id[event.event_id] = event
    return sorted(by_id.values(), key=lambda item: (item.case_seq, item.seq))


def normalize_trace_event(raw: dict[str, Any]) -> TraceEvent:
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    raw_kind = str(raw.get("kind") or "observation")
    kind = _event_kind(raw_kind, payload)
    status = _event_status(raw_kind, payload)
    seq = _int_value(raw.get("seq", raw.get("run_seq", 0)))
    case_seq = _int_value(raw.get("case_seq", 0))
    event_id = _event_id(raw, raw_kind, seq, case_seq)
    return TraceEvent(
        event_id=event_id,
        run_id=str(raw.get("run_id") or ""),
        case_id=str(raw.get("case_id") or ""),
        seq=seq,
        case_seq=case_seq,
        ts=str(raw.get("ts") or ""),
        kind=kind,
        raw_kind=raw_kind,
        name=str(raw.get("name") or raw_kind),
        status=status,
        summary=_event_summary(raw_kind, raw, payload),
        parent_event_id=str(raw.get("parent_event_id") or ""),
        caused_by_event_id=str(raw.get("caused_by_event_id") or ""),
        duration_ms=trace_duration_ms(payload, raw),
        token_count=trace_token_count(payload, raw),
        input_preview=_payload_preview(payload.get("input") or payload.get("request")),
        output_preview=_payload_preview(
            payload.get("output")
            or payload.get("result")
            or payload.get("final_answer")
            or payload.get("reasoning_excerpt")
        ),
        payload=payload,
        raw=raw,
    )


def status_label(status: str) -> str:
    return title_case(status or "new")


def title_case(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value or "").replace("-", "_").split("_") if part)


def json_preview(value: Any, max_chars: int = 1400) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 16].rstrip() + "\n...[truncated]"


def _generated_file_metadata(relative_path: str, path: Path, *, case_id: str) -> dict[str, Any]:
    data: Any = {}
    if relative_path.startswith("traces/artifacts/") and path.suffix.lower() == ".json":
        data = _read_json(path)
    return {
        "name": str(data.get("name") if isinstance(data, dict) and data.get("name") else path.name),
        "type": _generated_file_type(relative_path, path, data),
        "run_id": str(data.get("run_id") if isinstance(data, dict) and data.get("run_id") else ""),
    }


def _generated_file_type(relative_path: str, path: Path, data: Any) -> str:
    if isinstance(data, dict) and data.get("artifact_type"):
        return str(data["artifact_type"])
    if relative_path.startswith("reports/"):
        return "report"
    if relative_path.startswith("traces/artifacts/"):
        return "trace_artifact"
    if relative_path.startswith("evidence/previews/"):
        return "evidence_preview"
    if relative_path.startswith("evidence/crops/"):
        return "evidence_crop"
    return path.suffix.lower().lstrip(".") or "file"


def _content_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    if path.suffix.lower() == ".md":
        return "text/markdown"
    return "application/octet-stream"


def _case_file_url(case_id: str, relative_path: str, *, download: bool) -> str:
    return (
        f"/api/cases/{quote(case_id, safe='')}/files/content"
        f"?path={quote(relative_path, safe='')}&download={1 if download else 0}"
    )


def _artifact_sort_key(relative_path: str) -> tuple[int, str]:
    prefix = next((prefix.rstrip("/") for prefix in GENERATED_FILE_PREFIXES if relative_path.startswith(prefix)), "")
    return (GENERATED_FILE_ORDER.get(prefix, 99), relative_path.lower())


def _run_summary(path: Path) -> RunSummary | None:
    trace = _read_json(path)
    if not isinstance(trace, dict):
        return None
    events_path = path.with_suffix("") / "events.jsonl"
    display_events = normalize_trace_events(_read_jsonl(events_path))
    phase = str(trace.get("phase") or "")
    status = "completed" if trace.get("completed_at") else ("waiting_approval" if phase == "waiting_approval" else "running")
    return RunSummary(
        run_id=str(trace.get("run_id") or path.stem),
        status=status,
        started_at=str(trace.get("started_at") or ""),
        completed_at=str(trace.get("completed_at") or ""),
        updated_at=_mtime_iso(path),
        duration_ms=_duration_ms(trace.get("started_at"), trace.get("completed_at")),
        phase=phase,
        tool_count=len(trace.get("tool_calls") or []),
        role_count=len(trace.get("role_calls") or []),
        model_count=len(trace.get("model_calls") or []),
        checkpoint_count=len(trace.get("trace_checkpoints") or trace.get("checkpoints") or []),
        error_count=_trace_error_count(trace),
        event_count=len(display_events),
        current_goal=str(trace.get("current_goal") or ""),
        final_answer=str(trace.get("final_answer") or ""),
    )


def _event_kind(raw_kind: str, payload: dict[str, Any]) -> str:
    if payload.get("error") or payload.get("schema_validation_error"):
        return "error"
    if raw_kind == "model_call" and str(payload.get("role") or "").lower() == "summarizer":
        return "artifact_summary"
    return {
        "planner_action": "planner",
        "supervisor_decision": "planner",
        "model_thinking": "thinking",
        "model_call": "model",
        "role_call": "role",
        "tool_call": "tool",
        "approval_interrupt": "approval",
        "approval_decision": "approval",
        "observation": "observation",
        "checkpoint": "checkpoint",
        "final_answer": "artifact",
        "artifact": "artifact",
        "error": "error",
    }.get(raw_kind, "observation")


def _event_id(raw: dict[str, Any], raw_kind: str, seq: int, case_seq: int) -> str:
    if raw_kind == "model_thinking":
        role = str((raw.get("payload") or {}).get("role") or raw.get("name") or "model")
        parent = str(raw.get("parent_event_id") or raw.get("caused_by_event_id") or "")
        step = str(raw.get("step_count") or 0)
        group = parent or f"root_step_{step}"
        return f"{raw.get('run_id') or 'run'}:thinking:{_slug(role)}:{_slug(group)}"
    return str(raw.get("event_id") or f"{raw.get('run_id') or 'run'}:{seq or case_seq}")


def _event_summary(raw_kind: str, raw: dict[str, Any], payload: dict[str, Any]) -> str:
    if raw_kind == "model_call" and str(payload.get("role") or "").lower() == "summarizer":
        return "Attachment artifact summary completed"
    if raw_kind == "model_thinking":
        role = title_case(str(payload.get("role") or raw.get("name") or "model"))
        chars = _optional_int(payload.get("reasoning_chars")) or 0
        status = str(payload.get("status") or "streaming")
        label = "completed" if status == "completed" else "streaming"
        return f"{role} thinking {label}; {chars} chars"
    return str(raw.get("summary") or raw.get("payload_preview") or "")


def _event_status(raw_kind: str, payload: dict[str, Any]) -> str:
    if payload.get("error") or payload.get("schema_validation_error"):
        return "error"
    if raw_kind == "approval_interrupt":
        return "waiting"
    if raw_kind == "approval_decision":
        return "approved" if payload.get("approved") else "rejected"
    if raw_kind == "checkpoint":
        return "saved"
    return "ok"


def _trace_error_count(trace: dict[str, Any]) -> int:
    sources = list(trace.get("tool_calls") or []) + list(trace.get("role_calls") or []) + list(trace.get("model_calls") or [])
    errors = sum(1 for item in sources if isinstance(item, dict) and item.get("error"))
    errors += sum(1 for item in trace.get("observations") or [] if isinstance(item, dict) and item.get("error"))
    return errors


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    except (OSError, json.JSONDecodeError):
        return rows
    return rows


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _duration_ms(started_at: Any, completed_at: Any) -> int | None:
    start = _parse_dt(started_at)
    end = _parse_dt(completed_at)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload_preview(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value[:500]
    return json_preview(value, max_chars=500)


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return text[:96] or "root"
