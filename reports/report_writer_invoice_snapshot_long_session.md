# Report Writer Invoice Snapshot Long-Session Validation

Date: 2026-05-24
Case ID: `eval_report_invoice_screenshot_long_session`

## What Changed

- Report Writer prompt now requires `证据截图索引` to prioritize invoice JPG/PNG/PDF preview rows before PO, GRN, vendor, and duplicate-payment evidence.
- Report Writer PDF skill now states that invoice image/PDF sources should be visible early in the PDF original-snapshot section.
- PDF renderer now sorts evidence snapshots by material type and puts real invoice images/PDF previews first.
- PDF renderer skips `prompt_injection` source screenshots so unsafe attachment instructions do not appear verbatim in final reports.
- Planner prompt now treats `后续/稍后/最后再生成报告` as future intent, not current report generation.
- Runtime report-intent detection now respects negated requests such as `不要生成报告` and location-only questions such as `报告在哪里...是否包含截图`.
- Planner action trace fields are compacted by schema: `short_plan` max 3 items, `plan_progress` keeps `intent/route/step`, and `reason` is capped.

## Real LLM Session

The same case/session was used across 22 Chinese turns:

- Turns 1-10: created case, submitted a real JPG invoice, then PO, GRN, vendor record, duplicate-payment check, Clear Invoice process log, and prompt-injection attachment.
- Turns 11-12: submitted unsupported DOCX and corrupt PDF; runtime feedback returned terminal final answers without max-step loops.
- Turn 13: verified `不要生成报告` no longer starts report generation after the fix.
- Turn 14: generated Chinese final Markdown/PDF.
- Turn 15 exposed a bug: `报告在哪里 + 是否包含截图` was treated as report generation.
- Turns 16-18: added text-PDF invoice, checked long-session contamination, and submitted a mixed batch with irrelevant files.
- Turn 19 regenerated the PDF with screenshot ordering requirements.
- Turns 21-22 verified the fixes: location-only did not regenerate, and risk summary did not reuse the low-credibility prompt-injection invoice number.

Final report:

- Markdown: `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_report_invoice_screenshot_long_session\reports\final_report.md`
- PDF: `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_report_invoice_screenshot_long_session\reports\final_report.pdf`

Visual preview:

- Page 1: `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_report_invoice_screenshot_long_session\reports\assets\pdf_preview_latest\page_01.png`
- Invoice screenshot page: `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_report_invoice_screenshot_long_session\reports\assets\pdf_preview_latest\page_08.png`
- PDF invoice preview page: `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_report_invoice_screenshot_long_session\reports\assets\pdf_preview_latest\page_09.png`

Final PDF render result:

- `page_count=12`
- `evidence_snapshot_count=7`
- Snapshot order starts with:
  - `reports/assets/evidence_snapshots/01_FACTU2015050046.png`
  - `attachments/previews/invoice2data_FlipkartInvoice_p001.png`
  - `reports/assets/evidence_snapshots/03_02_purchase_order.png`

Final case-state summary:

- `evidence_count=10`
- `weak_materials=["invoice"]`
- `conflict_materials=["goods_receipt", "duplicate_payment_check"]`
- `risk_flags=16`

## Issues Found And Fixed

- False report generation on setup request: fixed by treating future report language as future intent unless the user asks to generate now.
- False report generation on `不要生成报告`: fixed by runtime negative-intent detection and Planner prompt rule.
- False report generation on `报告在哪里 + 是否包含截图`: fixed by narrowing location-only detection to explicit generation verbs.
- Invoice screenshot ordering bug: fixed by sorting on source filenames/content kind, not parent directory names or incidental `invoice` mentions in PO text.
- Clear Invoice screenshot ordering bug: fixed by classifying `clear_invoice/process_log` before generic invoice matching.
- Prompt-injection raw text appeared in PDF screenshots: fixed by excluding prompt-injection source files from automatic snapshot embedding.
- Planner trace was verbose: fixed by schema-level compaction and prompt output discipline.

## Verification

Commands run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_file_workspace.py backend/tests/test_guards.py backend/tests/test_runtime_layering.py backend/tests/test_prompt_loader.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests/test_file_workspace.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Observed results:

- 62 passed for runtime, file workspace, guard, and prompt-loader coverage.
- 14 passed for file workspace/PDF snapshot rendering coverage.
- 111 passed for the full backend suite.
