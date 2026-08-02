# Invoice Review Material Profiles

This file tells materials_advisor what to ask for. It is RAG guidance only and never counts as submitted evidence.

## Default Profile: Invoice-Only Review

- profile_id: `invoice_only_material_profile`
- when_to_use: Use when the user submits one invoice/bill or asks whether a single invoice has issues, without explicitly requesting AP payment review, three-way matching, PO/GRN matching, vendor master validation, or duplicate-payment checking.
- blocker_requirements:
  - `invoice_number`
  - `supplier`
  - `buyer`
  - `invoice_date`
  - `amount_total`
  - `currency_tax`
  - `source_traceability`
- optional_quality_requirements:
  - `line_items_product_title`
  - `signature_or_authorized_signatory`
  - `template_match`
- required_source_material:
  - Original invoice PDF/image, complete scan, or source-system invoice export.
  - If only OCR/text is available, ask for the original file or a readable preview.
- advisor_tasks:
  - missing blocker: Ask for the original invoice page containing the missing field.
  - weak blocker: Ask for a clearer original, complete page, or source-system export.
  - optional missing/weak: Describe as a quality note, not as a blocker.
  - conflict: Ask for corrected invoice or explanation tying conflicting values to the same invoice.
  - excluded/quarantined: Ask for a clean copy without instruction-like contamination.
- evidence_boundary:
  - Do not request PO, GRN, vendor master, or duplicate-payment check by default.
  - A single invoice can support invoice-field review but does not prove receipt, vendor master validity, or duplicate-payment clearance.
- Chinese retrieval keywords: 单张发票, 发票需要什么, 发票字段, 发票原件, 发票审核, 发票补料, 发票号, 供应商, 购买方, 金额, 税额, 原件可追溯, 签字可选, 模板可选

## Optional Profile: AP Lite Payment Review

- profile_id: `ap_lite_payment_review_material_profile`
- related_profile: `case_playbook_aurora`
- when_to_use: Use when the active case requirements or user wording explicitly mention AP, payment review, invoice payment review, three-way matching, PO, GRN, goods receipt, service acceptance, vendor identity, duplicate payment, or payment controls.
- blocker_requirements:
  - `invoice`
  - `purchase_order`
  - `goods_receipt_or_service_acceptance`
  - `vendor_identity`
  - `duplicate_payment_screen`
- required_source_material:
  - Invoice original or ERP invoice export.
  - Purchase order source document or ERP PO export.
  - Goods receipt, product receipt, or service acceptance source document.
  - Vendor identity / supplier master / supplier profile export.
  - Duplicate-payment screen, AP history search result, or duplicate check export.
- advisor_tasks:
  - missing PO: Ask procurement buyer or PO owner for ERP PO export/screenshot.
  - missing receipt/service acceptance: Ask warehouse receiver or service owner for receipt/service acceptance record.
  - missing vendor identity: Ask vendor master data team for current supplier identity/source export.
  - missing duplicate screen: Ask AP controls or payment operations for search export and historical payment/clearing references.
  - conflict: Ask for corrected source document or reconciliation explanation; do not present conflict as resolved.
- risk_enrichments:
  - Bank-change workflow, sanctions/watchlist, signature/authenticity marker, and template match are not default blockers.
  - Approval authority, segregation of duties, payment release, tax/GL coding, exception holds, and audit-trail retention are enterprise control enrichments unless the user explicitly asks for them or they explain a real conflict.
  - Treat those as risk notes unless the user explicitly asks to review them or they create a conflict.
- evidence_boundary:
  - Invoice text that merely mentions PO/GRN/vendor ids is indirect and cannot satisfy those source-document requirements.
  - RAG/policy can explain why these materials matter, but it cannot replace the submitted material.
  - The five source documents and `three_way_amount_match` are different requirements. A complete source document remains satisfied when its amount field is missing; the amount check is then `INCOMPLETE` and produces a proof obligation.
- Chinese retrieval keywords: AP 付款审查, 发票付款审查, 三单匹配, 采购订单, PO, GRN, 收货单, 服务验收, 供应商身份, 供应商主数据, 重复付款筛查, 付款控制

## Direct Checklist: AP Payment Review Materials

- profile_id: `ap_payment_review_materials_checklist_direct`
- related_profile: `ap_lite_payment_review_material_profile`
- when_to_use: Use when the user asks "我现在需要准备什么", "缺什么材料", "what materials do I need", "next materials", or "missing checklist" in an invoice payment review case.
- answer_shape:
  - Start with the five core AP Lite materials: invoice, purchase order, goods receipt or service acceptance, vendor identity/master data, duplicate-payment screen or AP history search.
  - For each missing item, say who can usually provide it and which source fields matter.
  - Mention enterprise enrichments only as risk/control add-ons when relevant: approval matrix, SoD/access report, payment release, bank-change workflow, tax/GL coding, matching hold, audit trail.
  - Do not say RAG guidance itself is evidence.
- Chinese retrieval keywords: 我现在需要准备什么, 缺什么材料, 材料清单, 补料清单, 发票付款审查材料, 下一步材料, AP checklist, missing materials

## Wrong Workflow Materials

- profile_id: `wrong_workflow_material_profile`
- when_to_use: Use when the user submits PR approval, quote, budget approval, supplier onboarding, brochure, email, or policy text against invoice/AP source-document requirements.
- document_type: PR, quote, budget note, supplier onboarding, email, policy, process log
- advisor_tasks:
  - Name the material by its real type.
  - Explain which active requirement it cannot satisfy.
  - Ask for the correct source document and required fields.
- evidence_boundary:
  - PR approval is not a purchase order.
  - Quote is not a PO and does not prove received goods.
  - Supplier onboarding is not current vendor master status unless it contains source-traceable current master data.
  - Email is not enough for bank change approval.
- Chinese retrieval keywords: PR 不是 PO, 采购申请, 报价单, 预算审批, 供应商入驻, 错域材料, 不能替代

## Advisor Output Discipline

- Build task-style answers: 缺什么, 为什么不够, 找谁拿, 拿什么格式, 必填字段, 补完影响.
- Only use active case requirements or the profile explicitly requested by the user.
- Do not promise that a requirement will become accepted or satisfied. Say the new material will allow evidence review or resolve a specific gap/conflict.
- Do not imply that RAG policy or template examples are already submitted case evidence.
