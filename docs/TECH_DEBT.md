# 技术债清单

> 更新时间：2026-08-01

这里只记录当前代码仍然存在的缺口。已经删除的 `agent.py`、`managed_agents.py`、`tools/`、`prompts.yml` 和旧 Agent loop 不再作为迁移项保留。

## P0：企业运行闭环

### TD-01 — Package provenance 和 usage 未完整持久化

当前 Package Hash、Prompt/Schema/Policy/Model Policy/eval Hash 已进入输出 Envelope，并在路由日志中记录 Package Hash；消息主链为了保留 LangGraph interrupt 状态会继续使用底层 `AgentResult`，尚未把完整 Envelope 原子写入 AgentRun/AuditEvent。

完成标准：AgentRun 保存完整 provenance、token/tool/cost usage 和终态；审批 resume、DLQ replay 和离线回放仍可定位原 Package 版本。

### TD-02 — 预算账本不是原子预留

进程内 `QuotaLedger` 可限制累计成本，但执行前没有跨进程原子 reservation，崩溃和并发部署下可能发生超支。

完成标准：执行前预留、结束后结算、失败释放；reservation 与 AgentRun 在同一持久事务边界内可审计。

### TD-03 — Model Policy 尚未完全驱动模型路由

Model Policy 已严格加载、校验 route 并进入 Package Hash；`model_class`、temperature 和按策略选择具体模型尚未接入 `ModelGateway`。

完成标准：Gateway 根据不可变 Model Policy snapshot 选 route/model，max output tokens 和 temperature 进入实际请求与审计。

### TD-04 — eval 还是静态门禁

Loader 已拒绝无 eval、重复 case、非法字段和模糊期望，但启动发布没有执行 contract/quality case。

完成标准：新版本激活前执行离线 contract eval；质量 eval 有阈值、结果 Hash、发布人和失败阻断；回滚不重新运行旧版本。

## P1：多 Agent 业务闭环

### TD-05 — Research / Presentation 尚未实现

旧代码中的三个硬编码 Runtime Agent 只是占位，已经删除。当前只有 `mmchat` 和 `link` 两个真实 Package。

完成标准：Research 产生版本化 `research-report` Artifact；Presentation 只消费通过 Schema 的 Artifact；handoff 的每一步、失败和返工可持久恢复。

### TD-06 — Artifact Repository 未进入默认消息链

控制面已有 Artifact 数据模型，Package 也声明 Artifact Schema，但默认对话输出还没有统一写入 Artifact Repository。

完成标准：Artifact 内容、Schema version、来源、Package provenance 和访问 scope 一起持久化；下游只接受 Artifact ref。

### TD-07 — Scope Resolver 仍有 Mattermost 适配逻辑

消息主链已构造 `mattermost:<team>/<channel>` 可信 scope，Policy 也执行资源参数匹配，但应用层仍直接从 Mattermost channel 组装 scope。

完成标准：平台 Adapter 只提供原始身份/资源，统一 Scope Resolver 解析组织、项目、会话和资源归属。

## P1：可观测性与运维

### TD-08 — 日志是可关联文本，不是完整结构化 telemetry

trace、Agent route、Package Hash、Runtime 和 Artifact 数量已经贯穿主链；指标尚未统一输出 Agent/Capability/approval/delivery 的 duration、status 和 error code。

完成标准：结构化日志字段稳定；OpenTelemetry trace 可选接入；指标基数受控；Secret 和消息正文默认不进入 telemetry。

### TD-09 — SQLite 高可用边界需要部署验收

单机 WAL、migration、备份和恢复原语已经具备；多副本单写者、容量阈值、备份恢复演练和升级回滚仍依赖目标企业环境。

完成标准：在目标部署拓扑完成故障演练，记录 RPO/RTO、磁盘告警和写入竞争数据。

## 维护规则

- 不用兼容 wrapper、假 Agent 或宽松解析掩盖技术债；
- 每项债务必须有可验证的完成标准；
- 解决后从本文件删除，并在 Roadmap/ADR 留下结果；
- 新文件超过 800 行才要求按职责拆分，避免无意义碎片化。
