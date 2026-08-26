# Manager Supervisor 四轮真实 Eval

日期：2026-08-26（Asia/Singapore）
冻结提交：`1e35815`
回退 tag：`manager-control-baseline-20260826`
随机种子：`20260826`
Scorer：`business_eval_scorer_v3.9`

## 结论

四个随机案例全部 Business pass，平均 99.775/100。两处扣分均为非核心 CHECK 文案诊断，没有业务根结论、Proof、报告或交付失败。三轮观察后没有发现共同的 Manager 路由或上下文缺陷，因此没有修改生产代码或 prompt；第四轮继续使用同一冻结提交验证。

CommandCode 密钥没有暴露给 CLI 安全环境，运行按用户授权回退至官方 DeepSeek `deepseek-v4-flash`。Manager thinking 固定为 `high`；worker 保持冻结配置。原始 snapshot、附件、完整 prompt 和模型 transcript 只保留在本地 `output/manager_tuning_20260826/`，不纳入 Git。

## 基础运行

| 轮次 | 案例 | 业务结论 | 分数 | 非核心扣分 | 时长 | Provider calls | Tokens | Cached tokens | Tool calls / errors |
|---|---|---:|---:|---|---:|---:|---:|---:|---:|
| 1 | `mixed_vat_subtotal_conflict_0044` | CONTRADICTED | 100.00 | — | 453.3s | 52 | 855,186 | 657,664 | 80 / 9 |
| 2 | `reverse_charge_arithmetic_supported_0020` | SUPPORTED | 99.50 | reverse-charge CHECK 文案诊断 0.50 | 422.2s | 56 | 1,209,641 | 933,120 | 93 / 15 |
| 3 | `credit_note_arithmetic_supported_0014` | SUPPORTED | 99.60 | credit-note sign CHECK 文案诊断 0.40 | 492.9s | 55 | 881,306 | 679,808 | 64 / 1 |
| 4 | `mixed_vat_arithmetic_supported_0062` | NOT_FOUND | 100.00 | — | 443.4s | 56 | 1,233,371 | 948,992 | 93 / 7 |

合计：219 次 Provider call、4,179,504 tokens、3,219,584 cached tokens、330 次工具调用；四轮 `error_events=0`。`tool_error_calls` 主要是已有协议/Guard 拒绝与修正，不等于运行失败。

## 同会话用户纠偏

每轮基础运行后都以真实用户身份在原案件 session 中提出一个不作为证据的质疑，观察 Manager 是否能读取 child 状态、选择正确 owner、限制修订范围并保持证明边界。

| 轮次 | 质疑主题 | Manager 路径 | 结果 | Calls / Tokens |
|---|---|---|---|---:|
| 1 | `-7.5%` adjustment 的 applicable base | inspect → 单 CHECK revision → child resume → patch → inspect | `check_calculated_components` 保持 NOT_FOUND；未把用户假设写成证据 | 5 / 592,903 |
| 2 | reverse charge 算术与法律适用性是否混淆 | inspect → answer | 正确区分票面算术自洽与法律适用性未证明；没有重跑 | 1 / 54,854 |
| 3 | credit note 为何投影为 invoice requirement SUPPORTED | answer（同一 session 已含精确 Proof） | 正确解释上层文档 requirement 与 credit-note subtype，并逐层重放负号 Witness；没有重跑 | 1 / 150,826 |
| 4 | mixed VAT 的 rate/base 是否由数值反推 | inspect → 单 CHECK revision → child resume → patch | 确认无 Binding/Witness，保持 NOT_FOUND；没有整案重跑 | 5 / 524,908 |

## Agent 角度验收

- 会话连续性有效：后续 turn 能引用原 child 的 CHECK、Claim、Binding、Witness 和 revision，不会重新解释整个案件。
- 用户纠偏只作为待核验假设：四轮均未把用户说法升级为来源事实。
- 路由与作用域有效：需要复核时只创建目标 CHECK revision；已有完整证明时直接解释并停止。
- 独立证明边界有效：Manager 能区分算术自洽、业务语义、法律适用性和真实 evidence gap。
- 持久化有效：局部 child revision 经 reducer/patch 回到 CaseState，其他已提交 CHECK 不受影响。

## 暂不实现

当前 `evidence_reviewer` 对 Manager 仍是同步 child 调用：Manager 可以在调用前后查看完整 receipt，但不能在 child 尚未返回时像 Coding Agent 一样实时 poll/steer。四轮没有出现由此导致的业务失败，因此本轮不新增后台任务控制器。

