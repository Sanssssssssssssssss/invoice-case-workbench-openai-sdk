# InvoiceTauBench

InvoiceTauBench is the local tau-bench style benchmark for the invoice payment review workbench. It is designed to verify the agent as a transactional AP reviewer, not as a free-form chatbot.

The benchmark follows the tau-bench pattern:

- Multi-turn user scripts define what the user says, what files they attach, and how approval prompts are answered.
- The runner exercises the real application API boundary through `AgentRuntime` and `TurnRunner`.
- Domain policy is encoded in scenario fixtures and deterministic verifiers.
- Success is judged by final case state, trace behavior, artifacts, safety constraints, budgets, and optional qualitative judging.
- `pass^k` is supported through repeated runs with `--k`.

## Modes

`scripted` is the default mode. It uses `ScriptedManagerRunner` and `ScriptedRoleRegistry` to provide deterministic manager and specialist outputs while still executing the real runtime tools, policy gate, approval resume path, case store, traces, and artifact writers. It does not call an LLM.

`live` keeps the same scenario harness but lets the real OpenAI Agents SDK manager and specialists run. This can spend model tokens and is not used by default.

The LLM judge is also off by default. Add `--llm-judge` to run it, and add `--require-llm-judge` only when judge pass/fail should affect the final benchmark pass.

## Commands

Run one scripted smoke scenario:

```powershell
python -m benchmarks.invoice_tau.run --mode scripted --scenario chat_capability_001
```

Run the full deterministic suite once:

```powershell
python -m benchmarks.invoice_tau.run --mode scripted --k 1
```

Run a representative low-cost batch:

```powershell
python -m benchmarks.invoice_tau.run --mode scripted --scenario chat_capability_001 --scenario material_advice_001 --scenario duplicate_conflict_001 --scenario prompt_injection_001 --scenario reject_pdf_approval_001
```

Run repeated scripted `pass^k`:

```powershell
python -m benchmarks.invoice_tau.run --mode scripted --k 5
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

The summary reports:

- `pass@1`: fraction of scenario runs that pass.
- `pass_all_k`: fraction of scenarios where every repeated run passed.
- `average_score`: mean deterministic verifier score.
- `total_tokens`: tokens observed in trace events, normally zero in scripted mode.
- `total_wall_time_ms`: wall-clock runtime across scenarios.

Scenario checks cover:

- Required and forbidden reply text.
- Requirement statuses and risk flags.
- Evidence types.
- Required and forbidden tool/role calls.
- Approval interrupts and decisions.
- Required and forbidden artifacts.
- RAG guidance profiles and source terms.
- Safety constraints such as prompt-injection leakage and approval bypass.
- Budgets for model calls, tool calls, role calls, tokens, and wall time.

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
