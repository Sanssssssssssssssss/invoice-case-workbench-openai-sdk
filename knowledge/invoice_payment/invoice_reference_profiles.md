# Invoice Reference Profiles and Case Examples

These profiles help evidence_reviewer compare submitted invoices with known examples. They are RAG guidance only. They are not case evidence unless the user submits the referenced original in the active case.

Each profile uses: `profile_id`, `source_examples`, `when_to_use`, `document_type`, `expected_fields`, `visual_layout_clues`, `validation_checks`, `pass_partial_fail`, `common_defects`, `advisor_guidance`, `evidence_boundary`, and `source_links`.

## Generic Single-Invoice Rubric

- profile_id: `generic_single_invoice_rubric`
- source_examples:
  - Google ADK invoice-processing rules book
  - Hugging Face InvoiceBenchmark
- when_to_use: Use when no exact invoice template/profile is retrieved.
- document_type: generic supplier invoice, retail invoice, service invoice, scanned invoice, PDF invoice
- expected_fields:
  - invoice number
  - supplier/seller
  - buyer/bill-to/customer
  - invoice date
  - subtotal, tax, total amount, currency
  - product/service line items
  - payment terms or due date when present
  - signature, authorized signatory, or electronic generation marker
  - source locator
- visual_layout_clues:
  - header with invoice/bill title and invoice number
  - supplier and buyer blocks
  - table or list of products/services
  - totals section near the end
  - signature or authenticity marker
- validation_checks:
  - First decide document type; do not assume every OCR text is an invoice.
  - Extract fields before judging support level.
  - Check arithmetic if numeric fields are visible.
  - Use retrieved profiles as comparison guidance, never as submitted evidence.
- pass_partial_fail:
  - full: original traceable invoice with readable key fields and no key conflict.
  - partial: OCR noise, crop, missing field, unclear signature/title, or weak source traceability.
  - none: RAG/profile/policy/process-only material or wrong document type.
- common_defects:
  - missing invoice number
  - missing buyer
  - unclear total or currency
  - line-item table cropped
  - signature/authenticity marker absent
  - OCR confusion between product code and invoice number
- advisor_guidance:
  - Ask for a complete original invoice image/PDF when fields are missing or OCR is weak.
  - Do not ask for PO/GRN/vendor/duplicate unless AP review is active.
- evidence_boundary:
  - This profile supports invoice-only review. It does not prove AP payment control completion.
- source_links:
  - https://github.com/google/adk-samples/blob/main/python/agents/invoice-processing/invoice_processing/data/reconstructed_rules_book.md
  - https://huggingface.co/datasets/jngb-labs/InvoiceBenchmark

## Profile: Flipkart Retail Invoice/Bill

- profile_id: `flipkart_retail_invoice_bill`
- source_examples:
  - `samples/cases/invoice_reference_originals/flipkart_invoice2data.pdf`
  - `samples/cases/invoice_reference_originals/flipkart_invoice2data.png`
  - `workspace/external_samples/github_invoice_real_files/invoice2data_FlipkartInvoice.pdf`
  - `workspace/external_samples/github_invoice_real_files/invoice2data_FlipkartInvoice.png`
- when_to_use: Use when invoice title, seller block, or source filename resembles Flipkart retail invoice/bill.
- document_type: retail invoice / bill
- expected_fields:
  - invoice number, often near the top right as `Invoice No`
  - seller / Sold By legal name, address, and tax identifiers
  - order id and order date
  - invoice date
  - billing address and shipping address
  - product/title line item, quantity, price, tax rate, tax amount, line total
  - grand total
  - authorized signatory or signature block
- visual_layout_clues:
  - title contains `Retail Invoice/Bill`
  - seller block on the left and invoice number on the right
  - billing and shipping addresses appear in parallel columns
  - product table includes Product, Title, Qty, Price, Tax, Total
  - grand total and signature block appear near the bottom
- validation_checks:
  - Confirm invoice number and order id are not confused with product id or tax id.
  - Check product/title row, quantity, price, tax, and grand total.
  - Check whether authorized signatory is visible or explicitly absent.
  - Check whether both billing and shipping addresses are readable.
- pass_partial_fail:
  - full: invoice number, seller, buyer/address, invoice date, product title, grand total, tax/currency, and signatory/source traceability are readable.
  - partial: top-right invoice number is cropped, signature block is unclear, tax id is missing, or OCR confuses line table values.
  - none: file is only a template/profile or unrelated retail text.
