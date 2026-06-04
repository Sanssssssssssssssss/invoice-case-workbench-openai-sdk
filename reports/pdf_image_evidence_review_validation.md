# PDF/JPG Evidence Reviewer Upgrade Validation

Date: 2026-05-24
Branch: `codex/flow-trace-prompt-validation`

## Scope

This validation covers the PDF/JPG evidence-review upgrade without adding a new agent, route, or Planner flow. The implementation keeps `read_attachment` as the document ingestion boundary, keeps Evidence Reviewer as the field extraction and review role, and keeps `render_pdf(markdown_path, pdf_path)` as the report rendering interface.

## Implemented Changes

- `read_attachment` now supports text files, PDFs, and image files (`jpg`, `jpeg`, `png`, `tif`, `tiff`, `webp`, `gif`, `bmp`).
- PDF ingestion uses PyMuPDF text extraction, renders page previews, and falls back to local Tesseract OCR when extracted text is sparse.
- Image ingestion uses local Tesseract OCR and stores the copied original in the case workspace.
- Attachment artifacts now include `original_ref`, `preview_paths`, `content_kind`, `extraction_method`, `warnings`, and per-file success/error status.
- Mixed attachment batches keep successful files and record failed files; only all-failed batches become terminal runtime feedback.
- Evidence Reviewer now loads `pdf_image_skill.md` with stricter PDF/OCR rules: original traceability, field extraction before review, no bundle evidence for multiple core materials, and conservative support levels for OCR or indirect references.
- Report PDF rendering now recovers originals and PDF previews from recent attachment artifacts, so final PDFs can include original-style evidence snapshots.

## Real LLM Session

Case root:
`E:\GPTProject2\NewERPAgnent\workspace\cases\eval_pdf_image_evidence_20turns_utf8`

Final report:
`E:\GPTProject2\NewERPAgnent\workspace\cases\eval_pdf_image_evidence_20turns_utf8\reports\final_report.md`

Final PDF:
`E:\GPTProject2\NewERPAgnent\workspace\cases\eval_pdf_image_evidence_20turns_utf8\reports\final_report.pdf`

Visual preview pages:

- `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_pdf_image_evidence_20turns_utf8\reports\assets\pdf_preview\page_01.png`
- `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_pdf_image_evidence_20turns_utf8\reports\assets\pdf_preview\page_09.png`
- `E:\GPTProject2\NewERPAgnent\workspace\cases\eval_pdf_image_evidence_20turns_utf8\reports\assets\pdf_preview\page_15.png`

The UTF-8 real session ran 20 planned Chinese user turns, then two recovery turns to reproduce and verify a report-guard failure. The planned turns covered: start case, JPG invoice, five-piece evidence, amount conflict, duplicate-payment hit, bank-change risk, Clear Invoice process log, wrong workflow PR materials, prompt-injection attachment, corrupt PDF, unsupported file, low-quality OCR image, text PDF invoice, report generation, batch 20 attachments with 5 relevant files, RAG policy versus user statement, and final PDF generation.

Final case-state summary from the session:

- `evidence_count`: 31
- Requirement statuses: invoice `conflict`, purchase_order `conflict`, goods_receipt `conflict`, vendor_record `weak`, duplicate_payment_check `conflict`
- `risk_flags`: 35
- Final PDF render result after the artifact collection fix: `page_count=15`, `evidence_snapshot_count=12`

## Observations And Fixes

- The first pressure-test attempt corrupted Chinese text in PowerShell output (`????`). The run was discarded for language validation and rerun with explicit UTF-8 console and `PYTHONIOENCODING=utf-8`.
- Report Writer initially triggered the execution-wording guard by listing unsupported unsafe phrases inside a limitation table. The prompt and PDF skill now require neutral labels such as `执行/审批/过账状态结论不受支持`, and the markdown sanitizer neutralizes those phrases before guard validation.
- After a terminal report guard error, the route contract could still force `write_case_file` for a fresh report request. RuntimeController now respects terminal runtime feedback before forcing report file writes.
- PDF evidence snapshots originally came only from the latest attachment artifact, which missed earlier original files. The renderer now collects successful candidate attachments across recent `read_attachment` artifacts and skips irrelevant batch files.
- The final visual PDF check showed risk-first structure, readable tables, and embedded original-style evidence snapshots without observed overlap or mojibake on the checked pages.

## Test Coverage

Commands run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_file_workspace.py backend/tests/test_prompt_loader.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests/test_guards.py backend/tests/test_runtime_layering.py backend/tests/test_file_workspace.py backend/tests/test_prompt_loader.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests/test_file_workspace.py backend/tests/test_guards.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Observed results:

- 15 passed for attachment and prompt-loader tests.
- 58 passed for runtime feedback, guard, attachment, and prompt-loader tests.
- 33 passed for attachment and guard tests after the final PDF snapshot/report guard fixes.
- 108 passed for the full backend test suite.

## Remaining Risks

- OCR is English-only by default (`eng/osd` available locally). Chinese OCR is not part of this v1 validation.
- Evidence Reviewer does not always persist `original_ref` into case evidence fields, so the PDF renderer recovers original source images from attachment artifacts.
- Low-quality OCR evidence is intentionally conservative and should remain `partial` or `none` unless field completeness and source quotes are clear.
- The final PDF embeds up to 12 relevant source snapshots. Very large cases may need a later pagination/index strategy, but the current implementation keeps the code path simple.
