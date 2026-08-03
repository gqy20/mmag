# Mattermost 原生体验接入规划

- 状态：Proposed
- 日期：2026-08-01
- 范围：Mattermost Adapter、回调入口、交互展示、原生检索与企业协作投影

本文整理 MMAG 后续可接入的 Mattermost 原生接口和功能。它是实施设计，不代表当前代码
已经支持全部能力。实现时必须保持 LangGraph、Policy、Inbox/Outbox、Artifact 和审计边界。

## 目标

1. 降低频道噪声，用 Reaction 和 Ephemeral 表达轻量状态。
2. 让用户通过 Slash Command、按钮和 Dialog 可发现地操作 Agent。
3. 通过编辑、删除、Reaction 和 Thread 事件完善人在回路和反馈闭环。
4. 用 Mattermost 原生 Thread/Search/MCP 增强上下文，但不绕过 MMAG 的动态资源授权。
5. 在有企业需求时将 Task 投影到 Priority、Acknowledgement、Scheduled Message 或 Playbooks。

## 当前基线

当前已实现：

- WebSocket `posted` 事件接入；
- Bot Token 的 Mattermost REST Client；
- 普通 Post、Thread reply、Post update、typing 和文件上传；
- `ResponseView` 到 Mattermost Markdown/attachment/action 的确定性渲染；
- LangGraph 文本增量到单一 Post 的流式更新；
- 带 HMAC、时效和一次性消费的批准/拒绝回调；
- `/mmag` 与 `/mmag help` 的 Token 认证和 Ephemeral 命令发现；
- Inbox 去重与 DLQ，以及最终响应、Artifact 和 action 的持久 Outbox。

项目文档记录的当前 Mattermost Server 为 `11.7.0` Entry。这是当前规划的版本基线，实施前应
使用受信运输重新探测，不把文档记录当作永久事实。

## 功能矩阵

| 原生能力 | 当前状态 | 建议用途 | 优先级 |
|---|---|---|---|
| Reaction REST + WebSocket | 未接入 | 接收、成功、失败和用户反馈 | P0 |
| Ephemeral Post | action 失败已部分使用 | 私有错误、权限提示、操作确认 | P0 |
| `post_edited/deleted` | 未消费 | 取消、过期标记和上下文一致性 | P0 |
| Interactive Dialog | 未接入 | 修改参数、拒绝原因、返工表单 | P1 |
| Slash Command | 已接入命令发现；业务子命令未接入 | `/mmag` 稳定入口和命令发现 | P1 |
| Thread/Search REST | 仅有频道分页 | 完整 Thread、编辑/删除后同步、受权检索 | P1 |
| Priority/Acknowledgement | 未接入 | 高风险审批和 SLA 升级 | P1 |
| Mattermost MCP | MCP Bridge 已有雏形 | 读取消息、搜索、用户/成员查询 | P2 |
| Scheduled Message | 未接入 | 到期提醒、延迟通知、摘要 | P2 |
| Playbooks | 未接入 | Task/Run 的企业协作投影 | P2 |
| Agents/Webapp Plugin UI | 未接入 | RHS、AI Actions、自定义 Post 视图 | 触发式 |

## 不可破坏的边界

```text
Mattermost WebSocket / Slash / Dialog / Reaction
  → Mattermost Adapter
  → InboundEvent / Inbox
  → AgentRouter / SkillResolver
  → LangGraph Runtime
  → ResponseView
  → Delivery / Outbox
  → Mattermost REST

Mattermost MCP
  → CapabilitySpec
  → Agent ∩ Skill ∩ Policy ∩ Request Scope
  → CapabilityExecutor
```

- WebSocket、Slash 和 Dialog 回调不直接执行长任务。
- Agent Prompt 不生成 Mattermost `props`、Dialog Schema、Priority 或回调 URL。
- Reaction 只表达状态和反馈，不作为正式审批。
- 最终响应、文件和可重试的外部副作用仍经过 Outbox。
- Bot 能看见资源不等于请求人有权读取；必须计算请求级权限交集。
- Playbooks、Scheduled Post 和 Mattermost Agent 只能是投影或入口，MMAG Control Plane 仍是 Task 状态真相。

## 1. Reaction 生命周期与反馈

建议映射：

| 时机 | Reaction |
|---|---|
| Inbox 已接收 | `eyes` |
| Task 成功 | `white_check_mark` |
| Task 失败 | `warning` |
| 用户正向反馈 | `+1` |
| 用户负向反馈 | `-1` |

要求：

