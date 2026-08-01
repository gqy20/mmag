# ADR-0006：动态资源 Policy、网络隔离与 Inbox replay

- 状态：Accepted
- 日期：2026-08-01

## 背景

全局 Bot 曾使用默认 ALLOW，Policy 只比较 action、permission、actor、scope 和 role，却忽略 Capability 的动态参数。模型因此可能把 `channel_id` 指向其他频道。外部 MCP stdio 还继承完整父进程环境，URL 客户端自动跟随重定向，失败 Inbox 也只有终态记录而没有受控回放语义。

## 决策

1. 全局 Bot 绑定版本化 `global-bot@1.0.0`，所有未匹配能力默认拒绝；外部 MCP 使用同一个 `CapabilityExecutor`，精确白名单只控制可见性，不代表执行授权。
2. `GovernanceContext.resources` 保存由入口解析的可信资源。Policy 的 `resource_arguments` 声明“Capability 参数 → 上下文资源”映射；参数缺失、资源缺失或值不相等都不匹配该规则。
3. 当前默认策略把频道读取与知识写入限制在来源会话，把画像读取限制为当前 actor；文件外发和所有 MCP 调用要求审批。
4. MCP stdio 子进程只继承运行必需环境，Server Secret 必须在 `.mcp.json` 中显式引用；日志不输出解析后的 endpoint。
5. HTTPX 禁用自动重定向和环境代理。每个重定向目标在发请求前重新执行协议、DNS 和公网 IP 校验，最多五跳。
6. 失败 Inbox 不原地复活。replay 克隆新事件并要求调用方提供幂等 `replay_id`，原失败记录保留，来源关系写入 payload 与 AuditEvent。

## 结果与边界

权限裁决现在可以阻止模型参数造成的频道/用户横向越权，MCP 与 URL 不再拥有明显的环境和重定向旁路，失败任务也可在保留证据的前提下恢复。

`resource_arguments` 只是比较机制；组织成员关系、项目归属和资源解析仍必须由可信 Scope Resolver 提供。最小环境不是 OS 沙箱，逐跳 DNS 检查也不能替代出站防火墙。DLQ 当前只有应用服务 API，认证管理端点、批量操作、告警和审批仍需在管理面实现。
