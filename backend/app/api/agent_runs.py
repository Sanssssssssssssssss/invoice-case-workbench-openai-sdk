from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.workbench import sse_payload
from app.runtime.streaming import TERMINAL_KINDS, stream_hub
from app.runtime.turn_runner import AgentRuntime
from app.state.case_store import FileBoundaryError
from app.state.schemas import AgentTurnRequest


router = APIRouter(prefix="/api", tags=["agent-runs"])


class AgentRunAccepted(BaseModel):
    case_id: str
    run_id: str
    status: str
    stream_url: str


class StreamingApprovalRequest(BaseModel):
    case_id: str = ""
    approved: bool
    reason: str = ""


@router.post("/agent/runs", response_model=AgentRunAccepted)
async def start_agent_run(request: AgentTurnRequest) -> AgentRunAccepted:
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    runtime = AgentRuntime()
    try:
        case_id = runtime.runner.store.validate_case_id(request.case_id)
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run_id = runtime.runner.harness.new_run_id()
    stream_hub.create(run_id=run_id, case_id=case_id)
    asyncio.create_task(_execute_turn(runtime, request, run_id))
    return AgentRunAccepted(
        case_id=case_id,
        run_id=run_id,
        status="accepted",
        stream_url=f"/api/agent/runs/{run_id}/stream",
    )


@router.get("/agent/runs/{run_id}/stream")
def stream_agent_run(run_id: str, after_seq: int = Query(default=0, ge=0)) -> StreamingResponse:
    try:
        stream_hub.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return StreamingResponse(
        _stream_run(run_id=run_id, after_seq=after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/agent/runs/{run_id}/approval", response_model=AgentRunAccepted)
async def resume_agent_run_approval(run_id: str, request: StreamingApprovalRequest) -> AgentRunAccepted:
    try:
        record = stream_hub.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    case_id = request.case_id or record.case_id
    runtime = AgentRuntime()
    asyncio.create_task(_execute_approval(runtime, case_id, run_id, request.approved, request.reason))
    return AgentRunAccepted(
        case_id=case_id,
        run_id=run_id,
        status="accepted",
        stream_url=f"/api/agent/runs/{run_id}/stream",
    )


async def _execute_turn(runtime: AgentRuntime, request: AgentTurnRequest, run_id: str) -> None:
    def emit(kind: str, payload: dict[str, Any] | None = None, *, summary: str = "") -> None:
        stream_hub.emit(run_id, kind, payload or {}, summary=summary)

    try:
        response = await runtime.run_turn_streamed(request, run_id=run_id, event_sink=emit)
        if str(response.trace.get("status") or "") == "waiting_approval" or response.trace.get("interrupts"):
            return
        stream_hub.emit(
            run_id,
            "final",
            {"response": response.model_dump(mode="json")},
            summary="Run completed.",
        )
    except Exception as exc:
        stream_hub.emit(
            run_id,
            "error",
            {"type": type(exc).__name__, "message": str(exc)},
            summary="Run failed.",
        )


async def _execute_approval(runtime: AgentRuntime, case_id: str, run_id: str, approved: bool, reason: str) -> None:
    def emit(kind: str, payload: dict[str, Any] | None = None, *, summary: str = "") -> None:
        stream_hub.emit(run_id, kind, payload or {}, summary=summary)

    try:
        response = await runtime.resume_approval_streamed(
            case_id,
            run_id,
            approved=approved,
            reason=reason,
            event_sink=emit,
        )
        if str(response.trace.get("status") or "") == "waiting_approval" or response.trace.get("interrupts"):
            return
        stream_hub.emit(
            run_id,
            "final",
            {"response": response.model_dump(mode="json")},
            summary="Run completed after approval.",
        )
    except Exception as exc:
        stream_hub.emit(
            run_id,
            "error",
            {"type": type(exc).__name__, "message": str(exc)},
            summary="Approval resume failed.",
        )


async def _stream_run(run_id: str, after_seq: int) -> AsyncIterator[str]:
    last_seq = after_seq
    while True:
        events = stream_hub.events_after(run_id, after_seq=last_seq)
        for event in events:
            last_seq = max(last_seq, event.seq)
            yield sse_payload(event.kind, asdict(event), event_id=event.event_id)
        record = stream_hub.get(run_id)
        if record.status in TERMINAL_KINDS | {"completed", "error"} and not stream_hub.events_after(run_id, after_seq=last_seq):
            break
        await asyncio.sleep(0.1)
