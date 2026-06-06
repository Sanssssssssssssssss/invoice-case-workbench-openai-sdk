from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.harness import HarnessRuntime
from app.runtime.streaming import stream_hub
from app.state.case_store import CaseStore
from app.state.schemas import AgentTurnRequest, AgentTurnResponse, CaseState


DEFAULT_SCENARIOS = ["simple", "material_advice", "small_attachment", "multi_attachment", "case_patch", "report_approval"]
DEFAULT_VARIANTS = ["blocking", "streaming"]


@dataclass
class BenchRow:
    scenario: str
    variant: str
    iteration: int
    first_sse_ms: float | None
    first_delta_ms: float | None
    final_ms: float
    approval_ms: float | None
    event_count: int
    error: str = ""


def main() -> None:
    args = _parse_args()
    if args.mode != "fake":
        raise SystemExit("Only --mode fake is supported for default infra benchmark runs.")
    scenarios = _split_arg(args.scenarios, DEFAULT_SCENARIOS)
    variants = _split_arg(args.variants, DEFAULT_VARIANTS)
    report_root = Path(args.report_dir or Path(__file__).resolve().parent / "reports")
    report_dir = report_root / f"infra_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    report_dir.mkdir(parents=True, exist_ok=True)

    rows = _run_fake_benchmark(scenarios=scenarios, variants=variants, iterations=args.iterations)
    payload = {
        "mode": args.mode,
        "iterations": args.iterations,
        "scenarios": scenarios,
        "variants": variants,
        "rows": [row.__dict__ for row in rows],
        "summary": _summary(rows),
    }
    (report_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "latest.md").write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"report_dir={report_dir}")


def _run_fake_benchmark(*, scenarios: list[str], variants: list[str], iterations: int) -> list[BenchRow]:
    rows: list[BenchRow] = []
    with tempfile.TemporaryDirectory(prefix="invoice_infra_bench_") as tmp:
        tmp_path = Path(tmp)
        import app.api.agent_runs as agent_runs
        import app.main as main_app

        store = CaseStore(tmp_path / "workspace" / "cases")
        fake = _FakeRuntime(store)
        original_stream_runtime = agent_runs.AgentRuntime
        original_turn_runtime = main_app.AgentRuntime
        agent_runs.AgentRuntime = lambda: fake  # type: ignore[assignment]
        main_app.AgentRuntime = lambda: fake  # type: ignore[assignment]
        try:
            with TestClient(main_app.app) as client:
                for iteration in range(1, iterations + 1):
                    for scenario in scenarios:
                        for variant in variants:
                            stream_hub.clear()
                            rows.append(_run_one(client, tmp_path, scenario=scenario, variant=variant, iteration=iteration))
        finally:
            agent_runs.AgentRuntime = original_stream_runtime  # type: ignore[assignment]
            main_app.AgentRuntime = original_turn_runtime  # type: ignore[assignment]
            get_settings.cache_clear()
    return rows


