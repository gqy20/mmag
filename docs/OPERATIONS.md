# Production Operations

mmag 继续采用模块化单体部署。一个实例包含 WebSocket ingress、分区执行器、
Delivery worker 和 SQLite 控制面，不需要额外消息队列。

## 推荐拓扑

```text
Mattermost ── WebSocket/REST ── mmag instance ── Model Gateway ── LLM provider
                                  │
                                  └── persistent volume
                                      ├── agent_memory.db
                                      ├── agent_checkpoints.db
                                      └── artifacts/
```

- `MEMORY_DB_PATH` 保存业务/control-plane 状态，`CHECKPOINT_DB_PATH` 只保存 LangGraph checkpoint；
  两者必须是不同文件。每个 SQLite 数据库只运行一个 mmag 写实例；横向扩展前必须先更换支持租约的存储。
- 数据目录与 `ARTIFACT_STORE_PATH` 使用同一故障域内的持久卷，数据库开启 WAL、foreign keys、busy timeout 和 NORMAL synchronous。
- 出站网络只允许 Mattermost、配置的模型端点和显式授权的 MCP Server。
- Secret 通过环境或部署平台 Secret 注入，不写入镜像、日志和数据库。

## Policy 与网络隔离

- 启动时先加载 Skill Package，再解析 Agent 的 `skills.allow` 精确版本。未知 Skill、Hash 漂移或 Required Capability 超出 Agent allowlist 会阻止整批 Agent 注册。
- 选中 Skill 后，模型 Tool Schema、CapabilityContext 和 Package Governance 同时收窄到该 Skill 的 required/available optional 集合。Skill 未声明的 Agent Capability 在本次请求不可执行。
- `SKILL.md` 在选中后加载；模板和参考资料只能通过 `load_skill_resource` 按精确 ref 加载，并受数量、字节和估算 token 预算限制。`scripts/` 不向模型披露，只能由 Agent、Skill、Policy 和 Execution Profile 共同允许的窄口 Capability 执行。
- Skill 资源首次按需加载时重新校验发布 Hash，之后按 Hash 缓存。审批 interrupt 保存已披露 ref，resume 只恢复这些资源；实际加载清单进入运行 provenance。
- 每次 Capability 调用根据当前 Agent Package 的 `policy_ref` 动态选择 Policy；`mmchat`、
  `link`、`report`、`ppt` 和 `project` 均使用各自版本化 Policy，默认效果为 `deny`。
  `resource_arguments` 将 Capability 参数映射到可信请求资源；缺参数、缺可信资源或值不一致都会拒绝。
- 当前频道资源来自已认证 Mattermost 事件，而不是 Prompt。`get_posts`、消息/知识检索和知识写入不能跨到模型自行提供的其他频道；画像默认只允许读取当前 actor。
- 文件外发和所有外部 MCP 调用默认进入 LangGraph 审批。审批恢复会还原原始 CapabilityContext，副作用前的用户意图与频道目标不会被审批消息覆盖。
- MCP stdio 子进程仅继承 `PATH`、语言、临时目录、Home 和 Windows 启动必需变量；其他值必须在 `.mcp.json` 的 Server `env` 中显式声明。环境隔离不是进程沙箱，高风险 Server 仍应放入容器并限制文件系统与网络。
- URL 分析禁用自动重定向与环境代理；初始 URL 和最多五个重定向目标均重新做协议、DNS 和公网 IP 校验。出站防火墙仍是最终网络边界。
- 受控 Python/CLI 要求 Linux、Bubblewrap namespace 权限和锁定的 `EXECUTION_RUNTIME_ROOT`；PDF 还要求 LibreOffice。启动时会协调 Run 工作区和 Artifact 中断提交，依赖或隔离不可用时能力失败关闭。完整门禁见 [受控执行平面](EXECUTION.md)。

## 容量与保护

- `PIPELINE_MAX_CONCURRENCY` 控制跨会话执行并发，默认 8。
- `PIPELINE_MAX_PENDING` 提供入口背压，默认 256。
- `RUNTIME_DEADLINE_SECONDS` 是单次 Run deadline，默认 120 秒。
- 每个 Execution Profile 独立限制输入、stdout/stderr、Artifact、wall time、CPU、地址空间、进程数和文件描述符；部署层仍应再使用 cgroup/systemd/container 限额保护整个服务。
- `MODEL_BUDGET_USD` 是进程内每个 actor 的成本上限基线，生产环境应由外部配额源同步。
- Delivery 最多尝试三次，失败记录保留在 Outbox，不会重新执行 Agent。
- Runtime/网络类瞬时处理错误默认最多尝试三次，次数与下次重试时间持久化在 Inbox；非瞬时错误直接进入 `failed`。
- Outbox 为每个 Run/响应分段生成稳定幂等键，并作为 Mattermost `pending_post_id`；进程内和重启后的重试复用同一键。
- 所有响应继承原消息 Thread root。LangGraph 默认对 `text-v1` Agent 流式更新同一条 Post；结构化 Agent 不流式暴露 JSON。`report,ppt` 默认先创建真实 `running` 状态 Post，最终结果由 Outbox 原地更新。可通过 `MM_LONG_TASK_AGENTS` / `MM_ACK_MESSAGE` 和 `MM_STREAM_*` 调整。
- 流式 Post 是可丢失的展示投影，更新失败不会中断 Agent；只有最终 ResponseView 进入持久 Outbox，不持久化每个 token。SDK 备选 Runtime 仍只交付最终结果。
- Outbox 同时保存消息种类、Scope、Artifact refs、已上传 file IDs、action 和 update target。Artifact 上传成功后立即保存 file IDs，发帖重试不会重新执行 Agent。

