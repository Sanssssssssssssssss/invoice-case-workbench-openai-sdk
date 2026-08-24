# Executor efficiency experiment — 2026-08-25

## Scope and rollback

- Frozen rollback point: `business-eval-0053-stop-gate-20260825` (`c45c173`).
- Experiment branch: `codex/executor-efficiency-exp`.
- Provider/model: configured DeepSeek endpoint / `deepseek-v4-flash`, thinking disabled.
- No DAG, new Agent, dependency, policy, Oracle, scorer, Verifier, TaskCompiler, Kernel, or report change.
- Raw snapshots, attachments, prompts, and model transcripts remain local under `output/` and are not tracked.

The experiment used frozen phase payloads to run one Executor CHECK without
calling TaskCompiler, Verifier, Kernel, Manager, or Report Writer. Each proposed
runtime change was tested on two randomly selected cases and committed only as
an independently reversible checkpoint. Failed changes were reverted without a
compatibility path.

## Replay infrastructure

Commit `17b0208` extends the existing phase replay command with Executor-only
replay. It reconstructs source identity and committed transitive upstream proof
terms from a frozen case state, then records actual provider responses, atomic
tool calls/errors, tokens, latency, submitted proof terms, and a semantic
fingerprint.

This is development infrastructure only. Relative to the frozen tag, production
Runtime has five changed lines: one counter on the existing Executor conversation
and one update from the SDK result's physical responses.

## Four rounds

| Round | Change | Random samples | Result | Decision |
|---|---|---|---|---|
| 1 | Replace atomic `bind_claim` with a per-item batch wrapper | 0020 line extensions; 0016 document role | 0020 calls 11→13, tokens 325,980→324,638, errors 12→22; 0016 calls 4→5, tokens 49,764→54,389 | Reverted before commit. Correct proof coverage remained, but round trips/errors worsened. |
| 2 | Batch independent same-level Witness calculations while retaining atomic validation/telemetry | 0006 line extensions; 0053 document role | 0006 calls 8→7, tokens 114,317→109,840 with 6/6 Claims/Witnesses and zero errors; 0053 calls 4→4, tokens 46,167→46,877 | Initially committed as `908f865`; later reverted by `ad0bb36` because it did not clear the full 0053 Stop Gate. |
| 3 | Remove model-visible `list_sources` because `source_catalog` is present | 0044 components; 001 currency/tax | Aggregate calls 13→12, tokens 193,736→178,509, errors 5→4; 0044 added two useful qualifiers | Initially committed as `82bd74b`; later reverted by `f69afc5` after the full 0053 regression. Local isolation was stochastic and did not establish safety. |
| 4 | Replace forced double `submit_check` with one required pre-submit acknowledgement | 0014 and 0062 document roles | 0014 calls 4→3 and tokens -28.7%; 0062 calls 4→6, tokens +70.7%, with two new quote errors | Reverted before commit. Moving coverage review before a candidate made behavior less convergent. |

Round 1 demonstrated that batching exact quotes concentrates bad guesses but
does not remove the recovery turn. Round 4 demonstrated that the existing
post-candidate review is expensive but more stable than open-ended pre-submit
review. Neither mechanism should be reintroduced without a stronger controlled
corpus.

## Full Business Eval gate

| Case/run | Score | Business | Calls | Total/cached tokens | Duration | Tool errors | Vetoes |
|---|---:|---|---:|---:|---:|---:|---:|
| 0053 `run_eval_20260824T201212_128920Z` | 89 | FAIL | 52 | 984,471 / 761,216 | 416,056 ms | 9 | 0 |
| 0025 `run_eval_20260824T201215_382341Z` | 99.50 | PASS | 52 | 870,772 / 660,480 | 437,349 ms | 3 | 0 |
| 0053 `run_eval_20260824T202608_238146Z` | 89 | FAIL | 60 | 1,054,796 / 824,448 | 489,150 ms | 13 | 0 |

Both 0053 runs had the correct root business outcome, replayable Witnesses,
zero vetoes/False Strong, successful report generation, and successful runtime
completion. Both failed the same core evidence check: the Executor grounded the
printed subtotal from `The subtotal including VAT is 135.234,00 EUR.` but did
not submit `including VAT` as an independently referenceable Claim, so final
total lineage lacked `tax_inclusive`.

The omission also reproduced in Executor-only replay with the original atomic
Witness tool, so the evidence does not identify Witness batching as its root
cause. Conversely, one replay with `list_sources` restored included the Claim
while a later replay with the same surface omitted it, so the evidence also does
not establish `list_sources` as causal. No business-specific fix was added.

Because no runtime optimization passed the full gate, all production behavior
changes were reverted. The final branch contains only replay/measurement
infrastructure; operational behavior remains at the frozen rollback point.

## Verification

- Executor replay/runtime targeted suite: 89 passed.
- Full offline suite: 1006 passed, 2 pre-existing failures, 8 warnings.
- The two failures match the frozen baseline note: stale prompt-prefix hashes,
  and the capability test expecting `max_retries=1` while deterministic Report
  Writer uses `max_retries=0`.
- Ponytail diff audit: no removable abstraction or dependency found. The frozen
  artifact-to-sandbox hydration is kept inside the development script instead
  of creating a production replay layer.

## Decision

Freeze the experiment branch for evidence and future A/B work, but do not merge
an Executor behavior optimization from these four rounds. The next efficiency
experiment should use repeated samples per frozen CHECK and treat qualifier
coverage as a hard semantic control, rather than relying on one run per case.
