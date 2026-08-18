# LLM 增量开发闸门

每次只增加一个最小能力；未通过真实案例前，不继续叠加架构。

每一阶段都必须查看：

- 实际 Prompt、注入的 Context 与模型原始输出；
- Compiler 接受、拒绝或保留 UNKNOWN 的原因；
- 问题属于 Prompt、Context、Schema、模型语义还是 Kernel。

一次只改一个变量。通过后保存 Trace 和离线 Golden；测试通过不能代替检查真实模型输出。
