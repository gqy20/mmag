# ADR-0011：统一 Run Control Plane 与可观测事件模型

- 状态：Proposed
- 日期：2026-08-15

## 背景

MMAG 当前已经具备单次 AgentRun 的 Agent/Skill 解析、Capability 治理、LangGraph checkpoint、原生审批、
AuditEvent、结构化日志、Artifact 和 Outbox。专业请求既可以由应用层 `AgentRouter` 直接选择目标 Agent，
也可以由默认 `mmchat` 通过 `delegate_*` Capability 同步调用子 Agent。

当前 delegation 已不再传递截断自由文本，并能解析固定 Agent/Skill、继承可信 actor/scope、返回结构化
结果及记录父子审计关系；但它仍直接 `await agent.run()`，子 run ID 不是重放稳定键，也没有独立生命周期、
父 checkpoint 等待状态或跨 Agent 审批恢复。因此它只能作为过渡实现，不能视为完整多 Agent 调度。

日志侧已经有 `log_event()`、`LogContext`、Deep Agents Callback 和独立 AuditEvent，但关联字段、事件名称、
状态词汇和 details Schema 尚未统一。当前诊断主要按一个 `trace_id` 平铺日志与审计，不能可靠还原父子 Run、
CapabilityCall、Approval、Artifact 和 Delivery 的因果图。

## 决策方向

本 ADR 在 Accepted 和对应门禁完成前只作为迁移目标，不代表代码已经支持以下能力。

### 1. 保持模块化单体，划分六个逻辑层

```text
Channel Adapter
  → Product Workflow
  → Run Control Plane
  → Agent Runtime
  → Capability Plane
  → State / Artifact / Delivery
```

- Channel Adapter 只接收平台事件并解析可信身份与资源；
- Product Workflow 管理会议纪要、任务草案、OKR、提醒、数字人和交付等确定性业务状态机；
- Run Control Plane 管理 AgentRun、父子关系、幂等、等待、审批恢复和 provenance；
- Agent Runtime 继续只使用 Agent Package、Skill 和 Deep Agents/LangGraph 完成认知工作；
- Capability Plane 是 Task、Lark、MCP、Mattermost 和执行入口的唯一动作边界；
- Control Plane、checkpoint、Memory、Artifact 和 Outbox 分别保存各自事实，不用日志反推业务状态。

不因逻辑分层拆分微服务。没有容量、隔离和部署证据时继续保持模块化单体。

### 2. 简化默认会话 Agent

`mmchat` 负责开放式对话、必要澄清和结构化结果汇总。可以由 `AgentRouter` 直接选择的单一专业请求，不再
经由 `mmchat → delegate_* → 专业 Agent` 形成第二套路由。跨 Agent 只用于真实多阶段 Workflow，目标
Agent/Skill 由服务端注册定义，模型不能用自由文本指定任意 Package 或扩大权限。

### 3. 用 RunCoordinator 取代同步 AgentDispatcher

目标实现使用应用层 `RunCoordinator`（名称以实现时现有模块边界为准）统一启动和恢复主/子 AgentRun。
模型侧可以看到一个薄的 Workflow/Delegation Tool，但该 Tool 只提交受治理命令，不直接同步拥有子 Agent
生命周期。

子运行至少持久化：

- `run_id`、`parent_run_id`、`workflow_id`、`parent_tool_call_id`；
- 固定 Agent/Skill ref 与不可变 Package snapshot；
- 原 actor、scope、Capability 与 Execution Profile 快照；
- 稳定 `execution_key`、checkpoint `thread_id`、状态、usage 和错误；
- 结构化 result、Artifact refs 和 provenance。

`execution_key` 由父 run、父 Tool call、目标 Agent/Skill 和 Package snapshot 的可信字段确定性生成。
LangGraph 重放只能读取或恢复同一个子运行，不能创建第二个外部副作用链。

AgentRun 生命周期增加 `waiting_child`。子 Agent 遇到审批时，子 Run 进入 `waiting_approval`，父 Run 进入
`waiting_child`；审批只使用子 Run 原稳定 `thread_id` 恢复子图。子 Run 完成并提交结构化结果后，再恢复
父图。父 Tool 节点重放时按 `execution_key` 读取已完成结果，不重复执行子 Agent。

不得把 `waiting_approval` 作为普通 Tool JSON 交给父模型猜测，也不得把子审批拍平成使用父 `thread_id`
恢复的审批。

