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
- 选中 Skill 只投影到当前 Deep Agents StateBackend；`SKILL.md`、模板和参考资料由原生 SkillsMiddleware 按需读取，并在投影前校验路径、Hash 与声明预算。平台 Renderer 只能由 Agent、Skill、Policy 和 Execution Profile 共同允许的窄口 Capability 执行。
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
- `MODEL_BUDGET_USD` 是每个 actor 的月度成本上限；运行前通过 SQLite 原子预占，完成后幂等结算，多个应用进程共享同一账本。
- `ANTHROPIC_LOW_MODEL` / `ANTHROPIC_MEDIUM_MODEL` 是平台控制的 Model Class 映射；未设置时继承 `ANTHROPIC_MODEL`，Agent Manifest 不能直接指定模型名。
- Delivery 最多尝试三次，失败记录保留在 Outbox，不会重新执行 Agent。
- Runtime/网络类瞬时处理错误默认最多尝试三次，次数与下次重试时间持久化在 Inbox；非瞬时错误直接进入 `failed`。
- Outbox 为每个 Run/响应分段生成稳定幂等键，并作为 Mattermost `pending_post_id`；进程内和重启后的重试复用同一键。
- 所有通过入口验收和幂等去重的用户消息都会立即回复默认状态 `get`；`MM_ACK_MESSAGE` 只能用非空值覆盖文案，空值不会关闭基础确认。所有响应继承原消息 Thread root。Deep Agents 模型运行流式更新状态 Post；结构化控制结果不作为 JSON 直接展示。可通过 `MM_STREAM_*` 调整流式展示。
- 流式 Post 是可丢失的展示投影，更新失败不会中断 Agent；只有最终 ResponseView 进入持久 Outbox，不持久化每个 token。
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
- `/mmag` 命令复用同一 callback gateway 的 `/integrations/commands`。`agents/skills/status` 在回调内只读 Registry 或当前 actor/channel 的 Lifecycle；`summary` 经持久 Inbox/Pipeline 异步执行并由 Outbox 交付。每次请求重新校验 Mattermost 成员关系，Thread 总结还会校验 root Post 的频道归属。将 Mattermost 生成的 Slash Token 作为 `MM_SLASH_COMMAND_TOKEN` 注入；Token 轮换后必须同步更新 Secret 并重启实例。
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

本地 callback gateway 始终提供不含业务数据的健康探针：

- `GET /health/live`：HTTP 服务线程存活时返回 `200`。
- `GET /health/ready`：仅在 Mattermost WebSocket 鉴权成功且连接仍有效时返回 `200`；启动中、鉴权失败或断线返回 `503`。

`application.ready` 事件与 readiness 使用同一鉴权门禁。真实 Mattermost E2E 应通过 Profile 的
`readiness_url_env` 配置探针地址，在创建测试消息前等待就绪，避免把启动竞争误判为业务失败。
交互 Action Token 同时绑定 Installation、Tenant 和当前 Bot user ID；重启升级后，旧版本按钮按失败关闭
处理，用户可使用对应文本命令继续操作。

普通运行日志使用稳定 `event`、`status`、UTC timestamp 和 `trace_id/run_id/thread_id/agent_ref/skill_ref/capability` 等关联字段。`LOG_FORMAT=json` 输出 JSON Lines；开发环境可保留 `text`。本地文件按大小轮转并带 PID，`LOG_DIR=` 可完全关闭文件输出。中心化 Filter 会遮蔽 Secret、Authorization、URL query 和异常正文，但业务代码仍不得主动记录 Prompt、消息正文、完整参数或结果。

目标可观测契约由 [ADR-0011](adr/0011-run-control-plane-observability.md) 规定。普通日志已开始传播
`workflow_id/parent_run_id/capability_call_id/execution_key/approval_id`，每条 `log_event()` 具有
独立 `event_id`；诊断报告已输出机器可读 `run_graph`。模型和工具调用已把 LangChain 原生
`run_id/parent_run_id` 投影为 `span_id/parent_span_id`。`artifact_id` 的全链传播、
原子生命周期/审计事件、完整因果树诊断、Metrics 和 OpenTelemetry 尚未实现，部署不得
依赖这些 Proposed 能力。

Deep Agents 通过 LangChain 原生 `AsyncCallbackHandler` 记录 `runtime.model.*` 和 `runtime.tool.*`，并通过 LangGraph 原生 `GraphCallbackHandler` 记录 `runtime.graph.interrupted|resumed`。Graph 事件只投影 checkpoint ID、namespace Hash/深度、状态和 interrupt 数量；不记录 interrupt payload、checkpoint state 或节点名称。模型/工具事件只投影 `langgraph_node`、`langgraph_step`、Provider、模型名、token、耗时、工具名及输入 Hash；正文不会进入日志或 AuditEvent。业务 `CapabilityExecutor` 使用独立的 `capability.*` 事件，避免把框架 Tool transport 成功误认为业务 Capability 成功。RunnableConfig 同时携带低基数 tags、动态 run name 和由可信 Context 覆盖的受控 metadata，可供后续 LangSmith/OpenTelemetry exporter 复用；生产环境不得启用会输出完整状态的 `debug/tasks/checkpoints` stream。

Agent 编排依次记录 `agent.route.selected`、`skill.route.selected|skipped` 和 `agent.tools.projected`，其中工具名来自受信 Catalog，不记录 Prompt。Policy 记录规则、permission、decision 和 `interrupt_check|tool_execute` 阶段。Outbox/Delivery 记录 enqueue、attempt、retry、终态、message kind 和幂等键 Hash，不记录消息正文、文件名或远端异常正文。

开发环境使用 `make debug-trace ID=<trace-id|run-id>` 精确查询上述日志和 AuditEvent，并聚合当前日志目录中的轮转文件；`JSON=1` 输出机器可读报告。`make debug-test` 根据 Mattermost `props.mmag_kind/mmag_status` 忽略 `get` ack 与 stream 更新，只在 result、error、approval 等终态回复后结束，失败和超时返回非零退出码。调试工具读取 `MEMORY_DB_PATH`/`LOG_DIR`，可用 `DEBUG_MEMORY_DB_PATH`/`DEBUG_LOG_DIR` 覆盖；远程用户登录要求 HTTPS，TLS 校验默认开启，只有显式 `DEBUG_TLS_VERIFY=false` 才能用于受信自签名开发环境。

Agent 路由、Skill 选择、Agent 成功/失败、模型调用、Runtime Tool、Capability、Policy、受控执行、审批、Inbox replay 和 Delivery 已进入 AuditEvent。查询支持 event、target、trace、actor、scope、decision、run 和时间游标。事件归档/导出、访问控制和防篡改仍是后续治理项。部署层应至少告警：

- Inbox `failed` 或 Outbox `failed` 增长；
- Runtime timeout/rate-limit 连续发生；
- 待审批数量或最老等待时间超阈值；
- 配额耗尽、数据库磁盘空间不足、WebSocket 长时间重连；
- `sandbox_unavailable`、执行超时/输出超限、Artifact reconciliation 或 scope/Hash 校验失败；
- 备份失败或恢复演练超期。

默认 CI 只做离线契约和并发测试。真实容量上限必须在目标 Mattermost、模型网关和
存储规格上压测，禁止把真实模型压测放进普通 CI。
