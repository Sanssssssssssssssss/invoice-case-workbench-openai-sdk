from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.read_models import TraceEvent, WorkbenchReadService
from app.api.workbench import sse_payload
from app.state.case_store import FileBoundaryError


router = APIRouter(prefix="/api", tags=["live-status"])

MAX_THINKING_CHARS = 8000


class LiveStatus(BaseModel):
    runId: str = ""
    phase: str = ""
    activeAgent: str = ""
    activeRole: str = ""
    currentStep: int = 0
    latestSummary: str = ""
    latestThinking: str = ""
    latestEventId: str = ""
    isRunning: bool = False
    thinkingSource: str = ""
    reasoningChars: int = 0
    reasoningChunks: int = 0
    runStartedAt: str = ""
    elapsedMs: int = 0
    activeStep: str = ""
    latestThoughtSummary: str = ""
    updatedAt: str = ""


@router.get("/cases/{case_id}/live-status")
def get_live_status(case_id: str) -> dict[str, Any]:
    try:
        return build_live_status(case_id).model_dump()
    except (FileBoundaryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases/{case_id}/live-status/stream")
def stream_live_status(case_id: str, after_case_seq: int = Query(default=0, ge=0)) -> StreamingResponse:
    return StreamingResponse(
        _stream_live_status(case_id=case_id, after_case_seq=after_case_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def build_live_status(case_id: str) -> LiveStatus:
    service = WorkbenchReadService()
    events = service.case_events(case_id, limit=1200)
    if not events:
        return LiveStatus(activeAgent="等待中", latestSummary="还没有运行记录。")
    latest = events[-1]
    latest_run_id = latest.run_id
    runs = service.runs(case_id)
    run = next((item for item in runs if item.run_id == latest_run_id), runs[0] if runs else None)
    thinking_event = _latest_thinking_event(events, latest_run_id)
    thinking = _thinking_from_event(thinking_event) if thinking_event else _thinking_fallback(events, latest_run_id)
    active_agent, active_role = _active_agent(latest)
    is_running = latest.raw_kind != "final_answer" and (run is None or run.status == "running")
    if latest.raw_kind == "final_answer":
        is_running = False
    run_started_at = str(getattr(run, "started_at", "") or latest.ts)
    elapsed_ms = _elapsed_ms(run_started_at, str(getattr(run, "completed_at", "") or ""), is_running)
    active_step = _active_step(latest, active_role)
    latest_thought_summary = _thought_summary(latest, thinking, active_role)
    return LiveStatus(
        runId=latest_run_id,
        phase=str(latest.raw.get("phase") or getattr(run, "phase", "") or ""),
        activeAgent=active_agent,
        activeRole=active_role,
        currentStep=_int_value(latest.raw.get("step_count") or latest.payload.get("step_count")),
        latestSummary=_summary_for(latest),
        latestThinking=thinking["text"],
        latestEventId=latest.event_id,
        isRunning=is_running,
        thinkingSource=thinking["source"],
        reasoningChars=thinking["chars"],
        reasoningChunks=thinking["chunks"],
        runStartedAt=run_started_at,
        elapsedMs=elapsed_ms,
        activeStep=active_step,
        latestThoughtSummary=latest_thought_summary,
        updatedAt=latest.ts,
    )


async def _stream_live_status(case_id: str, after_case_seq: int) -> AsyncIterator[str]:
    last_event_id = ""
    last_thinking = ""
    last_case_seq = after_case_seq
    while True:
        service = WorkbenchReadService()
        events = service.case_events(case_id, limit=1, after_case_seq=last_case_seq)
        if events:
            last_case_seq = max(last_case_seq, max(event.case_seq for event in events))
        status = build_live_status(case_id)
        changed = status.latestEventId != last_event_id or status.latestThinking != last_thinking
        if changed:
            last_event_id = status.latestEventId
            last_thinking = status.latestThinking
            yield sse_payload("live_status", status.model_dump(), event_id=status.latestEventId)
        await asyncio.sleep(0.45)


def _latest_thinking_event(events: list[TraceEvent], run_id: str) -> TraceEvent | None:
    for event in reversed(events):
        if run_id and event.run_id != run_id:
            continue
        if event.raw_kind == "model_thinking":
            return event
        if event.raw_kind == "model_call" and event.payload.get("reasoning_excerpt"):
            return event
    return None


def _thinking_from_event(event: TraceEvent) -> dict[str, Any]:
    role = str(event.payload.get("role") or event.name or "model")
    text = str(event.payload.get("reasoning_excerpt") or "").strip()
    return {
        "text": _clip(text),
        "source": "reasoning_content" if text else "",
        "chars": _int_value(event.payload.get("reasoning_chars")),
        "chunks": _int_value(event.payload.get("reasoning_chunks")),
    }


def _thinking_fallback(events: list[TraceEvent], run_id: str) -> dict[str, Any]:
    _ = events, run_id
    return {"text": "", "source": "", "chars": 0, "chunks": 0}


def _thought_summary(event: TraceEvent, thinking: dict[str, Any], active_role: str) -> str:
    if event.raw_kind in {"model_thinking", "model_call"}:
        role = str(event.payload.get("role") or active_role or event.name or "model")
        return _role_thought_summary(role, event)
    text = str(thinking.get("text") or "").strip()
    if text:
        return text
    if event.raw_kind == "model_call":
        return f"{_role_label(active_role or event.name)}刚完成模型调用，正在进入下一步。"
    if event.raw_kind == "role_call":
        return f"{_role_label(active_role or event.name)}已返回结构化结果，Supervisor 正在复盘下一步。"
    if event.raw_kind == "tool_call":
        return f"工具 {event.name} 已返回结果，Supervisor 正在检查是否需要继续。"
    return _summary_for(event)


def _role_thought_summary(role: str, event: TraceEvent) -> str:
    chars = _int_value(event.payload.get("reasoning_chars"))
    label = _role_label(role)
    if role == "planner":
        return f"{label}正在阅读案卷状态、最近 observation 和 PolicyGate 反馈，判断下一步。"
    if role == "materials_advisor":
        return f"{label}正在结合 case_state、附件摘要和 RAG 规则整理补料建议。"
    if role == "evidence_reviewer":
        return f"{label}正在按 Supervisor 任务核对附件、抽取字段、检查证据链。"
    if role == "case_patch_writer":
        return f"{label}正在把 reviewer 结果整理为可写入的 case patch。"
    if role == "report_writer":
        return f"{label}正在组织报告结构、风险摘要和证据引用。"
    if role == "summarizer":
        return f"附件摘要器正在压缩 artifact 供后续引用，不是 Supervisor 的下一步决策。"
    if role == "session_compactor":
        return f"{label}正在压缩长会话记忆，保留 refs 和当前任务。"
    return f"{label}正在处理当前步骤。{f' 已接收 {chars} 个 reasoning 字符。' if chars else ''}"


def _active_agent(event: TraceEvent) -> tuple[str, str]:
    if event.raw_kind == "model_thinking":
        role = str(event.payload.get("role") or event.name or "model")
        return f"{_role_label(role)}正在思考", role
    if event.raw_kind == "model_call":
        role = str(event.payload.get("role") or event.name or "model")
        if role == "summarizer":
            return "附件摘要已更新", "artifact_summary"
        return f"{_role_label(role)}刚完成模型调用", role
    if event.raw_kind in {"planner_action", "supervisor_decision"}:
        target = str(event.payload.get("role") or event.payload.get("tool") or event.name or "")
        return f"规划器正在安排{_role_label(target) if target else '下一步'}", target
    if event.raw_kind == "role_call":
        role = str(event.payload.get("role") or event.name or "")
        return f"{_role_label(role)}正在执行", role
    if event.raw_kind == "tool_call":
        tool = str(event.payload.get("tool") or event.name or "")
        return f"工具正在执行：{tool}", tool
    if event.raw_kind == "checkpoint":
        return "正在保存检查点", "checkpoint"
    if event.raw_kind == "final_answer":
        return "回复已生成", "assistant"
    return _role_label(event.name or event.raw_kind), event.name


def _summary_for(event: TraceEvent) -> str:
    if event.raw_kind == "model_thinking":
        role = str(event.payload.get("role") or event.name or "model")
        chars = _int_value(event.payload.get("reasoning_chars"))
        return f"{_role_label(role)}正在推理，已接收 {chars} 个字符。"
    if event.summary:
        return event.summary
    return event.raw_kind


def _active_step(event: TraceEvent, active_role: str) -> str:
    if event.raw_kind == "model_thinking":
        return _role_thought_summary(active_role or event.name, event)
    if event.raw_kind == "model_call" and str(event.payload.get("role") or event.name) == "summarizer":
        return "正在整理附件摘要"
    if event.raw_kind == "supervisor_decision":
        action = str(event.payload.get("action") or event.name or "")
        target = str(event.payload.get("target") or "")
        return f"Supervisor 决策：{action}{f' -> {target}' if target else ''}"
    if event.raw_kind == "policy_check":
        return "PolicyGate 正在检查动作是否合法"
    if event.raw_kind == "role_call":
        return f"{_role_label(active_role or event.name)}正在返回结构化结果"
    if event.raw_kind == "tool_call":
        return f"工具 {event.name} 正在执行"
    if event.raw_kind == "checkpoint":
        return "正在保存检查点"
    return _summary_for(event)


def _role_label(role: str) -> str:
    return {
        "planner": "规划器",
        "materials_advisor": "材料顾问",
        "evidence_reviewer": "证据审核员",
        "case_patch_writer": "案件更新员",
        "report_writer": "报告撰写员",
        "summarizer": "摘要器",
        "session_compactor": "上下文整理器",
        "artifact_summary": "附件摘要",
        "model": "模型",
    }.get(str(role or ""), str(role or "模型"))


def _clip(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_THINKING_CHARS:
        return text
    return text[: MAX_THINKING_CHARS - 18].rstrip() + "\n...[thinking trimmed]"


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _elapsed_ms(started_at: str, completed_at: str, is_running: bool) -> int:
    start = _parse_dt(started_at)
    if not start:
        return 0
    end = datetime.now(timezone.utc) if is_running else (_parse_dt(completed_at) or datetime.now(timezone.utc))
    return max(0, int((end - start).total_seconds() * 1000))


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