- common_defects:
  - cropped invoice number
  - missing seller tax identifiers
  - OCR confusion between product code and invoice number
  - missing authorized signatory image or unclear signature block
- advisor_guidance:
  - Ask for the full-page original if header or signature is cropped.
  - Ask for source PDF/image rather than user retyping totals.
- evidence_boundary:
  - A Flipkart profile match helps template comparison, but the submitted file must still provide source evidence.
- source_links:
  - invoice-x/invoice2data: https://github.com/invoice-x/invoice2data

## Profile: SAP DOX Invoice Validation Sample

- profile_id: `sap_dox_invoice_validation_sample`
- source_examples:
  - `samples/cases/invoice_reference_originals/sap_invoice_5435569865439.pdf`
  - `workspace/external_samples/github_invoice_real_files/sap_3420987413543.pdf`
  - `workspace/external_samples/github_invoice_real_files/sap_5435569865439.pdf`
  - `workspace/external_samples/github_invoice_real_files/sap_6632559877890.pdf`
- when_to_use: Use when source file or layout resembles SAP DOX invoice validation samples.
- document_type: supplier invoice PDF used in SAP document information extraction examples
- expected_fields:
  - invoice number / document number
  - supplier name and address
  - buyer or bill-to entity
  - invoice date
  - total amount and currency
  - tax amount if present
  - line items or service/material description
  - page/source locator
- visual_layout_clues:
  - business invoice PDF with supplier header and tabular details
  - totals section near lower page
  - may be text-based PDF rather than scanned image
- validation_checks:
  - Check that extracted supplier and buyer are not merged with address lines.
  - Check total and currency together because extraction can separate symbol and number.
  - Check whether table rows were dropped by PDF text extraction.
  - Use page preview when OCR/PDF text is incomplete.
- pass_partial_fail:
  - full: text or OCR source clearly supports invoice id, supplier, buyer, date, total/currency, and locator.
  - partial: table rows, tax, or address fields are merged/unclear.
  - none: SAP sample profile is retrieved but no submitted SAP invoice exists in the case.
- common_defects:
  - document extraction misses table rows
  - currency separated from numeric total
  - address lines merged into supplier or buyer field
  - page preview not attached
- advisor_guidance:
  - Ask for original PDF and page preview when text extraction is sparse.
  - Ask for corrected export if totals or supplier/buyer fields are merged.
- evidence_boundary:
  - SAP DOX examples are template guidance, not case evidence.
- source_links:
  - SAP-samples/btp-cap-dox-invoice-validation: https://github.com/SAP-samples/btp-cap-dox-invoice-validation

## Profile: Mouad Hamri Invoice Dataset Sample

- profile_id: `mouadhamri_invoice_dataset_FACTU`
- source_examples:
  - `samples/cases/invoice_reference_originals/mouadhamri_FACTU2015020048.jpg`
  - `samples/cases/invoice_reference_originals/mouadhamri_FACTU2015020048.tsv`
  - `workspace/external_samples/github_invoice_real_files/mouadhamri_FACTU2015020048.jpg`
  - `workspace/external_samples/github_invoice_real_files/mouadhamri_FACTU2015040047.jpg`
  - `workspace/external_samples/github_invoice_real_files/mouadhamri_FACTU2015050046.jpg`
- when_to_use: Use when a scanned invoice image has FACTU-style file names or comes with TSV/XML annotations.
- document_type: scanned invoice image with OCR ground-truth annotation
- expected_fields:
  - invoice number or document id
  - supplier identity
  - buyer/customer identity if visible
  - invoice date
  - total amount
  - line item/product or service description
  - source image locator and optional TSV/XML annotation locator
- visual_layout_clues:
  - image-based invoice, often OCR-sensitive
  - field positions vary between examples
  - TSV may contain bounding-box annotation; XML may contain key-value labels
- validation_checks:
  - Compare OCR text with TSV/XML only as reference profile guidance unless the TSV/XML is submitted in the active case.
  - Treat low resolution, rotated text, or partial crop as partial.
  - Do not let annotation files replace source invoice image when the user asks for visual evidence.
- pass_partial_fail:
  - full: submitted image or PDF preview clearly shows key fields and source locator.
  - partial: key fields exist only in OCR/annotation but are not visually clear.
  - none: only profile metadata is retrieved.
- common_defects:
  - low-resolution OCR errors
  - missing or ambiguous signature/authorized signatory
  - template variation causing weak `template_match`
  - annotation present but original image absent
