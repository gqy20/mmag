# 技术债清单

> 更新时间：2026-08-02

这里只记录当前代码仍然存在的缺口。已经删除的 `agent.py`、`managed_agents.py`、`tools/`、`prompts.yml` 和旧 Agent loop 不再作为迁移项保留。

## P0：企业运行闭环

### TD-01 — Package provenance 尚未原子进入 AgentRun

当前 Agent 与 Skill 的 Package/Instruction/Prompt/Schema/Policy/Model Policy/eval Hash 已进入输出 Envelope；成功消息运行也会写入 `agent.run` AuditEvent。它还没有与 AgentRun 终态、失败结果、审批 resume 和 DLQ replay 在同一事务中持久化。

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

### TD-05 — Report → PPT Artifact handoff 尚未闭环

`report`、`ppt` 和 `project` 已有真实 Agent/Skill Package、默认拒绝 Policy 和结构化结果契约；
`ppt` 已通过受控执行平面将 PPTX/PDF 原子写入 Artifact Repository。当前 `report` 结果仍直接返回
消息链，尚未形成只允许版本化 `research_report` ref 进入 PPT 的严格 handoff；预览与交付也未闭环。

完成标准：Report 产生版本化 `research_report` Artifact；PPT 只消费通过 Schema、版本和 scope
校验的 Artifact ref；补齐 PNG 预览与 Mattermost Artifact Presenter；每一步失败、审批、重试和返工可恢复。

### TD-06 — Artifact Repository 未进入默认消息链

控制面已有 Artifact 数据模型，受控执行产物已经落入文件型 Repository；普通结构化 Agent 输出尚未统一写入，Artifact ref 也尚未通过 Presenter/Outbox 交付。

完成标准：Artifact 内容、Schema version、来源、Package provenance 和访问 scope 一起持久化；下游只接受 Artifact ref。

### TD-07 — Scope Resolver 仍有 Mattermost 适配逻辑

消息主链已构造 `mattermost:<team>/<channel>` 可信 scope，Policy 也执行资源参数匹配，但应用层仍直接从 Mattermost channel 组装 scope。

完成标准：平台 Adapter 只提供原始身份/资源，统一 Scope Resolver 解析组织、项目、会话和资源归属。

## P1：可观测性与运维

### TD-08 — 内置 Metrics 已落地，外部 Trace 与运维验收尚未完成

日志已经使用版本化事件、自动恢复的 `LogContext`、JSON Lines、中心脱敏和安全异常格式；Deep Agents/LangGraph 原生 Callback 已记录模型、工具和 interrupt/resume 生命周期并写入内容无关的审计，原生调用 ID 已映射为 span。控制面现已提供拒绝高基数 label 的进程内 Metrics 和默认关闭的安全 Trace exporter 边界；当前仍没有实际 OpenTelemetry/LangSmith exporter、Policy 决策全量事件、告警规则和目标日志平台验收。
父子 Run 的 `parent_run_id/workflow_id/execution_key` 已进入第一批日志关联与机器可读
`run_graph`；Control Plane 已具备持久化 AgentRun 身份、唯一 delegation key、严格状态迁移和
`waiting_child`；`RunCoordinator` 已通过该服务幂等启动子 Run、联动父子状态并原子提交结构化终态，子审批
只恢复子 checkpoint，子终态后再恢复父图。诊断工具已能从 trace、父/子 Run 或审批 ID 重建持久化
AgentRun 与审批关系，也能从 CapabilityCall、Artifact 或 Delivery ID 关联并输出安全投影和状态矛盾。
CapabilityCall 已成为独立持久生命周期，审批等待、拒绝和重放从该事实源恢复。AgentRun、Approval、Delivery 和通用 Lifecycle 的创建与
迁移已经和版本化 AuditEvent 在同一事务中提交；Approval 决策投影及 Delivery 的认领、结果、重试和恢复
也与 Lifecycle 原子提交；Artifact 元数据与安全创建审计同事务提交。完整事件目录、外部 Trace 和目标平台
查询/采样/保留/权限仍未验收。

完成标准：实现 [ADR-0011](adr/0011-run-control-plane-observability.md) 的统一事件与关联
契约；指标基数受控；可选 Trace exporter 默认不采集正文；Policy/Approval/Artifact/Delivery 事件目录完整；
在目标平台验证查询、采样、保留和权限。

### TD-09 — SQLite 高可用边界需要部署验收

单机 WAL、migration、备份和恢复原语已经具备；多副本单写者、容量阈值、备份恢复演练和升级回滚仍依赖目标企业环境。

完成标准：在目标部署拓扑完成故障演练，记录 RPO/RTO、磁盘告警和写入竞争数据。

## 维护规则

- 不用兼容 wrapper、假 Agent 或宽松解析掩盖技术债；
- Capability 不允许裸 handler 注册或隐式 ALLOW，Mattermost 生产入口不允许绕过 Inbox Pipeline；
- 每项债务必须有可验证的完成标准；
- 解决后从本文件删除，并在 Roadmap/ADR 留下结果；
- 新文件超过 800 行才要求按职责拆分，避免无意义碎片化。