def _run_one(client: TestClient, tmp_path: Path, *, scenario: str, variant: str, iteration: int) -> BenchRow:
    case_id = f"infra_{scenario}_{iteration}_{variant}"
    message = _message_for(scenario)
    attachments = _upload_attachments(client, tmp_path, case_id, scenario)
    started = time.perf_counter()
    try:
        if variant == "blocking":
            response = client.post(
                "/api/agent/turn",
                json={"case_id": case_id, "message": message, "attachments": attachments},
            )
            response.raise_for_status()
            return BenchRow(
                scenario=scenario,
                variant=variant,
                iteration=iteration,
                first_sse_ms=None,
                first_delta_ms=None,
                final_ms=_elapsed_ms(started),
                approval_ms=None,
                event_count=0,
            )
        if variant != "streaming":
            raise ValueError(f"unknown variant: {variant}")
        accepted = client.post(
            "/api/agent/runs",
            json={"case_id": case_id, "message": message, "attachments": attachments},
        )
        accepted.raise_for_status()
        run = accepted.json()
        events = _read_stream(run["run_id"], started)
        approval_ms = None
        if scenario == "report_approval":
            approval_started = time.perf_counter()
            resumed = client.post(
                f"/api/agent/runs/{run['run_id']}/approval",
                json={"case_id": case_id, "approved": True, "reason": "infra_bench"},
            )
            resumed.raise_for_status()
            resume_events = _read_stream(run["run_id"], approval_started, after_seq=events[-1]["seq"])
            approval_ms = _elapsed_ms(approval_started)
            events.extend(resume_events)
        return BenchRow(
            scenario=scenario,
            variant=variant,
            iteration=iteration,
            first_sse_ms=_first_ms(events),
            first_delta_ms=_first_ms([event for event in events if event["event"] == "assistant_delta"]),
            final_ms=_elapsed_ms(started),
            approval_ms=approval_ms,
            event_count=len(events),
        )
    except Exception as exc:
        return BenchRow(
            scenario=scenario,
            variant=variant,
            iteration=iteration,
            first_sse_ms=None,
            first_delta_ms=None,
            final_ms=_elapsed_ms(started),
            approval_ms=None,
            event_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def _upload_attachments(client: TestClient, tmp_path: Path, case_id: str, scenario: str) -> list[dict[str, str]]:
    count = 0
    if scenario == "small_attachment":
        count = 1
    elif scenario in {"multi_attachment", "case_patch"}:
        count = 3
    attachments: list[dict[str, str]] = []
    for index in range(count):
        source = tmp_path / f"{case_id}_{index}.md"
        source.write_text(f"# Invoice artifact {index}\n\nInvoice INV-{index} amount 12800.00 CNY.", encoding="utf-8")
        response = client.post(
            f"/api/cases/{case_id}/attachments",
            files={"file": (source.name, source.read_bytes(), "text/markdown")},
        )
        response.raise_for_status()
        item = response.json()
        attachments.append({"name": item["name"], "path": item["path"], "content_type": item["content_type"]})
    return attachments


def _read_stream(run_id: str, started: float, after_seq: int = 0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    last_seq = after_seq
    deadline = time.perf_counter() + 10
    while time.perf_counter() < deadline:
        pending = stream_hub.events_after(run_id, after_seq=last_seq)
        for event in pending:
            last_seq = max(last_seq, event.seq)
            row = {"event": event.kind, "kind": event.kind, "seq": event.seq, "received_ms": _elapsed_ms(started)}
            events.append(row)
            if event.kind in {"final", "error", "approval_required"}:
                return events
        time.sleep(0.005)
    raise TimeoutError(f"stream events did not finish for {run_id}")
    return events


class _FakeRuntime:
    def __init__(self, store: CaseStore) -> None:
        self.runner = SimpleNamespace(store=store, harness=HarnessRuntime(store))

    def run_turn(self, request: AgentTurnRequest) -> AgentTurnResponse:
        _sleep_for(request.message)
        return AgentTurnResponse(
            case_id=request.case_id,
            reply=f"blocking final: {request.message}",
            case_state=CaseState(case_id=request.case_id),
            trace={"run_id": "blocking", "status": "completed"},
        )

    async def run_turn_streamed(self, request: AgentTurnRequest, *, run_id: str, event_sink: Any | None = None) -> AgentTurnResponse:
        assert event_sink is not None
        event_sink("context_loaded", {"case_id": request.case_id}, summary="context loaded")
        await asyncio.sleep(0.01)
        if "report approval" in request.message:
            interrupts = [{"type": "tool_approval", "case_id": request.case_id, "run_id": run_id, "tool": "write_case_file", "risk_level": "external_write", "reason": "approval"}]
            event_sink("approval_required", {"interrupts": interrupts}, summary="approval required")
            return AgentTurnResponse(
                case_id=request.case_id,
                reply="approval required",
                case_state=CaseState(case_id=request.case_id),
                trace={"run_id": run_id, "status": "waiting_approval", "interrupts": interrupts},
            )
        event_sink("model_started", {"role": "planner"}, summary="model started")
        await asyncio.sleep(0.02)
        event_sink("assistant_delta", {"delta": "ok"}, summary="delta")
        await asyncio.sleep(_async_delay_for(request.message))
        event_sink("tool_started", {"tool": "fake_tool"}, summary="tool started")
        await asyncio.sleep(0.005)
        event_sink("tool_finished", {"tool": "fake_tool", "status": "success"}, summary="tool finished")
        return AgentTurnResponse(
            case_id=request.case_id,
            reply="ok",
            case_state=CaseState(case_id=request.case_id),
            trace={"run_id": run_id, "status": "completed"},
        )

    async def resume_approval_streamed(self, case_id: str, run_id: str, approved: bool, reason: str = "", *, event_sink: Any | None = None) -> AgentTurnResponse:
        assert event_sink is not None
        event_sink("approval_decision", {"approved": approved, "reason": reason}, summary="approval decision")
        await asyncio.sleep(0.02)
        event_sink("assistant_delta", {"delta": "report done"}, summary="delta")
        return AgentTurnResponse(
            case_id=case_id,
            reply="report done",
            case_state=CaseState(case_id=case_id),
            trace={"run_id": run_id, "status": "completed"},
        )


def _message_for(scenario: str) -> str:
    return {
        "simple": "hello",
        "material_advice": "请告诉我还需要哪些材料 material advice",
        "small_attachment": "review small attachment",
        "multi_attachment": "review multiple attachments",
        "case_patch": "review and patch case",
        "report_approval": "generate report approval",
    }.get(scenario, scenario)


def _sleep_for(message: str) -> None:
    time.sleep(0.02 + _async_delay_for(message))


def _async_delay_for(message: str) -> float:
    if "multiple" in message or "patch" in message:
        return 0.04
    if "material" in message:
        return 0.03
    return 0.01


def _summary(rows: list[BenchRow]) -> dict[str, Any]:
    grouped: dict[str, list[BenchRow]] = {}
    for row in rows:
        grouped.setdefault(f"{row.scenario}:{row.variant}", []).append(row)
    return {
        key: {
            "count": len(items),
            "errors": [item.error for item in items if item.error],
            "final_ms": _stats([item.final_ms for item in items]),
            "first_sse_ms": _stats([item.first_sse_ms for item in items if item.first_sse_ms is not None]),
            "first_delta_ms": _stats([item.first_delta_ms for item in items if item.first_delta_ms is not None]),
            "approval_ms": _stats([item.approval_ms for item in items if item.approval_ms is not None]),
            "event_count_mean": statistics.mean([item.event_count for item in items]) if items else 0,
        }
        for key, items in grouped.items()
    }


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "mean": statistics.mean(ordered),
        "std": statistics.pstdev(ordered) if len(ordered) > 1 else 0.0,
        "p50": _percentile(ordered, 50),
        "p90": _percentile(ordered, 90),
        "p95": _percentile(ordered, 95),
        "max": max(ordered),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = (len(values) - 1) * percentile / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[int(index)]
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def _first_ms(events: list[dict[str, Any]]) -> float | None:
    return float(events[0]["received_ms"]) if events else None


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _markdown(payload: dict[str, Any]) -> str:
    lines = ["# Streaming Infra Benchmark", ""]
    for key, summary in payload["summary"].items():
        final_p50 = (summary.get("final_ms") or {}).get("p50")
        first_sse = (summary.get("first_sse_ms") or {}).get("p50")
        lines.append(f"- `{key}` final_p50={final_p50} first_sse_p50={first_sse} errors={len(summary.get('errors') or [])}")
    return "\n".join(lines) + "\n"


def _split_arg(value: str, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local fake infra latency benchmarks.")
    parser.add_argument("--mode", default="fake", choices=["fake"])
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--report-dir", default="")
    return parser.parse_args()


if __name__ == "__main__":
    main()
