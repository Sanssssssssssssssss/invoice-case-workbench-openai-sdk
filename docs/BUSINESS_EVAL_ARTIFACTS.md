# Business Eval artifact SOP

## Storage contract

- `output/business_benchmarks/` is the only canonical root for new Business Eval artifacts.
- Single-case runs go under `output/business_benchmarks/runs/`; benchmark summaries go under `output/business_benchmarks/benchmarks/`.
- Use a new run directory for every live run. Never overwrite or mix two runs.
- Keep `snapshot.json`, every score revision, `eval_report.md`, reports, traces, and provider metrics together inside that run.
- Record the Git commit, provider/model, reasoning mode, case IDs, and command in the run label or accompanying benchmark report.

The runner defaults already implement this contract. Prefer them over ad-hoc output roots:

```powershell
$env:PYTHONPATH='.;backend'
python backend/scripts/run_business_eval.py <case-id>
python backend/scripts/run_business_benchmark.py <case-id> [<case-id> ...]
```

Offline rescoring must point at an existing canonical snapshot and write only a score revision. It must not be presented as a fresh model run.

## Disposable test data

The following directories are local, reproducible test workspaces and must never be treated as Eval evidence:

- `.bt/` and `backend/.bt/`
- `.pytest-*/` at the repository root or under `backend/`
- `.pytest_cache*/` and `.tmp_pytest_cache*/`
- `.rpv*/`
- `backend/.ttr*/`

Delete them only after tests have stopped. Resolve every target first and confirm it is inside the repository. Never delete tracked paths, `output/business_benchmarks/`, or a directory selected only by a broad recursive wildcard.
If a legacy cache has broken ACLs, record and ignore it rather than changing permissions or forcing deletion during routine cleanup.

## Legacy evidence

`o/` contains historical Eval evidence created before the canonical root was adopted. It is **read-only legacy data pending a manual archive decision**. Do not delete, move, rename, or rewrite it during routine cleanup.

## Cleanup gate

1. Confirm the current work is committed and pushed; record `git rev-parse --short HEAD`.
2. Inventory candidate paths, sizes, and latest write times.
3. Preserve canonical and legacy evidence using the rules above.
4. Delete only verified disposable test directories.
5. Run deterministic targeted tests, then inspect `git status --short`.
6. Run live Eval only when validating behavior changes; filesystem cleanup alone does not justify model calls.

If a directory's purpose is uncertain, leave it in place and add it to the cleanup report for manual review.