- advisor_guidance:
  - Ask for original image plus annotation/export if the image is too noisy.
  - Ask for a higher-resolution scan when amount/date/supplier cannot be read.
- evidence_boundary:
  - Dataset annotations are reference material unless submitted as part of the active case.
- source_links:
  - mouadhamri/invoice_dataset: https://github.com/mouadhamri/invoice_dataset

## Profile: invoice2data AWS/Azure/Coolblue/QualityHosting/Saeco Samples

- profile_id: `invoice2data_multi_vendor_samples`
- source_examples:
  - `workspace/external_samples/github_invoice_real_files/invoice2data_AmazonWebServices.pdf`
  - `workspace/external_samples/github_invoice_real_files/invoice2data_AzureInterior.pdf`
  - `workspace/external_samples/github_invoice_real_files/invoice2data_coolblue1.pdf`
  - `workspace/external_samples/github_invoice_real_files/invoice2data_QualityHosting.pdf`
  - `workspace/external_samples/github_invoice_real_files/invoice2data_saeco.pdf`
- when_to_use: Use for multi-vendor invoice template comparison or when the user wants examples of different invoice layouts.
- document_type: real-world PDF invoice samples from invoice extraction tests
- expected_fields:
  - vendor/supplier
  - invoice number
  - invoice date
  - buyer/account/customer
  - amount total and currency
  - tax/VAT when present
  - line item or service description
- visual_layout_clues:
  - vendor-specific header and footer conventions vary widely
  - some samples use compact service billing layout; others use retail/product table layout
  - currency, tax, or invoice id may appear outside the main table
- validation_checks:
  - Do not force one vendor layout onto another vendor's invoice.
  - Use profile only to remind reviewer to inspect header, buyer, line table, totals, and authenticity markers.
  - Treat missing vendor-specific profile as generic invoice review rather than failure.
- pass_partial_fail:
  - full: exact submitted invoice has readable source and core fields.
  - partial: profile similarity only or OCR/text extraction misses vendor-specific fields.
  - none: only a sample profile was retrieved.
- common_defects:
  - vendor layout mismatch
  - totals outside OCR reading order
  - missing tax id or VAT line
  - line descriptions merged with account metadata
- advisor_guidance:
  - Ask for complete PDF/image rather than only copied text when layout-specific fields matter.
  - Use generic invoice rubric if exact vendor profile is not in RAG.
- evidence_boundary:
  - Multi-vendor samples are comparison references. They cannot satisfy active case requirements.
- source_links:
  - invoice-x/invoice2data: https://github.com/invoice-x/invoice2data

## External Dataset Notes

- profile_id: `external_invoice_dataset_notes`
- when_to_use: Use when discussing future evaluation data, OCR benchmark coverage, or sample expansion.
- document_type: dataset note, not case evidence
- source_examples:
  - Hugging Face `Lukaszl/clearocr-invoice-document-ai`: original invoice images, OCR text, markdown reconstruction, structured invoice JSON, visual verification metadata.
  - Hugging Face `jngb-labs/InvoiceBenchmark`: synthetic invoices with cent-perfect ground truth for amount and arithmetic evaluation.
  - Hugging Face `Voxel51/high-quality-invoice-images-for-ocr`: synthetic invoice images and structured metadata; license needs review before committing subsets.
- validation_checks:
  - Use these datasets for eval design and small sampled fixtures only after license/size review.
  - Do not bulk import entire datasets into the source repo.
- evidence_boundary:
  - Dataset notes are not submitted case evidence.
- source_links:
  - https://huggingface.co/datasets/Lukaszl/clearocr-invoice-document-ai
  - https://huggingface.co/datasets/jngb-labs/InvoiceBenchmark
  - https://huggingface.co/datasets/Voxel51/high-quality-invoice-images-for-ocr

## Six-Case Template Library Boundary

- profile_id: `six_case_template_library_boundary`
- when_to_use: Use when the reviewer/advisor is asked about signature, signatory, template match, layout match, same-vendor examples, or the six local invoice test cases.
- document_type: template/reference guidance, not active case evidence
- validation_checks:
  - Match the submitted invoice to the closest same-vendor or same-template-family reference before applying layout expectations.
  - Use exact same-vendor samples where available. Otherwise use same-dataset or same-template-family references and state that limitation.
  - Do not force a traditional VAT/tax-invoice layout onto Flipkart retail bills, SAP DOX samples, service invoices, or scanned FACTU dataset invoices.
  - Signature and template matching are consistency signals. They can support visual/source quality, but cannot prove supplier existence, legal signature validity, absence of forgery, absence of duplicate payment, bank-account integrity, or payment readiness.
