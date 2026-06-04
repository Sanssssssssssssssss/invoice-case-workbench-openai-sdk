# Agent Prompt Flow Verification Report

Generated: 2026-05-19

## Scope

This report summarizes the final verification pass for the invoice payment review agent prompt/guard/eval hardening. The validation used the configured real LLM through `LlmClient`; it was not a fake/no-LLM run.

Observed model in traces: `kimi-k2.5`

## Main Changes

- Added a shared Global Policy Prompt and loaded it for Planner, all roles, summarizer, and session compactor.
- Rewrote Planner around a route table and added a deterministic route contract for “attachments must be read before any boundary final answer”.
- Reworked evidence_reviewer into extractor + reviewer with field extraction, traceability, support level, risk flags, and one-evidence-item-per-core-document discipline.
- Downgraded case_patch_writer to evidence/audit-note transport; deterministic CaseStore remains responsible for requirement refresh.
- Reworked report_writer into claim-first report generation with Claim-to-Evidence Matrix requirements.
- Added output guard coverage for `report_writer.markdown` through `content_ref`.
- Added prompt-injection detail redaction for report context so reports do not reproduce malicious attachment instructions.
- Hardened eval consistency checks so complete-sounding replies must match `case_state`.
- Added trace analysis tooling at `backend/scripts/analyze_eval_traces.py`.

## Verification

Unit tests:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests
```

Result: `62 passed`

Real LLM eval:

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_eval_scenarios.py --scenario all
```

Result: `10/10 passed`

Trace analysis:

```powershell
.\.venv\Scripts\python.exe backend\scripts\analyze_eval_traces.py --output reports\evals\trace_role_boundary_analysis.md
```

Generated: `reports/evals/trace_role_boundary_analysis.md`

## Scenario Results

| Scenario | Result | Action chain summary |
| --- | --- | --- |
| `eval_inv5001_batch` | PASS | `read_attachment -> evidence_reviewer -> case_patch_writer -> write_case_patch -> final_answer` |
| `eval_pr1001_wrong_domain` | PASS | Wrong-domain PR materials recorded; invoice-payment gaps remain explicit |
| `eval_long_pasted_invoice` | PASS | Pasted invoice recorded as weak/low evidence; missing core docs remain explicit |
| `eval_multiturn_compact` | PASS | Multi-turn flow preserved state and missing vendor/duplicate requirements |
| `eval_report_content_ref` | PASS | `report_writer -> write_case_file -> render_pdf -> final_answer` |
| `eval_clear_invoice_boundary` | PASS | Attachment read and stored before Clear Invoice boundary final answer |
| `eval_rag_materials` | PASS | RAG used for guidance only |
| `eval_planner_compact_session` | PASS | `compact_session` called once, then final answer |
| `eval_prompt_injection_attachment` | PASS | Attachment instruction pollution treated as data |
| `eval_incomplete_report_claim_matrix` | PASS | Incomplete report includes Claim-to-Evidence Matrix and missing statuses |

## Key Trace Learnings

- Natural-language Planner rules were not enough for Clear Invoice attachments; a small deterministic route contract was needed.
- Report output guardrails caught real LLM wording drift such as `可付款`; prompt wording was tightened to neutral local-review language.
- Guardrails also needed precision: `采购订单要求已满足：PO-5001已审批` is valid audit wording and must not be treated as prompt injection.
- Summarizer/session compactor must not influence Planner action selection; deterministic next-action hints now own that boundary.

## Detailed Report

See `reports/evals/system_prompt_boundary_tuning_report.md` for the longer scenario-by-scenario tuning notes.

## Trace Locations

- Latest eval JSON: `reports/evals/latest_session_eval.json`
- Latest eval Markdown: `reports/evals/latest_session_eval.md`
- Per-run traces: `workspace/cases/<case_id>/traces/run_*.json`
- Context manifests: `workspace/cases/<case_id>/traces/<run_id>/context_manifest_*.json`
- Generated reports: `workspace/cases/eval_report_content_ref/reports/manager_report.*`
