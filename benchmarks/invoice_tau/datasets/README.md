# InvoiceTauBench datasets

This directory is the canonical home for reusable, source-backed benchmark cases.

Every dataset keeps three boundaries separate:

- `manifest.json` records where the material came from, its license, hash, and lineage.
- `scenario.json` is the only case input intended for the Agent.
- `expected.json` and `oracle/` contain evaluator-only truth and must never enter model context.

Run structural validation with:

```powershell
python -m benchmarks.invoice_tau.dataset_validation benchmarks/invoice_tau/datasets/public_invoice_arithmetic_v1
```

The existing `scenarios/` directory remains for runtime, chat, report, and HITL regression tests. New evidence corpora belong here instead of creating another benchmark runner.