- Reaction 增删作为独立 `InboundEvent` 并持久去重。
- Bot 添加 Reaction 后不再为短任务创建 Ack Post。
- `+1/-1` 进入反馈与 eval 数据，保留 actor、post、run、agent 和 package provenance。
- 移除 Reaction 只撤销反馈投影，不逆转已完成的 Task 或审批。

## 2. Ephemeral Delivery

建议在平台无关交付契约中增加：

```text
DeliveryVisibility = CHANNEL | THREAD | EPHEMERAL
```

适合 Ephemeral 的内容：

- Slash Command 帮助、参数错误和排队确认；
- 权限不足、重复点击、Token 过期和操作成功确认；
- Agent/Skill 路由提示和只对请求人可见的安全警告。

不得用 Ephemeral 交付最终 Artifact、正式审批记录或需要重启恢复的业务状态。

## 3. WebSocket 事件扩展

建议消费：

- `post_edited`：请求已改变，将当前 Run 标记为 stale 并提供重新运行；
- `post_deleted`：取消尚未开始的 Task，已执行任务保留审计并标记来源消失；
- `reaction_added/removed`：轻量状态和用户反馈；
- `thread_updated`：使上下文索引失效；
- `thread_follow_changed`：作为用户任务通知偏好，不自动修改用户设置；
- `status_change`：主动通知前考虑 DND/离线状态。

幂等基线：`event_type + Mattermost object id + update_at`。所有事件先进 Inbox，WebSocket 回调不等待
LLM、Capability 或 Delivery。

## 4. Interactive Dialog

使用场景：

- 填写拒绝原因；
- 修改待审 Capability 参数；
- 选择目标频道、用户或项目；
- 选择 PPT 风格、页数和输出格式；
- 提交返工说明。

安全链：

```text
Action Token + trigger_id + Dialog state
  → 校验 actor/channel/scope/resource/current state
  → Lifecycle Command
```

Dialog 是输入 UI，不能直接修改 checkpoint 或调用 Capability。服务端必须重新校验字段和权限。

## 5. `/mmag` Slash Command

### 平台配置

Mattermost 需要启用 Custom Integrations / Slash Commands，然后创建：

| 字段 | 建议值 |
|---|---|
| Title | `MMAG` |
| Trigger | `mmag` |
| Method | `POST` |
| Request URL | `https://<mmag-domain>/integrations/commands` |
| Autocomplete | 开启 |
| Hint | `[ask|agents|skills|run|status|cancel|retry] [args]` |

Mattermost 会为命令生成 Token。Token 必须以 Secret 注入 MMAG，不写入代码、普通配置、日志或文档实例。

### Request URL 是什么

`https://<mmag-domain>/integrations/commands` 是 MMAG 提供给 Mattermost Server 的回调接口，不是 Mattermost API、
LLM API 或 MCP 地址。用户执行 `/mmag` 后，Mattermost 会向该地址发送 HTTP POST。

示例拓扑：

```text
Mattermost Server
  → https://ai.company.com/integrations/commands
  → Nginx / trusted reverse proxy
  → http://127.0.0.1:8787/integrations/commands
  → MMAG Callback Gateway
```

同一 Docker 网络中可使用 `http://mmag:8787/integrations/commands`。Mattermost 运行在容器内时，
`127.0.0.1` 指向 Mattermost 容器自身，不是宿主机上的 MMAG。内网 HTTP 需经 Mattermost 服务端连接
白名单；生产默认使用 HTTPS 反向代理。

### 代码边界

将现有 action HTTP Server 收口为通用 Callback Gateway，不为 action、command 和 dialog 启动三个独立服务：

```text
/integrations/actions   → 按钮操作
/integrations/commands  → Slash Command
/integrations/dialogs   → Dialog 提交和刷新
```

建议内部职责：

```text
CallbackGateway       HTTP 大小限制、路由、认证和超时
SlashCommandAdapter   form payload 解析与 CommandRequest/InboundEvent 规范化
CommandService        help/status/cancel/retry 的业务命令
DialogService         Dialog schema、打开、刷新和提交
EphemeralDelivery     私有响应投影
```

`MessageHandler` 不解析 HTTP form，`CallbackGateway` 不直接调用 LangGraph。

### 命令建议

当前只实现空命令和 `help` 的命令发现；其他业务子命令仍是规划，不得当作已实现能力：

```text
/mmag help
/mmag ask <goal>
/mmag agents
/mmag skills [agent]
/mmag run <agent> <goal>
/mmag status [run-id]
/mmag cancel <run-id>
/mmag retry <run-id>
/mmag approve <approval-id>
/mmag reject <approval-id>
```

`approve/reject` 只能调用已有 ApprovalService 和 LangGraph resume 链，Slash Handler 不直接改 checkpoint。

