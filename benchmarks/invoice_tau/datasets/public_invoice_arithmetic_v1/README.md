# Public Invoice Arithmetic Benchmark v1

This pack contains 50 unique public synthetic invoice PDFs with reproducible, cent-level oracle data.

It is the first document pack for the Evidence Compiler benchmark. Its scope is intentionally narrow:

- read a real PDF attachment;
- compile a natural-language payment-review request;
- ground invoice claims to the source;
- distinguish an arithmetically consistent invoice from a contradicted one;
- preserve trace, cost, and provenance expectations.

It does **not** claim to cover the complete ERP approval workflow. Purchase orders, goods receipts, payment lifecycle records, vendor master evidence, and approval controls will be added as separate packs under the same contract.

## Contents

- 50 cases and 50 unique PDFs.
- 30 `dev`, 10 `validation`, and 10 `holdout` cases.
- 20 `SUPPORTED` and 30 `CONTRADICTED` invoice-calculation truths.
- English, German, and Swiss number formats.
- Table, paragraph, and mixed layouts.
- Credit notes, reverse charge, mixed VAT, and single-item edge cases.
- Exact source revision, SHA-256, license evidence, natural-language prompt, oracle facts, trace expectations, and cost budget for every case.

All invoice entities are synthetic. They are public test documents, not real enterprise transactions.

## Case contract

```text
cases/<case_id>/
├── manifest.json
├── scenario.json
├── expected.json
├── attachments/
│   └── invoice.pdf
└── oracle/
    ├── source_ground_truth.json
    └── source_text.md
```

Only files listed by `scenario.json.user_script[].attach` may enter Agent context. The `oracle/` directory and `expected.json` are evaluator-only.

Each `expected.json` carries a unique `ORACLE_SENTINEL_DO_NOT_SEND` value. A later live-run adapter must assert that this sentinel never appears in prompts, tool inputs, or trace context.

## Source and license

The files are byte-identical copies from [jngb-labs/InvoiceBenchmark](https://huggingface.co/datasets/jngb-labs/InvoiceBenchmark) pinned to commit `f0699d8c94468fb2fefaa22f669684b22e68c3d3`.

The pinned dataset card declares the dataset MIT licensed. A copy of that card is stored at `upstream/InvoiceBenchmark_README.md` together with the full upstream manifest and distribution summary.

## Local upstream tools

The pinned upstream generator, original benchmark runner, and original prompts are stored under `upstream/`. `upstream/SOURCE.json` records their exact SHA-256 values.

On Windows, run the generator in UTF-8 mode because the unmodified upstream verifier reads JSON with the platform default encoding:

```powershell
python -X utf8 upstream/invoice_generator.py `
  --output tmp/invoicebenchmark-smoke `
  --count 200 `
  --seed 42 `
  --verify `
  --stats
```

The upstream generator emits Markdown invoices, JSON ground truth, a manifest, and a distribution summary. It does **not** render PDF or PNG files. The pinned upstream repository publishes 200 pre-rendered PDF and PNG files, but does not include the renderer that created them. For that reason this pack treats the published PDFs as immutable, hash-pinned source artifacts rather than claiming that their bytes can be regenerated locally.

The original `run_benchmark.py` remains available for reference, but it is not the evaluator for this pack. It measures invoice parsing, exact totals, error detection, runtime, and cost; InvoiceTauBench adds Requirement outcomes, source grounding, Compiler-stage trace expectations, forbidden strong conclusions, and oracle-leakage checks.

## Qualification state

The pack is `machine_qualified`:

- all 50 PDF signatures, byte sizes, and SHA-256 values are validated;
- all 50 PDFs parse as exactly one page and expose a non-empty text layer (minimum 613 extracted characters);
- all 50 source JSON oracles and exact source quotes are present;
- all 50 PDFs are unique by SHA-256;
- four representative cases were rendered and visually reviewed;
- full independent human review of all 50 cases has not yet been performed.

The four visual samples cover a Swiss total error, a German single-item invoice, a credit note, and mixed VAT.

## Validate

```powershell
python -m benchmarks.invoice_tau.dataset_validation benchmarks/invoice_tau/datasets/public_invoice_arithmetic_v1
```

Expected summary:

```json
{
  "case_count": 50,
  "unique_pdf_count": 50,
  "split_counts": {"dev": 30, "validation": 10, "holdout": 10}
}
```

## Rebuild

The reproducible builder is `backend/scripts/build_public_invoice_arithmetic_cases.py`. It uses a deterministic stratified selection from the pinned 200-document upstream manifest. The build keeps all 20 upstream edge cases, balances the three arithmetic states, and then balances format and layout dimensions.

Rebuilding requires the pinned upstream `README.md`, `output/manifest.csv`, `output/summary.json`, and the selected PDF/JSON/Markdown triples. The generated dataset must pass the validator before use.
