from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.api.read_models import WorkbenchReadService, case_summary_from_state
from app.runtime.turn_runner import AgentRuntime
from app.state.case_store import CaseStore, FileBoundaryError
from app.state.schemas import timestamp_case_id


router = APIRouter(prefix="/api", tags=["workbench"])


class ApprovalResumeRequest(BaseModel):
    approved: bool
    reason: str = ""


def _service() -> WorkbenchReadService:
    return WorkbenchReadService()


@router.get("/cases")
def list_cases() -> list[dict[str, Any]]:
    return [_to_json(item) for item in _service().list_cases()]


@router.post("/cases")
def create_case() -> dict[str, Any]:
    service = _service()
    state = service.load_case(timestamp_case_id())
    return _to_json(case_summary_from_state(service.store, state))


@router.delete("/cases/{case_id}")
def archive_case(case_id: str) -> dict[str, str]:
    store = CaseStore()
    try:
        root = store.case_dir(case_id)
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not root.exists():
        raise HTTPException(status_code=404, detail="case not found")
    archive_root = store.workspace_root / ".archived_cases"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target = archive_root / f"{root.name}_{stamp}"
    shutil.move(str(root), str(target))
    return {"case_id": case_id, "archived_to": str(target)}


@router.get("/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    try:
        return _service().load_case(case_id).model_dump(mode="json")
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/conversation")
def get_conversation(case_id: str, limit: int = Query(default=150, ge=1, le=500)) -> list[dict[str, Any]]:
    try:
        return _service().conversation(case_id, limit=limit)
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/requirements")
def get_requirements(case_id: str) -> list[dict[str, Any]]:
    try:
        return [item.model_dump(mode="json") for item in _service().load_case(case_id).requirements]
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/evidence")
def get_evidence(case_id: str) -> list[dict[str, Any]]:
    try:
        return [item.model_dump(mode="json") for item in _service().load_case(case_id).evidence_items]
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/artifacts")
def get_artifacts(case_id: str) -> list[dict[str, Any]]:
    try:
        return _service().artifacts(case_id)
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/files/metadata")
def get_case_file_metadata(case_id: str, path: str = Query(..., min_length=1)) -> dict[str, Any]:
    try:
        return _service().case_file_metadata(case_id, path)
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc


@router.get("/cases/{case_id}/files/content")
def get_case_file_content(
    case_id: str,
    path: str = Query(..., min_length=1),
    download: bool = Query(default=False),
) -> FileResponse:
    try:
        metadata = _service().case_file_metadata(case_id, path)
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    return FileResponse(
        metadata["absolute_path"],
        media_type=metadata["content_type"],
        filename=metadata["name"],
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/cases/{case_id}/runs")
def get_runs(case_id: str) -> list[dict[str, Any]]:
    try:
        return [_to_json(item) for item in _service().runs(case_id)]
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/runs/{run_id}")
def get_run(case_id: str, run_id: str) -> dict[str, Any]:
    try:
        return _to_json(_service().run(case_id, run_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/runs/{run_id}/events")
def get_run_events(
    case_id: str,
    run_id: str,
    limit: int = Query(default=1000, ge=1, le=5000),
    after_seq: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    try:
        return [_to_json(item) for item in _service().run_events(case_id, run_id, limit=limit, after_seq=after_seq)]
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/cases/{case_id}/runs/{run_id}/approval")
def resume_run_approval(case_id: str, run_id: str, request: ApprovalResumeRequest) -> dict[str, Any]:
    try:
        return AgentRuntime().resume_approval(case_id, run_id, approved=request.approved, reason=request.reason).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/events")
def get_case_events(
    case_id: str,
    limit: int = Query(default=1000, ge=1, le=5000),
    after_case_seq: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    try:
        return [_to_json(item) for item in _service().case_events(case_id, limit=limit, after_case_seq=after_case_seq)]
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/runs/{run_id}/events/stream")
def stream_run_events(case_id: str, run_id: str, after_seq: int = Query(default=0, ge=0)) -> StreamingResponse:
    return StreamingResponse(
        _stream_run_events(case_id=case_id, run_id=run_id, after_seq=after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/cases/{case_id}/events/stream")
def stream_case_events(case_id: str, after_case_seq: int = Query(default=0, ge=0)) -> StreamingResponse:
    return StreamingResponse(
        _stream_case_events(case_id=case_id, after_case_seq=after_case_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_run_events(case_id: str, run_id: str, after_seq: int) -> AsyncIterator[str]:
    service = _service()
    last_seq = after_seq
    while True:
        events = service.run_events(case_id, run_id, limit=1000, after_seq=last_seq)
        for event in events:
            last_seq = max(last_seq, event.seq)
            yield sse_payload("trace_event", _to_json(event), event_id=event.event_id)
        await asyncio.sleep(0.5)


async def _stream_case_events(case_id: str, after_case_seq: int) -> AsyncIterator[str]:
    service = _service()
    last_case_seq = after_case_seq
    while True:
        events = service.case_events(case_id, limit=1000, after_case_seq=last_case_seq)
        for event in events:
            last_case_seq = max(last_case_seq, event.case_seq)
            yield sse_payload("trace_event", _to_json(event), event_id=event.event_id)
        await asyncio.sleep(0.5)


def sse_payload(event: str, data: Any, *, event_id: str = "") -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    encoded = json.dumps(data, ensure_ascii=False, default=str)
    for line in encoded.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _to_json(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value
