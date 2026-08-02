# InvoiceTauBench

InvoiceTauBench is the local tau-bench style benchmark for the invoice payment review workbench. It is designed to verify the agent as a transactional AP reviewer, not as a free-form chatbot.

The benchmark follows the tau-bench pattern:

- Multi-turn user scripts define what the user says, what files they attach, and how approval prompts are answered.
- The runner exercises the real application API boundary through `AgentRuntime` and `TurnRunner`.
- Company policy is loaded from the canonical `policies/aurora_ap_policy_v1.json`; scenario fixtures provide evidence, and deterministic verifiers assert the resulting proof contract.
- Contract pass is judged by final case state, trace behavior, artifacts, safety constraints, and budgets.
- LLM quality score is reported separately through a judge model; a contract pass is not a perfect quality score.
- `pass^k` is supported through repeated runs with `--k`.

## Modes

`scripted` is the default mode. It uses `ScriptedManagerRunner` and `ScriptedRoleRegistry` to provide deterministic manager and specialist outputs while still executing the real runtime tools, policy gate, approval resume path, case store, traces, and artifact writers. It does not call an LLM.

`live` keeps the same scenario harness but lets the real OpenAI Agents SDK manager and specialists run. This can spend model tokens and is not used by default.

The LLM judge is enabled by default for the live core/full/material profiles and disabled for smoke/scripted profiles. Add `--no-llm-judge` to save judge tokens, add `--llm-judge` to force judging, and add `--require-llm-judge` only when judge pass/fail should affect the final benchmark pass.

Judge configuration defaults to the app LLM settings and can be overridden without changing the tested agent:

- `INVOICE_TAUBENCH_JUDGE_MODEL`
- `INVOICE_TAUBENCH_JUDGE_BASE_URL`
- `INVOICE_TAUBENCH_JUDGE_API_KEY`
- `INVOICE_TAUBENCH_JUDGE_TIMEOUT_SECONDS`

## Profiles

Named profiles are the preferred way to run chain-level evals:

- `chain_live_smoke`: real LLM smoke for chat, material advice, and empty case creation.
- `chain_live_core`: real LLM core benchmark for smoke plus exactly three long material-review regressions.
- `chain_live_full`: real LLM full benchmark; discovers every scenario and exercises review, report, PDF approval, prompt-injection safety, and rejection paths.
- `material_live_regression`: real LLM benchmark for only the three long material-review scenarios.
- `scripted_full`: deterministic full suite for CI and cheap local regression.

When a profile is used, its mode is the default. You can still pass `--mode scripted` or `--mode live` to override it intentionally.

## Commands

Use UTF-8 stdio in PowerShell before live benchmark runs:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
```

Run one scripted smoke scenario:

```powershell
python -m benchmarks.invoice_tau.run --mode scripted --scenario chat_capability_001
```

Run the full deterministic suite once:

```powershell
python -m benchmarks.invoice_tau.run --profile scripted_full --k 1
```

Run the six evidence-backed golden hand-test cases with the real model:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m benchmarks.invoice_tau.run --mode live --scenario-root golden_cases/session_invoice_cases_v1 --no-llm-judge
```

The golden root reuses the same runner and deterministic verifiers. Each case owns `scenario.json`, `expected.json`, `upload_to_app/`, and `originals/`; generated reports remain outside the dataset.

Run the live LLM smoke benchmark:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m benchmarks.invoice_tau.run --profile chain_live_smoke
```

Run the live LLM core chain benchmark:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m benchmarks.invoice_tau.run --profile chain_live_core
```

Run the full live LLM benchmark:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m benchmarks.invoice_tau.run --profile chain_live_full
```

Score a saved report with the LLM judge without rerunning the agent:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m benchmarks.invoice_tau.score_report --report-dir C:\Users\ROG\AppData\Local\Temp\invoice_tau_chain_live_full_20260610_185141 --write
```

