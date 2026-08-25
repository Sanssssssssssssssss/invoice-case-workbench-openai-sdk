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

## Six rounds

| Round | Change | Random samples | Result | Decision |
|---|---|---|---|---|
| 1 | Replace atomic `bind_claim` with a per-item batch wrapper | 0020 line extensions; 0016 document role | 0020 calls 11→13, tokens 325,980→324,638, errors 12→22; 0016 calls 4→5, tokens 49,764→54,389 | Reverted before commit. Correct proof coverage remained, but round trips/errors worsened. |
| 2 | Batch independent same-level Witness calculations while retaining atomic validation/telemetry | 0006 line extensions; 0053 document role | 0006 calls 8→7, tokens 114,317→109,840 with 6/6 Claims/Witnesses and zero errors; 0053 calls 4→4, tokens 46,167→46,877 | Initially committed as `908f865`; later reverted by `ad0bb36` because it did not clear the full 0053 Stop Gate. |
| 3 | Remove model-visible `list_sources` because `source_catalog` is present | 0044 components; 001 currency/tax | Aggregate calls 13→12, tokens 193,736→178,509, errors 5→4; 0044 added two useful qualifiers | Initially committed as `82bd74b`; later reverted by `f69afc5` after the full 0053 regression. Local isolation was stochastic and did not establish safety. |
| 4 | Replace forced double `submit_check` with one required pre-submit acknowledgement | 0014 and 0062 document roles | 0014 calls 4→3 and tokens -28.7%; 0062 calls 4→6, tokens +70.7%, with two new quote errors | Reverted before commit. Moving coverage review before a candidate made behavior less convergent. |
| 5 | Return compact successful Claim/Witness receipts to the model while retaining lossless internal telemetry and error feedback | 0062 final total; 0053 line extensions | Aggregate calls 17→21, tokens 375,587→511,249, errors 3→5. 0053 improved slightly, but 0062 calls 7→12 and gained `WITNESS_CHECK_MISMATCH` | Reverted before commit. Removing repeated success detail did not remove the full conversation replay and made recovery variance dominate the small receipt saving. |
| 6 | Require the second post-candidate review only for CHECKs with facet, semantic-role, or policy contracts | 0062 document role; 0006 document role | Aggregate calls 10→6, tokens 111,532→67,833, latency 31,505→23,211 ms, errors 1→0; both retained document type, invoice number, and buyer | Retained. It deletes one protocol-mandated round trip only where no typed business contract needs the extra coverage review. |

Round 1 demonstrated that batching exact quotes concentrates bad guesses but
does not remove the recovery turn. Round 4 demonstrated that the existing
post-candidate review is expensive but more stable than open-ended pre-submit
review. Neither mechanism should be reintroduced without a stronger controlled
corpus.

## Round 5: compact success receipts

The model-visible success response for `bind_claim` and `compute_witness` was
temporarily reduced to the identifiers and values needed by later tools. The
full result still went to Runtime telemetry, and every rejected tool response
remained lossless. This was a direct test of whether repeated successful tool
objects were the dominant input-token cost.

| Sample | Variant | Calls | Tools/errors | Proof terms | Total/cached tokens | Latency |
|---|---|---:|---:|---:|---:|---:|
| 0062 final total | baseline | 7 | 8 / 0 | 4 Claims, 3 Witnesses | 202,635 / 173,696 | 31,592 ms |
| 0062 final total | compact | 12 | 13 / 1 | 4 Claims, 4 Witnesses | 354,767 / 341,888 | 38,622 ms |
| 0053 line extensions | baseline | 10 | 25 / 3 | 9 Claims, 9 Witnesses | 172,952 / 151,296 | 52,745 ms |
| 0053 line extensions | compact | 9 | 26 / 4 | 9 Claims, 9 Witnesses | 156,482 / 139,904 | 48,811 ms |

The 0053 proof fingerprint changed only in generated ids/wording and its
normalized Claim/Witness contents were equivalent. The 0062 compact run added
a different subtotal path and hit `WITNESS_CHECK_MISMATCH`; five extra provider
responses then outweighed the bytes removed from successful receipts. Across
both samples, calls increased 23.5%, tools 18.2%, and tokens 36.1%.

This does not prove that verbose receipts are always optimal. It proves that
receipt compaction alone is not a stable optimization here: each provider turn
still replays the much larger source and accumulated session, while a single
recovery branch costs more than all receipt savings. The change was removed.

## Round 6: typed stop-hook boundary