### 4. 明确身份、会话和记忆层次

默认保持一个企业 Surface Bot。Agent Package、Skill、Capability Catalog 和 Runtime 全局共享；actor、
owner、tenant、conversation、Run、checkpoint、Memory、Artifact、Approval 和 Delivery 按可信 Scope 隔离。
不按用户或群聊自动创建物理 Bot。

记忆分为：

- Run Memory：LangGraph checkpoint 与当前 Skill 工作文件；
- Conversation Memory：Thread、近期消息和有界摘要；
- Personal Memory：owner 的偏好、事实、关系与承诺；
- Shared Knowledge：带 ACL/Scope/来源的团队决策和项目知识；
- Source/Artifact：原始文档、会议纪要、报告和二进制产物。

文档继续留在源系统。MMAG 默认按需读取 SourceRef，只保存有界缓存、显式记忆、结构化业务实体和
provenance，不把全文复制成无边界的全局记忆。

### 5. 统一观测事件，不统一事实存储

Lifecycle State、Audit Event、Operational Log、Metrics 和 Trace 使用同一个关联模型，但保留不同职责与
存储。普通日志不是业务事实，AuditEvent 不承担性能分析，Metrics 不包含高基数业务身份。

统一事件 Envelope 至少包含：

```text
schema_version, event_id, timestamp, event, status
trace_id, workflow_id, run_id, parent_run_id
span_id, parent_span_id, thread_id
task_id, capability_call_id, approval_id, artifact_id, delivery_id
actor_id, scope_id, agent_ref, skill_ref, capability, policy_ref
duration_ms, attempt, error_code, attributes
```

事件命名使用 `<domain>.<entity>.<action>`，状态词汇收敛为 `queued/running/waiting_child/
waiting_approval/succeeded/failed/rejected/cancelled/exhausted`。事件专属 attributes 使用版本化 Schema 和
字段 allowlist，禁止记录 Prompt、消息/文档正文、完整 Tool 参数/结果、Secret、未脱敏异常或 Artifact
内容。

关键生命周期迁移与 AuditEvent 在控制面事务内原子提交；提交后再投影普通日志、低基数 Metrics 和可选
Trace。业务代码不再手工维护两份语义可能不同的 `log_event()` 与 `append_audit()`。

### 6. 诊断输出运行因果图

`debug-trace` 最终应支持从 message/post、trace、workflow、run、approval、artifact、task 或 delivery
标识定位同一运行图，并输出父子 Run 树、当前状态、路由与工具投影、Policy、审批、Artifact、Delivery、
根因和状态矛盾。Audit 不采样；错误与终态日志不采样；Metrics 只使用 agent、skill、capability、status、
error_code 等低基数标签。

## 当前实现与迁移边界

当前已实现：

- 单 AgentRun 的稳定 LangGraph `thread_id`、checkpoint 和审批恢复；
- Agent/Skill Capability 精确投影与二次强制收窄；
- delegation 的固定目标、可信 actor/scope 继承、结构化结果和父子审计字段；
- 内容无关 Runtime Callback、结构化日志、AuditEvent 和按 trace/run 查询。

尚未实现：

- 独立持久化子 AgentRun 与稳定 delegation `execution_key`；
- `waiting_child`、子审批完成后自动恢复父图；
- 原子生命周期/审计事件记录器；
- 完整事件目录与 attributes Schema；
- 父子运行图诊断、Metrics 和 OpenTelemetry exporter。

迁移采用单向替换：当 RunCoordinator 覆盖现有行为并通过崩溃/重放/越权测试后，删除过渡
`AgentDispatcher`，不保留两套调度路径或兼容转发层。

## 验收条件

1. 一个专业请求只有一个确定的首跳路由；多阶段协作由显式 Workflow 创建子 Run；
2. 任意 checkpoint 重放不会创建重复子 Run 或重复外部副作用；
3. 子审批只恢复子 `thread_id`，父 Run 在子终态后恢复；
4. 任意运行可回答谁、在哪个 Scope、使用什么 Package/Skill/Policy、为何允许、执行了什么和交付到哪里；
5. `debug-trace` 可重建父子 Run、Capability、Approval、Artifact 和 Delivery 因果树；
6. 普通 telemetry 不包含 Secret、正文、完整参数或结果；
7. 默认单 Bot 服务多用户和多会话，隔离语义不依赖 Bot Account 数量。
