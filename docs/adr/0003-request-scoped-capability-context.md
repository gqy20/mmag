# ADR 0003: 请求级 Capability 上下文

- 状态：Accepted
- 日期：2026-07-31

## 背景

旧 `send_file` 通过 `Agent.tool_context.current_post` 读取当前消息。该对象是整个 Agent 共享的可变单槽；两个请求一旦交错，后到消息会覆盖先到消息，导致文件意图、频道或回复线程串线。

Claude SDK 还有一个额外约束：持久客户端在启动时创建独立的 MCP reader task，后续 Agent task 中设置的 `ContextVar` 不会自动传播到这个已经存在的后台任务。

## 决策

- 使用 frozen `CapabilityContext` 表达 trace、actor、conversation、原始消息 ID、消息文本和 scope；
- `Agent` 只在一次 Runtime 调用的生命周期内通过 `ContextVar` 绑定上下文，并在 `finally` 语义下恢复旧值；
- Capability handler 通过只读 provider 获取上下文，不持有或修改 Mattermost post；
- Claude SDK 查询使用实例级锁串行化，并在查询开始时桥接当前不可变上下文、结束时无条件清空；SDK 工具读取该桥接 provider；
- `send_file` 作为 `WRITE` Capability 声明 `mattermost:file:write`，继续在副作用前检查用户是否明确请求文件、文件大小和频道边界。

## 结果

- 删除了全局 `ToolContext.current_post` 和 SDK 专属的手写返回 formatter；
- 并发 task 的普通 Capability 上下文互相隔离；
- 持久 SDK transport 不会在并发查询间复用错误上下文；
- SDK 查询当前仍是串行资源。跨会话并行要在 Step 4 的分区调度中结合 Runtime 实例/会话策略设计，不能仅移除这把锁；
- 外部 MCP 尚未进入该上下文和 Policy 链路，是下一阶段的明确工作项。
