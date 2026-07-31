# ADR 0004: 外部 MCP 统一适配为 Capability

- 状态：Accepted
- 日期：2026-07-31

## 背景

旧 Legacy 路径把 MCP discovery 结果直接注册为裸 `Tool`，只经过名称白名单；Claude SDK 则忽略这批连接，另外硬编码加载 `sdk_crawl_tools.py` 并使用静态权限列表。两套 Runtime 因而拥有不同的 schema、工具集合、来源格式和授权入口。

## 决策

- `.mcp.json` 仍是外部 Server 的连接配置，`MCP_ALLOWED_TOOLS` 仍是默认拒绝的可见性边界；
- 每个白名单命中的 discovery 结果先转换为唯一 `CapabilitySpec`；
- MCP `readOnlyHint=true` 映射为 `READ`，缺失或 false 保守映射为 `WRITE`；权限名为 `mcp:<server>:<tool>:invoke`；
- 同一 Spec 与 `CapabilityExecutor` 生成 ToolRegistry 和 SDK binding，参数校验、授权、超时、错误及来源策略不再由 transport 单独实现；
- Claude SDK 权限回调从实际 binding 集合构造 allowlist，不维护第二份工具名单；
- 删除 SDK crawl 专用工厂、重复 formatter 和直接依赖。crawl 能力与其他外部 MCP 一样显式配置、发现和授权。

## 结果

- SDK 与 Legacy 对外部 MCP 具有相同的可见性、完整 JSON Schema 和 Policy 结果；
- 拒绝或待审批发生在 `session.call_tool` 副作用之前；
- MCP 返回内容进入共享来源归一化逻辑；
- 未声明只读的第三方能力默认按写能力治理；
- MCP 子进程环境隔离、持久审计和按 actor/scope 授权仍属于后续治理阶段。
