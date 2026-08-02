# Aurora AP Lite Case Playbook

This playbook is RAG guidance for the local invoice review workbench. It is not submitted case evidence and cannot satisfy a case requirement by itself.

Use it when a user asks for 发票付款审查, invoice payment review, AP review, PO/GRN matching, duplicate-payment checking, or when the submitted facts look like the local Aurora/Northstar AP examples.

Common local-style fields:

- supplier: Aurora Office Supplies Ltd
- buyer: Northstar Retail Group
- invoice number: INV-2026-001
- purchase order: PO-2026-7788
- goods receipt: GRN-2026-8899
- amount/currency: 10000 CNY
- optional AP controls: vendor identity, duplicate payment screen, bank-change workflow

## Profile: AP Lite Payment Review

- profile_id: `case_playbook_aurora`
- related_profile: `ap_lite_payment_review_material_profile`
- document_type: local AP payment review case playbook
- when_to_use: Use when the user says 发票付款审查, AP review, invoice payment review, 三单匹配, PO/GRN, payment controls, duplicate payment, or provides Aurora/Northstar style fields such as INV-2026-001, PO-2026-7788, GRN-2026-8899, 10000 CNY.
- expected_requirements:
  - `invoice`: source invoice or bill with invoice number, supplier, buyer, date, amount, currency/tax, and source locator.
  - `purchase_order`: PO source with PO number, supplier, approval/status, line items or service, quantity/price when available, currency, and source locator.
  - `goods_receipt_or_service_acceptance`: GRN/product receipt/service acceptance with receipt id, PO reference, received quantity or accepted service period, receiver/approver, date, and source locator.
  - `vendor_identity`: supplier master or vendor identity source with legal name, vendor id/status, tax/registration or payment terms when available, and source locator.
  - `duplicate_payment_screen`: duplicate-payment/AP history screen or export with search criteria, matched records, conclusion, and source locator.
- evidence_boundary: This playbook explains what a local AP lite case needs. It is not proof that Aurora/Northstar submitted any of those documents.
- advisor_guidance: Ask for the missing active AP lite source document only. Do not add bank-change, sanctions, signature, or template-match as blocker requirements unless the user asks or a conflict appears.

## Microcase: Happy Path

- profile_id: `aurora_happy_path`
- source_examples: `INV-2026-001`, `PO-2026-7788`, `GRN-2026-8899`, Aurora Office Supplies Ltd, Northstar Retail Group, 10000 CNY.
- expected_behavior: Invoice, PO, GRN/service acceptance, vendor identity, and duplicate-payment screen are all source-traceable and key fields match.
- pass_partial_fail:
  - full: all five AP lite requirements have source evidence and no key conflict.
  - partial: any AP document is only referenced indirectly or has weak OCR/source locator.
  - fail: any key amount, supplier, PO, GRN, currency, vendor identity, or duplicate-payment finding conflicts.

## Microcase: Invoice Missing PO Reference

- profile_id: `invoice_missing_po_reference`
- trigger: Invoice source is present, but INV-2026-001 does not show a readable PO-2026-7788 reference.
- review_behavior: The invoice can support invoice identity fields, but it does not support `purchase_order` unless the actual PO source is submitted.
- advisor_guidance: Ask procurement or the PO owner for the PO export/screenshot with PO number, supplier, approval/status, line items or service, quantity/price, currency, and source locator.

## Microcase: Amount Higher Than PO

- profile_id: `amount_higher_than_po`
- trigger: Invoice amount is 10000 CNY, but PO amount is lower or PO line/unit price does not reconcile.
- review_behavior: Keep the invoice, PO, and GRN source requirements separate from the amount result. If the fields are traceable and comparable but exceed Aurora's inclusive 2% tolerance, compile `three_way_amount_match=DISPROVED`. The evidence is sufficient to report the finding; it is not a payment approval or a resolved conflict.
- advisor_guidance: Report the supported mismatch first. Ask for a corrected invoice/PO, approved change order, or reconciliation evidence only when the user wants remediation or a later re-review; do not treat it as missing evidence for the current finding.

## Microcase: GRN Quantity Short

- profile_id: `grn_quantity_short`
- trigger: Invoice quantity or service amount exceeds GRN-2026-8899 received quantity or accepted service period.
- review_behavior: Keep readable source documents satisfied. When line identity, ordered quantity, received quantity, invoiced quantity, and configured quantity policy are complete and comparable, record the semantic result in `three_way_quantity_match`; do not attach a cross-document quantity conclusion back to the GRN source Requirement.
- advisor_guidance: Ask warehouse receiver or service owner for corrected GRN/service acceptance or documented partial delivery explanation.

## Microcase: Vendor Bank Mismatch

