# ADR-0002：Capability 写入授权与原生审批边界

- 状态：Accepted（2026-07-31 修订）
- 日期：2026-07-31

## 决策

所有 Capability 在 handler 执行前经过 `CapabilityAuthorizer`，并返回三种确定性结果：

- `ALLOW`：进入工具执行；
- `DENY`：返回 `forbidden`，不调用 handler；
- `REQUIRE_APPROVAL`：LangGraph `review_tools` 节点原生暂停，不把它伪装成工具错误。

`PolicyCapabilityAuthorizer` 只计算策略，不创建审批单。LangGraph Runtime 将待审工具名、参数、原因、`thread_id` 和 interrupt id 作为结构化中断返回；应用控制面据此持久化 `ApprovalRequest`。人工可选择 `approve`、`edit` 或 `reject`，其中批准与修改使用 `CapabilityExecutor.execute_approved()`，避免恢复后重复授权或重复创建审批单。

## 不变量

1. `interrupt()` 前不执行 Capability handler。
2. 恢复必须使用原 `thread_id`；interrupt id 用作审批幂等 token。
3. 被拒绝的调用只向模型回填结构化拒绝结果，不产生外部副作用。
4. 修改参数后重新执行 schema 校验。
5. Task、AgentRun 与 ApprovalRequest 的业务状态必须和图的暂停/恢复同步。

## 当前交互

Mattermost 收到暂停结果后发送审批 ID。同一作用域内的用户可回复：

- `批准 <approval_id>` / `approve <approval_id>`
- `拒绝 <approval_id>` / `reject <approval_id>`

审批人角色与更细粒度 RBAC 仍由后续企业身份策略补充；当前至少强制审批请求与回复处于同一 Mattermost scope。