### 快命令与长任务

快命令只读 Registry/Control Plane，在回调内返回 Ephemeral：

```text
help / agents / skills / status
```

长任务不在 HTTP 回调中等待 Agent：

```text
ask / run / retry
  → 验证请求
  → 持久 InboundEvent
  → 立即返回 Ephemeral 排队确认
  → Pipeline 异步执行
  → ResponseView + Outbox 交付最终结果
```

Slash Command 本身不是频道 Post，因此没有可用的 `root_id`。第一阶段可让管理命令只返回
Ephemeral，长任务完成后由 Outbox 创建新的频道根 Post。若需与 @mention 一样流式更新，后续增加：

```text
Command → Outbox 创建 running Post → 回写真实 post_id → 启动 Run → 流式更新 → 最终覆盖
```

不使用 `trigger_id`、伪 Post ID 或未持久的直接发帖来模拟 Thread。

### Slash 安全

- 使用常量时间比较验证 Command Token。
- Token 不进入任何 Agent、Prompt、MCP、Artifact 或普通日志。
- `trigger_id` 或平台请求 ID 进入幂等键。
- 回调已认证不代表用户已授权；业务命令仍重新查询 actor 和 channel/team 成员关系。
- `cancel/retry/approve/reject` 校验原 actor、scope、当前状态和 Policy。
- `response_url` 是短时敏感能力 URL；若后续使用，必须限定为已配置 Mattermost host、脱敏日志并经 Outbox。

## 6. Thread 与 Search

建议补充受权读取接口：

- 获取指定 Post 及完整 Thread；
- 按时间或 Post cursor 增量获取频道消息；
- 按 team/channel 约束搜索 Post；
- 查询用户、频道成员和团队成员。

用 Bot Token 读取时必须使用：

```text
Bot 可见范围
∩ 请求人实际成员关系
∩ Agent/Skill allowlist
∩ 当前 Policy 和 Request Scope
```

搜索不以模型提供的 channel/team ID 作为可信授权事实。

## 7. Mattermost MCP

Mattermost Agents 插件的 MCP Server 可提供 `read_post`、`read_channel`、`search_posts`、`search_users`、
`get_channel_members`、`get_team_members`、`create_post` 等工具。

当前 MMAG MCP Bridge 将 `http` 和 `streamable-http` 都交给 SSE Client。官方 Mattermost MCP 外部端点要求
Streamable HTTP，不支持传统 SSE，因此不能直接接入。

实施顺序：

1. 使用 MCP SDK 真正的 Streamable HTTP Client；
2. Capability Probe 确认 Server 版本、Agents 插件和 MCP HTTP 端点状态；
3. 优先使用请求级用户 OAuth，不共享一个无边界的 PAT；
4. 第一阶段只开放 read/search/user/member 工具；
5. 禁止 MCP `create_post`，所有发帖仍走 Delivery/Outbox；
6. 每个 MCP Tool 先适配成 `CapabilitySpec`，再进入共享 Executor 和 Policy。

## 8. Priority、Acknowledgement 与 Thread Follow

仅用于：

- 高风险审批即将过期；
- 严重运行故障；
- 用户明确要求通知指定负责人；
- 企业 SLA 超时升级。

不为普通 Agent 回复设置 Urgent。需持续通知时必须明确 @mention 具体用户，通知目标经 Policy 和
用户成员关系校验。Thread follow 只提供用户主动操作，Bot 不自动修改用户跟随设置。

## 9. Scheduled Message

适合：

- 审批到期提醒；
- 定期项目摘要；
- 会议前材料提醒；
- 交付后回访；
- DND 或非工作时间延迟通知。

MMAG 调度和 Outbox 仍记录真实到期状态，Mattermost Scheduled Post 仅是通知投影。取消或重排不能
直接改变原 Task 终态。

## 10. Playbooks 与 Agents UI

### Playbooks

建议映射：

```text
MMAG Task        → Playbook Run projection
Agent milestone  → Status Update
Artifact         → Run attachment/link
Approval         → Checklist item
Final feedback   → Retrospective input
```

Playbooks 的 Run、Checklist、Status Update、Reminder、Follower 和 Daily Digest 可改善企业交付可见性，但只作为
MMAG Control Plane 的外部投影。

### Agents/Webapp Plugin

Mattermost Agents 插件可提供 RHS、AI Actions、自定义 Prompt 和频道摘要体验，但其自身也拥有 Agent/Tool 运行
系统。当前不用它替代 MMAG LangGraph、Policy 和 Artifact 链。只在能保持 MMAG 为执行与治理真相时，
再评估将其作为 UI Shell。