- profile_id: `vendor_bank_mismatch`
- trigger: Invoice/remittance details, vendor master, or email show different bank/payment details for Aurora Office Supplies Ltd.
- review_behavior: Treat bank-change email as risk evidence, not as validation. Bank mismatch is a conflict/risk enrichment, not a default AP lite blocker unless the case already has a bank-change requirement.
- advisor_guidance: Ask AP master data or vendor master team for current vendor master export, workflow approval history, current/proposed values, and trusted supplier confirmation.

## Microcase: Duplicate Payment Hit

- profile_id: `duplicate_payment_hit`
- trigger: Duplicate-payment screen shows same supplier, same or near amount, near invoice number, or a historical clearing/payment reference for Aurora Office Supplies Ltd.
- review_behavior: A complete source-traceable search may satisfy `duplicate_payment_screen`. When lifecycle review is requested, put the candidate's resolved, active, or unknown economic effect in the separate compiled `no_active_duplicate` Requirement; do not encode that semantic finding as missing source material.
- advisor_guidance: Ask payment operations for duplicate-check export, historical payment record, clearing voucher, and business explanation distinguishing true duplicate, reversal, installment, recurring service, or false positive.

## Microcase: Process Log Only Clear Invoice

- profile_id: `process_log_only_clear_invoice`
- trigger: User submits only a Clear Invoice/BPI/ERP process log and asks whether the invoice can be paid.
- review_behavior: Record process evidence only. A Clear Invoice event does not replace invoice, PO, GRN/service acceptance, vendor identity, or duplicate-payment screen evidence.
- advisor_guidance: Ask for the source invoice and active AP lite materials if the user wants AP payment review.

## Microcase: Inclusive Amount Tolerance Boundary

- profile_id: `amount_tolerance_boundary`
- trigger: Invoice total is 10200 CNY; PO total and full GRN received value are both 10000 CNY; all three sources explicitly share one PO identity, currency, gross tax basis, full coverage, and compatible amount bases.
- expected_behavior: Aurora's configured variance is exactly 2%, so `three_way_amount_match=PROVED`. With the same facts at 10201 CNY, it is `DISPROVED` and `EVIDENCE_SUFFICIENT_FOR_REPORT`.
- evidence_boundary: This boundary validates the configured Aurora demo value only. It does not establish a universal industry tolerance.

## Microcase: Partial Receipt and Tax-Basis Gap

- profile_id: `partial_receipt_tax_basis_gap`
- trigger: Invoice, PO, and GRN amounts are readable, but GRN coverage is partial or one document is gross while another is net without a source reconciliation.
- expected_behavior: Source Requirements remain satisfied; `three_way_amount_match=INCOMPLETE`; `JDG_AMOUNT_SCOPE_COMPARABLE=UNKNOWN`; request coverage/tax-basis reconciliation. Do not report a proved amount mismatch.

## Microcase: Duplicate Candidate Fully Reversed

- profile_id: `duplicate_candidate_fully_reversed`
- trigger: A complete duplicate search finds one same-obligation candidate, and source records bind that payment to a posted full reversal of the same payment identity.
- expected_behavior: `duplicate_payment_screen=satisfied`, candidate judgment `SUPPORTED`, and `no_active_duplicate=PROVED`. An incomplete, draft, partial, or unbound reversal instead leaves the conclusion `INCOMPLETE`.

## Microcase: Bank Change and SoD Are Separate

- profile_id: `bank_change_and_sod_separate`
- trigger: Invoice and current vendor identity agree, an email proposes a new bank account, and the same operator appears in bank-change and payment-release activity.
- expected_behavior: Keep `vendor_identity` satisfied. If the bank workflow or trusted confirmation is missing, `vendor_bank_account_authorized=INCOMPLETE`. Set `sod_control_satisfied=DISPROVED` only when an authoritative actor/action record proves a prohibited combination with no compensating control; email alone is only a trigger.

## Microcase: Approval Limit Is Not Invented

- profile_id: `approval_limit_unconfigured`
- trigger: User asks whether an approver may authorize a 10000 CNY invoice, but no approved Aurora authority matrix or delegation record is submitted.
- expected_behavior: Activate `approval_control` only for the requested scope. Keep `approval_authority_satisfied=INCOMPLETE` and request the matrix/approval record; do not borrow example limits from ERP product documentation.

## Retrieval Keywords

Aurora Office Supplies, Northstar Retail Group, INV-2026-001, PO-2026-7788, GRN-2026-8899, 10000 CNY, 发票付款审查, 付款审查, AP review, AP lite, invoice payment review, 三单匹配, PO GRN, duplicate payment screen, vendor identity, goods receipt or service acceptance, Clear Invoice.
