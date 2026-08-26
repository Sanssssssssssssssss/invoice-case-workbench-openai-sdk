# Manager Active Supervision 验收

日期：2026-08-26（Asia/Singapore）

## 结论

本轮把 Manager 对 Evidence Compiler 的边界收敛为：持久化 child receipt、增量事件游标、可恢复审批 checkpoint、每个 durable frontier 重置 Executor session、失败 frontier 暂停并返回 Manager 检查。没有增加 Debugger Agent、第二套 session、轮询服务或中央 failure taxonomy。

三个真实案例均得到正确业务根结论；0025 从真实前端完成附件上传、连续 trace、两次 HITL 审批和 Markdown/PDF 交付。CommandCode 短请求兼容，但长 TaskCompiler 请求出现 HTTP 524，因此三个正式验收使用官方 DeepSeek `deepseek-v4-flash`。密钥、原始附件、完整 prompts 和 transcripts 不进入 Git。

## 模块提交

| Commit | 模块 |
|---|---|
| `666915b` | provider 前缀下的 thinking 配置识别 |
| `807439c` | Compiler correction 幂等化 |
| `6b14e31` | lossless compiler event cursor |
| `11293c9` | 审批 checkpoint 可恢复 |
| `e4bbc1c` | durable frontier 间重置 Executor session |
| `739db16` | 失败 frontier 暂停并返回 supervision receipt |
| `d17047b` | paused child 禁止进入 patch delivery，必须先 inspect |

回退点：tag `manager-control-pre-active-supervision-20260826`（`b99cc24`）。

## 三个真实验收

| 案例 | 入口 | 业务结果 | 分数 | Provider calls | Tokens / cached | 时长 |
|---|---|---|---:|---:|---:|---:|
| `invoice_subtotal_conflict_0006` | Business Eval + 同 session HITL | `CONTRADICTED`，正确 | 89（raw 96.9） | 58 | 1,062,359 / 797,440 | 645.9s |
| `tax_inclusive_arithmetic_supported_0053` | Business Eval | `BUSINESS_EVIDENCE_GAP`，正确 | 89（raw 90.05） | 51 | 915,199 / 700,544 | 497.5s |
| `invoice_total_conflict_0025` | 真实前端 | `CONTRADICTED`，正确 | 99.5，Business pass | 52 | 794,440 / 618,368 | 474.6s |

### 0006

- 首次运行暴露确定性 PolicyGate 冲突：Compiler 已暂停，但旧 gate 强迫未完成 child 进入 CasePatch。
- 最小修复后，Manager 只允许 inspect paused child；根结论、Proof、报告和交付均正确。
- HITL 中，用户指出 final-total 可能遗漏含税语义。Manager inspect 精确 child，第一次误把 Claim ID 当 evidence ref，被确定性工具拒绝；同一 Manager loop 随即改用 admitted source ID，创建 revision 2，只定位 `check_final_total`，没有擅自 resume、报告或 patch。
- 余下扣分是 `tax_inclusive` Claim 未进入 final-total lineage，不是 supervision/runtime 回归。

### 0053

- Kernel、Witness、根结论和报告生成正确：调整率的 applicable base 无直接证据，因此保持业务证据缺口。
- 扣分来自未把源文 `The subtotal including VAT...` 独立绑定为目标 Claim，连带报告没有命中该缺口的标准中文表达。
- 不为这一随机模型遗漏增加规则或重跑。

### 0025 前端端到端

- UI 实时显示同一 durable child run、revision、当前 CHECK、Executor/Verifier/Kernel 事件和 `1/6` 到 `6/6` 进度。
- Manager 在 Compiler 完成后选择 CasePatch；用户仅批准 `write_case_file` 与 `render_pdf`，没有业务纠偏。
- 最终报告正确指出：票面 `13,156.92 EUR`，独立重算 `13,563.84 EUR`，差额 `406.92 EUR`。
- Business scorer：证据 20/20、业务核查 25/25、Proof 20/20、报告 15/15、中文沟通 10/10；总分 99.5。框架 FAIL 仅因一次可恢复 `bind_claim` Hook rejection 与 `max_tool_errors=0` 冲突。

本地证据：

- `output/business_benchmarks/runs/20260826T122100_036982Z/`（0006）
- `output/business_benchmarks/runs/20260826T123456_583382Z/`（0053）
- `output/frontend_acceptance/0025/`（0025 snapshot、score、报告与 trace）
- `output/playwright/.playwright-cli/page-2026-08-26T13-00-57-271Z.png`（前端最终截图）

## 前端验收修复

真实运行发现 Compiler child 卡片在 `2/6 CHECK`、Fine Verifier 仍运行时误显示“已完成”。根因是 UI 把每个 CHECK 都会产生的阶段性 DecisionProof 计数当作整条 child 完成信号。

修复后只有全部唯一 CHECK `frontier_committed` 才显示完成；`frontier_rolled_back` 不计完成。Renderer 全量 69/69、typecheck 通过；同一条真实 0025 trace 回放验证中途为 running、最终 `6/6` 才 completed。

## 当前边界

Manager 不是持续消费每个健康 child 事件的并发模型。当前成熟边界是：UI 连续观察；Runtime 在高信号失败 frontier 暂停；Manager 收到精确 receipt 后 inspect/recheck/resume。只有出现可复现的“健康运行中途必须由 Manager 主动打断”需求时，才考虑新增可取消后台 child handle。
