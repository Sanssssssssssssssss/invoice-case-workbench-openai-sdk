from __future__ import annotations

import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from app.evals.business.models import (
    BusinessEvalCase,
    EvalResult,
    EvalSnapshot,
    ReportArtifact,
    validate_case_bundle,
    validate_case_input,
)
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, Attachment


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CASES_ROOT = REPO_ROOT / "evals" / "business_v1" / "cases"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output" / "evals"
MAX_APPROVALS = 2


@dataclass(frozen=True)
class BusinessEvalPaths:
    run_dir: Path
    snapshot: Path
    score: Path
    report: Path


RuntimeFactory = Callable[[CaseStore], Any]


async def run_business_eval(
    case_dir: Path,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    runtime_factory: RuntimeFactory | None = None,
) -> BusinessEvalPaths:
    """Run exactly one business case through the production streaming boundary.

    Only ``case.json`` and its hash-checked attachments are loaded before the
    Agent finishes. ``oracle.json`` is first loaded by ``score_snapshot`` after
    ``snapshot.json`` is already on disk.
    """

    case_dir = case_dir.resolve()
    case = validate_case_input(case_dir)
    stamp = _timestamp_id()
    run_dir = (output_root / stamp).resolve()
    workspace_root = run_dir / "w"
    storage_root = run_dir / "s"
    workspace_root.mkdir(parents=True, exist_ok=False)
    storage_root.mkdir(parents=True, exist_ok=False)

    started_at = _utc_now()
    run_id = f"run_eval_{stamp}"
    approved_tools: list[str] = []
    stream_events: list[dict[str, Any]] = []
    runtime_error = ""
    response: Any | None = None
    runtime: Any | None = None

    with _isolated_runtime_settings(workspace_root, storage_root):
        store = CaseStore(workspace_root)
        request = AgentTurnRequest(
            case_id=case.case_id,
            message=case.user_message,
            attachments=_copy_attachments(case_dir, case, store),
        )
        try:
            runtime = runtime_factory(store) if runtime_factory else _production_runtime(store)
            response = await runtime.run_turn_streamed(
                request,
                run_id=run_id,
                event_sink=_event_collector(stream_events),
            )
            response, approval_error = await _resolve_allowed_approvals(
                runtime,
                case,
                response,
                run_id=run_id,
                approved_tools=approved_tools,
                stream_events=stream_events,
            )
            runtime_error = approval_error
        except Exception as exc:  # A failed Agent run must still be scoreable.
            runtime_error = f"{type(exc).__name__}: {exc}"

        case_state = store.load(case.case_id).model_dump(mode="json")
        conversation = _load_conversation(store, case.case_id)
        if not conversation:
            conversation = _fallback_conversation(case, response)
        trace = _load_trace(store, case.case_id, run_id, response)
        events = _load_events(store, case.case_id, run_id) or stream_events
        transcript_file = store.resolve_case_path(case.case_id, f"traces/{run_id}/deepseek_calls.txt")
        transcript_path = _relative_artifact_path(run_dir, transcript_file) if transcript_file.is_file() else ""
        reports = _load_reports(run_dir, store, case.case_id)
        provider, model = _provider_and_model(response, runtime)

    snapshot = EvalSnapshot(
        case_id=case.case_id,
        case_version=case.case_version,
        run_id=run_id,
        agent_commit=_agent_commit(),
        provider=provider,
        model=model,
        policy_version=case.policy_version,
        started_at=started_at,
        completed_at=_utc_now(),
        case_state=case_state,
        conversation=conversation,
        reports=reports,
        trace=trace,
        events=events,
        transcript_path=transcript_path,
        approved_tools=approved_tools,
        runtime_error=runtime_error,
    )
    snapshot_path = run_dir / "snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")

    # This is deliberately after snapshot capture: it is the first point at
    # which oracle.json may be parsed in this process.
    return score_snapshot(snapshot_path, case_dir=case_dir)