## 版本与能力门禁

| 能力 | 官方版本信息 | `11.7` 基线 |
|---|---|---|
| Message Priority | 7.7+ | 可评估 |
| Persistent Notification | 8.0+ | 可评估 |
| Scheduled Message | 10.3+ | 可评估 |
| Dialog 动态字段刷新 | 11.1+ | 可评估 |
| External Mattermost MCP | 11.2+ 且需 Agents 插件 | 版本满足，插件/传输待确认 |
| Markdown 内联 Action `mmaction://` | 11.8+ | 当前不可用 |
| Dialog 堆叠 | 11.10+ | 当前不可用 |

在输出富交互前，Capability Probe 应产生显式功能矩阵：

- Server 版本与 Edition；
- 文件、Interactive Message、Slash Command 和插件开关；
- Agents/Playbooks 插件状态；
- MCP HTTP 端点与认证方式；
- `mmaction://`、Dialog refresh 和 Priority 能力；
- Web、Desktop 和 Mobile 的目标版本验收结果。

功能不可用时保留降级路径：

```text
Markdown inline action → attachment action → text command
Dialog → text form/command
Reaction status → typing/status Post
MCP read/search → scoped REST Capability
```

## 实施阶段

### 阶段 A：低风险原生体验

1. 扩展 Capability Probe 和版本门禁。
2. Reaction 生命周期与 `+1/-1` 反馈。
3. 通用 Ephemeral Delivery。
4. `post_edited/deleted/reaction_*` 进入 Inbox。

退出标准：短任务无额外 Ack 噪声；反馈可追溯；编辑/删除不让运行与来源静默分叉。

### 阶段 B：结构化人在回路

1. 通用 Callback Gateway。
2. `/mmag` Slash Command 与自动补全。
3. Dialog 参数编辑、拒绝原因和返工表单。
4. 长任务进 Inbox，快命令返回 Ephemeral。

退出标准：Slash/Action/Dialog 共用认证、限制和审计边界；没有第二条 Agent 执行链。

### 阶段 C：原生上下文与 MCP

1. Thread 和受约束 Search REST Capability。
2. MCP Streamable HTTP 传输修复。
3. 请求级 OAuth 和 Mattermost MCP 只读工具。
4. 未授权跨频道/跨团队负向测试。

退出标准：任何搜索结果都是 actor、scope、Agent、Skill 和 Policy 的权限交集；MCP 不能绕过 Outbox 发帖。

### 阶段 D：企业投影

1. Priority/Acknowledgement 升级 Policy。
2. Scheduled Reminder。
3. Playbooks Run/Status/Checklist 投影。
4. 按真实使用需求决定是否开发 Webapp Plugin/RHS。

退出标准：投影失败不改变 MMAG Task 真实状态；紧急通知不能被普通 Agent 自行触发。

## 明确不做

- 不用 Outgoing Webhook 替换已有 WebSocket；
- 不让 Incoming Webhook、Slash response URL 或 MCP `create_post` 绕过 Outbox；
- 不用 Reaction 取代签名审批；
- 不用 Mattermost Agents 插件自带 Runtime 替换 MMAG LangGraph；
- 不在没有明确 RHS/自定义 Post 验收标准时开发完整 Go/React Plugin；
- 不在未升级和未探测时向 `11.7` 输出 `mmaction://` 或 Dialog 堆叠协议。

## 官方参考

- [Mattermost REST 与 WebSocket API](https://developers.mattermost.com/api-documentation/)
- [Interactive Messages](https://developers.mattermost.com/integrate/plugins/interactive-messages/)
- [Interactive Dialogs](https://developers.mattermost.com/integrate/plugins/interactive-dialogs/)
- [Custom Slash Commands](https://developers.mattermost.com/integrate/slash-commands/custom/)
- [Markdown Action Buttons](https://developers.mattermost.com/integrate/reference/markdown-actions/)
- [Message Priority 与 Acknowledgement](https://docs.mattermost.com/end-user-guide/collaborate/message-priority.html)
- [Scheduled Messages](https://docs.mattermost.com/end-user-guide/collaborate/schedule-messages.html)
- [Mattermost MCP Server](https://docs.mattermost.com/administration-guide/configure/agents-admin-guide.html#mattermost-mcp-server)
- [Mattermost Agents](https://docs.mattermost.com/end-user-guide/agents.html)
- [Playbooks Notifications and Updates](https://docs.mattermost.com/end-user-guide/workflow-automation/notifications-and-updates.html)
- [Mattermost v11 Release Notes](https://docs.mattermost.com/product-overview/mattermost-v11-changelog.html)
