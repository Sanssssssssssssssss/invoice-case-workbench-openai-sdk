# INV-5001 Complete Invoice Payment Review

This sample is a positive invoice payment review set. It is designed to pass the local workbench's core material review:

- invoice
- purchase_order
- goods_receipt
- vendor_record
- duplicate_payment_check

Suggested desktop test:

1. Create or switch to a fresh case, for example `case_inv5001_pass`.
2. Send: `帮我新建一个发票付款审查 case。`
3. Drag the five files in `evidence/` into the attachment area.
4. Send: `请审核这套发票付款材料，告诉我是否可以形成完整证据链。`

Expected high-level result: the agent should identify that the documents are aligned for invoice payment review, with invoice `INV-5001`, PO `PO-5001`, GRN `GRN-5001`, supplier `Orion Parts Co., Ltd.`, amount `12800.00 CNY`, and no duplicate payment found.