- pass_partial_fail:
  - full: exact vendor/profile match and key layout cues are visible from the submitted source with locator/crop/preview.
  - partial: same-template-family reference only, some layout cues missing, OCR/crop is weak, or only a signatory label is visible.
  - none: only RAG/profile text is available, or the submitted document is a different document type.
- advisor_guidance:
  - Explain which reference profile was used and whether it is exact-vendor or family-level.
  - If template/signature matching is weak but optional, say it is a quality note and not a blocker for invoice-only review.
- evidence_boundary:
  - The template library is RAG guidance. Validation samples are for consistency comparison and tests only.

## Profile: Case 01 Mouadhamri FACTU2015020048 Clean

- profile_id: `case_01_mouadhamri_FACTU2015020048_clean`
- source_examples:
  - canonical: `samples/cases/invoice_reference_originals/mouadhamri_FACTU2015020048.jpg`
  - canonical sidecar: `samples/cases/invoice_reference_originals/mouadhamri_FACTU2015020048.tsv`
  - session canonical: `workspace/external_samples/session_invoice_cases_v1/case_01_clean_match_real_jpg_FACTU2015020048/originals/FACTU2015020048.jpg`
  - validation samples: `workspace/external_samples/session_invoice_cases_v1/case_02_amount_conflict_real_jpg_FACTU2015040047/originals/FACTU2015040047.jpg`, `workspace/external_samples/session_invoice_cases_v1/case_03_duplicate_hit_real_jpg_FACTU2015050046/originals/FACTU2015050046.jpg`, `workspace/external_samples/session_invoice_cases_v1/case_04_bank_change_risk_real_jpg_FACTU2015060039/originals/FACTU2015060039.jpg`
- when_to_use: Use for Mouadhamri/FACTU scanned invoice images, especially invoice `FA02/2015/020059`, supplier `Marc Demo`, PO `BC06263`, total `82,003.30 EUR`.
- document_type: scanned invoice image with TSV/XML OCR annotation
- expected_fields: invoice number, supplier, buyer/customer if visible, invoice date, due date, PO reference, EUR total, tax/TVA lines, line items, source image locator.
- visual_layout_clues: FACTU-style scanned page; supplier/header and invoice identifiers near the top; dense French line item table; totals and TVA summary near the lower half; signature/stamp may be absent or template-dependent and is not a default blocker.
- validation_checks: Compare OCR/image fields against TSV/XML only as reference guidance unless sidecars are submitted in the active case. Do not turn missing signature into a hard failure.
- common_defects: OCR accents garbled, table rows merged, field positions vary across FACTU examples.
- evidence_boundary: Same-template-family reference only; it guides extraction and layout consistency but is not active evidence.
- source_links:
  - https://github.com/mouadhamri/invoice_dataset

## Profile: Case 02 Mouadhamri FACTU2015040047 Amount Conflict

- profile_id: `case_02_mouadhamri_FACTU2015040047_amount_conflict`
- source_examples:
  - canonical: `workspace/external_samples/session_invoice_cases_v1/case_02_amount_conflict_real_jpg_FACTU2015040047/originals/FACTU2015040047.jpg`
  - canonical sidecars: `workspace/external_samples/session_invoice_cases_v1/case_02_amount_conflict_real_jpg_FACTU2015040047/originals/FACTU2015040047.tsv`, `workspace/external_samples/session_invoice_cases_v1/case_02_amount_conflict_real_jpg_FACTU2015040047/originals/FACTU2015040047.xml`
  - validation samples: `workspace/external_samples/session_invoice_cases_v1/case_01_clean_match_real_jpg_FACTU2015020048/originals/FACTU2015020048.jpg`, `workspace/external_samples/session_invoice_cases_v1/case_03_duplicate_hit_real_jpg_FACTU2015050046/originals/FACTU2015050046.jpg`
