# Expected Results

Because Planner and roles are model-driven, wording may vary. The run should still satisfy these checks:

- The local case workspace exists at `workspace/cases/case_sample_001/`.
- `case_state.json` contains evidence items after evidence submission turns.
- `conversation.jsonl` records user and assistant turns.
- `traces/run_*.json` records planner actions, role calls, model calls, and tool calls.
- `reports/manager_report.md` is created after the report request.
- `reports/manager_report.pdf` is created when PDF rendering succeeds.
- The final answer must not claim ERP approval, payment, posting, routing, or submission happened.

