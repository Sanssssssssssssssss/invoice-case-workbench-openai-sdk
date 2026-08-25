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
