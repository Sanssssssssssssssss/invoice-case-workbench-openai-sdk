from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi import File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent_runs import router as agent_runs_router
from app.api.live_status import router as live_status_router
from app.api.workbench import router as workbench_router
from app.runtime.agents_sdk import close_shared_openai_clients, enable_shared_openai_clients
from app.runtime.turn_runner import AgentRuntime
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, AgentTurnResponse


@asynccontextmanager
async def lifespan(_app: FastAPI):
    enable_shared_openai_clients()
    try:
        yield
    finally:
        await close_shared_openai_clients()


app = FastAPI(title="Invoice Case Workbench Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(agent_runs_router)
app.include_router(workbench_router)
app.include_router(live_status_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/agent/turn", response_model=AgentTurnResponse)
def agent_turn(request: AgentTurnRequest) -> AgentTurnResponse:
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    try:
        return AgentRuntime().run_turn(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cases/{case_id}/attachments")
async def upload_attachment(case_id: str, file: UploadFile = File(...)) -> dict[str, object]:
    store = CaseStore()
    safe_case_id = store.validate_case_id(case_id)
    original_name = Path(file.filename or "attachment.txt").name
    if not original_name or original_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="attachment filename is required")
    target_name = f"{uuid4().hex[:10]}_{original_name}"
    target = store.resolve_case_path(safe_case_id, f"attachments/{target_name}")
    content = await file.read()
    target.write_bytes(content)
    return {
        "case_id": safe_case_id,
        "name": original_name,
        "path": str(target),
        "relative_path": f"attachments/{target_name}",
        "content_type": file.content_type or "",
        "bytes": len(content),
    }
