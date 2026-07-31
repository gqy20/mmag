# ADR-0001：Runtime 选择与 Legacy 退出策略

- 状态：Accepted
- 日期：2026-07-31

## 背景

mmag 同时保留 Claude Agent SDK 与基于 LangGraph 的 Legacy LLM 循环。此前 `Agent` 和 `MemoryCompactor` 直接选择后端、捕获两套异常并处理 fallback，导致应用层依赖实现细节。

## 决策

1. 应用层只依赖 `AgentRuntime.run(RunRequest) -> AgentResult`。
2. `USE_SDK_LLM=true` 且 SDK 启动成功时，使用 `ClaudeSDKRuntimeAdapter`；否则使用 `LegacyRuntimeAdapter`。
3. 后端选择只发生在启动阶段。单次 Run 失败时不自动切换后端，避免能力调用或外部写入被重复执行。
4. timeout、rate-limit、rejected、unavailable 和 internal 统一翻译为 Runtime 错误；应用层不再导入后端私有异常。
5. “工具循环耗尽后无工具重试”的兼容行为由 Adapter 负责，调用方不再复制该策略。

## Legacy 退出条件

满足以下条件后，才移除 LangGraph Legacy Runtime：

- Capability schema、执行器和权限策略已有单一事实来源，并可生成两套 binding；
- 两个 Adapter 对成功、错误、deadline、多模态和能力调用通过同一组契约测试；
- Claude SDK 已具备所需 usage、能力调用记录和错误分类；
- 默认 SDK 路径经过生产观察期，并保留一个版本周期的配置回退能力；
- 移除前发布迁移说明，并删除 `USE_SDK_LLM=false` 的运维依赖。

## 后果

- 好处：应用层不感知供应商，实现切换和测试边界清晰。
- 代价：Capability 尚未统一前，两个 Adapter 内部仍连接不同工具 binding。
- 风险控制：禁止 Run 内自动跨后端重试；写能力的恢复交给后续 Inbox/Outbox 和幂等协议。