- when_to_use: Use for FACTU invoice `FA04/2015/027622`, supplier `Deco Addict`, PO `BC03277`, total `38,086.30 EUR`, or amount-conflict scenarios using this template family.
- document_type: scanned invoice image with AP amount-conflict test context
- expected_fields: invoice number, supplier, invoice date, due date, PO reference, untaxed amount, tax amount, grand total, line item rows, source image locator.
- visual_layout_clues: Same FACTU scanned-image family; line item table has many rows; totals/TVA appear after table.
- validation_checks: Template match checks whether field positions and line-table structure resemble FACTU family. Amount conflict is not a template defect; it is a business reconciliation defect against PO/GRN.
- common_defects: OCR row wrapping, total/tax parsing confusion, amount conflict with external AP materials.
- evidence_boundary: The invoice can support invoice fields only. PO/GRN amount conflict requires active AP materials.
- source_links:
  - https://github.com/mouadhamri/invoice_dataset

## Profile: Case 03 Mouadhamri FACTU2015050046 Duplicate Hit

- profile_id: `case_03_mouadhamri_FACTU2015050046_duplicate_hit`
- source_examples:
  - canonical: `workspace/external_samples/session_invoice_cases_v1/case_03_duplicate_hit_real_jpg_FACTU2015050046/originals/FACTU2015050046.jpg`
  - canonical sidecars: `workspace/external_samples/session_invoice_cases_v1/case_03_duplicate_hit_real_jpg_FACTU2015050046/originals/FACTU2015050046.tsv`, `workspace/external_samples/session_invoice_cases_v1/case_03_duplicate_hit_real_jpg_FACTU2015050046/originals/FACTU2015050046.xml`
  - validation samples: `workspace/external_samples/session_invoice_cases_v1/case_01_clean_match_real_jpg_FACTU2015020048/originals/FACTU2015020048.jpg`, `workspace/external_samples/session_invoice_cases_v1/case_04_bank_change_risk_real_jpg_FACTU2015060039/originals/FACTU2015060039.jpg`
- when_to_use: Use for FACTU invoice `FA05/2015/065587`, supplier `The Jackson Group`, PO `BC01963`, total `579,675.62 EUR`, or duplicate-payment-hit scenarios.
- document_type: scanned invoice image with duplicate-payment test context
- expected_fields: invoice number, supplier, invoice date, PO reference, total amount, currency, tax amount, line items, source locator.
- visual_layout_clues: Same FACTU scanned-image family; dense multi-row product/service table; no default signature requirement.
- validation_checks: Template match is separate from duplicate-payment risk. A duplicate hit comes only from submitted duplicate screen/history, not from the invoice template.
- common_defects: Historical duplicate screen may refer to near-match invoice numbers; reviewer must keep duplicate risk separate from invoice field support.
- evidence_boundary: Same-template-family reference only; duplicate-payment conclusions require active duplicate-check evidence.
- source_links:
  - https://github.com/mouadhamri/invoice_dataset

## Profile: Case 04 Mouadhamri FACTU2015060039 Bank Change Risk

- profile_id: `case_04_mouadhamri_FACTU2015060039_bank_change`
- source_examples:
  - canonical: `workspace/external_samples/session_invoice_cases_v1/case_04_bank_change_risk_real_jpg_FACTU2015060039/originals/FACTU2015060039.jpg`
  - canonical sidecars: `workspace/external_samples/session_invoice_cases_v1/case_04_bank_change_risk_real_jpg_FACTU2015060039/originals/FACTU2015060039.tsv`, `workspace/external_samples/session_invoice_cases_v1/case_04_bank_change_risk_real_jpg_FACTU2015060039/originals/FACTU2015060039.xml`
  - validation samples: `workspace/external_samples/session_invoice_cases_v1/case_01_clean_match_real_jpg_FACTU2015020048/originals/FACTU2015020048.jpg`, `workspace/external_samples/session_invoice_cases_v1/case_02_amount_conflict_real_jpg_FACTU2015040047/originals/FACTU2015040047.jpg`
- when_to_use: Use for FACTU invoice `FA06/2015/088665`, supplier `Biotech`, PO `BC05603`, total `347,064.06 EUR`, or AP cases with supplier-bank-change risk.
- document_type: scanned invoice image with bank-change-risk test context
- expected_fields: invoice number, supplier, invoice date, PO reference, total amount, currency/tax, source locator.
- visual_layout_clues: Same FACTU scanned-image family; line item table and totals are the invoice-quality checks; bank-account risk is external to invoice layout.
- validation_checks: A bank-change email or vendor-master mismatch is a risk enrichment. Do not claim bank integrity from invoice template matching.
- common_defects: OCR accents garbled, bank-change email over-trusted, supplier identity conflated with bank-account validity.
- evidence_boundary: Invoice profile supports invoice field extraction; bank-change control requires active vendor master/workflow evidence.
- source_links:
  - https://github.com/mouadhamri/invoice_dataset

