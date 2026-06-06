# Streaming Infra

This project keeps the blocking `POST /api/agent/turn` contract and adds a default streaming run path for the renderer.

## API

- `POST /api/agent/runs`
  - Body: the existing `AgentTurnRequest`.
  - Returns `{ case_id, run_id, status, stream_url }`.
- `GET /api/agent/runs/{run_id}/stream?after_seq=0`
  - Server-sent events for the run.
  - Supports bounded replay with `after_seq`.
- `POST /api/agent/runs/{run_id}/approval`
  - Body: `{ case_id, approved, reason }`.
  - Continues the same run stream after an approval decision.

The legacy `POST /api/cases/{case_id}/runs/{run_id}/approval` remains available for blocking fallback.

## Events

The stream uses these event names:

- `run_started`
- `context_loaded`
- `model_started`
- `assistant_delta`
- `tool_started`
- `tool_finished`
- `approval_decision`
- `approval_required`
- `final`
- `error`

Intermediate events are summary-only. They must not include raw prompts, raw attachment text, full tool input, or hidden reasoning. The `final` event carries an `AgentTurnResponse`-compatible payload so the renderer can reuse the existing refresh path.

## Runtime

The streaming manager path uses OpenAI Agents SDK `Runner.run_streamed(...)`. The synchronous path still uses `Runner.run(...)` through `run_agent_sync(...)`.

FastAPI lifespan enables a shared `AsyncOpenAI` client pool keyed by API key, base URL, and timeout. CLI and test paths that do not start the app lifespan still create temporary clients and close them after the run.

## Benchmark

Default benchmark runs are fake/scripted and do not call an LLM:

```powershell
python -m benchmarks.infra.run --mode fake --variants blocking,streaming --scenarios simple,material_advice,small_attachment,report_approval
```

Reports are written under `benchmarks/infra/reports/`.

Metrics include final latency, first SSE latency, first assistant delta latency, approval resume latency, event count, and p50/p90/p95/max/mean/std summaries.

## Regression Targets

- First SSE p95 under 300 ms on local fake/scripted runs.
- Streaming final latency no more than 5% slower than blocking for comparable fake scenarios.
- Shared client reuse should reduce repeated live simple-run p95 final latency once real network/model overhead is included.
- Approval resume must either continue to final or surface an `error` event without breaking the next chat command.
