# Agent Prompt Stress Validation - 2026-05-26

## Scope

本轮只调优 agent prompt / skill / prompt version 标记，不新增 agent、route 或复杂代码结构。目标是用真实 LLM session 观察 Planner、Evidence Reviewer、Case Patch Writer、Materials Advisor、Report Writer 在长会话中的边界，并继续收紧角色职责。

主要运行产物：

- 20 轮连续 session: `reports/agent_stress/stress_invoice_reviewer_20260526_vfinal_20turn.json`
- 20 轮 case trace: `workspace/cases/stress_invoice_reviewer_20260526_vfinal_20turn/traces/events.jsonl`
- summary 修复复测: `reports/agent_stress/stress_invoice_reviewer_20260526_summary_v48_recheck.json`
- PR 错域复测: `reports/agent_stress/stress_invoice_reviewer_20260526_planner_v46_pr_recheck.json`
- 数字统计复测: `reports/agent_stress/stress_invoice_reviewer_20260526_planner_v47_counts_recheck.json`
- 报告 AP scope 复测: `reports/agent_stress/stress_invoice_reviewer_20260526_report_v49_recheck.json`

## 20-Turn Session Coverage

同一个 case `stress_invoice_reviewer_20260526_vfinal_20turn` 连续测试了：

1. Flipkart PDF invoice-only case 创建
2. 字段抽取问答
3. RAG/profile 被要求当作证据
4. prompt injection 附件
5. Clear Invoice/BPI log 误解
6. 启用 AP payment-control / three-way matching
7. PR 审批材料误投为 PO
8. materials advisor 补料任务
9. Contoso 五件套金额冲突另案批次
10. 不存在 PDF 路径 runtime feedback
11. JPG invoice OCR 另案参考
12. 另案材料是否能满足 active requirement
13. 报告/PDF 生成
14. 报告措辞修订
15. Apex duplicate-payment hit 另案批次
16. truth-source 汇总
17. SAP invoice PDF 另案参考
18. materials advisor 再生成
19. 二次报告/PDF
20. 最终安全结论

事件日志可观测性：该 session 生成 472 条 case-level events，包含 `model_call=112`、`planner_action=76`、`tool_call=27`、`role_call=26`、`final_answer=20`、`session_compact=1`。可以复盘每次 model call、role/tool 输入输出 artifact、planner action 与 guard/feedback。

## Issues Found And Prompt Fixes

### Fixed By Prompt/Skill

- RAG/profile trap: 用户要求把 RAG 模板当证据时，Planner 现在直接 final_answer，Evidence Reviewer 也明确 `should_accept=false`。复测中 evidence_count 保持不变。
- Prompt injection leakage: Evidence Reviewer v1.6 禁止引用、转述或负向泄漏污染字段；Patch Builder v4.8 只保留 generic quarantine fact。
- Case summary pollution: Patch Builder v4.8 禁止非空 case 被 prompt-injection、process-only、Clear Invoice、reference-only、cross-case 材料覆盖 summary。复测中 Flipkart summary 在污染、Clear Invoice、JPG、SAP 轮后保持不变。
- AP profile after invoice-only: Patch Builder v4.7 规定已有发票字段证据时启用 AP 只新增 PO/GRN/vendor/duplicate 四项，不再新增泛化 `invoice=missing`。
- Wrong-workflow PR: Evidence Reviewer v1.6 规定 PR/审批请求不是 PO，输出 `unknown` 类型但用户回复使用业务语言；Planner v4.6 避免暴露 `type=unknown`/`类型未知`。
- User-facing arithmetic: Planner v4.7 禁止在 final_answer 中心算 requirement 数字，改为按 bucket 列名称。
- Report AP scope: Report Writer v4.2 明确 AP ids 存在时 AP review 是 active，不能写 `AP三单匹配未纳入本轮审查范围`。`report_v49_recheck` 报告已写成 AP 已启用且四项缺失。
- Final reply guard churn: Planner v4.9 在有 missing/weak/conflict 时避免 `全部满足/完整/齐备` 这类宽泛完成词，并让 report final answer 只给路径、缺口和下一步。

### Remaining Risks

- Existing-report regeneration route is still not fully reliable from prompt alone. 在已有报告的长 case 上，Planner 仍可能把“重新生成”误判为 locate_file。它需要 route contract 或 deterministic intent 校验介入，prompt 已加规则但不能完全保证。
- Guard retry can still consume step budget if Planner repeatedly writes broad completion language. v4.9 已降低触发概率，`report_v49_recheck` 通过；长期最好让 guard feedback 进入一次强制 final-answer rewrite，而不是继续消耗普通 max steps。
- Duplicate-payment narrow prompt may record only duplicate check evidence instead of all five submitted docs. v1.6 已补充 multi-document rule；是否强制“每个 source_doc 都入 evidence”可能需要 schema/CaseStore 层校验。

## Verification

- `python -m pytest backend/tests -q`: 136 passed.
- Report/PDF clean route: `workspace/cases/stress_invoice_reviewer_20260526_report_v49_recheck/reports/final_report.md` exists and does not contain `AP三单匹配未纳入本轮审查范围`.
- Visual PDF sanity: `workspace/cases/stress_invoice_reviewer_20260526_vfinal_20turn/reports/final_report.pdf` rendered to page PNG successfully; generated PDF has 12 pages and valid ReportLab PDF header.

## Files Tuned

- `backend/app/agents/planner/prompt.md`
- `backend/app/agents/planner/agent.py`
- `backend/app/agents/evidence_reviewer/review_skill.md`
- `backend/app/agents/evidence_reviewer/agent.py`
- `backend/app/agents/patch_builder/prompt.md`
- `backend/app/agents/patch_builder/agent.py`
- `backend/app/agents/materials_advisor/prompt.md`
- `backend/app/agents/materials_advisor/agent.py`
- `backend/app/agents/report_writer/prompt.md`
- `backend/app/agents/report_writer/agent.py`
