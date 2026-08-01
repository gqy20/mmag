# Roadmap

> 状态：Active
>
> 更新时间：2026-08-01
>
> 当前阶段：硬迁移完成，进入企业闭环与专业 Agent 阶段

本文档只维护当前基线、未完成步骤和验收标准。设计理由见 [AI_NATIVE_REFACTORING.md](AI_NATIVE_REFACTORING.md)，具体风险见 [TECH_DEBT.md](TECH_DEBT.md)。

## 当前基线

### 工程与持久化

- [x] 锁定依赖、Ruff、mypy、coverage、wheel smoke 和 CI 统一门禁；
- [x] 默认测试完全离线，外部测试显式标记；
- [x] SQLite forward-only migration、FTS、事务回滚、备份恢复原语；
- [x] Mattermost posted 去重、幂等 Outbox、重试、DLQ 和 replay；
- [x] 同会话串行、跨会话并发、入口/执行/投递解耦。

### Runtime 与人在回路

- [x] 不可变 `RunRequest` / `AgentResult` 和统一错误语义；
- [x] LangGraph 作为默认 Runtime；
- [x] SQLite checkpoint、稳定 thread ID、原生 interrupt/resume；
- [x] 审批 approve/edit/reject、资格校验、过期与重复恢复保护；
- [x] Claude Agent SDK 仅作为显式可选 Runtime，不再携带旧手写 Agent loop 参数。

### Capability 与安全

- [x] 八个内置能力只有一份 `CapabilitySpec`；
- [x] `CapabilityRegistry` 统一 LangGraph 与 MCP 的运行时 binding；
- [x] Policy 默认拒绝，执行前检查 actor/scope/permission/resource；
- [x] 文件外发和 MCP 副作用进入审批；
- [x] MCP 精确 allowlist、最小 stdio 环境；
- [x] URL 重定向逐跳执行 SSRF 校验。

### Agent 系统与 Package

- [x] `application/`、`agent_system/`、`agent_packages/`、`capabilities/` 职责收口；
- [x] 删除 `agent.py`、`managed_agents.py`、`tools/` 和全局 `prompts.yml`；
- [x] 默认消息主链强制经过 `AgentRouter`；
- [x] 删除 research/project/presentation 的硬编码假 Agent；
- [x] Agent Package 采用扁平 `agents/<name>/agent.yml`，版本只保留在 Manifest；
- [x] `execution/routing`、可信 Provider Registry、AgentFactory 和原子自动注册；
- [x] `mmchat` 和 `link` 成为真实 Package，Link 使用通用 Capability Provider；
- [x] Capability 根据当前 Package Policy 动态授权，不再绑定全局 Bot Policy；
- [x] Prompt/Schema/eval/Policy/Model Policy 进入 Package Hash 和 provenance；
- [x] strict Prompt render、输入/输出/Artifact Schema 和预算强制；
- [x] Model Policy Registry 严格加载并校验 route。

## 实施原则

1. 单向硬迁移，不保留旧模块转发层；
2. Manifest 负责声明，Schema 负责格式，Policy/Executor 负责安全；
3. 只注册具备 Manifest、Prompt、Schema、Policy、eval 和真实执行器的 Agent；
4. Agent 之间只传 Artifact ref 和严格 Envelope，不猜测自由文本；
5. 模块化单体优先，没有容量或隔离证据不拆微服务；
6. 文件超过 800 行才按稳定职责拆分，避免碎片化。

## 下一步 1：持久化运行 provenance 与预算

优先级：P0。

- [ ] 在 AgentRun 保存 Package、Prompt、Schema、Policy、Model Policy 和 eval Hash；
- [ ] 保存模型调用、Capability 调用、token、cost、repair 和 Artifact usage；
- [ ] 给 `QuotaLedger` 增加持久化原子 reservation/settlement/release；
- [ ] 审批 resume 和 DLQ replay 复用原始 Package snapshot；
- [ ] AuditEvent 记录 route decision、policy decision 和版本信息。

退出标准：任意历史 Run 可回答“谁、在什么 scope、使用哪个版本、调用了什么、花费多少、为何允许”，且并发/崩溃不能突破预算。

## 下一步 2：让 Model Policy 驱动 Gateway

优先级：P0。

- [ ] 将 `model_class` 映射到允许的模型集合；
- [ ] route/model/max output tokens/temperature 从 snapshot 进入实际调用；
- [ ] 对不支持的模型参数在启动时失败，不在运行中静默忽略；
- [ ] 为不同 Agent 建立成本、延迟、质量基线。

退出标准：修改 Model Policy 只能通过新版本生效，每次调用都能复现实际模型参数。

## 下一步 3：执行 eval 发布门禁

优先级：P0。

- [ ] contract case 在激活前离线执行；
- [ ] quality case 定义数据集版本、阈值和评分器版本；
- [ ] eval 结果 Hash、发布时间和发布人进入发布记录；
- [ ] 失败 Package 不得生成发布制品或进入部署；
- [ ] 旧制品可按 Package Hash 原子回滚。

退出标准：坏 Prompt、坏 Schema、越权能力和质量回退不能进入新 Run。

## 下一步 4：Research Package

优先级：P1。

- [ ] 定义 research Manifest、只读 Policy、Prompt 和输入/输出 Schema；
- [ ] 定义 `research-report` Artifact Schema；
- [ ] 接入来源去重、时效性和证据覆盖 eval；
- [ ] 将报告持久化到 Artifact Repository；
- [ ] 覆盖取消、超时、部分来源失败和预算耗尽。

退出标准：Research 只能读允许来源，输出始终是可验证、有来源、有版本的 Artifact。

## 下一步 5：Presentation Package 与严格 handoff

优先级：P1，依赖下一步 4。

- [ ] Presentation 只接受 `research-report` ref；
- [ ] 输入 Artifact 在执行前校验版本和 scope；
- [ ] 输出 presentation outline/file Artifact；
- [ ] 文件交付仍经过审批和 Outbox；
- [ ] handoff 每一步持久化状态、失败、重试和成本。

退出标准：Research → Presentation 不传自由文本；非法或越权 Artifact 不能进入下游。

## 下一步 6：反馈、返工与业务闭环

优先级：P1。

- [ ] 用户可接受、驳回或请求返工；
- [ ] 返工创建新 Run，关联原 Artifact，不覆盖历史；
- [ ] 反馈进入质量数据集但默认不进入 Prompt；
- [ ] Task 只有在交付被接受或明确终止后才闭环；
- [ ] 建立任务、AgentRun、Artifact、Delivery、Feedback 的端到端报表。

退出标准：企业任务从请求到交付、审批、验收、返工和审计形成可查询闭环。

## 下一步 7：结构化可观测性与部署验收

优先级：P1。

- [ ] 统一 Agent/Runtime/Capability/Approval/Delivery 结构化事件字段；
- [ ] 输出 duration、status、error code、queue depth 和 cost 指标；
- [ ] 可选接入 OpenTelemetry，控制 label 基数和正文采集；
- [ ] 完成目标环境容量、备份恢复、升级回滚和灾难演练。

退出标准：告警可定位到 Run/Package/Capability，目标环境有可验证 RPO/RTO。

## 当前明确不做

- 不恢复旧 Agent、Tool 或 Prompt 兼容入口；
- 不为了“多 Agent”数量重新注册没有 Package 契约的占位 Agent；
- 不用 Prompt 代替权限、审批、幂等和预算；
- 不在 Artifact/Scope/恢复语义未完成前拆微服务；
- 不把真实公网或 LLM 测试放进默认 CI。