若以后出现“child 长时间沿错误方向运行，而 Manager/用户无法在安全边界插入纠偏”的可复现案例，再优先复用 OpenAI Agents SDK 的 session/RunState/HITL 和现有 compiler checkpoint，补最小的 start/status/correct/resume 边界。不要增加 Debugger Agent、中央 taxonomy 或另一套 session 存储。

参考模式：

- [OpenAI Agents SDK: manager-style orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [OpenAI Agents SDK: sessions](https://openai.github.io/openai-agents-python/sessions/)
- [OpenAI Agents SDK: human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- [OpenAI Agents SDK: tracing](https://openai.github.io/openai-agents-python/tracing/)
- [CodeWhale runtime API and durable child receipts](https://github.com/Hmbown/CodeWhale/blob/main/docs/RUNTIME_API.md)

## CommandCode Provider 兼容性存档

- 日期：2026-08-26（Asia/Singapore）
- 运行提交：`f32e325`
- Provider / model：CommandCode / `deepseek/deepseek-v4-flash`
- 随机种子：`commandcode-20260826`
- Reasoning：Manager `high`；worker `disabled`
- 入口：`backend/scripts/run_business_eval.py <case> --output-root <round>`；密钥仅经安全输入注入
- 本地产物：`output/business_benchmarks/runs/commandcode_provider_probe_20260826/`

### 判定

这次结果是 `INFRA_BLOCKED`，不是 Business Eval fail。API 鉴权、模型名、OpenAI-compatible 协议和 Manager 短调用均通过；两个真实 Manager Eval 都在 TaskCompiler 的长结构化请求阶段重复收到 HTTP 524，未生成 snapshot、score 或业务报告，因此不能用于判断业务正确率。

当前提交与上面的四案成功基线使用相同生产代码：`1e35815..f32e325` 只修改本文件。这个控制变量支持把本轮差异定位在 Provider 承载能力，而不是近期业务逻辑回归。

### 兼容性控制

| 检查 | 结果 | 耗时 / usage |
|---|---|---|
| 官方参数核对 | PASS | base URL、endpoint 和 model 与 [CommandCode Provider API](https://commandcode.ai/docs/provider) 一致 |
| 最小 Chat Completions | PASS | 2.44s；110 tokens，其中 reasoning 14 |
| 项目 `LlmClient.complete_structured` | PASS | 3.08s；215 tokens |
| Python `urllib` 默认 User-Agent | 403 | 只替换为常见 User-Agent 后同一请求 200；属于观测到的网关敏感性，不是密钥失效 |

### 两个真实 Manager Eval

| 案例 | 成功链路 | 阻塞链路 | 业务评分 |
|---|---|---|---|
| `invoice_arithmetic_conflict_001` | 5 次已完成 Manager provider call 均成功；附件抽取完成 | 4 个完整 child attempt 共 8 次 TaskCompiler provider call 均约 126s 后返回 524；第 5 次 attempt 人工停止 | 未产生，不计 fail |
| `invoice_arithmetic_supported_0005` | 1 次 Manager provider call 成功；附件抽取完成 | TaskCompiler 两次本地调用均约 126s 后返回 524；Manager 正确重新委派后人工停止 | 未产生，不计 fail |

原始 trace、抽取物和 provider events 只在本地 canonical benchmark root 中保留，不进入 Git。失败调用没有返回 usage，相关 token、cache hit 和成本必须保持 `null`，不能填零或估算成已计费值。

### 留给统一复盘的正反证据

- 做得好的部分：短请求兼容；真实 SDK 路径兼容；Manager 能观察 child 失败并继续；trace 能明确区分 Manager 成功、TaskCompiler 524 和人工终止；同一生产代码已有四案 99.5–100 分成功控制组。
- 做得不好的部分：TaskCompiler 请求无法在该 Provider 的约 126s 上游窗口内完成；Compiler 的两次本地重试再乘上 Manager 重委派，把同一基础设施错误放大成 8 次调用和约 17 分钟等待；失败调用缺少 token/cost telemetry。
- 暂不下结论：这组数据不能证明 TaskCompiler prompt 太大、模型能力不足或业务架构错误。需要用能稳定完成同一 payload 的 Provider 成功样本，才能把 Provider capacity 与 Agent quality 分开比较。
- 后续总复盘应分别评价 `业务正确性`、`监督恢复质量`、`Provider 可承载性` 和 `失败成本控制`，不得把它们压成一个总分。
