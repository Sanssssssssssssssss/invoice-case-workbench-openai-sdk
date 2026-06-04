---
name: report_writer_pdf_skill
version: report_writer_pdf_skill_v2.1
owner: invoice_payment_review_agent
last_updated: 2026-05-30
input_contract: report_writer case_state/evidence payload
output_contract: PDF-friendly Markdown inside ReportWriterResult.markdown
---

# PDF-Friendly Report Writing Skill

Write Markdown that renders into a clean, formal, black-and-white Chinese PDF.

- Use short headings and dense tables.
- Do not use emoji, decorative symbols, colored-label wording, or marketing language.
- The report is for Chinese readers. Main prose, headings, table headers, risk explanations, captions, and next steps must be Chinese.
- When source evidence is English, keep the original text only as evidence data and add a Chinese translation/explanation beside it. Use columns such as `原文描述` and `中文说明`.
- Use Chinese section titles. Write `结论与证据矩阵`, not `Claim-to-Evidence Matrix`, unless the English phrase is only a parenthetical explanation inside the paragraph.
- Use Chinese table labels: `证据编号`, `支持要求`, `审核结论`, `主张/结论`. Do not use English labels such as `Evidence ID`, `Requirements`, `Reviewer conclusion`, or `Claim` in visible report text.
- Keep the document to three chapters: `第一章 概览`, `第二章 证据审核`, `第三章 证据图录`.
- Put `## 目录` after the H1 title and before `## 第一章 概览`; the table of contents is outside the chapters.
- The PDF renderer owns clickable TOC links, PDF outline entries, and chapter page breaks. Do not put the TOC inside 第一章 and do not write manual page numbers.
- The cover/first page must let a reader see: this turn's request, review scope, submitted materials, top risks, summary conclusion, and next material actions.
- Do not create path-heavy sections. The renderer embeds evidence images automatically; the Markdown should explain what the images prove.
- Do not create `报告生成说明`, `证据截图索引`, or a long `证据明细` table.
- Use Chinese labels for field ids: 发票编号、供应商、购买方、发票日期、总金额、币种/税额、商品/服务行项目、签名/授权签章、来源可追溯性.
- Every strong claim must include evidence ids.
- Use `evidence_chain_context.evidence_items[].proof_cards` and `field_inventory` for field proof wording.
- When confidence appears in both `case_state` metadata and `evidence_chain_context`, prefer the `evidence_chain_context.field_inventory/proof_cards` value because it reflects the latest extraction dossier.
- Treat `block_crops` as debug material. Do not put raw block ids such as `p1_b002` or `p2_amount_total_context` into report prose.
- Field screenshot captions must say in Chinese: proof point, 证据编号, related 主张/要求, limitation.
- Original file snapshots are handled by the PDF renderer from `preview_paths` and manifest refs; mention them as an appendix/catalog, not as paths.
- If `evidence_chain_context` contains `line_items[]`, write a complete line-item table with Chinese headers: `序号`, `项目编号`, `原文描述`, `中文说明`, `数量`, `单价`, `金额`, `页码`. Prefer each row's `chinese_description`. Do not leave a mostly English table without Chinese explanation.
- If `line_item_count` equals the provided `line_items[]` length (or the provided rows cover the reported count), treat line-item extraction as complete/high-confidence table evidence. Do not call it `partial`, `weak`, `OCR truncated`, or `截断` because older reviewer notes or context previews used truncation markers.
- For line-item fields, trust `evidence_chain_context.line_items`, `line_item_count`, and table locators over stale `case_state` support text. Only write a truncation limitation when the current `quality_notes` explicitly say the source document/table extraction failed or pages are missing.
- If `evidence_chain_context` contains `bank_details`, add a short "银行/付款信息观察" subsection. Bank/BIC/IBAN/SWIFT can remain as original abbreviations, but every explanation sentence must be Chinese and must state that this does not prove the account is authentic or unchanged.
- For high-risk duplicate-payment cases, explain the prior payment/clearing record, matching invoice/supplier/amount/date relationship, why it is high risk, and what must be checked next.
- If a field has OCR/table uncertainty, page quality notes, or no locator, write it as limitation/partial instead of a confirmed conclusion.
- RAG/profile/template references are guidance only, not case evidence.
