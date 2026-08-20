---
license: mit
task_categories:
  - question-answering
language:
  - en
tags:
  - benchmark
  - invoices
  - arithmetic
  - structured-extraction
  - enterprise-ai
  - llm-evaluation
  - number-formatting
  - vat
  - financial-documents
  - document-understanding
  - vision
  - multimodal
  - ocr
pretty_name: InvoiceBenchmark
size_categories:
  - n<1K
configs:
  - config_name: default
    data_files:
      - split: test
        path: "output/manifest.csv"
---

# InvoiceBenchmark

**200 synthetic invoices with cent-perfect ground truth, designed to measure the one thing language models are supposed to be able to do: read a number.**

## The Pitch

Invoice processing is the use case every enterprise AI pitch deck opens with. The numbers are either right or wrong, and the distance between right and wrong can be measured to the cent. This dataset exists because we ran the experiment and discovered that the gap between "this looks easy" and "this actually works" is wider than the industry would like to admit.

Five open-weight models. Four architectures. The best one scored 83%. The largest one scored 77%. The reasoning models performed worse than the plain models at every size. The full write-up is at [jngb.online/notes/06-too-dangerous-to-release](https://www.jngb.online/notes/06-too-dangerous-to-release).

## What's in the Box

| Component | Path | Format | Count |
|---|---|---|---|
| Invoices (text) | `output/invoices/` | Markdown | 200 |
| Invoices (visual, PDF) | `output/pdf/` | PDF | 200 |
| Invoices (visual, PNG) | `output/png/` | PNG | 200 |
| Ground truth | `output/ground_truth/` | JSON | 200 |
| Manifest | `output/manifest.csv` | CSV | 1 |
| Distribution summary | `output/summary.json` | JSON | 1 |
| Generator | `invoice_generator.py` | Python | 1 |
| Evaluation harness | `run_benchmark.py` | Python | 1 |
| Prompts | `prompts/` | Text | 2 |

Each invoice exists in two formats. The **Markdown version** is plain text — the kind of thing you would paste into a prompt. The **PDF version** is a rendered, styled document — the kind of thing a multimodal or vision model would receive as an image. Both formats share the same ground truth: same numbers, same structure, same controlled dimensions.

Each ground-truth JSON records the canonical correct values, the variant parameters that control how the invoice was constructed, and (for error-injected invoices) both the correct and the deliberately-wrong number.

All monetary values use Python's `Decimal` with `ROUND_HALF_UP` rounding. No floating-point arithmetic touches the money pipeline. The total is correct to the cent unless it has been deliberately broken.

## The Five Dimensions

Every invoice varies along five controlled axes. The point is not to produce "hard" invoices — it is to hold everything else constant and vary one thing at a time, so that when a model fails, the failure is attributable.

**VAT phrasing** (4 variants): `explicit_included` — prices include VAT, the model must not double-count. `explicit_excluded` — VAT added on top. `implicit_rate_stated` — rate visible, inclusion ambiguous. `implicit_no_rate` — VAT amount shown, rate omitted.

**Discount phrasing** (5 variants): `none` — no discount, a sanity check for models that invent one. `explicit_percentage` — "5% early payment discount applied". `explicit_amount` — a fixed rebate. `trade_terms` — "2/10 net 30", which is **conditional** and should NOT be applied. `obfuscated` — the percentage buried inside a reference string.

**Number format** (3 variants): `english` (1,234.56), `german` (1.234,56), `swiss` (1'234.56). The German format is the single most consequential trap in the dataset. A model that reads `1.234` as 1.234 rather than 1,234 will be off by three orders of magnitude — and the resulting number will still look plausible.

**Layout** (3 variants): `table` (Markdown pipe-table), `paragraph` (prose), `mixed` (table for line items, prose for summary).

**Consistency** (3 variants): `correct` (60%) — the invoice adds up. `subtotal_error` (20%) — the stated subtotal is wrong by ±5 to ±50 EUR. `total_error` (20%) — the stated total is wrong by ±1–3%. The model should flag these.

A sixth dimension covers **edge cases** (10% of the corpus): `reverse_charge` (0% VAT, Article 196), `mixed_vat` (two VAT rates), `credit_note` (negative amounts), `single_item` (one line, nowhere to hide).

## Baseline Results

Tested in May 2026 on five open-weight models. Two evaluation conditions: **autopilot** (the model reads the invoice and reports the total) and **hybrid** (the model extracts structured fields, Python recomputes the total).

| | Llama 3.1 8B | Qwen3 8B | Gemma 4 31B | QwQ 32B | Llama 3.3 70B |
|---|:---:|:---:|:---:|:---:|:---:|
| Parse rate | 99% | 57% | 100% | 81% | 100% |
| Exact match (autopilot) | 69% | ~85%* | 83% | ~73%* | 77% |
| Exact match (hybrid) | 43% | ~77%* | 83% | ~77%* | 81% |
| Wrong but would pass review | 23% | ~13%* | 18% | ~11%* | 16% |
| Error detection rate | 18% | 45% | 83% | 38% | 75% |
| Worst single error | 99.9% | 100%* | 3% | 100%* | 99.9% |
| Time per invoice | 25s | 32s | 19s | 112s | 3s |
| Hardware | MacBook Air | 1× H100 | 1× H100 | 1× H100 | 8× H100 |
| Running cost | Free | €2.73/hr | €2.73/hr | €2.73/hr | €23/hr |

*\* Of the invoices where the model produced parseable output.*

Three findings that may save someone a weekend:

1. **Reasoning models are worse.** At 8B, the reasoning variant failed to produce parseable output 43% of the time. At 32B, it reasoned its way to €0.00 on twenty-two invoices. Thinking longer about an invoice does not produce a better answer.

2. **Bigger is not better — at least not here.** Llama 70B on eight GPUs lost to Gemma 31B on one. Nine times the parameters, eight times the cost, worse results.

3. **The German comma is the most expensive punctuation mark in Europe.** An invoice for €364,065.64, formatted as `364.065,64`, comes back as €363.07. The error is invisible in the right typeface. No amount of parameters retrains the prior.

## How to Run

### Generate the corpus

```bash
python invoice_generator.py --output ./output --count 200 --seed 42
```

Fully deterministic. Python 3.10+, no external dependencies.

### Verify the ground truth

```bash
python invoice_generator.py --output ./output --verify
```

### Run the benchmark

```bash
# Local model via Ollama
python run_benchmark.py --models llama3.1:8b

# Remote model via vLLM
python run_benchmark.py --models vllm:google/gemma-4-31b-it --vllm-url http://gpu-server:8000/v1

# Multiple models, single condition
python run_benchmark.py --models llama3.1:8b,qwen3:8b --conditions B
```

Results are written to `results/` as timestamped CSVs. The harness supports two conditions: **B** (autopilot — the model does everything) and **C** (hybrid — the model extracts, Python calculates).

### Scoring

Exact match means the model's reported total equals the ground-truth total to the cent. "Wrong but close enough" means the answer is within 5% — the kind of error that sails through manual review. Both are worth tracking. The first tells you whether the model works. The second tells you how dangerous it is when it doesn't.

## Data Format

### Invoice (Markdown)

A plain-text rendering of a European B2B invoice. Company names are fictional (Pierce & Pierce, Vandelay Industries, Cyberdyne Systems, Wonka Industries, and so on). Street addresses are invented. IBANs have correct country prefixes and lengths but random digits — they are not real bank accounts.

### Invoice (PDF)

The same invoice, rendered as a styled single-page PDF (and converted to 200 DPI PNG) with a line-item table, header block, and summary section. The PNG versions are the recommended input for vision models — most inference engines (including Ollama) accept PNG/JPG but not PDF. German number formatting (`1.234,56`), Swiss formatting (`1'234.56`), and English formatting (`1,234.56`) are all preserved visually — the model must read the numbers from the rendered document, not from parsed text.

Use the PDF versions to benchmark multimodal and vision-language models on document understanding. The text versions test reading comprehension; the PDF versions test whether the model can extract the same information when it has to *see* the invoice instead of *read* it.

### Ground Truth (JSON)

```json
{
  "invoice_id": "INV-2026-0042",
  "vendor": "Pierce & Pierce Holdings Ltd",
  "subtotal": "7542.50",
  "vat_rate": "0.20",
  "vat_amount": "1508.50",
  "discount": {
    "type": "percentage",
    "value": "0.05",
    "applied_to": "subtotal",
    "conditional": false
  },
  "discount_amount": "377.13",
  "total": "8673.87",
  "variants": {
    "vat_variant": "explicit_excluded",
    "discount_variant": "explicit_percentage",
    "number_format": "english",
    "layout": "table",
    "consistency": "correct",
    "edge_case": "none"
  },
  "rendered_subtotal": "7542.50",
  "rendered_total": "8673.87",
  "error_note": null
}
```

All monetary values are two-decimal strings. The distinction between `total` and `rendered_total` matters: for error-injected invoices, `rendered_total` is what the model sees, and `total` is the correct answer.

## Known Limitations

All invoices are rendered in English regardless of the vendor's implied nationality. A bilingual corpus (English/German) would be a natural extension.

A single VAT rate of 20% is used throughout the main corpus. Country-specific rates (19% DE, 21% NL, 25% NO) are not modelled.

The corpus is 200 invoices by design — dense enough for per-dimension signal, small enough to run against expensive models. Scale to any size with `--count`.

Company names are drawn from films, TV, and novels. If you are benchmarking a model that was trained on Office Space quotes, Initech Solutions GmbH may be easier to parse than it should be.

## Citation

If you use this dataset, please cite:

```bibtex
@misc{invoicebenchmark2026,
  title={InvoiceBenchmark: A Controlled Corpus for Measuring LLM Invoice Processing Accuracy},
  author={Neugebauer, Jakob},
  year={2026},
  url={https://www.jngb.online/notes/06-too-dangerous-to-release},
  note={200 synthetic invoices varying across five controlled dimensions with cent-perfect ground truth}
}
```

## Licence

MIT. Use it, break it, publish your results, tell people about it.
