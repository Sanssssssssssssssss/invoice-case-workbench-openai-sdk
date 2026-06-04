# Full Replay Logging Report

Run date: 2026-05-21

## What Changed

The runtime now keeps two observability layers:

- `traces/<run_id>.json`: lightweight trace for UI and quick flow review.
- `traces/<run_id>/events.jsonl`: append-only full debug event stream for replay.
- `traces/events.jsonl`: case/session-level append-only event stream, including compaction events that happen outside a specific run.
- `traces/artifacts/<run_id>/*.json`: large role/tool artifacts remain available as structured payload files.

Each JSONL event includes `kind`, `name`, `summary`, `payload_sha256`, `payload_preview`, and full `payload`.

## New Replay Coverage

- Planner decisions: full planner action payload is recorded as `planner_action`.
- Model calls: full system prompt, full payload, raw response, latency, finish reason, usage, provider request id when available.
- Role calls: full hydrated role input and full role result.
- Tool calls: full tool input and full tool result.
- Context manifests: now include actual context payload, payload preview, and payload hash.
- Step feedback: every checkpoint records case-state snapshot, quality checks, next-action hint, workspace file snapshot, and file changes.
- Session compaction: `SessionStore.update_session_summary()` writes a `session_compact` event even when compaction is triggered outside the LangGraph run.

## Real LLM Validation

Scenarios run:

- `06_clear_invoice_boundary`: PASS
- `08_planner_compact_session`: PASS

Observed logs:

- `workspace/cases/eval_clear_invoice_boundary/traces/run_2efa6e89451b/events.jsonl`
  - 33 events
  - 5 planner actions
  - 9 model calls
  - 2 role calls
  - 2 tool calls
  - 5 checkpoints
  - 1 final answer event
  - feedback summary: 5 ok, 0 warnings, 0 errors

- `workspace/cases/eval_planner_compact_session/traces/run_9eda85563b13/events.jsonl`
  - 12 events
  - 2 planner actions
  - 3 model calls
  - 1 tool call
  - 2 checkpoints
  - 1 final answer event
  - feedback summary: 2 ok, 0 warnings, 0 errors

- `workspace/cases/eval_planner_compact_session/traces/events.jsonl`
  - contains `session_compact` with strategy `llm` and reason `planner_requested`

## Remaining Boundaries

- JSONL logs are intentionally verbose and stored in workspace, not committed by normal source-control flow.
- Token usage and provider request id depend on what the provider returns.
- File changes are captured as workspace snapshots and added/modified/removed lists, not byte-level diffs.
- Older trace files generated before this change will not have full replay events.

## Verification

- `python -m pytest backend/tests`: 78 passed.
- Real LLM evals above passed and produced full replay logs.