## Profile: Case 05 Flipkart WS Retail Invoice/Bill

- profile_id: `case_05_flipkart_ws_retail_invoice_bill`
- source_examples:
  - canonical: `samples/cases/invoice_reference_originals/flipkart_invoice2data.pdf`
  - canonical render: `samples/cases/invoice_reference_originals/flipkart_invoice2data.png`
  - session canonical: `workspace/external_samples/session_invoice_cases_v1/case_05_clean_match_real_pdf_flipkart/originals/invoice2data_FlipkartInvoice.pdf`
  - same-vendor reference links: Scribd `Prasad HTC Invoice` (`BLR_WFLD20150601112323`), Scribd `Invoice-OD203180249444987700` (`BLR_WFLD20150600865869`), Scribd `Retail Invoice for Leather Jacket` (`DEL20151000306523`), referenceglobe `Retail Invoices/Bill`, Alteryx community `FlipkartInvoice.pdf`
- when_to_use: Use for Flipkart / WS Retail Services Pvt. Ltd. retail invoice/bill, especially invoice `BLR_WFLD20151000982590`, order `OD304175096047380001`, total `319.00 INR`.
- document_type: retail invoice / bill
- expected_fields: invoice number, seller/Sold By, warehouse address, order id, order date, invoice date, VAT/TIN, service tax id, billing and shipping addresses, product/title, quantity, price, tax rate, tax amount, total, grand total, authorized signatory area.
- visual_layout_clues: `Retail Invoice/Bill` title; seller block left; invoice number top right; billing and shipping blocks in parallel columns; product table with Product/Title/Qty/Price/Tax/Total; grand total near bottom; `WS Retail Services Pvt. Ltd :` plus `(Authorized Signatory)` near the lower right.
- validation_checks: For signature visibility, distinguish signatory area, signatory label, and visual signature mark. If label and mark are visible near the lower-right signatory block, treat signature visibility as a positive quality signal. Do not require traditional VAT invoice layout.
- common_defects: OCR confuses order id/invoice number/product identifiers; product table columns wrap; signature mark may be faint but label is visible.
- evidence_boundary: Same-vendor references are public comparison examples only. They do not prove the submitted invoice is genuine or unpaid.
- source_links:
  - https://github.com/invoice-x/invoice2data
  - https://www.scribd.com/document/293902673/Prasad-Htc-Invoice
  - https://www.scribd.com/document/271890208/Invoice-OD203180249444987700
  - https://www.scribd.com/document/334882251/Jacket
  - https://referenceglobe.com/CollegeLibrary/library_books/767483745.pdf
  - https://community.alteryx.com/pvsmt99345/attachments/pvsmt99345/designer-discussions/151545/1/FlipkartInvoice.pdf

## Profile: Case 06 SAP DOX Invoice Duplicate

- profile_id: `case_06_sap_dox_invoice_duplicate`
- source_examples:
  - canonical: `samples/cases/invoice_reference_originals/sap_invoice_5435569865439.pdf`
  - session canonical: `workspace/external_samples/session_invoice_cases_v1/case_06_duplicate_hit_real_pdf_sap/originals/sap_5435569865439.pdf`
  - validation samples: `workspace/external_samples/github_invoice_real_files/sap_3420987413543.pdf`, `workspace/external_samples/github_invoice_real_files/sap_6632559877890.pdf`
- when_to_use: Use for SAP DOX invoice sample family, especially invoice `5435569865439`, supplier `Electric Installations - Urs Schmid`, PO `245675647654`, total `36,592.33 EUR`, or duplicate-risk SAP sample cases.
- document_type: SAP document information extraction sample invoice PDF
- expected_fields: document/invoice number, supplier name/address, buyer/bill-to, invoice date, due date, PO reference, total amount, currency, tax amount, line/service item, page locator.
- visual_layout_clues: Business invoice PDF; supplier header and address; document-number/date block; line/service table; total/currency block; may not have a retail-style authorized signatory block.
- validation_checks: Use SAP sample family comparison; do not require Flipkart retail signatory layout or FACTU scanned-image layout. Duplicate risk is from active duplicate-payment screen, not from SAP template match.
- common_defects: PDF extraction merges address lines, separates currency from amount, or drops service rows.
- evidence_boundary: Same-dataset references support layout consistency only. They do not prove no duplicate payment or supplier authenticity.
- source_links:
  - https://github.com/SAP-samples/btp-cap-dox-invoice-validation
