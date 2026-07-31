# ADR 0002: Capability 写入授权边界

- 状态：Accepted
- 日期：2026-07-31

## 背景

`save_knowledge` 是首个迁移到 Capability Catalog 的写能力。SDK 原有工具名白名单只能决定工具是否可调用，Legacy ToolRegistry 没有等价的写入策略入口；只在 `CapabilitySpec` 声明 `effect=WRITE` 和权限字符串，无法阻止副作用。

## 决策

所有 Capability 在参数校验后、handler 执行前经过 `CapabilityAuthorizer`。策略返回三种确定性结果：

- `ALLOW`：继续执行；
- `DENY`：返回 `forbidden`，不调用 handler；
- `REQUIRE_APPROVAL`：返回 `approval_required`，不调用 handler，并为后续审批恢复保留稳定状态。

默认使用 `DeclaredPermissionAuthorizer`：读取能力保持兼容；写能力必须声明非空 permission，否则拒绝。SDK 工具白名单仍作为外层工具可见性边界，Capability Authorizer 是 SDK/Legacy 共享的内层业务授权边界。

## 结果

- 两个 Runtime 对写入拒绝和待审批使用同一错误结构；
- 策略裁决发生在任何持久化副作用之前；
- 可以注入组织、用户或作用域策略，而无需修改 Capability handler；
- 当前默认策略不代表已经实现按用户授权或人工审批。接入企业身份与 `RunContext` 后，需替换默认 Authorizer，并持久化审批状态。
