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
- review_behavior: Record an `amount_conflict` on the affected AP requirement. Do not describe the case as ready until a corrected source or reconciliation note explains the difference.
- advisor_guidance: Ask for corrected invoice/PO, approved change order, or reconciliation evidence showing the approved variance.

## Microcase: GRN Quantity Short

- profile_id: `grn_quantity_short`
- trigger: Invoice quantity or service amount exceeds GRN-2026-8899 received quantity or accepted service period.
- review_behavior: Record a `quantity_conflict` or `service_acceptance_conflict`; invoice and PO may remain partial, but `goods_receipt_or_service_acceptance` is conflicted until resolved.
- advisor_guidance: Ask warehouse receiver or service owner for corrected GRN/service acceptance or documented partial delivery explanation.

## Microcase: Vendor Bank Mismatch

- profile_id: `vendor_bank_mismatch`
- trigger: Invoice/remittance details, vendor master, or email show different bank/payment details for Aurora Office Supplies Ltd.
- review_behavior: Treat bank-change email as risk evidence, not as validation. Bank mismatch is a conflict/risk enrichment, not a default AP lite blocker unless the case already has a bank-change requirement.
- advisor_guidance: Ask AP master data or vendor master team for current vendor master export, workflow approval history, current/proposed values, and trusted supplier confirmation.

## Microcase: Duplicate Payment Hit

- profile_id: `duplicate_payment_hit`
- trigger: Duplicate-payment screen shows same supplier, same or near amount, near invoice number, or a historical clearing/payment reference for Aurora Office Supplies Ltd.
- review_behavior: Accept the duplicate screen as evidence of a risk finding, but keep `duplicate_payment_screen` partial/conflict until the historical payment/clearing relationship is reconciled.
- advisor_guidance: Ask payment operations for duplicate-check export, historical payment record, clearing voucher, and business explanation distinguishing true duplicate, reversal, installment, recurring service, or false positive.

## Microcase: Process Log Only Clear Invoice

- profile_id: `process_log_only_clear_invoice`
- trigger: User submits only a Clear Invoice/BPI/ERP process log and asks whether the invoice can be paid.
- review_behavior: Record process evidence only. A Clear Invoice event does not replace invoice, PO, GRN/service acceptance, vendor identity, or duplicate-payment screen evidence.
- advisor_guidance: Ask for the source invoice and active AP lite materials if the user wants AP payment review.

## Retrieval Keywords

Aurora Office Supplies, Northstar Retail Group, INV-2026-001, PO-2026-7788, GRN-2026-8899, 10000 CNY, 发票付款审查, 付款审查, AP review, AP lite, invoice payment review, 三单匹配, PO GRN, duplicate payment screen, vendor identity, goods receipt or service acceptance, Clear Invoice.
