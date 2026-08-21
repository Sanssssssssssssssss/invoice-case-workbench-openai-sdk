---
name: report_writer_pdf_skill
version: report_writer_pdf_skill_v4
owner: invoice_payment_review_agent
last_updated: 2026-08-22
input_contract: canonical_consumer_packet
output_contract: PDF-friendly Markdown inside ReportWriterResult.markdown
---

# PDF-Friendly Canonical Proof Report

- 使用简洁中文标题、短段落和紧凑表格，不用 emoji 或营销文案。
- 目录位于 H1 之后、第一章之前；PDF renderer 负责书签和分页。
- 可见标签写“要求编号、叶检查、审核结论、主张/绑定/计算编号、来源定位、限制”。
- 所有业务结论只来自 `canonical_consumer_packet`。
- 不写原始附件路径、field inventory、完整行项目、银行详情、crop path 或调试 block id。
- PDF renderer 会在 canonical 正文之后追加带免责声明的“原始材料附录”；截图和
  完整附件只供人工核对，不构成系统结论。Markdown 不复制这些内容，也不声称最终
  PDF 不包含原始材料。
- `PARTIAL` 必须突出“部分已核实”，分开列已核实、已证实冲突和未决义务；
  不得给出整体支持、付款、过账或最终审批结论。
- `FULL` 也只是证明审查完整，不等于 ERP 执行授权。
