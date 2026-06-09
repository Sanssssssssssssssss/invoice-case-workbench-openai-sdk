# Live Golden Cases

Generated: 2026-06-06T22:52:43

Passed: 6 / 6

| case | terminal | status | conflicts | final_ms | error |
|---|---:|---|---|---:|---|
| case_01_clean_match_real_jpg_FACTU2015020048 | final | ready_for_report |  | 282757.9 |  |
| case_02_amount_conflict_real_jpg_FACTU2015040047 | final | ready_for_report |  | 4085.5 |  |
| case_03_duplicate_hit_real_jpg_FACTU2015050046 | final | collecting_materials | duplicate_payment_screen | 309216.7 |  |
| case_04_bank_change_risk_real_jpg_FACTU2015060039 | final | collecting_materials | vendor_identity | 246519.1 |  |
| case_05_clean_match_real_pdf_flipkart | final | ready_for_report |  | 101116.0 |  |
| case_06_duplicate_hit_real_pdf_sap | final | collecting_materials | duplicate_payment_screen | 605303.2 |  |

## Reply Previews

### case_01_clean_match_real_jpg_FACTU2015020048

本轮本地材料审查已经记录到案卷，重点结论：当前材料未显示重复付款命中。
- 识别到的核心对象：供应商 Marc Demo；金额 82,003.30 EUR。
- 当前案卷状态：status=ready_for_report；evidence=5；satisfied=invoice, purchase_order, goods_receipt_or_service_acceptance, vendor_identity, duplicate_payment_screen；conflict=-；missing=-；weak=-。
- 风险标记：limited_bank_detail_visibility_only_last_four_digits_shown
结论：case_state=ready_for_report；可继续生成本地报告草稿。

### case_02_amount_conflict_real_jpg_FACTU2015040047

本轮本地材料审查已经记录到案卷，重点结论：当前材料未显示重复付款命中。
- 识别到的核心对象：发票 FA04/2015/027622；供应商 Deco Addict；金额 38,086.30。
- 当前案卷状态：status=ready_for_report；evidence=5；satisfied=invoice, purchase_order, goods_receipt_or_service_acceptance, vendor_identity, duplicate_payment_screen；conflict=-；missing=-；weak=-。
结论：case_state=ready_for_report；可继续生成本地报告草稿。

### case_03_duplicate_hit_real_jpg_FACTU2015050046

本轮本地材料审查已经记录到案卷，重点结论：重复付款检查命中风险。
- 识别到的核心对象：供应商 The Jackson Group；金额 579,675.62 EUR。
- 当前案卷状态：status=collecting_materials；evidence=5；satisfied=invoice, purchase_order, goods_receipt_or_service_acceptance, vendor_identity；conflict=duplicate_payment_screen；missing=-；weak=-。
- 风险标记：duplicate_payment_positive_hit
- 待核对问题：请提供历史付款凭证PAY-FA05_2015_065587-HIST的详细清账状态及关联凭证
结论：在上述冲突解释清楚前，duplicate_payment_screen 保持 conflict；不要把该项写成 satisfied。

### case_04_bank_change_risk_real_jpg_FACTU2015060039

根据审查结果，现对发票付款材料包进行完整评估：

## 材料完整性评估

**已提交材料（6项）：**
1. **发票原件** (01_invoice_extract_from_real_source.md) — 完整
2. **采购订单** (02_purchase_order.md) — 完整
3. **收货凭证/服务验收** (03_goods_receipt.md) — 完整
4. **供应商主数据** (04_vendor_record.md) — 部分
5. **重复付款核查** (05_duplicate_payment_check.md) — 完整
6. **供应商银行账户变更邮件** (06_supplier_bank_change_email.md) — 完整

## 风险识别

### 1. 金额风险 — **低风险**
- 发票金额：347,064.06 EUR
- 采购订单金额：347,064.06 EUR
- **核对结果**：金额一致，无差异

### 2. 供应商风险 — **中等风险**
- 供应商：Biotech
- 供应商主数据存在，但**供应商字段在发票中缺失**（仅能从其他材料推断）
- 建议补充发票上的供应商完整信息

### 3. 重复付款风险 — **低风险**
- 重复付款核查结果：**未发现重复付款记录**
- 历史付款记录无冲突

### 4. 银行账户变更风险 — **高风险 ⚠️**
- **关键发现**：供应商通过邮件请求银行账户变更
- **风险点**：邮件中提及"bank-change request without vendor-master approval"（未经供应商主数据审批的银行变更请求）
- **尾号核对**：
  - 供应商主数据记录尾号：9012
  - 变更请求涉及新账户信息
- **建议**：该变更缺乏正式的供应商主数据审批流程，存在潜在的欺诈或误操作风险，需进一步核实变更的合法性和真实性

### 5. 来源可追溯性风险 — **中等风险**
- 发票-采购订单-收货凭证三单匹配：
  - 发票号：FA06/2015/088665
  - 采购订单号：BC05603
  - 收货凭证号：GRN-BC05603
- **匹配结果**：PO/GRN引用一致，可追溯
- **不足**：

### case_05_clean_match_real_pdf_flipkart

本轮本地材料审查已经记录到案卷，重点结论：当前材料未显示重复付款命中。
- 识别到的核心对象：发票 BLR_WFLD20151000982590；供应商 WS Retail Services Pvt. Ltd.；金额 319.00。
- 当前案卷状态：status=ready_for_report；evidence=5；satisfied=invoice, purchase_order, goods_receipt_or_service_acceptance, vendor_identity, duplicate_payment_screen；conflict=-；missing=-；weak=-。
结论：case_state=ready_for_report；可继续生成本地报告草稿。

### case_06_duplicate_hit_real_pdf_sap

本轮本地材料审查已经记录到案卷，重点结论：重复付款检查命中风险。
- 识别到的核心对象：发票 5435569865439；供应商 Electric Installations；金额 36,592.33。
- 重复付款线索：历史付款记录 PAY-5435569865439-HIST。
- 当前案卷状态：status=collecting_materials；evidence=5；satisfied=invoice, purchase_order, goods_receipt_or_service_acceptance, vendor_identity；conflict=duplicate_payment_screen；missing=-；weak=-。
- 风险标记：duplicate_payment_risk: prior payment or clearing record found
- 待核对问题：Please reconcile the historical payment and clearing document before treating the invoice as clear.
结论：在上述冲突解释清楚前，duplicate_payment_screen 保持 conflict；不要把该项写成 satisfied。
