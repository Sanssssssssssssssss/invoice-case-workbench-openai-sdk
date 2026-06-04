# Evidence Reviewer Tuning Validation

Date: 2026-05-25

## Reference Patterns Used

- `invoice-x/invoice2data`: template matching should use positive/negative keywords, required fields, field parsers, and line/table boundaries. The useful lesson for this project is not to add regex templates, but to make the reviewer produce a field ledger with source quotes, locators, confidence, and required-field checks.
- `SAP-samples/btp-cap-dox-invoice-validation`: invoice validation should preserve extracted values, corrections, and rationale over time. The useful lesson here is to keep reviewer notes, field matrices, profile comparison, and self-check metadata instead of returning only a verdict.

## Fixed Session

Case id: `fixed_invoice_reviewer_tuning`

Files used:

- `samples/cases/invoice_reference_originals/flipkart_invoice2data.pdf`
- `samples/cases/invoice_reference_originals/sap_invoice_5435569865439.pdf`
- `samples/cases/invoice_reference_originals/mouadhamri_FACTU2015020048.jpg`

Final generated files:

- `workspace/cases/fixed_invoice_reviewer_tuning/reports/final_report.md`
- `workspace/cases/fixed_invoice_reviewer_tuning/reports/final_report.pdf`

## Iterations

1. Flipkart PDF then SAP PDF in the same case exposed state pollution: reviewer correctly said SAP was a different case, but still emitted full supports for active Flipkart requirements. Case state became too optimistic.
2. Added reviewer/patch-writer cross-case rules and a narrow CaseStore truth-source guard. Cross-case evidence is now recorded but has empty supports and does not update active requirements.
3. The first guard was too broad because `metadata.self_check.cross_case_check=pass` triggered it. Narrowed the guard to explicit cross-case classification, conflicts, and clear cross-case wording.
4. Planner final reply initially described cross-case evidence as merged into the case. Planner prompt now requires cross-case replies to say the material is recorded as out-of-scope and not used for active requirements.
5. Report/PDF generation route was too easy for Planner to skip. A report/PDF route gate was moved into the Planner core contract, and route contract enforcement already forces `report_writer -> write_case_file -> render_pdf`.
6. Planner failed final JSON once because it repeated a user-specified banned phrase in unescaped double quotes. Planner prompt and retry instruction now forbid literal double quotes inside JSON text fields and instruct it to avoid repeating user-provided banned phrases.
7. PDF snapshots originally came from recent attachment artifacts, so evidence ids could drift from images. Renderer now prefers `case_state.evidence_items[].metadata.original_ref/preview_paths`, preserving evidence id to source image mapping.
8. Report Writer put inactive AP controls in `风险速览`. Prompt now bans inactive AP controls from the risk section and keeps them in `缺口与限制`.

## Final Observed Chain

Final report regeneration action chain:

`call_role:report_writer -> call_tool:write_case_file -> call_tool:render_pdf -> final_answer`

Final state:

- `status=ready_for_report`
- `evidence_count=3`
- Active requirements are satisfied only by `ev_001`
- `ev_002` and `ev_003` are cross-case records, not active requirement support
- risk flags include `cross_case_or_mixed_case_document`, `signature_block_not_visible_in_ocr`, and `moderate_ocr_quality`

## Remaining Notes

- Shell-based Chinese test strings can be corrupted by PowerShell pipe encoding; app/UI input should not have this issue. For shell automation, use UTF-8 files or escaped strings.
- v1 still uses full-page previews, not field-level crops. The reviewer now records field locators and confidence, which is the right base for later crop work without adding a new agent.