The prior Runtime forced every CHECK to submit twice, even a document-role
CHECK with no facet, semantic-role, or policy contract. Round 6 keeps the
existing post-candidate review unchanged for typed business CHECKs and lets a
structurally simple CHECK finish after its first accepted submission.

| Sample | Variant | Calls | Tools/errors | Claims | Total/cached tokens | Latency |
|---|---|---:|---:|---:|---:|---:|
| 0062 document | baseline | 5 | 8 / 1 | 3 | 56,865 / 45,056 | 15,927 ms |
| 0062 document | selective review | 3 | 7 / 0 | 4 | 34,706 / 21,632 | 12,264 ms |
| 0006 document | baseline | 5 | 8 / 0 | 4 | 54,667 / 45,440 | 15,578 ms |
| 0006 document | selective review | 3 | 6 / 0 | 4 | 33,127 / 22,528 | 10,947 ms |
| 0016 invoice/credit-note taxonomy control | baseline | 4 | 8 / 0 | 4 | 49,764 / 38,272 | 13,511 ms |
| 0016 invoice/credit-note taxonomy control | selective review | 3 | 6 / 0 | 3 | 33,702 / 23,552 | 10,238 ms |

Both samples preserved the CHECK's decisive observations: `INVOICE`, invoice
number, and buyer. Non-decisive metadata varied stochastically (date versus
supplier, and one extra line-item summary), but no required document fact was
lost. Aggregate provider calls fell 40.0%, tokens 39.2%, and latency 26.3%.

A targeted positive control then replayed 0016, whose source explicitly states
both `INVOICE` and `CREDIT NOTE`. The one-submit path preserved both taxonomy
levels plus the credit-note number while reducing calls 4→3 and tokens
49,764→33,702. This guards against accepting the optimization only on ordinary
single-label invoices.

The retained implementation changes no model role or proof semantics. It uses
the existing Plan fields to decide whether the existing second review has typed
work to perform. One regression check proves that a contracted CHECK still
requires two accepted submissions while a simple CHECK stops after one.

## Why five directions did not produce a retained optimization

The four-round report initially looked like “no effect” because it mixed three
different outcomes:

1. Rounds 1 and 4 were genuine local regressions. They changed how the model
   searched: batching concentrated quote mistakes, while pre-submit review
   encouraged open-ended collection before a valid candidate existed.
2. Rounds 2 and 3 showed small local efficiency wins, but the full 0053 run
   still omitted the independent `tax_inclusive` Claim. That is a semantic
   attention failure, not a Witness batching or source-listing failure, so the
   Stop Gate correctly refused to attribute safety to either optimization.
3. Round 5 optimized bytes inside a turn, while the dominant unit of cost was
   the turn itself. A new recovery branch caused five additional provider
   responses and erased the saving.

The common mistake was treating tool-call count as equivalent to provider-turn
count. DeepSeek can already emit multiple tool calls in one response. Combining
or shortening tools therefore does not guarantee fewer model responses; when
an atomic validation fails, the whole accumulated session is sent again.

Round 6 worked because it removed a deterministic, Runtime-required response
without weakening a typed proof boundary. The measured rule for future work is
therefore:

> Remove protocol-mandated model turns where the postcondition is already
> deterministic; do not save tokens by hiding evidence or moving semantic
> review earlier in the model's search.

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

The full Business Eval gate above belongs to rounds 2–3 and explains why those
changes were reverted. Round 6 was evaluated only on frozen Executor payloads:
its structural predicate leaves every 0053 arithmetic CHECK on the original
double-review path, so no additional full case or full benchmark was spent on
this narrow experiment.

## Verification

- Executor replay/runtime targeted suite after round 6: 89 passed.
- Full offline suite: 1006 passed, 2 pre-existing failures, 8 warnings.
- The two failures match the frozen baseline note: stale prompt-prefix hashes,
  and the capability test expecting `max_retries=1` while deterministic Report
  Writer uses `max_retries=0`.
- Ponytail diff audit: no removable abstraction or dependency found. The frozen
  artifact-to-sandbox hydration is kept inside the development script instead
  of creating a production replay layer.

## Decision

Retain round 6 only. Rounds 1, 4, and 5 were removed immediately; rounds 2 and
3 remain explicitly reverted. The branch therefore contains the replay and
measurement infrastructure plus one bounded Runtime optimization, with no new
Agent, dependency, persisted state, policy, scorer, or business special case.

Do not continue random Executor prompt/tool reshaping from this evidence. The
next experiment should target provider-turn elimination using repeated frozen
CHECK samples and hard qualifier controls. A full Business Benchmark is not
justified until a change improves several complex CHECKs without increasing
tool errors or weakening accepted Claim/Binding/Witness coverage.
