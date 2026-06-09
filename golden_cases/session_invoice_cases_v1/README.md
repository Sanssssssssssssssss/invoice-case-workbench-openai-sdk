# Golden Session Invoice Cases v1

This folder contains the six reusable material-review cases copied from the legacy NewERPAgnent workspace.

Use each case as an isolated app session:

1. Create a new case in the desktop app.
2. Upload only the files in that case's `upload_to_app/` folder.
3. Ask the agent to review the invoice payment materials.
4. For report generation, approve `write_case_file` and `render_pdf` when prompted.

The `originals/` folders are kept for visual inspection, OCR experiments, and source provenance. They are not required for the normal text-readable app flow.

## Case List

- `case_01_clean_match_real_jpg_FACTU2015020048`: clean three-way match based on a real JPG invoice.
- `case_02_amount_conflict_real_jpg_FACTU2015040047`: invoice amount conflicts with PO/GRN.
- `case_03_duplicate_hit_real_jpg_FACTU2015050046`: duplicate payment check reports a historical payment.
- `case_04_bank_change_risk_real_jpg_FACTU2015060039`: supplier bank change risk without approval evidence.
- `case_05_clean_match_real_pdf_flipkart`: clean match based on a real Flipkart PDF invoice extract.
- `case_06_duplicate_hit_real_pdf_sap`: duplicate risk based on a real SAP sample PDF extract.

## Folder Contract

- `case_index.json`: machine-readable case list.
- `<case>/README.md`: short case-specific note from the legacy workspace.
- `<case>/upload_to_app/`: files to upload to the app for the standard review flow.
- `<case>/originals/`: raw source invoice files and sidecars, kept for inspection only.
