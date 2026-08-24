# Business Eval deterministic post-fix baseline — 2026-08-24

## Provenance

- Source live baseline: `output/business_benchmarks/benchmarks/full_baseline_ds4f_cny_20260824/`
- Source scorer: `business_eval_scorer_v3.5`
- Source code: `d4d546bd25b5623936ba29b7b31068daccef71d4`
- Scorer-only rescore: `output/business_benchmarks/benchmarks/full_baseline_ds4f_cny_20260824_rescore_v3_6/`
- Deterministic counterfactual replay: `output/business_benchmarks/benchmarks/full_baseline_ds4f_cny_20260824_postfix_replay/`
- Replay manifest: `output/business_benchmarks/benchmarks/full_baseline_ds4f_cny_20260824_postfix_replay/replay_manifest.json`

The source snapshots remain the immutable record of the live model run. The
post-fix replay is derived from copies of those snapshots and is not a new live
run. It re-renders only the Canonical Consumer report and final report delivery,
then scores the copies with `business_eval_scorer_v3.6`.

## Deterministic fixes

1. Bare page locators such as `page 1` resolve only inside the matching
   `[page 1 text]` section. A quote on another page is still rejected.
2. Once report persistence is complete, deterministic report delivery wins over
   a same-turn generic Manager final answer in both Markdown-only and PDF paths.
3. A contradicted numeric leaf reports the exact printed value, recomputed value,
   and difference directly from its admitted `ABS_DIFF` Witness operands. This
   covers subtotal conflicts without adding invoice-specific arithmetic rules.

## Results

| Case | Raw v3.5 | Scorer v3.6 | Post-fix replay |
|---|---:|---:|---:|
| 0014 | 59.00 | 99.60 | 99.60 |
| 0016 | 51.20 | 51.20 | 63.20 |
| 001 | 59.00 | 59.00 | 59.00 |
| 0005 | 6.00 | 6.00 | 6.00 |
| 0006 | 83.22 | 83.22 | 89.00 |
| 0025 | 99.00 | 99.00 | 99.00 |
| 0062 | 89.00 | 89.00 | 100.00 |
| 0044 | 58.39 | 81.93 | 88.93 |
| 0020 | 59.00 | 59.00 | 59.00 |
| 0053 | 88.05 | 88.05 | 89.00 |

| Metric | Raw v3.5 | Scorer v3.6 | Post-fix replay |
|---|---:|---:|---:|
| Strict passes | 1/10 | 2/10 | 3/10 |
| Mean score | 65.186 | 71.600 | 75.273 |
| Core checks | 431/594 | 466/594 | 487/594 |
| Report complete | 5/10 | 5/10 | 8/10 |
| Communication complete | 2/10 | 2/10 | 8/10 |
| Root truth | 7/10 | 7/10 | 7/10 |

The scorer-only delta is limited to the page-locator false negatives: 0014 and
0044 improve, while all other case scores stay unchanged. The deterministic
replay improves report and communication projection but does not change root
truth, Proof lineage, Verifier judgments, or TaskCompiler output.

## Verification

- Business scorer: 91 tests passed.
- Canonical Consumer/report: 27 tests passed.
- Turn runner/delivery, including sync, streamed, approval, step-limit,
  Markdown-only, PDF, and same-turn Manager final paths: 62 tests passed.
- Full offline suite: 987 tests passed. Two pre-existing consistency tests fail:
  the prompt-prefix hash fixture is stale, and `test_role_capabilities` expects
  every role to use `max_retries=1` while current `main` configures the
  deterministic report writer with `max_retries=0`. Neither file is touched by
  this change.
- Original snapshot hashes are recorded beside replay snapshot hashes in the
  replay manifest.
- No provider or model API was called.

## Baseline audit correction

The original note classified 0005 as a deterministic contract conflict. The
prompt and `PlanConformanceGate` actually agree that each `stated_components`
CHECK must declare all required semantic roles. The failure is repeated
TaskCompiler non-conformance amplified by whole-run recovery, not conflicting
deterministic contracts. The frozen source document is left unchanged; this
post-fix document records the correction.
