# ADR-0007：Deep Agents 默认 Harness 与可替换执行 Backend

- 状态：Accepted（2026-08-02；Sandbox 后端部分仍待实现）
- 日期：2026-08-02

## 背景

当前 LangGraph Runtime 手写 `agent → review_tools → tools` 循环，并通过 `text-v1`、`json-v1` 和
`single-v1` Provider 区分执行方式。该结构已经具备 checkpoint 和原生审批恢复，但与 Deep Agents
提供的 Agent loop、filesystem、Skills、summarization、subagent 和 HITL Harness 重复。

项目同时需要类似 Pi/Claude Code 的真实工作区体验。当前 `ppt.shell` 可以在宿主机执行完整 Shell，
适合内部 Demo，但 `cwd` 和临时目录不是安全边界；远程 Sandbox 目前又会增加部署成本。需要先把
Agent Harness 和执行位置解耦，再逐步替换执行 Backend。

## 决策

1. LangGraph 继续作为唯一图 Runtime；Deep Agents 成为所有模型驱动 Agent 的默认 Harness，不新增
   `execution.kind=deepagent`。
2. Agent 运行方式只保留 `agent` 和 `direct`。`agent` 为默认值，`direct` 用于确定性单 Capability；
   删除所有带 `-v1` 的 Runtime Provider 名称且不保留兼容别名。
3. Deep Agents 通过现有 `RunRequest -> AgentResult` 契约接入，不替换 Agent Router、Package Registry、
   control plane、Artifact 或 Delivery。
4. Deep Agents Tool 必须由 `CapabilitySpec` 投影或由 `GovernedWorkspaceBackend` 映射回 canonical
   Capability；授权、审批、预算和审计继续进入 `CapabilityExecutor`。
5. MMAG Skill Registry 是源事实。Deep Agents SkillsMiddleware 只消费可信投影；脚本不进入投影，
   同名冲突直接失败，实际披露资源记录 Hash、字节和 provenance。
6. 默认通用 Subagent 关闭。Subagent 只能从受信 Agent Registry 生成，权限为父 Agent、子 Agent 和
   当前 Policy 的交集。
7. 文件和进程统一通过 `GovernedWorkspaceBackend`；底层 `ExecutionBackend` 首期为显式不安全的本地
   完整 Shell，后续可替换为远程 Sandbox。Agent/Skill Manifest 不感知具体 Provider。
8. 本地执行必须由危险开关启用，使用独立 Run Workspace、最小环境、超时、资源/输出限制、审计、
   Artifact staging 和回收，并明确声明不是生产 Sandbox。
9. 模型、Mattermost、MCP 和数据库 Secret 留在控制面；Artifact 只能经过 scope/kind/Hash 校验进入
   或离开 Workspace。
10. 不在 Runtime 或执行 Backend 之间自动回退。Tool、命令、Artifact 提交和销毁使用稳定 execution
    key，LangGraph 恢复不得重复外部副作用。

## 结果与边界

- 手写 LangGraph Agent loop、text/json Provider、Claude SDK 并行 Agent 路径和专用 `ppt.shell`
  在迁移完成后删除。
- Demo 可以在专用可信环境中快速获得真实工作区与完整 Shell，但风险必须通过配置、日志和 provenance
  显式暴露，不能伪装为 Sandbox。
- 远程 Sandbox 只替换 `ExecutionBackend`，不要求修改 Agent/Skill Package 或 DeepAgentRuntime。
- 完整设计、迁移批次和验收标准见 [Deep Agents 原生化重构方案](../DEEP_AGENTS_REFACTORING.md)。
- 在 Capability 映射、动态审批、Workspace、Artifact、Secret 和幂等边界完成前，本 ADR 保持
  Proposed，不得把本地高权限执行部署到公网或多租户生产环境。