Run only the three long material-review live regressions:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m benchmarks.invoice_tau.run --profile material_live_regression
```

Run live mode with the optional judge:

```powershell
python -m benchmarks.invoice_tau.run --mode live --scenario clean_complete_report_001 --llm-judge
```

## Outputs

Each run writes an isolated report directory under:

```text
benchmarks/invoice_tau/reports/<run>/
```

The important files are:

- `latest.json`: structured benchmark summary.
- `latest.md`: human-readable report.
- `runs/<scenario>_<k>.json`: full scenario result with checks, final case state, trace, metrics, and judge output.
- `workspace/cases/<case_id>/`: isolated case workspace, traces, artifacts, reports, previews, and audit files.
- `storage/`: isolated sessions and memory DBs for the benchmark run.

The benchmark sets `INVOICE_AGENT_WORKSPACE_ROOT`, `INVOICE_AGENT_STORAGE_ROOT`, `INVOICE_AGENT_SESSION_DB`, and `INVOICE_AGENT_MEMORY_DB` only for the duration of each scenario and restores the previous environment afterward.

## Metrics

The summary reports two separate tracks:

- `contract_pass@1`: fraction of scenario runs that satisfy hard deterministic checks.
- `pass_all_k`: fraction of scenarios where every repeated run passed.
- `deterministic_score`: mean deterministic verifier score.
- `llm_quality_score`: mean LLM judge score over judged runs.
- `judge_dimension_scores`: mean judge score for state correctness, evidence grounding, tool process, safety/approval, report quality, user communication, and efficiency.
- `total_tokens`: tokens observed in trace events, normally zero in scripted mode.
- `total_wall_time_ms`: wall-clock runtime across scenarios.

Scenario checks cover:

- Required and forbidden reply text.
- Case status and evidence counts.
- Requirement statuses and risk flags.
- Compiled proof status, outcome, policy version, and required proof obligations.
- Evidence types.
- Required and forbidden tool/role calls.
- Approval interrupts and decisions.
- Required and forbidden artifacts.
- RAG guidance profiles and source terms.
- Safety constraints such as prompt-injection leakage and approval bypass.
- Encoding cleanliness: benchmark outputs must not contain `???`, replacement characters, `undefined`, `not valid JSON`, streaming failure text, or generic manager failure text.
- Budgets for model calls, tool calls, role calls, tokens, and wall time.

`semantic_duplicate_reversal_001` is the evidence-language Compiler acceptance case. It seeds the stable `duplicate_payment_screen` input Requirement, requires a source-bound `candidate_found` lifecycle IR and validated LLM judgment, and expects the Compiler to derive `no_active_duplicate=PROVED` consistently across canonical proof, Requirement projection, and the final reply.

## Adding A Scenario

Create a directory:

```text
benchmarks/invoice_tau/scenarios/<scenario_id>/
```

Required files:

- `scenario.json`: user script, attachments, optional initial case state, scripted role outputs, budgets, and judge settings.
- `expected.json`: deterministic verifier expectations.
- `attachments/*`: every filename referenced by `user_script[].attach`.

Keep scripted role outputs valid against the same Pydantic schemas used by the runtime. Prefer exercising the deterministic runtime continuation instead of manually scripting every manager step when the intent is attachment review or report generation.

For approval scenarios, put per-turn decisions in:

```json
{"approvals": {"write_case_file": true, "render_pdf": false}}
```

Unspecified approvals default to reject unless `--default-approval` is passed.

## Reading Failures

Start with `latest.md`; it lists failed checks per scenario. For details, open the matching `runs/<scenario>_<k>.json` and inspect:

- `final_reply`
- `case_state`
- `trace.tool_calls`
- `trace.role_calls`
- `trace.interrupts`
- `metrics.events`
- `checks[].details`

If an approval test fails, confirm that `approval_interrupt` and `approval_decision` events exist and that side-effect tools only execute after an approved decision. If an artifact test fails, inspect the isolated case directory under `workspace/cases/<case_id>/`.