def score_snapshot(snapshot_path: Path, *, case_dir: Path | None = None) -> BusinessEvalPaths:
    """Score an immutable snapshot without invoking the Agent or a provider."""

    snapshot_path = snapshot_path.resolve()
    snapshot = EvalSnapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
    resolved_case_dir = (case_dir or DEFAULT_CASES_ROOT / snapshot.case_id).resolve()
    case, oracle = validate_case_bundle(resolved_case_dir)
    if snapshot.case_id != case.case_id or snapshot.case_version != case.case_version:
        raise ValueError("snapshot case identity/version does not match case.json")

    # Delayed imports make the runtime/oracle boundary obvious and keep a
    # stored snapshot independently re-scoreable as the scorer evolves.
    from app.evals.business.report import render_eval_report
    from app.evals.business.scorer import score_business_eval

    result: EvalResult = score_business_eval(case, oracle, snapshot)
    report_text = render_eval_report(case, oracle, snapshot, result)
    run_dir = snapshot_path.parent
    score_path = run_dir / "score.json"
    report_path = run_dir / "eval_report.md"
    score_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    report_path.write_text(report_text.rstrip() + "\n", encoding="utf-8")
    return BusinessEvalPaths(
        run_dir=run_dir,
        snapshot=snapshot_path,
        score=score_path,
        report=report_path,
    )


async def _resolve_allowed_approvals(
    runtime: Any,
    case: BusinessEvalCase,
    response: Any,
    *,
    run_id: str,
    approved_tools: list[str],
    stream_events: list[dict[str, Any]],
) -> tuple[Any, str]:
    allowed = set(case.auto_approve_tools)
    approval_count = 0
    while _is_waiting_approval(response):
        tools = _approval_tools(response)
        if not tools:
            return response, "ApprovalError: waiting approval response did not identify a tool"
        forbidden = [tool for tool in tools if tool not in allowed]
        if forbidden:
            return response, f"ApprovalDenied: tool is not auto-approved: {', '.join(forbidden)}"
        if approval_count + len(tools) > MAX_APPROVALS:
            return response, f"ApprovalLimitExceeded: more than {MAX_APPROVALS} approvals requested"
        approved_tools.extend(tools)
        approval_count += len(tools)
        response = await runtime.resume_approval_streamed(
            case.case_id,
            run_id,
            approved=True,
            reason="Business Eval 仅自动批准案例声明的本地报告写入。",
            event_sink=_event_collector(stream_events),
        )
    return response, ""


def _approval_tools(response: Any) -> list[str]:
    trace = getattr(response, "trace", None)
    if not isinstance(trace, dict):
        return []
    tools: list[str] = []
    for item in trace.get("interrupts") or []:
        if not isinstance(item, dict):
            continue
        policy_check = item.get("policy_check") if isinstance(item.get("policy_check"), dict) else {}
        approval_payload = (
            policy_check.get("approval_payload")
            if isinstance(policy_check.get("approval_payload"), dict)
            else {}
        )
        tool = str(item.get("tool") or item.get("name") or approval_payload.get("tool") or "").strip()
        if tool and tool not in tools:
            tools.append(tool)
    return tools


def _is_waiting_approval(response: Any) -> bool:
    trace = getattr(response, "trace", None)
    if not isinstance(trace, dict):
        return False
    return str(trace.get("status") or "") == "waiting_approval" or bool(trace.get("interrupts"))


def _copy_attachments(
    case_dir: Path,
    case: BusinessEvalCase,
    store: CaseStore,
) -> list[Attachment]:
    target_root = store.ensure_case_dirs(case.case_id) / "attachments"
    request_attachments: list[Attachment] = []
    used_names: set[str] = set()
    for item in case.attachments:
        source = (case_dir / item.path).resolve()
        name = source.name
        if name in used_names:
            name = f"{item.id}_{name}"
        used_names.add(name)
        destination = target_root / name
        shutil.copy2(source, destination)
        request_attachments.append(
            Attachment(
                name=name,
                path=f"attachments/{name}",
                content_type=item.content_type,
            )
        )
    return request_attachments


