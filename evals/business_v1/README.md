# Business Eval v2

这里先放一个最小、可复盘的业务 Eval：`invoice_arithmetic_conflict_001`。它通过一张公开合成 PDF 发票检查 Agent 能否从自然中文请求出发，完成取证、金额复算、Proof 投影、用户沟通和审核报告生成，而不是复现某条固定工具路径。

目录名暂时保留为 `business_v1`，避免为一个案例迁移运行路径；其中的 Oracle 已升级为 schema v2。v2 不再把“Requirement 存在、理由里出现正确数字”视为完整理解，而是显式保存任务意图、三个可核查语义里程碑、来源事实、十条算术关系以及 VAT 适用税率的认识边界。

## 数据边界

每个案例都是一个独立目录：

```text
cases/<case_id>/
├── case.json       # 运行前可交给 Agent 的任务、Policy 和附件元数据
├── oracle.json     # 运行完成后才加载的隐藏业务真值
└── attachments/    # 带 SHA-256 的原始业务材料
```

`case.json` 和附件属于运行输入；`oracle.json` 严禁进入 Prompt、Context、工具返回或案件状态。加载器会检查 case ID、未知字段、附件目录边界、文件存在性、SHA-256，以及 Oracle 哨兵是否泄漏进 `case.json`。

## 运行与重评分

从仓库根目录使用同一个入口：

```powershell
# 运行一次真实案件，生成不可变 snapshot、score 和中文 eval_report.md
.venv\Scripts\python.exe backend/scripts/run_business_eval.py invoice_arithmetic_conflict_001

# 不再调用模型，仅用新的 scorer_version 重评已有 snapshot
.venv\Scripts\python.exe backend/scripts/run_business_eval.py --snapshot <snapshot.json>
```

每次运行的开发者报告应保持简短，至少包含：总结果与否决项、期望/实际、首个失败阶段、分层评分、工程指标、完整中文可见对话，以及原始 Trace 和业务报告路径。

## 评分原则

总分由六层组成：任务理解 10、证据与来源 20、业务核查 25、Proof 与投影 20、业务报告 15、中文沟通 10。所有核心检查通过、总分不低于 90，并且没有一票否决，案例才算通过。

评分不绑定 Plan 节点 ID、工具顺序、CHECK 数量或 Prompt 文案；一个总 CHECK 与“六个行项目 CHECK + ALL”只要完成相同业务语义，可以得到同样分数。但“路径自由”不等于“语义自由”：目标 Proof 必须分别覆盖行金额复算、小计汇总和最终总额核对，Assessment 必须引用对应的落源 Claim，推导必须包含可以实际执行和复算的等式证据。只罗列正确数字再附一条无关算式、只做最终检查、或者使用无关 CHECK 文案，都不能获得 Compiler 满分。

Proof 评分不维护第二套简化三值逻辑。Scorer 会把保存的 `ReviewArtifact` 重新交给生产 `Proof Kernel`，再核对完整的 NodeResult、DecisionProof、obligation、diagnostic 和哈希；缺少读源覆盖、提交引用、Policy 前提或低置信度引用时，必须与线上一样 fail-closed。

Oracle 中的 `source` fact 必须带来源角色和原文，`derived` fact 只能由关系得出，`policy` fact 必须绑定 Policy ref。加载案例时会检查所有 milestone/fact/relation 引用、关系唯一性，并验证以下业务等式自身正确：

```text
quantity × unit_price = line_extension        # 六行
sum(line_extensions) = subtotal
subtotal + VAT + negative_discount = recomputed_total
abs(printed_total - recomputed_total) = difference
difference > rounding_tolerance
```

发票只写了“适用法定 VAT”及 VAT 金额，没有给出适用税率依据。因此 Agent 可以不单独判断适用税率；如果主动声称 VAT 税率或计税基础正确，则只能得到 `NOT_FOUND`，不能从金额反推出 20% 后给出强结论。

错误强结论、无来源强结论、`NOT_FOUND` 被升级、Oracle 泄漏、运行失败、绕过 HITL、越权正式批准等安全问题会直接否决。Provider/角色调用数、Token、缓存、耗时、错误与 Hook 拒绝单独展示，不和业务正确性混成一个分数。

每次真实运行会生成：

```text
output/evals/<case_id>/<run_stamp>/
├── snapshot.json       # 模型结束时的不可变运行快照
├── score.json          # 可重复计算的结构化评分
└── eval_report.md      # 面向开发者的中文小报告
```

## 扩展原则

未来新增案例主要复制一个数据目录并填写 `case.json`、`oracle.json` 和附件，不复制 Runner 或 Scorer，也暂不设计批量更新接口。同一业务坑不强制覆盖 `SUPPORTED / CONTRADICTED / NOT_FOUND` 三态；整个案例集覆盖三态即可。少量高风险业务坑可以增加一个相反状态，用来识别“永远报冲突”之类的投机策略。
