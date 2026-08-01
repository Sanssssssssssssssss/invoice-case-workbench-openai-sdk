# Agent Runtime / Harness 旗舰作品打磨方案（4–6 周）

## 结论与定位

截至 2026-07-31，国内 Agent 岗位和产业资料的共同要求已经从“会调用模型、会写 Prompt”转向：

1. 业务流程建模与可衡量验收。
2. Agent Runtime：状态、上下文、工具调用、检查点、恢复、并发与流式执行。
3. MCP/API/数据库等企业系统集成。
4. Eval、Trace、成本、延迟和 Bad Case 闭环。
5. 权限、人工审批、提示注入防护和数据边界。
6. Docker、CI/CD、故障诊断等普通但关键的软件工程能力。

这与中国软件行业协会的 [T/SIA 057—2026 能力标准](https://www.ttbz.org.cn/standardDetail.html?id=vyp698qyqeghl08fcuknhr6u77tbm7a)、[详细能力矩阵](https://www.cstpchina.cn/Upload/UEditor/file/2025120813404833523.pdf)，以及近期的[平台型岗位](https://www.v2ex.com/t/1228582)、[生产部署岗位](https://www.nowcoder.com/jobs/detail/425581?urlSource=sitemap)、[端到端 Agent 岗位](https://career.nankai.edu.cn/correcruit/content/id/116181.html)要求一致。

当前应用最集中在数据分析、知识检索、客服、办公与流程自动化、研发编程、运维和金融风控。[中国信通院报告](https://www.sscc.com/upload/1/editor/1761033810209.pdf)及其[首批 27 个落地案例](https://www.aidqc.com/html/web/xhyfb/cgfb/yxal/1912429982099697666.html)也表明，真正落地的是接入领域数据、业务工具和人工控制的垂直智能体。

因此继续打磨现有发票审核项目，不另建通用聊天 Agent。作品的核心论点定为：

> 大模型负责不确定性的理解与规划；确定性的 Harness 负责状态、权限、工具、恢复、审计和验收。

## 项目改造

### 第 1 周：建立可公开的作品基线

- 保留并先验证当前未提交的持久化安全、TTFT/Token 指标、golden case 和 benchmark 改动，避免重复实现。
- 修复 README 中文乱码，将首页改造成招聘人员能在两分钟读懂的产品页：问题、架构、工程取舍、快速演示、基准结果和已知边界。
- 增加中文研究文档，记录能力需求、常见场景、Agent 落地本质、岗位样本及本项目能力映射；明确这是定性样本，不伪装成全市场统计。
- 用一张 Mermaid 图展示 `Manager → Harness/Policy Gate → Tool/MCP → Case State`，Trace、Eval、审批作为横切能力。
- 固定五分钟演示故事：提交发票 → 外部重复付款检查 → 发现冲突 → 阻断提示注入 → 人工确认报告写入 → 查看 trace、成本和 benchmark。

### 第 2–3 周：补一个真实 MCP 闭环

- 增加一个独立的演示 ERP MCP 服务，仅提供只读工具：
  `check_duplicate_payment(invoice_number, supplier, amount, currency)`。
- Runtime 通过 OpenAI Agents SDK 的 Streamable HTTP MCP 支持连接；只允许上述工具，禁止动态暴露其他工具和所有外部写操作。
- MCP 结果必须带来源、时间和结构化字段，进入 artifact/observation，再经过 evidence reviewer 与 case patch 流程；不能绕过现有 Harness 直接修改业务状态。
- MCP 不可用、超时、返回非法结构时，生成明确的 runtime feedback，保留已有证据并要求人工补充，不重试失控、不伪造查询结果。
- MCP 输出按不可信外部输入处理；即使返回提示注入文本，也不能泄漏提示词、修改审批策略或触发文件写入。

### 第 3–4 周：完成 AgentOps 证据

- 完成现有 latency、TTFT、Token、缓存命中和估算成本指标，并在 Inspector 与 benchmark 报告中统一展示。
- Trace 增加 MCP 调用记录：服务名、工具名、状态、耗时和脱敏摘要；不保存密钥、完整参数或敏感原文。
- InvoiceTauBench 增加“命中重复付款”“未命中”“服务超时”“恶意 MCP 输出”四个场景。
- Benchmark 汇总增加 MCP 调用数/失败数、工具成功率、人工介入率和估算成本。
- 发布一份可复现的脱敏 benchmark 快照，同时诚实保留失败案例和根因分析。

### 第 4–6 周：交付与作品包装

- 增加 GitHub Actions：Python 测试、前端测试与类型检查、构建、`scripted_full` benchmark、Docker 构建。
- Docker Compose 只包含 FastAPI 后端和演示 ERP MCP 服务；Electron 仍作为本地桌面入口，不为展示强行改造成云端 SaaS。
- 无模型密钥时可运行确定性 benchmark；有密钥时运行 `chain_live_core --k 3`。
- 补一组 DeepSeek OpenAI-compatible 配置示例，当前模型名使用官方文档中的 `deepseek-v4-flash`；不新增模型路由框架。
- 输出简历项目描述、架构取舍清单、常见面试追问和五分钟演示脚本，使代码、文档、指标能够相互印证。

## 公共接口与数据契约

- 新增可选配置：
  - `INVOICE_AGENT_MCP_URL`：为空时完全保持现有行为。
  - `INVOICE_AGENT_MCP_TIMEOUT_SECONDS`：默认 5 秒。
- 新增 trace 事件 `mcp_tool`，载荷固定为：
  `server`、`tool`、`status`、`duration_ms`、`input_summary`、`output_summary`、`error_type`。
- Benchmark summary 增加：
  `mcp_calls`、`mcp_errors`、`tool_success_rate`、`human_intervention_rate`、`estimated_cost`。
- 现有 REST、SSE、审批和 case-state 接口保持兼容，不改变已有客户端调用方式。

## 测试与验收

- 全部现有 Python、桌面及 renderer 测试通过；CI 总超时设为 10 分钟。
- `scripted_full` 达到 100% contract pass。
- Live core 执行三次，安全场景必须 100% 通过；公开 pass@1、pass-all-k、Judge 分数、Token、成本和延迟，不挑选单次最好结果。
- MCP 覆盖连接、发现、正常调用、超时、非法结构、断线和提示注入。
- MCP 调用不能绕过 ToolCatalog/Harness 的状态与审计边界；任何写文件动作仍需现有人工审批。
- 保持当前流式性能目标：首个 SSE p95 小于 300ms、事件循环延迟 p95 小于 50ms。
- `docker compose up --build` 后两个服务健康；无密钥仍可运行 scripted demo。

## 明确不做

- 不增加新的 Agent、通用编排平台、A2A、Computer Use、GraphRAG、微调、Kubernetes、消息队列或 RBAC。
- 小型规则知识库没有证据需要 GraphRAG；本地单用户桌面没有证据需要分布式平台。
- 不把框架数量当作能力证明，重点展示 Runtime、故障路径、工程边界、评测数据和技术取舍。
- Agent Reach CLI 当前未安装，因此本轮研究使用可用联网搜索和中文原始资料完成；它不作为项目运行依赖。