def _load_conversation(store: CaseStore, case_id: str) -> list[dict[str, Any]]:
    from app.state.session_repository import SessionRepository

    try:
        return SessionRepository(store).get_conversation_items(case_id, limit=150)
    except Exception:
        return []


def _fallback_conversation(case: BusinessEvalCase, response: Any) -> list[dict[str, Any]]:
    reply = str(getattr(response, "reply", "") or "")
    return [
        {
            "role": "user",
            "content": case.user_message,
            "attachments": [
                {"name": Path(item.path).name, "path": item.path, "content_type": item.content_type}
                for item in case.attachments
            ],
        },
        {"role": "assistant", "content": reply, "attachments": []},
    ]


def _load_trace(store: CaseStore, case_id: str, run_id: str, response: Any) -> dict[str, Any]:
    path = store.resolve_case_path(case_id, f"traces/{run_id}.json")
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("path", f"workspace/cases/{case_id}/traces/{run_id}.json")
                return value
        except (OSError, json.JSONDecodeError):
            pass
    trace = getattr(response, "trace", None)
    return dict(trace) if isinstance(trace, dict) else {}


def _load_events(store: CaseStore, case_id: str, run_id: str) -> list[dict[str, Any]]:
    path = store.resolve_case_path(case_id, f"traces/{run_id}/events.jsonl")
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _load_reports(run_dir: Path, store: CaseStore, case_id: str) -> list[ReportArtifact]:
    root = store.ensure_case_dirs(case_id) / "reports"
    reports: list[ReportArtifact] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".pdf"}:
            continue
        kind = "markdown" if path.suffix.lower() == ".md" else "pdf"
        reports.append(
            ReportArtifact(
                path=_relative_artifact_path(run_dir, path),
                kind=kind,
                bytes=path.stat().st_size,
                text=_report_text(path),
            )
        )
    return reports


def _report_text(path: Path) -> str:
    if path.suffix.lower() == ".md":
        return path.read_text(encoding="utf-8", errors="replace")
    try:
        import fitz  # type: ignore[import-not-found]

        with fitz.open(str(path)) as document:
            return "\n\n".join(str(page.get_text("text") or "").strip() for page in document).strip()
    except Exception:
        return ""


def _event_collector(target: list[dict[str, Any]]) -> Callable[..., None]:
    def emit(kind: str, payload: dict[str, Any] | None = None, *, summary: str = "") -> None:
        target.append({"kind": kind, "payload": payload or {}, "summary": summary})

    return emit


def _provider_and_model(response: Any, runtime: Any | None) -> tuple[str, str]:
    _ = response
    settings = getattr(getattr(runtime, "runner", None), "settings", None)
    provider = str(getattr(settings, "llm_provider", "") or os.getenv("LLM_PROVIDER", ""))
    model = str(getattr(settings, "llm_model", "") or os.getenv("LLM_MODEL", ""))
    return provider, model


def _production_runtime(store: CaseStore) -> Any:
    from app.runtime.turn_runner import AgentRuntime

    return AgentRuntime(store=store)


def _relative_artifact_path(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _agent_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


@contextmanager
def _isolated_runtime_settings(workspace_root: Path, storage_root: Path) -> Iterator[None]:
    from app.config import get_settings

    values = {
        "INVOICE_AGENT_WORKSPACE_ROOT": str(workspace_root),
        "INVOICE_AGENT_STORAGE_ROOT": str(storage_root),
        "INVOICE_AGENT_SESSION_DB": str(storage_root / "sessions.sqlite"),
        "INVOICE_AGENT_MEMORY_DB": str(storage_root / "memory.sqlite"),
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()
