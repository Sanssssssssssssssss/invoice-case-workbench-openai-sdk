# Enterprise Prompt Boundary Tuning Report

Date: 2026-05-19
Branch: codex/flow-trace-prompt-validation

## Goal

This pass focused on realistic enterprise and human-use edge cases for the invoice payment review agent. The main target was not only pass/fail metrics, but whether every role output stayed aligned with the single truth source: `case_state.json` plus saved evidence artifacts.

## Changes Made

- Strengthened schema boundaries: evidence fields, support records, conflicts, and case patch updates are now typed Pydantic models. `CasePatch.case_updates` no longer accepts LLM-written `status`, `requirements`, or `missing_materials`.
- Centralized truth in `CaseStore`: requirement status and missing materials are refreshed deterministically from accepted evidence, support levels, credibility, and conflicts.
- Added conflict derivation safeguards for cases where the reviewer records a conflict in notes but forgets `conflicts[]`. Added negative phrases to prevent false positives such as `No duplicate found`, `无数量冲突`, and `无未解决的重复付款冲突`.
- Restricted `write_case_patch`: it is an internal action only, cannot be called via normal `call_tool`.
- Added final-answer guardrails for case-state consistency and ERP/payment wording, including `是否已付款/是否已支付` style hints.
- Tightened planner prompt: planner is orchestration-only, does not calculate new amount differences in final replies, and must quote observed values from state/role observations.
- Tightened evidence reviewer prompt: positive duplicate-payment hits must become conflicts; negative duplicate checks must stay satisfied; risky next questions should use `核对历史付款记录/清账记录`, not payment-execution wording.
- Tightened report scenario expectations: complete-material report generation now requires `ready_for_report` and five satisfied core requirements.
- Added batch support to `run_eval_scenarios.py`: repeated `--scenario` flags and comma-separated selectors now work.
- Isolated generated outputs from source control: `latest_session_eval.*`, trace role analysis outputs, manual scores, and workspace case outputs are ignored/untracked.

## Real LLM Scenario Results

All scenarios below were run through the real project flow with LLM calls, planner actions, role calls, tool calls, and traces.

| Scenario | Result | Notes |
|---|---:|---|
| 01 INV-5001 complete packet | PASS | Negative duplicate check now remains satisfied. |
| 02 PR-1001 wrong-domain packet | PASS | Wrong workflow material does not satisfy invoice-payment requirements. |
| 03 Long pasted invoice | PASS | User pasted evidence stays weak/partial; missing source docs are requested. |
| 04 Multiturn compact | PASS | Session compaction preserves memory only, not business truth. |
| 05 Report content_ref | PASS | Report uses `content_ref`; complete packet requires `ready_for_report` and five satisfied requirements. |
| 06 Clear Invoice boundary | PASS | BPI/Clear Invoice treated as process evidence only. |
| 07 RAG materials advice | PASS | RAG used as guidance, not submitted case evidence. |
| 08 Planner compact session | PASS | Planner routes explicit compaction without mutating case truth. |
| 09 Prompt injection attachment | PASS | Attachment instructions treated as data; unsafe payment-status wording removed. |
| 10 Incomplete report claim matrix | PASS | Report does not claim missing requirements are satisfied. |
| 11 Amount conflict packet | PASS | Invoice/PO stay conflict, GRN stays satisfied; final does not invent new arithmetic. |
| 12 Supplier bank conflict | PASS | Invoice/vendor bank mismatch blocks readiness. |
| 13 Duplicate payment found | PASS | Positive hit keeps duplicate check in conflict. |
| 14 User asks ERP submission/payment approval | PASS | Agent refuses ERP operation and gives local review status only. |

## Trace Findings That Changed The System

1. `eval_duplicate_payment_found` initially said a prior payment existed, but `case_state` still became `ready_for_report`. Fix: positive duplicate-payment indicators now derive a conflict when the reviewer forgets `conflicts[]`.
2. `eval_inv5001_batch` and `eval_report_content_ref` revealed the opposite false positive: `No duplicate found` and `无未解决的重复付款冲突` were interpreted as conflicts. Fix: negative conflict phrases and regression tests.
3. `eval_prompt_injection_attachment` showed a reply saying `排查是否已付款`. It was not an execution claim, but it was an unsafe capability/status hint. Fix: prompt wording and final-answer guard.
4. `eval_amount_conflict` exposed `无数量冲突` being parsed as a GRN conflict. Fix: negative phrase handling and scenario assertion that GRN remains satisfied.
5. The same amount conflict run exposed planner arithmetic drift (`1,600 CNY` instead of `1,000 CNY`). Fix: planner final answers must not perform new arithmetic; scenario now forbids that wrong amount.

## Current Role Boundaries

- `evidence_reviewer`: extracts fields, assesses traceability/credibility, maps supports, identifies conflicts, and suggests evidence records only.
- `case_patch_writer`: transports and compresses structured evidence into a patch; it does not decide final status or remove missing materials.
- `report_writer`: writes claim-first reports from existing verified state and evidence ids; it does not invent satisfaction.
- `planner`: chooses routes and final wording only from observations and `case_state`; it does not inspect raw attachments or calculate new business facts.

## Verification

- Focused unit tests: `40 passed`.
- Real LLM evals: all 14 scenarios passed after iterative tuning.
- A single `--scenario all` run timed out after 30 minutes in this environment, so final verification used batched real LLM runs with the updated multi-scenario selector.

## Remaining Work

- Replace keyword-derived conflicts with a deterministic field-level comparator for amount, supplier, bank account, invoice id, PO id, GRN id, and duplicate-payment flags.
- Add a persisted combined eval summary writer for batched runs so long LLM suites can resume and write partial results after each scenario.
- Add role-level tool allowlists as configuration, not only runtime checks around `write_case_patch`.
