# Business Eval tuning SOP

## Recovery baseline

- Tag: `business-eval-baseline-20260823`
- Commit: `9497a53ea1b0eab20e706fe5125c4083977ff278`
- Safe recovery: create a new branch from the tag, or revert an experimental commit. Do not rewrite `main` history.

## One-change loop

1. Locate the first layer that diverges: Scope → Plan → Executor → Binding → Witness → Verifier → Kernel → Consumer → Runtime.
2. State one falsifiable hypothesis. Do not mix scorer, Kernel, prompt, and delivery changes in one experiment unless they share one deterministic contract.
3. Prefer replacing an existing rule over adding a new type, repair loop, business formula, or compatibility path.
4. Run focused deterministic tests.
5. Run only the affected live case. Record the actual provider, model, reasoning mode, commit, score revision, root/facet outcomes, False Strong count, completion, calls, tokens, latency, and tool errors.
6. Keep the change only when it fixes the targeted layer without introducing a new false strong or integrity failure. Otherwise revert it and record the open gap.
7. After a kept 0025 or 0006 change, run the other arithmetic sentinels before merging.

On Windows, keep live-run output labels short enough to stay below legacy path limits; put the full hypothesis in this SOP rather than in the directory name.

## Provider order

1. Prefer the configured AMD DeepSeek-compatible gateway when a credential is available.
2. If its health check, authentication, rate limit, or response protocol fails, use the DeepSeek official API.
3. Never switch provider inside one run. Record the fallback reason in the run label or benchmark report.
4. Read credentials from ignored environment configuration only. Never write them to source, traces, reports, or Git.

## Acceptance gate

- Correct target root and causal facet outcomes.
- False Strong = 0.
- Witness replay, Binding relevance, policy lineage, and Kernel integrity pass.
- Canonical packet contains every business fact and proof term used by the report.
- Markdown, PDF, and final reply are generated when reportable.
- Runtime completes without MaxTurns or repeated deterministic retries.
- Efficiency is recorded but does not override correctness.

## Active experiments

| ID | Case | Hypothesis | Change | Keep condition | Status |
|---|---|---|---|---|---|
| E001 | 0025 | The dense component rule lets Executor stop after observation even when explicit applicability plus one unambiguous base is present. | Replace it with an ordered observation → applicability → reconciliation checklist; no schema or Kernel change. | Component submits Binding + Witness, final total closes, no false strong. | Keep: fresh run closed 5/5 CHECKs with the correct root; report exposed a separate internal-ID delivery problem. |
| E002 | 0025 | A business report matrix that asks for internal proof IDs invites the model to place a canonical Finding ID in a CHECK-ID column. | Replace the internal-ID matrix with business meaning, status, facts, and locator; keep the write guard strict. | Report contains no internal proof IDs and the original ID-column rejection disappears. | Keep: fresh report used the business matrix; its next rejection was an independent numeric parser bug. |
| E003 | 0025 / 0006 | The report number tokenizer can start inside a CJK-adjacent grouped amount and can treat a Markdown bullet as a negative sign. | Fix the token boundary and strip only a line-leading list marker before numeric comparison; do not loosen admitted values or currency checks. | The exact failed 0025 report replays successfully; invented, relabelled, and true negative values remain guarded. | Keep offline: 65 projection/report tests pass and the exact rejected report now passes. Fresh 0025 was inconclusive because TaskCompiler failed before delivery. |
| E004 | 0025 | Component reconciliation is sometimes accepted and sometimes rejected despite the same grounded rate/base/amount Witness chain. | Do not change yet. First compare accepted vs rejected Verifier inputs and identify the smallest semantic wording mismatch. | Two fresh runs classify the facet consistently without weakening the grounded-base rule. | Open gap. |
| E005 | 0006 | Plan/Executor treat an upstream rate/base gap as if it invalidated the separately grounded component amount and sign, while numeric grounding cannot represent `-EUR amount` as a signed operand. | Separate full component validity from narrower downstream inclusion, require signed numeric Claim values, and teach the existing localized-number parser the sign-before-currency form; no schema, Kernel, Eval, or retry change. | Final uses the derived line sum and grounded signed component amount; component rate/base remains NOT_FOUND; no false strong. | Keep: fresh E006 produced `15502.44 + (-645.94) = 14856.50`, final SUPPORTED, component NOT_FOUND, root CONTRADICTED, and clean Kernel closure. The 69.22 score reflects a separate Report Writer invalid-JSON failure and an Eval tax-inclusive-ref requirement. |
| E006 | 0053 | Eval rejects uniquely grounded atomic numeric excerpts and misses the common Chinese term “行扩展金额”. | Accept a numeric excerpt only when it is inside the Oracle-locked source row, uniquely occurs in that row, and matches value/role/currency; normalize “行扩展金额” to “行金额”. | Restore third-line fact/relation and subtotal semantics without accepting ambiguous token-only quotes. | Keep: 141 Eval tests pass; the same snapshot raw score rose 66.29 → 72.05, while ambiguous `20` remains rejected. Remaining failures are tax-inclusive Proof lineage and RUN_FAILED report delivery. |
| E007 | 0053 | A canonical partial report already exists, but `collecting_materials` prevents the deterministic file-write continuation and causes repeated Report Writer delegation. | Keep `ready_for_report` as the gate for starting Report Writer, but allow an existing canonical report to continue to `write_case_file` and PDF approval. | Partial cases persist the existing report without creating reports for unready cases or changing proof/reportability. | Keep as one half of the delivery fix: fresh E009 stopped repeated Writer delegation and requested write approval, exposing the independent step-budget boundary below. |
| E008 | 0053 | An approved deterministic report action is discarded when the Manager has already consumed its step budget. | Let only an already-selected runtime-policy continuation cross the Manager step limit; ordinary Manager and tool actions remain capped. | At the exact step limit, approved write executes, PDF receives its own approval, and delivery finalizes without reopening Manager reasoning. | Keep: fresh E010 executed the approved `write_case_file` beyond the step limit. The strict write guard then exposed a separate ambiguous report table. |
| E009 | 0025 / 0053 | A redundant business matrix repeats a root Requirement ID on leaf rows, so a valid local `SUPPORTED` leaf is misread as a conflicting root status. | Delete the duplicate matrix; show Requirement IDs only in the root table and canonical Check IDs in the leaf table. Keep status validation strict. | Mixed leaf statuses under a `NOT_FOUND` root persist without weakening root or leaf validation. | Deterministic prompt/projection gate passed: 64/64. Fresh Eval pending. |