Task 与 AgentRun 的终态语义不同：AgentRun 在模型执行成功后结束；存在出站消息时，Task 必须等全部 Delivery 成功后才进入 `succeeded`，任何最终 Delivery 失败都会把 Task 标为 `failed`。

## Inbox DLQ 与 replay

非瞬时错误或超过重试次数的事件保持 `failed`，它们就是持久 DLQ。应用管理层可用 `SQLiteControlPlane.list_dead_letters(limit=..., conversation_id=...)` 查询，并在运行中的 Pipeline 上调用 `MessagePipeline.replay_dead_letter(event_id, replay_id=..., actor_id=...)`。

replay 不会把原记录从 `failed` 改回 `accepted`：它克隆出一个新事件，在 `_mmag_replay` 中记录来源、操作人、原尝试次数和错误。操作方必须使用稳定、唯一的 `replay_id`（建议来自工单或管理命令 ID）；相同来源与 ID 的重复调用返回 `False`，ID 与其他事件碰撞则拒绝。成功入队写入 `inbox.replayed` 审计。

当前接口是应用服务 API，尚未暴露为远程管理端点。生产接入管理面时必须增加管理员认证、scope 授权、批量限速和二次确认；在此之前不要直接修改 `inbox_events` 状态。

## 审批安全

- 过期、已处理或 resume token 不一致的审批不会恢复 LangGraph checkpoint；
- 原请求人可以处理自己的审批；其他用户必须是当前 Mattermost 频道管理员或系统管理员；
- 身份或频道成员查询失败时默认拒绝；
- 审批请求、拒绝和最终决定写入 AuditEvent。
- 可选交互按钮要求同时配置 `MM_ACTION_CALLBACK_URL` 与至少 32 bytes 的 `MM_ACTION_SIGNING_SECRET`。公网 callback 只接受 HTTPS；进程默认监听 `127.0.0.1:8787`，由受信反向代理转发 callback path。
- Action token 使用 HMAC、最长 10 分钟有效期和 SQLite 原子一次性消费；Callback 在消费前后都重新校验 actor、频道、Scope、审批状态和 Mattermost 管理角色。按钮不可用时继续支持 `批准 <id>` / `拒绝 <id>`。

启动时会执行 Mattermost 能力探测并记录 `mattermost.capabilities` 审计，包括 Server 版本、Edition、文件、插件和交互开关。认证级探测在非本机明文 HTTP 上直接跳过，不发送 Bot Token。

当前策略允许请求人自批。需要职责分离的部署仍应在下一阶段接入组织级审批矩阵，按 Capability 风险要求独立审批人或双人审批。

## 备份与恢复

1. 停止实例写入后，分别对 `MEMORY_DB_PATH` 和 `CHECKPOINT_DB_PATH` 使用
   `mmag.governance.backup_sqlite` 或 SQLite Online Backup API 生成配对的一致性备份。
2. 将备份复制到独立故障域并按组织的数据保留策略轮转。
3. 恢复时停止旧实例，将两份数据库备份恢复到配置路径，再启动单个实例。
4. 启动过程会运行 forward-only migration，并 reconciliation 遗留的
   `RUNNING`、`SENDING`、`PROCESSING/RETRYING` 和未投递记录。
5. 恢复演练必须验证 Inbox 不重复执行、Outbox 可继续投递、审批仍可读取、Artifact 文件与 SQLite metadata 一致，并能用原 `thread_id` 从 LangGraph SQLite checkpoint 恢复。

## 升级与回滚

- 升级前备份数据库并先运行 `make verify`。
- 从共库版本升级且存在待审批 Run 时，必须先停止实例并迁移 `checkpoints`、`writes` 表到
  `CHECKPOINT_DB_PATH`，或先处理完待审批 Run；不能只切换路径后丢弃旧 checkpoint。
- migration 只向前；应用回滚前必须确认旧版本支持当前 schema。
- 优雅关闭顺序为：停止 WebSocket 接收、排空分区队列、排空 Delivery、关闭外部连接、关闭 SQLite。

## 可观测与告警

所有 Task、AgentRun、CapabilityCall、ApprovalRequest 和 Delivery 都有版本化状态历史；成功的 `agent.run` 审计还记录 Agent/Skill provenance 与本次 Capability 集合。
Policy 决策、配额和审计事件使用 actor/scope/trace 关联。部署层应至少告警：

- Inbox `failed` 或 Outbox `failed` 增长；
- Runtime timeout/rate-limit 连续发生；
- 待审批数量或最老等待时间超阈值；
- 配额耗尽、数据库磁盘空间不足、WebSocket 长时间重连；
- `sandbox_unavailable`、执行超时/输出超限、Artifact reconciliation 或 scope/Hash 校验失败；
- 备份失败或恢复演练超期。

默认 CI 只做离线契约和并发测试。真实容量上限必须在目标 Mattermost、模型网关和
存储规格上压测，禁止把真实模型压测放进普通 CI。
