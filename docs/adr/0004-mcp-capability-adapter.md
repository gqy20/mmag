# ADR 0004: 外部 MCP 统一适配为 Capability

- 状态：Accepted
- 日期：2026-07-31

## 背景

旧路径把 MCP discovery 结果直接注册为裸 `Tool`，只经过独立名称白名单；模型 Runtime 还曾维护另一套静态工具列表。两套入口因而拥有不同的 schema、工具集合、来源格式和授权入口。

## 决策

- `.mcp.json` 是外部 Server 连接、启停状态和平台精确工具清单的唯一配置源；
- 配置使用严格 JSON Schema，在应用启动时只加载一次并生成带 Hash 的不可变快照；Agent 装配与 MCP Bridge 必须使用同一快照；
- 每个白名单命中的 discovery 结果先转换为唯一 `CapabilitySpec`；
- MCP `readOnlyHint=true` 映射为 `READ`，缺失或 false 保守映射为 `WRITE`；权限名为 `mcp:<server>:<tool>:invoke`；
- 同一 Spec 与 `CapabilityExecutor` 生成 CapabilityRegistry 和 Deep Agents binding，参数校验、授权、超时、错误及来源策略不再由 transport 单独实现；
- Agent Manifest 分配 MCP Capability，Skill 只能缩小，Package Policy 负责逐调用授权；
- 删除 SDK crawl 专用工厂、重复 formatter 和直接依赖。crawl 能力与其他外部 MCP 一样显式配置、发现和授权。

## 结果

- Deep Agents/LangGraph 对外部 MCP 具有统一可见性、完整 JSON Schema 和 Policy 结果；
- 拒绝或待审批发生在 `session.call_tool` 副作用之前；
- MCP 返回内容进入共享来源归一化逻辑；
- 未声明只读的第三方能力默认按写能力治理；
- stdio 子进程只继承最小运行环境加 Server 显式 `env`；SSE 与 Streamable HTTP 使用各自原生传输；修改配置需要重启，以保证一个 Run 不跨越配置快照。持久逐调用审计和细粒度 MCP 资源语义仍属于后续治理阶段。
