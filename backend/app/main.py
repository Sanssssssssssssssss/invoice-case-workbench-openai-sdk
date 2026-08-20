from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi import File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent_runs import router as agent_runs_router
from app.api.live_status import router as live_status_router
from app.api.workbench import router as workbench_router
from app.state.case_store import CaseStore, FileBoundaryError


app = FastAPI(title="Invoice Case Workbench Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "null",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(agent_runs_router)
app.include_router(workbench_router)
app.include_router(live_status_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


@app.post("/api/cases/{case_id}/attachments")
async def upload_attachment(case_id: str, file: UploadFile = File(...)) -> dict[str, object]:
    store = CaseStore()
    try:
        safe_case_id = store.validate_case_id(case_id)
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    original_name = Path(file.filename or "attachment.txt").name
    if not original_name or original_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="attachment filename is required")
    target_name = f"{uuid4().hex[:10]}_{original_name}"
    target = store.resolve_case_path(safe_case_id, f"attachments/{target_name}")
    partial = target.with_name(f".{target.name}.uploading")
    total = 0
    try:
        with partial.open("xb") as handle:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="attachment exceeds 50 MiB limit")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "case_id": safe_case_id,
        "name": original_name,
        "path": str(target),
        "relative_path": f"attachments/{target_name}",
        "content_type": file.content_type or "",
        "bytes": total,
    }
