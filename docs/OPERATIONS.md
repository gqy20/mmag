# Production Operations

mmag 继续采用模块化单体部署。一个实例包含 WebSocket ingress、分区执行器、
Delivery worker 和 SQLite 控制面，不需要额外消息队列。

## 推荐拓扑

```text
Mattermost ── WebSocket/REST ── mmag instance ── Model Gateway ── LLM provider
                                  │
                                  └── persistent volume / agent_memory.db
```

- 每个 SQLite 数据库只运行一个 mmag 写实例；横向扩展前必须先更换支持租约的存储。
- 数据目录使用持久卷，数据库开启 WAL、foreign keys、busy timeout 和 NORMAL synchronous。
- 出站网络只允许 Mattermost、配置的模型端点和显式授权的 MCP Server。
- Secret 通过环境或部署平台 Secret 注入，不写入镜像、日志和数据库。

## 容量与保护

- `PIPELINE_MAX_CONCURRENCY` 控制跨会话执行并发，默认 8。
- `PIPELINE_MAX_PENDING` 提供入口背压，默认 256。
- `RUNTIME_DEADLINE_SECONDS` 是单次 Run deadline，默认 120 秒。
- `MODEL_BUDGET_USD` 是进程内每个 actor 的成本上限基线，生产环境应由外部配额源同步。
- Delivery 最多尝试三次，失败记录保留在 Outbox，不会重新执行 Agent。

## 备份与恢复

1. 使用 `mmag.governance.backup_sqlite` 或 SQLite Online Backup API 生成一致性备份。
2. 将备份复制到独立故障域并按组织的数据保留策略轮转。
3. 恢复时停止旧实例，将备份恢复到新路径，再启动单个实例。
4. 启动过程会运行 forward-only migration，并 reconciliation 遗留的
   `RUNNING`、`SENDING` 和未投递记录。
5. 恢复演练必须验证 Inbox 不重复执行、Outbox 可继续投递、审批仍可读取。

## 升级与回滚

- 升级前备份数据库并先运行 `make verify`。
- migration 只向前；应用回滚前必须确认旧版本支持当前 schema。
- 优雅关闭顺序为：停止 WebSocket 接收、排空分区队列、排空 Delivery、关闭外部连接、关闭 SQLite。

## 可观测与告警

所有 Task、AgentRun、CapabilityCall、ApprovalRequest 和 Delivery 都有版本化状态历史；
Policy 决策、配额和审计事件使用 actor/scope/trace 关联。部署层应至少告警：

- Inbox `failed` 或 Outbox `failed` 增长；
- Runtime timeout/rate-limit 连续发生；
- 待审批数量或最老等待时间超阈值；
- 配额耗尽、数据库磁盘空间不足、WebSocket 长时间重连；
- 备份失败或恢复演练超期。

默认 CI 只做离线契约和并发测试。真实容量上限必须在目标 Mattermost、模型网关和
存储规格上压测，禁止把真实模型压测放进普通 CI。
