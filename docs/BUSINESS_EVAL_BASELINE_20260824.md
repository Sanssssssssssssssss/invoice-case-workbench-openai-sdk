# Business Eval baseline — 2026-08-24

## Frozen run

- Pre-run commit: `de3ee7e7540479939ab954392f7e87f89c00472e`
- Recovery tag: `business-eval-full-baseline-pre-run-20260824`
- Provider/model: DeepSeek official API / `deepseek-v4-flash`
- Thinking: disabled
- Scorer: `business_eval_scorer_v3.5`
- Oracle versions: `4`, `5`
- Live artifacts: `output/business_benchmarks/benchmarks/full_baseline_ds4f_cny_20260824/`
- Offline rescore: `output/business_benchmarks/benchmarks/full_baseline_ds4f_cny_20260824_rescore/`

The live artifacts are intentionally kept under the ignored canonical output root. They contain the immutable snapshots, score revisions, reports, traces, provider request IDs, per-call timestamps, and raw token/cache usage needed for later rescoring or repricing.

## Pricing snapshot

Official price page: <https://api-docs.deepseek.com/zh-cn/quick_start/pricing>

All 471 provider calls ran between 05:46:33 and 06:24:29 Singapore/Beijing time (UTC+8), inside the published off-peak period.

| Usage | CNY / 1M tokens | Tokens | Cost CNY |
|---|---:|---:|---:|
| Input, cache miss | 1.50 | 1,590,171 | 2.3852565 |
| Input, cache hit | 0.05 | 5,771,776 | 0.2885888 |
| Output | 4.50 | 259,647 | 1.1684115 |
| **Total** | — | **7,621,594** | **3.8422568** |

Input cache-hit ratio: **78.4001%**. Reasoning tokens are unavailable because thinking was disabled. TTFT remains partially observed and is `null`, not zero, for synchronous Compiler roles.

## Results

Strict pass: **1/10**. Mean: **65.186**. Minimum: **6.00**. Runtime completed: **10/10**. Offline rescoring reproduced every score, failed check, and veto exactly.

| Case | Root | Score | First failed stage | Provider calls | Tokens | Cost CNY |
|---|---|---:|---|---:|---:|---:|
| 0014 | SUPPORTED | 59.00 | evidence | 46 | 558,828 | 0.2513348 |
| 0016 | CONTRADICTED | 51.20 | evidence | 44 | 728,426 | 0.3579684 |
| 001 | NOT_FOUND | 59.00 | evidence | 86 | 1,386,408 | 0.8416024 |
| 0005 | INTEGRITY_REJECTED | 6.00 | understanding | 10 | 84,917 | 0.0383081 |
| 0006 | CONTRADICTED | 83.22 | evidence | 49 | 677,626 | 0.3422164 |
| 0025 | CONTRADICTED | 99.00 | — | 46 | 611,857 | 0.3046947 |
| 0062 | NOT_FOUND | 89.00 | communication | 49 | 1,138,520 | 0.5594912 |
| 0044 | CONTRADICTED | 58.39 | evidence | 49 | 694,389 | 0.3437705 |
| 0020 | NOT_FOUND | 59.00 | evidence | 41 | 848,656 | 0.4219788 |
| 0053 | NOT_FOUND | 88.05 | evidence | 51 | 891,967 | 0.3808915 |

## Eval audit

- **0014 — scorer false negative.** The production Kernel admitted a complete five-CHECK proof with exact source quotes and replayable Witness lineage. The scorer rejected valid one-page locators written as `page 1`, then cascaded that mismatch into missing facts and `UNGROUNDED_STRONG_CONCLUSION`.
- **0044 — mixed.** The same `page 1` scorer mismatch caused false grounding failures, but the run also genuinely left component/final-total checks unresolved.
- **0016 — calculation correct, other model/delivery errors real.** The final-total conflict was proved, but the Verifier treated invoice and credit-note subtype labels as mutually exclusive, downgraded the source role to unknown, and the final delivery omitted the concrete variance.
- **001 / 0006 / 0053 — real semantic handoff gaps.** A component applicability gap incorrectly prevented downstream use of separately grounded component amounts or omitted tax-inclusive treatment from final lineage.
- **0005 — deterministic contract conflict.** Task plans split component semantic roles across CHECKs, while `ProofSignature` still requires every component CHECK to declare every role; bounded repair exhausted before execution.
- **0020 — real reverse-charge semantic error.** The model treated a source-stated reverse-charge treatment with no payable VAT component as missing evidence instead of a supported zero-component treatment.
- **0062 — small delivery-only failure.** Typed proof and generated Markdown/PDF passed; the final user reply omitted the report links, printed total, and required business wording.
- **0025 — clean pass.** Only non-core CHECK wording diagnostics remained.

This is the frozen raw baseline. Do not compare its absolute score against a future scorer version without rescoring these same snapshots and labeling both scorer versions.
