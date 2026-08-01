# ADR-0001：LangGraph + Deep Agents 作为唯一模型 Runtime

- 状态：Accepted（2026-08-02 复核）
- 日期：2026-07-31

## 背景

项目曾把基于 LangGraph 的循环称为 Legacy，并默认启动 Claude Agent SDK。该结构虽然统一了上层 `AgentRuntime` 契约，却没有使用 LangGraph 的 checkpoint、稳定 `thread_id`、原生 `interrupt()` 与 `Command(resume=...)`，人工审批只能作为工具错误返回，无法形成可靠的人在回路。

## 决策

1. LangGraph 是默认且长期维护的 Agent Runtime；应用层继续只依赖 `AgentRuntime.run(RunRequest) -> AgentResult`。
2. 每个业务 Run 使用稳定的 `RunContext.run_id` 作为 LangGraph `thread_id`，生产环境通过官方 SQLite checkpointer 持久化图状态。
3. 需要审批的 Capability 由 Deep Agents 原生 Human-in-the-loop middleware 调用 `interrupt()`；批准或拒绝后，用同一 `thread_id` 和 `Command(resume=...)` 恢复。
4. 审批必须发生在工具副作用之前。策略评估保持纯函数，业务审批单与审计状态由 control plane 持久化。
5. Deep Agents 是唯一模型 Harness；Claude Agent SDK 和手写 LangGraph loop 已删除，不保留运行时双轨。
6. Run 内禁止自动跨后端重试，避免外部写入被重复执行。
7. Deep Agents 不改变 LangGraph 作为唯一 checkpoint、interrupt 和恢复 Runtime 的决策；业务层仍只依赖统一 `AgentRuntime` 契约。

## 后果

- LangGraph checkpoint 是执行状态的单一事实来源，control plane 是任务、审批和审计状态的单一事实来源。
- 旧 Runtime Adapter、Provider Registry、自定义 Anthropic client 和 `USE_SDK_LLM` 被移除。
- 节点恢复会从节点开头重放，因此 `interrupt()` 前只允许确定性、无副作用的策略计算；真正的 Capability handler 只在恢复后的 `tools` 节点运行。
- SQLite 适合当前单实例部署；横向扩容前需把 checkpointer 切换为共享的生产级后端。
