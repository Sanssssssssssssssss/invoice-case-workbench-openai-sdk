# Latest Session Eval

Passed: 3/3

## PASS - PR-1001 错域六附件

- case_id: `eval_pr1001_wrong_domain`
- action_chain: `call_tool:read_attachment -> call_role:evidence_reviewer -> call_role:case_patch_writer -> write_case_patch -> final_answer`
- generated_files: `case_state.json, conversation.jsonl, session.json, traces/artifacts/run_d70ef11f5ba3/art_001_attachment_batch_read_attachment.json, traces/artifacts/run_d70ef11f5ba3/art_002_role_result_evidence_reviewer.json, traces/artifacts/run_d70ef11f5ba3/art_003_role_result_case_patch_writer.json, traces/artifacts/run_d70ef11f5ba3/art_004_tool_result_write_case_patch.json, traces/case_audit.jsonl, traces/run_d70ef11f5ba3.json, traces/run_d70ef11f5ba3/context_manifest_000_planner.json, traces/run_d70ef11f5ba3/context_manifest_001_planner.json, traces/run_d70ef11f5ba3/context_manifest_002_planner.json, traces/run_d70ef11f5ba3/context_manifest_002_role_evidence_reviewer.json, traces/run_d70ef11f5ba3/context_manifest_003_planner.json, traces/run_d70ef11f5ba3/context_manifest_003_role_case_patch_writer.json, traces/run_d70ef11f5ba3/context_manifest_004_planner.json`
- bug_notes: none

Final reply:

已审查提交的6份文件，本地case已记录。这些文件不能用于当前发票付款审查，存在显著缺口：

【已记录材料】
- 采购申请审批记录（01_approval_request.md）
- 预算证据（02_budget_evidence.md）
- 供应商准入及风险材料（03_vendor_onboarding_and_risk.md）
- 报价依据（04_quote_price_basis.md）
- 框架协议（05_framework_agreement.md）
- 采购政策摘录（06_procurement_policy_excerpt.md）

【核心缺口】
1. 发票（Invoice）：缺失，无法核对金额、税额、开票日期
2. 采购订单（PO）：缺失，无法建立采购-付款链路
3. 收货/服务验收记录（GRN）：缺失，无法确认履约完成
4. 供应商银行信息核对材料：缺失，无法核验付款账户一致性
5. 重复付款检查/历史付款清账记录：缺失，无法排查重复付款风险

【风险标记】
- 当前材料为采购申请阶段文档，非应付账款付款审查所需材料
- 全部材料为本地模拟数据，非真实ERP导出或供应商原件，业务真实性需人工复核

请补充上述5类核心材料后重新提交。本工具只生成本地审查材料；如需进入ERP流程，请由人工在ERP中处理。

## PASS - Clear Invoice boundary

- case_id: `eval_clear_invoice_boundary`
- action_chain: `call_tool:read_attachment -> call_role:evidence_reviewer -> call_role:case_patch_writer -> write_case_patch -> final_answer`
- generated_files: `case_state.json, conversation.jsonl, session.json, traces/artifacts/run_fd0231c4450f/art_001_attachment_batch_read_attachment.json, traces/artifacts/run_fd0231c4450f/art_002_role_result_evidence_reviewer.json, traces/artifacts/run_fd0231c4450f/art_003_role_result_case_patch_writer.json, traces/artifacts/run_fd0231c4450f/art_004_tool_result_write_case_patch.json, traces/case_audit.jsonl, traces/run_fd0231c4450f.json, traces/run_fd0231c4450f/context_manifest_000_planner.json, traces/run_fd0231c4450f/context_manifest_001_planner.json, traces/run_fd0231c4450f/context_manifest_002_planner.json, traces/run_fd0231c4450f/context_manifest_002_role_evidence_reviewer.json, traces/run_fd0231c4450f/context_manifest_003_planner.json, traces/run_fd0231c4450f/context_manifest_003_role_case_patch_writer.json, traces/run_fd0231c4450f/context_manifest_004_planner.json`
- bug_notes: none

Final reply:

不是。Clear Invoice 事件或 process log 只能作为历史流程证据，不能证明付款、批准、过账、路由或提交动作发生。

当前 case 状态：
- 已记录证据：BPI/process log 1 份（可信度：中）
- 仍缺材料：发票原件、采购订单、收货记录、供应商主数据、重复付款检查结果

本工具只生成本地审查材料；如需进入 ERP 流程，请由人工在 ERP 中处理。

## PASS - RAG 问材料

- case_id: `eval_rag_materials`
- action_chain: `call_tool:rag_search -> call_role:materials_advisor -> final_answer`
- generated_files: `case_state.json, conversation.jsonl, session.json, traces/artifacts/run_7fc209156083/art_001_rag_result_rag_search.json, traces/artifacts/run_7fc209156083/art_002_role_result_materials_advisor.json, traces/run_7fc209156083.json, traces/run_7fc209156083/context_manifest_000_planner.json, traces/run_7fc209156083/context_manifest_001_planner.json, traces/run_7fc209156083/context_manifest_002_planner.json, traces/run_7fc209156083/context_manifest_002_role_materials_advisor.json`
- bug_notes: none

Final reply:

根据本地知识库，发票付款审查至少需要以下五类核心材料：

1. 发票（Invoice）：需包含发票号码、日期、金额、税额、供应商信息、付款条款等关键字段。
2. 采购订单（Purchase Order, PO）：需包含PO编号、供应商、物料/服务描述、数量、单价、金额、审批状态。
3. 收货/服务验收记录（GRN/Service Receipt）：证明货物已收或服务已完成，需包含收货日期、数量、质检状态。
4. 供应商记录（Vendor Record）：含供应商主数据、银行账户信息、付款条款、供应商状态（活跃/冻结）。
5. 重复付款检查（Duplicate Payment Check）：核对历史付款记录、清账记录，排查同一发票多次付款风险。

当前 case 尚未提交任何材料。请准备上述材料后，可通过粘贴内容或上传附件方式提交审查。

本工具只生成本地审查材料；如需进入 ERP 流程，请由人工在 ERP 中处理。
