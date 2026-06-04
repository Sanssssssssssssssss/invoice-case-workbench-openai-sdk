# Harness Context Compiler

本项目的 Harness 不再只是 trace recorder 和 step limiter。当前版本加入了一个轻量 Context Compiler，用来决定哪些内容进入 Planner/Role prompt，哪些内容只进 trace artifact。

## 核心链路

每次工具或 role 返回结果后，Harness 做四步：

1. 保存原始输出到 artifact：
   `workspace/cases/{case_id}/traces/artifacts/{run_id}/art_*.json`
2. 对原始输出生成摘要：
   - 小结果走本地启发式摘要
   - 大于阈值的结果调用 `summarizer` LLM role
3. 把摘要写入 `state.observations`
4. 下一次 Planner 调用时只传 `context_pack`

也就是说，Planner 不再直接读取附件全文、完整 role result、完整 RAG snippets 或完整 report markdown。

## Planner 看到什么

Planner 输入只有：

```json
{
  "case_brief": "case_id=...; status=...; requirements=...",
  "current_goal": "...",
  "current_plan": ["..."],
  "attachments": [{"name": "...", "path": "..."}],
  "recent_observations": [
    {
      "kind": "tool",
      "name": "read_attachment",
      "summary": "读取 6 个附件，属于 PR 采购申请材料，不满足发票付款审查核心证据。",
      "artifact_ref": "traces/artifacts/run_x/art_001_attachment_batch_read_attachment.json",
      "next_action_hint": "call_role:evidence_reviewer"
    }
  ],
  "open_questions": [],
  "next_expected_action": "call_role:evidence_reviewer"
}
```

Planner 只做调度决策，不靠 raw text 做审查。

## Role 如何拿到原文

Role 不直接吃 Planner 的摘要。Harness 会按 role 类型补充需要的 artifact 原文：

- `evidence_reviewer`：注入最近一次 `read_attachment` 的原始附件内容
- `case_patch_writer`：注入最近一次 role 的原始 JSON 结果
- `materials_advisor`：注入最近一次 RAG artifact 中的 selected snippets
- `report_writer`：注入压缩后的 case evidence 和 RAG context

这样可以避免 Planner 分心，同时保证真正做审查的 role 仍然能看到证据原文。

## 记忆分层

当前实现对应五层记忆：

- System prompt：固定边界、工具/role 能力、禁止 ERP 执行动作声明
- Working memory：`short_plan`、`plan_progress`、`next_action_hint`
- Case memory：`case_state.json` 里的材料状态、风险、缺口、证据项
- Evidence memory：artifact store 中的原始附件、role result、RAG result、报告内容
- Summary memory：`state.observations` 中的短摘要

## 判断标准

一次 Planner 调用中不应出现：

- 附件全文
- 完整 role result
- 完整 report markdown
- 完整 RAG snippet 列表
- 完整 case_state evidence content

真实回归中，INV-5001 和 PR-1001 的 Planner input preview 均未出现附件正文关键短语，原文只保存在 artifact 中。
